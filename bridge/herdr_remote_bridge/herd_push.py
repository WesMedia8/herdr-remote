"""Optional APNs support for the Herd bridge. No network work at import time."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

APPLE_EPOCH = 978307200
LOG = logging.getLogger(__name__)


def caller_dir():
    return Path(getattr(sys.modules.get('__main__'), '__file__', Path.cwd() / 'server.py')).resolve().parent


def to_apple_date(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            raise ValueError('datetime must be timezone-aware')
        dt = dt.timestamp()
    return float(dt) - APPLE_EPOCH


@dataclass
class PushConfig:
    key_path: str = ''
    key_id: str = ''
    team_id: str = ''
    bundle_id: str = 'app.herdr-remote'

    @classmethod
    def load(cls, directory=None):
        directory = Path(directory) if directory else caller_dir()
        file = directory / 'herd-push.json'
        data = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
        values = {key: os.environ.get('HERD_APNS_' + key.upper(), os.environ.get('APNS_' + key.upper(), data.get(key, '')))
                  for key in ('key_path', 'key_id', 'team_id')}
        if values['key_path']:
            path = Path(values['key_path']).expanduser()
            values['key_path'] = str(path if path.is_absolute() else directory / path)
        return cls(**values, bundle_id=data.get('bundle_id', 'app.herdr-remote'))

    @property
    def ready(self):
        return bool(self.key_path and self.key_id and self.team_id)


def validate_token(token, env):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-fA-F]+', token):
        raise ValueError('token must be nonempty hexadecimal')
    if env not in ('development', 'production'):
        raise ValueError('env must be development or production')
    return token.lower()


class TokenStore:
    """Single-process, thread-safe persistence; do not share across bridge processes."""
    def __init__(self, path=None):
        self.path = Path(path) if path else caller_dir() / 'herd-tokens.json'
        self.lock = threading.RLock()
        self.data = {'devices': [], 'activities': [], 'starts': []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        os.replace(temp, self.path)

    def add(self, token, env, kind='devices', machine=None):
        token = validate_token(token, env)
        if kind not in self.data:
            raise ValueError('invalid token kind')
        key = 'start_token' if kind == 'starts' else 'token'
        with self.lock:
            record = next((r for r in self.data[kind] if r[key] == token and r['env'] == env), None)
            if record is None:
                record = {key: token, 'env': env}
                if kind == 'devices':
                    record['added'] = datetime.now(timezone.utc).isoformat()
                self.data[kind].append(record)
            if kind == 'activities':
                record['machine'] = machine
            self._save()

    def snapshot(self, kind='devices'):
        with self.lock:
            return copy.deepcopy(self.data[kind])

    def remove(self, token, env, kind='devices'):
        key = 'start_token' if kind == 'starts' else 'token'
        with self.lock:
            self.data[kind] = [r for r in self.data[kind] if not (r[key] == token.lower() and r['env'] == env)]
            self._save()


def alert_payload(title, body, address=None, badge=0, category=None):
    aps = {'alert': {'title': title, 'body': body}, 'sound': 'default', 'badge': badge}
    if category:
        aps['category'] = category
    payload = {'aps': aps}
    if address is not None:
        aps['thread-id'] = address
        payload['t'] = address
    return payload


def live_payload(event, content_state, stale_date, attributes=None, now=None):
    if event not in ('start', 'update', 'end'):
        raise ValueError('invalid Live Activity event')
    now = time.time() if now is None else now
    aps = {'timestamp': int(now), 'event': event, 'content-state': content_state,
           'stale-date': int(stale_date.timestamp() if isinstance(stale_date, datetime) else stale_date)}
    if event == 'start':
        if not attributes or not isinstance(attributes.get('machine'), str):
            raise ValueError('start requires machine attributes')
        aps.update({'attributes-type': 'HerdActivityAttributes', 'attributes': attributes,
                    'alert': {'title': 'Herd', 'body': 'Agent activity on ' + attributes['machine']}})
    return {'aps': aps}


class APNsClient:
    def __init__(self, config, store, client=None):
        self.config, self.store = config, store
        self.lock = threading.Lock()
        self._jwt, self._issued = None, 0
        self.client = client
        if not config.ready:
            LOG.warning('APNs configuration incomplete; push disabled')

    def provider_token(self):
        if not self.config.ready:
            raise ValueError('APNs configuration incomplete: set key_path, key_id and team_id')
        import jwt
        with self.lock:
            now = time.time()
            if self._jwt is None or now - self._issued >= 3000:
                key = Path(self.config.key_path).read_text(encoding='utf-8')
                self._jwt = jwt.encode({'iss': self.config.team_id, 'iat': int(now)}, key,
                                       algorithm='ES256', headers={'kid': self.config.key_id})
                self._issued = now
            return self._jwt

    def _send(self, token, env, payload, push_type, priority, kind, collapse_id=None):
        validate_token(token, env)
        try:
            headers = {'authorization': 'bearer ' + self.provider_token(), 'content-type': 'application/json',
                       'apns-topic': self.config.bundle_id + ('.push-type.liveactivity' if push_type == 'liveactivity' else ''),
                       'apns-push-type': push_type, 'apns-priority': str(priority)}
            if collapse_id:
                headers['apns-collapse-id'] = collapse_id
            if len(json.dumps(payload).encode()) > 4096:
                return {'ok': False, 'status': 0, 'reason': 'PayloadTooLarge'}
            with self.lock:
                if self.client is None:
                    import httpx
                    self.client = httpx.Client(http2=True, timeout=10.0)
            host = 'api.sandbox.push.apple.com' if env == 'development' else 'api.push.apple.com'
            attempts = 1 if push_type == 'liveactivity' else 3
            for attempt in range(attempts):
                try:
                    response = self.client.post('https://' + host + '/3/device/' + token, headers=headers, json=payload)
                except Exception:
                    if attempt + 1 == attempts:
                        raise
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                    break
                retry = response.headers.get('retry-after')
                delay = 2 ** attempt
                if retry:
                    try:
                        delay = max(0, float(retry))
                    except ValueError:
                        try:
                            delay = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                # Do not retry earlier than Retry-After; abandon an excessive delay.
                if delay > 30:
                    break
                time.sleep(delay)
            try:
                reason = response.json().get('reason', 'APNsError') if response.status_code != 200 else None
            except (ValueError, AttributeError):
                reason = 'APNsError'
            # Only known reason strings are exposed, never response bodies or exception strings.
            reason = reason if reason in {'BadDeviceToken', 'Unregistered', 'DeviceTokenNotForTopic', 'TooManyRequests', 'ExpiredProviderToken', 'InvalidProviderToken', 'BadTopic'} else ('APNsError' if reason else None)
            if response.status_code == 410 or reason in {'BadDeviceToken', 'Unregistered', 'DeviceTokenNotForTopic'}:
                self.store.remove(token, env, kind)
            if response.status_code != 200:
                LOG.warning('APNs rejected push: status=%s reason=%s', response.status_code, reason)
            return {'ok': response.status_code == 200, 'status': response.status_code, 'reason': reason}
        except Exception:
            return {'ok': False, 'status': 0, 'reason': 'APNs configuration, dependency or transport failure'}

    def send_alert(self, token, env, payload, collapse_id=None, thread_id=None):
        payload = copy.deepcopy(payload)
        if thread_id is not None:
            payload.setdefault('aps', {})['thread-id'] = thread_id
        return self._send(token, env, payload, 'alert', 10, 'devices', collapse_id)

    def send_background(self, token, env):
        return self._send(token, env, {'aps': {'content-available': 1}}, 'background', 5, 'devices')

    def send_live_activity(self, token, env, event, content_state, stale_date, attributes=None):
        return self._send(token, env, live_payload(event, content_state, stale_date, attributes),
                          'liveactivity', 10, 'starts' if event == 'start' else 'activities')

    def close(self):
        if self.client:
            self.client.close()


def friendly_name(agent):
    value = agent or ''
    return {'claude-code': 'Claude', 'codex': 'Codex', **dict.fromkeys(
        ('sh', 'bash', 'zsh', 'pwsh', 'powershell', 'cmd', 'shell'), 'Shell')}.get(value.lower(), value.title() if value else 'Terminal')


ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b[@-_]')


def content_state(terminals, now=None):
    blocked = sorted((t for t in terminals if t.get('status') == 'blocked'), key=lambda t: t['address'])
    working = sum(t.get('status') == 'working' for t in terminals)
    top = blocked[0] if blocked else None
    headline = (f"{(top.get('where') or 'Other').split(' / ', 1)[0]} · {friendly_name(top.get('agent'))} needs you" if top else
                f"{working} agent{'s' if working != 1 else ''} working" if working else f'{len(terminals)} agents · all quiet')
    preview = top.get('preview') if top else None
    return {'needsYou': len(blocked), 'working': working, 'done': sum(t.get('status') == 'done' for t in terminals),
            'total': len(terminals), 'headline': headline,
            'detail': re.sub(r'\r\n|[\n\r\v\f\x85\u2028\u2029]', ' ', ANSI.sub('', preview))[:240] if preview is not None else None,
            'topAddress': top['address'] if top else None, 'updatedAt': to_apple_date(time.time() if now is None else now)}


class StatusWatcher(threading.Thread):
    def __init__(self, get_terminals, client, store, machine, poll_interval=2):
        super().__init__(name='herd-status', daemon=True)
        self.get_terminals, self.client, self.store, self.machine = get_terminals, client, store, machine
        if getattr(client, 'is_relay', False):
            self.store = RelayWatcherStore(machine)
            client.machine = machine
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.previous = None
        self.pending, self.last_alert, self.live_sent, self.start_attempts = {}, {}, {}, set()
        # A single delivery job at a time keeps polling independent of network timeouts.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='herd-apns')
        self.delivery = None
        self.queued = {}

    def poll(self, terminals, now=None):
        """Deterministic detection seam; returns jobs without performing network I/O."""
        now = time.time() if now is None else now
        current = {t['address']: t for t in terminals}
        jobs = []
        if self.previous is None:
            self.previous = {a: t.get('status') for a, t in current.items()}
            return jobs
        for address, terminal in current.items():
            status = terminal.get('status')
            if address not in self.previous:
                self.previous[address] = status
            old = self.previous[address]
            if status == old:
                self.pending.pop(address, None)
            elif address not in self.pending or self.pending[address][0] != status:
                self.pending[address] = (status, now)
            elif now - self.pending[address][1] >= 4:
                self.previous[address] = status
                self.pending.pop(address)
                if status in ('blocked', 'done') and now - self.last_alert.get(address, -float('inf')) >= 30:
                    self.last_alert[address] = now
                    body = f"{friendly_name(terminal.get('agent'))} {'needs you' if status == 'blocked' else 'finished'}: {terminal.get('preview') or ''}"
                    payload = alert_payload(terminal.get('where') or 'Other', body, address,
                                            sum(t.get('status') == 'blocked' for t in terminals),
                                            'AGENT_BLOCKED' if status == 'blocked' else None)
                    for record in self.store.snapshot():
                        jobs.append(('alert', record, payload))
        for address in set(self.previous) - set(current):
            self.previous.pop(address, None)
            self.pending.pop(address, None)
        state = content_state(terminals, now)
        signature = {k: v for k, v in state.items() if k != 'updatedAt'}
        updates = [r for r in self.store.snapshot('activities') if r.get('machine') == self.machine]
        for record in updates:
            key = (record['token'], record['env'])
            previous, sent = self.live_sent.get(key, (None, -float('inf')))
            if signature != previous and now - sent >= 10:
                jobs.append(('update', record, state))
                self.live_sent[key] = (signature, now)
        if (not updates or getattr(self.client, 'is_relay', False)) and state['needsYou']:
            for record in self.store.snapshot('starts'):
                key = (record['start_token'], record['env'])
                if key not in self.start_attempts:
                    self.start_attempts.add(key)
                    jobs.append(('start', record, state))
        return jobs

    def _deliver(self, jobs):
        for event, record, payload in jobs:
            if self.stop_event.is_set():
                return
            if event == 'alert':
                self.client.send_alert(record['token'], record['env'], payload, thread_id=payload['t'])
            else:
                self.client.send_live_activity(record.get('token') or record['start_token'], record['env'], event,
                                               payload, time.time() + 600, {'machine': self.machine} if event == 'start' else None)

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    # Poll even while APNs is slow; retain only the newest job per token/kind.
                    for job in self.poll(self.get_terminals()):
                        event, record, _ = job
                        key = (event, record.get('token') or record.get('start_token'), record['env'])
                        self.queued[key] = job
                    if (self.delivery is None or self.delivery.done()) and self.queued:
                        jobs = list(self.queued.values())
                        self.queued.clear()
                        self.delivery = self.executor.submit(self._deliver, jobs)
                except Exception:
                    LOG.warning('Herd status poll failed')
                self.stop_event.wait(self.poll_interval)
        finally:
            self.executor.shutdown(wait=True)

    def stop(self):
        self.stop_event.set()


def handle_register(data, store):
    try:
        if not isinstance(data, dict):
            raise ValueError('expected JSON object')
        store.add(data.get('token'), data.get('env'))
        return 200, {'ok': True}
    except ValueError as exc:
        return 400, {'error': str(exc)}


def handle_activity(data, store):
    try:
        if not isinstance(data, dict) or not (data.get('token') or data.get('start_token')):
            raise ValueError('token or start_token required')
        env = data.get('env')
        for key in ('token', 'start_token'):
            if data.get(key) is not None:
                validate_token(data[key], env)
        if data.get('token') and (not isinstance(data.get('machine'), str) or not data['machine'].strip()):
            raise ValueError('machine required for activity update token')
        if data.get('token'):
            store.add(data['token'], env, 'activities', data['machine'])
        if data.get('start_token'):
            store.add(data['start_token'], env, 'starts')
        return 200, {'ok': True}
    except ValueError as exc:
        return 400, {'error': str(exc)}


def handle_test_push(data, client, store):
    if not isinstance(data, dict):
        return 400, {'error': 'expected JSON object'}
    if not client.config.ready:
        return 503, {'error': 'APNs configuration incomplete: set key_path, key_id and team_id'}
    if getattr(client, 'is_relay', False):
        result = client.send_alert(None, None, alert_payload('Herd test notification', 'Herd is connected.'))
        return 200, {'sent': result.get('sent', 0),
                     'errors': [] if result['ok'] else [f"{result['status']}: {result['reason']}"]}
    results = [client.send_alert(r['token'], r['env'], alert_payload('Herd test notification', 'Herd is connected.'))
               for r in store.snapshot()]
    return 200, {'sent': sum(r['ok'] for r in results), 'errors': [f"{r['status']}: {r['reason']}" for r in results if not r['ok']]}

@dataclass
class RelayConfig:
    relay_url: str
    bridge_id: str
    bridge_secret: str
    pair_code: str

    @property
    def ready(self):
        return bool(self.relay_url and self.bridge_id and self.bridge_secret)

    @classmethod
    def load(cls, directory=None):
        path = (Path(directory) if directory else caller_dir()) / 'herd-relay.json'
        return cls(**json.loads(path.read_text(encoding='utf-8')))


def register_bridge(relay_url, name=None, directory=None, client=None):
    """Create credentials once and save them owner-readable; never print secrets."""
    owned = client is None
    if client is None:
        import httpx
        client = httpx.Client(timeout=10)
    try:
        response = client.post(relay_url.rstrip('/') + '/v1/bridges', json={'name': name})
        response.raise_for_status()
        config = RelayConfig(relay_url.rstrip('/'), **response.json())
        path = (Path(directory) if directory else caller_dir()) / 'herd-relay.json'
        # Do not accidentally replace credentials belonging to another bridge.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(config.__dict__, out, indent=2)
        return config
    finally:
        if owned: client.close()


class RelayClient:
    """APNsClient-compatible adapter. Each call fans out once at the relay.

    token/env arguments are compatibility placeholders; never forwarded as targets.
    """
    is_relay = True

    def __init__(self, config, store=None, client=None, machine=None):
        self.config, self.store, self.machine = config, store, machine
        if client is None:
            import httpx
            client = httpx.Client(timeout=15)
        self.client = client

    def _post(self, payload):
        try:
            response = self.client.post(self.config.relay_url.rstrip('/') + '/v1/push',
                headers={'Authorization': 'Bearer ' + self.config.bridge_secret,
                         'X-Bridge-Id': self.config.bridge_id}, json=payload)
            if response.status_code != 200:
                return {'ok': False, 'status': response.status_code, 'reason': 'RelayError'}
            result = response.json()
            return {**result, 'ok': not result.get('failed') and not result.get('throttled'),
                    'status': 200, 'reason': None}
        except Exception:
            return {'ok': False, 'status': 0, 'reason': 'Relay transport failure'}

    def send_alert(self, token, env, payload, collapse_id=None, thread_id=None):
        aps = payload['aps']
        data = {'type': 'alert', 'title': aps['alert']['title'][:120],
                'body': aps['alert']['body'][:400], 'sound': bool(aps.get('sound'))}
        for key in ('badge', 'category'):
            if key in aps: data[key] = aps[key]
        if thread_id is not None or 'thread-id' in aps:
            data['thread_id'] = thread_id if thread_id is not None else aps['thread-id']
        if 't' in payload: data['t'] = payload['t']
        return self._post(data)

    def send_background(self, token, env):
        return self._post({'type': 'background'})

    def send_live_activity(self, token, env, event, content_state, stale_date, attributes=None):
        data = {'type': 'activity_start' if event == 'start' else 'activity',
                'content_state': content_state,
                'stale_date': int(stale_date.timestamp() if isinstance(stale_date, datetime) else stale_date)}
        if event == 'start': data['attributes'] = attributes
        else:
            data['event'] = event
            if self.machine: data['machine'] = self.machine
        return self._post(data)

    def close(self):
        self.client.close()


class RelayWatcherStore:
    """Synthetic targets: relay registrations live remotely, not in TokenStore."""
    def __init__(self, machine):
        self.machine = machine

    def snapshot(self, kind='devices'):
        return [{'start_token' if kind == 'starts' else 'token': 'relay',
                 'env': 'production', 'machine': self.machine}]
