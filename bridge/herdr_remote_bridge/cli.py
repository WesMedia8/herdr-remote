"""Local setup and diagnostics for Herdr Remote."""
import argparse
import json
import os
import platform
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from . import server
from .herd_push import RelayConfig, RelayClient, register_bridge


DEFAULT_RELAY_URL = "https://herdr-remote-relay-production.up.railway.app"

def save_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def build_pair_url(host, port, token, name, relay=None):
    host = '[' + host + ']' if ':' in host and not host.startswith('[') else host
    fields = {'url': f'http://{host}:{port}', 'token': token, 'name': name}
    if relay:
        fields.update(relay=relay.relay_url, bridge=relay.bridge_id, pair=relay.pair_code)
    return 'herdr-remote://pair?' + urlencode(fields)


def is_wsl():
    return platform.system() == 'Linux' and 'microsoft' in platform.release().lower()


def open_file(path):
    """Open a file in this computer's default viewer. True if a viewer was started."""
    try:
        system = platform.system()
        if system == 'Windows':
            os.startfile(str(path))  # noqa: only exists on Windows
            return True
        if system == 'Darwin':
            return subprocess.run(['open', str(path)], capture_output=True).returncode == 0
        if is_wsl() and shutil.which('wslpath') and shutil.which('explorer.exe'):
            win = subprocess.run(['wslpath', '-w', str(path)], capture_output=True, text=True).stdout.strip()
            subprocess.run(['explorer.exe', win], capture_output=True)  # explorer exits 1 even on success
            return bool(win)
        if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('xdg-open'):
            return subprocess.run(['xdg-open', str(path)], capture_output=True).returncode == 0
    except OSError:
        pass
    return False


def show_qr(url, directory, open_image=True):
    """Print the QR in the terminal and, when possible, open it as an image too.
    Returns the image path (it holds the token, so the caller deletes it)."""
    try:
        import segno
    except ImportError:
        print(url)
        print('No QR code: the segno package is missing. Install it with: python3 -m pip install segno')
        print('(or open the link above on the iPhone some other way)')
        return None
    qr = segno.make(url, micro=False, error='m')
    qr.terminal(compact=True)
    if not open_image:
        return None
    image = Path(directory) / 'pair-qr.png'
    try:
        qr.save(str(image), scale=10, border=4, dark='#000000', light='#ffffff')
    except OSError:
        return None
    if open_file(image):
        print('Also opened as an image (it\'s deleted once you\'re paired).')
    return image


