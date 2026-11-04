import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from bridge.herdr_remote_bridge.herd_push import (APNsClient, PushConfig, StatusWatcher, TokenStore, alert_payload,
                              content_state, handle_activity, handle_register, live_payload, to_apple_date)
from bridge.herdr_remote_bridge.herd_extras import (SpaceStore, handle_dirs, handle_space, handle_upload,
                                parse_transcript, sanitize_filename)


class HerdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TokenStore(self.root / 'tokens.json')

    def test_apple_date(self):
        self.assertEqual(to_apple_date(datetime(2001, 1, 1, tzinfo=timezone.utc)), 0)
        self.assertEqual(to_apple_date(978307201.5), 1.5)
        with self.assertRaises(ValueError):
            to_apple_date(datetime(2001, 1, 1))

    def test_jwt(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            self.skipTest('PyJWT/cryptography unavailable')
        key = ec.generate_private_key(ec.SECP256R1())
        path = self.root / 'throwaway.p8'
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        client = APNsClient(PushConfig(str(path), 'KEY123', 'TEAM123'), self.store)
        token = client.provider_token()
        self.assertEqual(jwt.get_unverified_header(token)['alg'], 'ES256')
        self.assertEqual(jwt.get_unverified_header(token)['kid'], 'KEY123')
        claims = jwt.decode(token, key.public_key(), algorithms=['ES256'])
        self.assertEqual(set(claims), {'iss', 'iat'})
        self.assertEqual(claims['iss'], 'TEAM123')
        self.assertIsInstance(claims['iat'], int)
        self.assertEqual(client.provider_token(), token)
        client._issued -= 3001
        client.provider_token()
        self.assertGreater(client._issued, 0)

    def test_payloads_and_transport(self):
        state = content_state([{'address': 't2', 'status': 'blocked', 'agent': 'pwsh', 'where': 'Project / 1', 'preview': '\x1b[31mHi\r\nthere'}], 978307205)
        self.assertEqual(set(state), {'needsYou', 'working', 'done', 'total', 'headline', 'detail', 'topAddress', 'updatedAt'})
        self.assertEqual(state['updatedAt'], 5)
        self.assertEqual(state['detail'], 'Hi there')
        self.assertEqual(state['headline'], 'Project · Shell needs you')
        payload = live_payload('start', state, 978307805, {'machine': 'Windows'}, now=978307205)
        self.assertEqual(payload['aps']['timestamp'], 978307205)
        self.assertEqual(payload['aps']['attributes-type'], 'HerdActivityAttributes')
        self.assertEqual(payload['aps']['content-state'], state)
        alert = alert_payload('Project / 1', 'Shell needs you: Hi', 't2', 1, 'AGENT_BLOCKED')
        self.assertEqual(alert['aps']['thread-id'], 't2')
        self.assertEqual(alert['t'], 't2')
        self.assertNotIn('category', alert_payload('x', 'done')['aps'])
        class Response:
            status_code = 410
            def json(self):
                return {'reason': 'Unregistered'}
        class Transport:
            def post(inner, url, **kwargs):
                self.assertIn('api.sandbox.push.apple.com', url)
                self.assertEqual(kwargs['headers']['apns-topic'], 'app.herdr-remote.push-type.liveactivity')
                self.assertEqual(kwargs['headers']['apns-priority'], '10')
                return Response()
        self.store.add('ab', 'development', 'starts')
        self.store.add('ab', 'production', 'starts')
        self.store.add('ab', 'development')
        client = APNsClient(PushConfig('unused', 'kid', 'team'), self.store, Transport())
        client.provider_token = lambda: 'fake'
        result = client.send_live_activity('ab', 'development', 'start', state, 978307805, {'machine': 'Windows'})
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.store.snapshot('starts')), 1)
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_registration(self):
        self.assertEqual(handle_register({'token': 'AB', 'env': 'development'}, self.store)[0], 200)
        handle_register({'token': 'ab', 'env': 'development'}, self.store)
        self.assertEqual(len(self.store.snapshot()), 1)
        self.assertEqual(handle_register({'token': 'x', 'env': 'development'}, self.store)[0], 400)
        self.assertEqual(handle_activity({'token': 'cd', 'env': 'development'}, self.store)[0], 400)
        self.assertEqual(handle_activity({'token': 'cd', 'start_token': 'ef', 'env': 'development', 'machine': 'PC'}, self.store)[0], 200)
        self.assertEqual(TokenStore(self.store.path).snapshot(), self.store.snapshot())

    def test_watcher(self):
        self.store.add('ab', 'development')
        self.store.add('cd', 'development', 'activities', 'PC')
        watcher = StatusWatcher(lambda: [], None, self.store, 'PC')
        self.addCleanup(watcher.executor.shutdown)
        t = {'address': 't1', 'agent': 'claude-code', 'where': 'Work / 1', 'status': 'working', 'preview': 'Approve?'}
        self.assertEqual(watcher.poll([t], 0), [])
        t['status'] = 'blocked'
        self.assertFalse(any(j[0] == 'alert' for j in watcher.poll([t], 2)))
        t['status'] = 'working'
        watcher.poll([t], 4)
        t['status'] = 'blocked'
        watcher.poll([t], 6)
        self.assertFalse(any(j[0] == 'alert' for j in watcher.poll([t], 8)))
        jobs = watcher.poll([t], 10)
        alerts = [j for j in jobs if j[0] == 'alert']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][2]['aps']['alert']['body'], 'Claude needs you: Approve?')
        self.assertFalse(any(j[0] == 'alert' for j in watcher.poll([t], 12)))
        # Throttle retains the latest changed preview until its window expires.
        t['preview'] = 'Newest'
        watcher.poll([t], 14)
        t['preview'] = 'Latest'
        watcher.poll([t], 16)
        updates = [j for j in watcher.poll([t], 24) if j[0] == 'update']
        self.assertEqual(updates[0][2]['detail'], 'Latest')
        t['status'] = 'done'
        watcher.poll([t], 42)
        alerts = [j for j in watcher.poll([t], 46) if j[0] == 'alert']
        self.assertNotIn('category', alerts[0][2]['aps'])
        self.assertIn('finished', alerts[0][2]['aps']['alert']['body'])

    def test_watcher_preview_body(self):
        self.store.add('ab', 'development')
        preview = 'word ' * 20 + '. Another sentence.'
        for status, summarize, expected in (
            ('done', None, ' '.join(['word'] * 12) + '…'),
            ('done', lambda terminal: None, ' '.join(['word'] * 12) + '…'),
            ('done', None, 'Built the page.'),
            ('blocked', None, preview),
        ):
            with self.subTest(status=status, summarize=summarize, expected=expected):
                watcher = StatusWatcher(lambda: [], None, self.store, 'PC', summarize=summarize)
                self.addCleanup(watcher.executor.shutdown)
                text = 'Built the page. Tested it too.' if expected == 'Built the page.' else preview
                terminal = dict(address='t1', agent='codex', status='working', preview=text)
                watcher.poll([terminal], 0)
                terminal['status'] = status
                watcher.poll([terminal], 2)
                jobs = watcher.poll([terminal], 6)
                body = next(j[2]['aps']['alert']['body'] for j in jobs if j[0] == 'alert')
                label = 'needs you' if status == 'blocked' else 'finished'
                self.assertEqual(body, f'Codex {label}: {expected}')

    def test_watcher_finished_transitions(self):
        cases = (
            ('working to idle', 'working', [(2, 'idle'), (5, 'idle'), (6, 'idle'), (40, 'idle')], 1),
            ('finished flicker', 'working', [(2, 'done'), (4, 'idle'), (6, 'idle'), (40, 'done')], 1),
            ('idle to done', 'idle', [(2, 'done'), (6, 'done'), (40, 'done')], 0),
            ('unknown to idle', 'unknown', [(2, 'idle'), (6, 'idle'), (40, 'idle')], 0),
            ('initial idle', 'idle', [(2, 'idle'), (6, 'idle')], 0),
        )
        self.store.add('ab', 'development')
        for name, initial, polls, expected in cases:
            with self.subTest(name=name):
                watcher = StatusWatcher(lambda: [], None, self.store, 'PC')
                self.addCleanup(watcher.executor.shutdown)
                terminal = {'address': 't1', 'agent': 'codex', 'status': initial, 'preview': 'Ready'}
                self.assertEqual(watcher.poll([terminal], 0), [])
                alerts = []
                for now, status in polls:
                    terminal['status'] = status
                    jobs = [j for j in watcher.poll([terminal], now) if j[0] == 'alert']
                    if now < 6:
                        self.assertEqual(jobs, [])
                    if expected and now == 6:
                        self.assertEqual(len(jobs), 1)
                    alerts.extend(jobs)
                self.assertEqual(len(alerts), expected)
                self.assertEqual(watcher.previous['t1'], polls[-1][1])
                for _, _, payload in alerts:
                    self.assertEqual(payload['aps']['alert']['body'], 'Codex finished: Ready')
                    self.assertNotIn('category', payload['aps'])

    def test_start_once_and_machine_filter(self):
        self.store.add('ab', 'development', 'starts')
        self.store.add('cd', 'development', 'activities', 'Other')
        watcher = StatusWatcher(lambda: [], None, self.store, 'PC')
        self.addCleanup(watcher.executor.shutdown)
        t = {'address': 't1', 'status': 'blocked'}
        watcher.poll([t], 0)
        self.assertEqual([j[0] for j in watcher.poll([t], 2)], ['start'])
        self.assertEqual(watcher.poll([t], 12), [])

    def test_transcripts(self):
        path = self.root / 'session.jsonl'
        entries = [
            {'type': 'system', 'message': {'content': 'ignore'}},
            {'type': 'user', 'uuid': 'u', 'message': {'role': 'user', 'content': 'Hello'}},
            {'type': 'assistant', 'uuid': 'a', 'timestamp': '2026-10-31T10:00:00Z', 'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'One'}, {'type': 'text', 'text': 'Two'}, {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pwd'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'SECRET'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Codex'}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'arguments': '{"command":"ls"}'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'duplicate'}}]
        path.write_text('\n'.join(map(json.dumps, entries)) + '\n{partial', encoding='utf-8')
        messages = parse_transcript(path)
        self.assertEqual([m['text'] for m in messages], ['Hello', 'One\nTwo', 'Bash(pwd)', 'Codex', 'exec_command(ls)'])
        self.assertEqual(messages[2]['role'], 'tool')
        self.assertEqual(parse_transcript(path, messages[1]['id']), messages[2:])
        self.assertEqual(parse_transcript(path), messages)

    def test_files_and_spaces(self):
        self.assertEqual(sanitize_filename('photo nice.JPG'), 'photo_nice.JPG')
        for name in ('../bad.jpg', 'a\\b.png', 'CON.jpg', 'a.exe', '/tmp/a.jpg'):
            with self.assertRaises(ValueError):
                sanitize_filename(name)
        self.assertEqual(handle_upload({'filename': 'a.jpg', 'data': base64.b64encode(b'not an image').decode()}, self.root)[0], 400)
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')
        # Build a CRC-valid minimal structural fixture.
        import struct, zlib
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff')) + chunk(b'IEND', b'')
        status, result = handle_upload({'filename': 'a.png', 'data': base64.b64encode(png).decode()}, self.root)
        self.assertEqual(status, 200)
        self.assertTrue(Path(result['path']).is_absolute())
        (self.root / '.hidden').mkdir()
        (self.root / 'AppData').mkdir()
        (self.root / 'Project').mkdir()
        status, result = handle_dirs({'path': str(self.root)}, root=self.root)
        self.assertEqual(result['dirs'], ['Project'])
        self.assertIsNone(result['parent'])
        store = SpaceStore(self.root / 'spaces.json')
        self.assertEqual(handle_space({'space': 'Project', 'cwd': str(self.root)}, store)[0], 200)
        self.assertEqual(len(store.spaces(['Project', 'Project'])), 1)


if __name__ == '__main__':
    unittest.main()