def bridge_running(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def wait_for_phone(directory, token, timeout=600, relay_client=None, baseline=None):
    """Wait until the bridge reports a request made with this token (see Runtime.note_paired)."""
    marker = Path(directory) / 'herd-paired.json'
    fingerprint = server.token_fingerprint(token)
    print('Scan the code with your iPhone camera. Waiting for Herdr Remote to connect... (Ctrl+C to stop)')
    deadline = time.time() + timeout
    connected = False
    notification_deadline = None
    next_poll = 0
    while time.time() < deadline:
        try:
            info = json.loads(marker.read_text(encoding='utf-8'))
            if info.get('token') == fingerprint:
                if not connected:
                    print('✓ Herdr Remote connected')
                    connected = True
                    notification_deadline = time.time() + 60
                    deadline = max(deadline, notification_deadline + 1)
                if relay_client is None:
                    return True
        except (OSError, ValueError):
            pass
        if relay_client is not None and time.time() >= next_poll:
            next_poll = time.time() + 2
            try:
                status = relay_client.status()
                if baseline is not None and (
                    (status.get('last_registered_at') or 0) > (baseline.get('last_registered_at') or 0)
                    or any(status.get('devices', {}).get(k, 0) > baseline.get('devices', {}).get(k, 0)
                           for k in ('alert', 'activity', 'start'))):
                    if connected:
                        print('✓ Notifications on')
                        return True
            except Exception:
                pass
        if connected and time.time() >= notification_deadline:
            print("Connected, but the phone hasn't turned on notifications yet — open Herdr Remote and allow notifications.")
            return False
        time.sleep(1)
    print('No phone connected yet. Run pair again whenever you\'re ready.')
    return False


def pair(relay_url=None, new_token=False, wait=True, open_image=True, no_relay=False):
    cfg = server.load_config()
    host = cfg.get('host') or server.tailscale_ip()
    if not host:
        raise ValueError('Start Tailscale or set host in config.json before pairing.')
    token = (None if new_token else cfg.get('token')) or secrets.token_urlsafe(32)
    path = server.config_path()
    stored = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if stored.get('token') != token:
        stored['token'] = token
        save_private(path, stored)
    name = socket.gethostname()
    relay_path = path.parent / 'herd-relay.json'
    relay = None
    relay_url = relay_url or os.environ.get('HERD_RELAY_URL')
    if not no_relay:
        import httpx
        try:
            if relay_path.exists():
                relay = RelayConfig.load(path.parent)
                if relay_url and relay_url.rstrip('/') != relay.relay_url.rstrip('/'):
                    raise ValueError('Existing relay differs; use a separate HERD_CONFIG directory for another bridge.')
                with httpx.Client(timeout=10) as client:
                    result = client.post(relay.relay_url.rstrip('/') + '/v1/bridges/rotate-pair',
                                         headers={'Authorization': 'Bearer ' + relay.bridge_secret,
                                                  'X-Bridge-Id': relay.bridge_id})
                    result.raise_for_status()
                    relay.pair_code = result.json()['pair_code']
                save_private(relay_path, relay.__dict__)
            else:
                relay_url = relay_url or DEFAULT_RELAY_URL
                parsed = urlsplit(relay_url)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Relay URL must be HTTPS without credentials.')
                relay = register_bridge(relay_url, name, path.parent)
        except httpx.RequestError:
            relay = None
            print('Warning: relay unavailable; continuing with local-only pairing (no push).')
    port = int(cfg.get('port') or 8787)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show progress at once, even when piped
    except (AttributeError, ValueError):
        pass
    # Closing the window or killing pair must still delete the QR image (it holds the token).
    def stop(*_):
        raise KeyboardInterrupt
    for sig_name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, sig_name):
            try:
                signal.signal(getattr(signal, sig_name), stop)
            except (ValueError, OSError):
                pass  # not the main thread
    print(f'Config: {path}')
    if new_token:
        print('New token made: phones paired before need to scan this code again.')
    (path.parent / 'herd-paired.json').unlink(missing_ok=True)
    relay_client = RelayClient(relay) if relay and wait else None
    baseline = None
    if relay_client:
        try:
            baseline = relay_client.status()
        except Exception:
            print('Warning: unable to check relay registration; notification success cannot be verified.')
    image = show_qr(build_pair_url(host, port, token, name, relay), path.parent, open_image)
    try:
        if not bridge_running(host, port):
            print(f'Heads up: the bridge isn\'t running on {host}:{port}, so the phone can\'t connect yet.')
            print('Start it in another window with: herdr-remote-bridge serve  (or: herdr-remote-bridge service install)')
        if wait:
            wait_for_phone(path.parent, token, relay_client=relay_client, baseline=baseline)
    except KeyboardInterrupt:
        print()
    finally:
        if relay_client:
            relay_client.close()
        if image:
            try:
                image.unlink(missing_ok=True)
            except OSError:
                print(f'Delete {image} when you\'re done: it contains your bridge token.')


def doctor(relay_url=None):
    cfg = server.load_config()
    print(f'Config: {server.config_path()}')
    checks = []
    def check(ok, label, fix):
        checks.append(bool(ok))
        print(('✓ ' if ok else '✗ ') + label + ('' if ok else ' — ' + fix))
    check(sys.version_info >= (3, 10), 'Python >=3.10', 'Install Python 3.10 or newer.')
    herdr_hint = {
        'Darwin': 'Enable herdr CLI tools in the app; check /Applications/herdr.app/Contents/MacOS and ~/.local/bin.',
        'Windows': 'Check herdr under %LOCALAPPDATA%\\Programs and add its CLI tools directory to PATH.',
    }.get(platform.system(), 'Check the herdr installation bin directory (often ~/.local/bin) and add it to PATH.') + ' Or set hc/herdr_app absolute paths in config.json.'
    for key, executable in [('hc', 'hc'), ('herdr_app', 'herdr-app')]:
        check(shutil.which(cfg.get(key) or executable), executable + ' executable', herdr_hint)
    ip, method = server.detect_tailscale()
    fix = 'sudo tailscale up' if platform.system() == 'Linux' else 'Open the Tailscale app and sign in'
    check(ip, 'Tailscale IPv4 detected' + (f': {ip} via {method}' if ip else ''), fix)
    host, port = cfg.get('host') or ip, int(cfg.get('port') or 8787)
    if host:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f'✓ Port {port} listening (verify this is your bridge)')
        except OSError:
            try:
                with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as sock:
                    sock.bind((host, port))
                print(f'✓ Port {port} free; run herdr-remote-bridge serve')
            except OSError:
                check(False, f'Port {port} unavailable', 'Check host binding or choose another port.')
    else:
        check(False, 'Port check', 'Configure a local host address first.')
    check(server.config_path().is_file(), 'config.json present', 'Run herdr-remote-bridge pair.')
    check(cfg.get('token'), 'API token configured', 'Run herdr-remote-bridge pair.')
    relay_path = server.config_path().parent / 'herd-relay.json'
    relay_url = (relay_url or os.environ.get('HERD_RELAY_URL')
                 or (RelayConfig.load(relay_path.parent).relay_url if relay_path.exists() else None)
                 or DEFAULT_RELAY_URL)
    if relay_url:
        try:
            with urlopen(relay_url.rstrip('/') + '/healthz', timeout=5) as response:
                healthy = response.status == 200
        except Exception:
            healthy = False
        check(healthy, 'Relay /healthz reachable', 'Check relay URL, network and relay service.')
    else:
        print('✓ Relay not configured (optional; use pair --relay https://<relay-host>)')
    check((server.config_path().parent / 'herd-push.json').is_file(), 'Direct APNs config present',
          'Optional with relay; otherwise create herd-push.json beside config.json for pushes.')
    return 0 if all(checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('serve', help='Run the bridge (default)')
    pairing = commands.add_parser('pair', help='Create token and print pairing QR; rotates existing relay pair code')
    relay_options = pairing.add_mutually_exclusive_group()
    relay_options.add_argument('--relay', metavar='URL')
    relay_options.add_argument('--no-relay', action='store_true', help='Pair locally without relay push notifications')
    pairing.add_argument('--new-token', action='store_true', help='Replace the token (phones must scan again)')
    pairing.add_argument('--no-wait', action='store_true', help="Don't wait for the phone to connect")
    pairing.add_argument('--no-open', action='store_true', help="Only print the QR; don't open it as an image")
    diagnostics = commands.add_parser('doctor', help='Check local prerequisites and configuration')
    diagnostics.add_argument('--relay', metavar='URL')
    service = commands.add_parser('service', help='Manage per-user startup')
    service.add_argument('action', choices=['install', 'uninstall', 'status'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'service':
            from .service import manage
            return manage(args.action)
        elif args.command == 'pair':
            pair(args.relay, new_token=args.new_token, wait=not args.no_wait, open_image=not args.no_open, no_relay=args.no_relay)
        elif args.command == 'doctor':
            return doctor(args.relay)
        else:
            server.serve()
    except Exception:
        parser.exit(1, 'Bridge setup failed. Check configuration, file permissions and service connectivity; no credentials logged.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
