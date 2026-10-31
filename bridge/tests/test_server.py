"""Offline HTTP boundary tests: in-memory sockets and fake herdr/push providers."""
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from urllib.parse import parse_qs, urlsplit

from bridge.herdr_remote_bridge import server, cli
from bridge.herdr_remote_bridge.herd_extras import SpaceStore
from bridge.herdr_remote_bridge.herd_push import RelayConfig


class Socket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()
    def makefile(self, *args, **kwargs):
        return self.input
    def sendall(self, data):
        self.output.write(data)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = server.Runtime(self.root)

    def request(self, method, path, body=None, auth=None, token='', raw=None, headers=''):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        request = f'{method} {path} HTTP/1.0\r\nContent-Length: {len(data)}\r\n{headers}'
        if auth is not None:
            request += f'Authorization: {auth}\r\n'
        sock = Socket(request.encode() + b'\r\n' + data)
        with patch.object(server, 'CONFIG', {'token': token}):
            server.Handler(sock, ('127.0.0.1', 1234), SimpleNamespace(runtime=self.runtime))
        head, payload = sock.output.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split()[1]), json.loads(payload)

    def test_auth_required_optional_and_unknown_routes(self):
        with patch.object(server, 'agents_snapshot', return_value={'terminals': [], 'extra': True}):
            self.assertEqual(self.request('GET', '/api/agents')[0], 200)
            for method in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'CONNECT'):
                for path in ('/api/agents', '/api/not-found', '/api/send'):
                    self.assertEqual(self.request(method, path, token='secret'), (401, {'error': 'unauthorized'}))
            self.assertEqual(self.request('GET', '/api/agents', token='secret', auth='Bearer secret'),
                             (200, {'terminals': [], 'extra': True}))
            self.assertEqual(self.request('GET', '/api/agents', token='secret', auth='Bearer wrong')[0], 401)
            for method, path in [('GET', '/api/new'), ('POST', '/api/agents'), ('GET', '/api/agents/'),
                                 ('POST', '/api/space/'), ('GET', '/api/nope')]:
                self.assertEqual(self.request(method, path), (404, {'error': 'not found'}))

    def test_register_activity_and_json_limits(self):
        self.assertEqual(self.request('POST', '/api/register', {'token': 'aa', 'env': 'production'})[0], 200)
        self.assertEqual(self.request('POST', '/api/activity', {'start_token': 'bb', 'env': 'development'})[0], 200)
        self.assertEqual(self.request('POST', '/api/test-push')[0], 503)
        for raw in (b'[]', b'{broken'):
            self.assertEqual(self.request('POST', '/api/register', raw=raw)[0], 400)
        self.assertEqual(self.request('POST', '/api/register', raw=b' ' * 20001)[0], 413)
        self.assertEqual(self.request('POST', '/api/register', headers='Transfer-Encoding: chunked\r\n')[0], 400)

    def test_spaces_merge(self):
        self.runtime.spaces.set('Demo', str(self.root))
        self.runtime.spaces.set('Saved', str(self.root))
        with patch.object(server, 'app_spaces', return_value=[{'label': 'demo', 'folder': '/live'}]):
            spaces = self.request('GET', '/api/spaces')[1]['spaces']
            self.assertEqual(spaces, [{'name': 'demo', 'cwd': '/live'}, {'name': 'Saved', 'cwd': str(self.root)}])
        with patch.object(server, 'app_spaces', return_value=[{'label': 'demo', 'folder': ''}]):
            self.assertEqual(self.request('GET', '/api/spaces')[1]['spaces'][0]['cwd'], str(self.root))

    def test_new_uses_saved_default_and_preserves_space_rule(self):
        self.runtime.spaces.set('Demo', str(self.root))
        existing = [{'id': 's1', 'label': 'Demo', 'aliases': {'Demo'}, 'folder': '/unused'}]
        for same_space in (False, True):
            with patch.object(server, 'app_spaces', return_value=existing), \
                 patch.object(server, 'live_terminals', side_effect=[[], [{'address': 't2', 'where': 'Demo / 1'}]]), \
                 patch.object(server, 'app', return_value=(0, '{"terminal":"t2"}', '')) as app:
                self.assertEqual(self.request('POST', '/api/new', {'space': 'Demo', 'agent': 'sh', 'same_space': same_space})[0], 200)
                args = app.call_args.args
                self.assertEqual(args[1], 'tab.new' if same_space else 'space.new')
                self.assertEqual(json.loads(args[2])['folder'], str(self.root))

    def test_chat_upload_and_address_checks(self):
        with patch.object(server, 'live_terminals', return_value=[{'address': 't1', 'agent': 'claude-code', 'cwd': '/demo'},
                                                                 {'address': 't2', 'cwd': '/demo'}]):
            self.assertEqual(self.request('GET', '/api/chat?t=t1')[0], 400)
            self.assertEqual(self.request('GET', '/api/chat?t=t3')[0], 404)
            self.assertEqual(self.request('POST', '/api/upload', {'t': 't3'})[0], 404)
        for address in ('t', 't123456789', 'tA', 't1\n'):
            self.assertIsNone(server.ADDRESS.fullmatch(address))
        with patch.object(server, 'live_terminals', return_value=[{'address': 't1', 'agent': 'codex', 'session_id': 'id'}]), \
             patch.object(server, 'handle_chat', return_value=(200, {'messages': []})) as chat:
            self.assertEqual(self.request('GET', '/api/chat?t=t1&since=x')[0], 200)
            self.assertEqual(chat.call_args.kwargs['session_id'], 'id')

    def test_watcher_wiring_and_selection(self):
        self.assertIsNone(self.runtime.watcher)
        (self.root / 'herd-push.json').write_text('{}')
        terms = Mock(return_value=[{'address': 't1', 'status': 'working'}])
        with patch.object(server, 'APNsClient') as apns:
            runtime = server.Runtime(self.root, get_terminals=terms)
            self.assertIs(runtime.watcher.get_terminals, terms)
            self.assertEqual(runtime.watcher.poll(runtime.watcher.get_terminals()), [])
            runtime.watcher.executor.shutdown()
        (self.root / 'herd-relay.json').write_text(json.dumps(dict(relay_url='https://relay.example', bridge_id='id', bridge_secret='secret', pair_code='pair')))
        with patch.object(server, 'RelayClient') as relay, patch.object(server, 'APNsClient') as apns:
            runtime = server.Runtime(self.root, get_terminals=terms)
            relay.assert_called_once()
            apns.assert_not_called()
            runtime.watcher.executor.shutdown()
        with patch.object(server, 'agents_snapshot', return_value={'terminals': [{'address': 't1'}]}):
            self.assertEqual(server.live_terminals(), [{'address': 't1'}])

    def test_remaining_routes_and_rpc_environment(self):
        with patch.object(server, 'hc', return_value=(0, '\x1b[31mhello', '')) as hc:
            self.assertEqual(self.request('GET', '/api/read?t=t1'), (200, {'text': 'hello'}))
            self.assertEqual(self.request('POST', '/api/keys', {'t': 't1', 'key': 'enter'})[0], 200)
            self.assertEqual(self.request('POST', '/api/keys', {'t': 't1', 'key': 'bad'})[0], 400)
        with patch.object(server, 'hc_rpc', return_value=(0, '{}', '')) as rpc:
            self.assertEqual(self.request('POST', '/api/send', {'t': 't1', 'text': 'hello'})[0], 200)
            rpc.assert_called_once_with('terminal.send', {'terminal': 't1', 'text': 'hello'})
        process = Mock()
        process.stdout.readline.side_effect = ['{}', '{"result":{}}']
        with patch.object(server.subprocess, 'Popen', return_value=process) as popen, \
             patch.dict('os.environ', {'HC_TERMINAL_ID': 't9'}):
            self.assertEqual(server.hc_rpc('terminal.send', {'terminal': 't1', 'text': 'hello'})[0], 0)
            self.assertNotIn('HC_TERMINAL_ID', popen.call_args.kwargs['env'])
        with patch.object(server, 'handle_dirs', return_value=(200, {'dirs': []})):
            self.assertEqual(self.request('GET', '/api/dirs')[0], 200)
        with patch.object(server, 'app_spaces', return_value=[]), \
             patch.object(server, 'app', return_value=(0, '{}', '')) as app:
            self.assertEqual(self.request('POST', '/api/space', {'space': 'Demo', 'cwd': str(self.root)})[0], 200)
            self.assertEqual(app.call_args.args[1], 'space.new')
        with patch.object(server, 'app_spaces', return_value=[{'aliases': {'Demo'}}]), patch.object(server, 'app') as app:
            self.assertEqual(self.request('POST', '/api/space', {'space': 'Demo', 'cwd': str(self.root)})[0], 200)
            app.assert_not_called()
        with patch.object(self.runtime.spaces, 'set', side_effect=OSError('private-path')):
            self.assertEqual(self.request('POST', '/api/space', {'space': 'Demo', 'cwd': str(self.root)}),
                             (500, {'error': 'storage unavailable'}))

    def test_relay_pair_registration_and_rotation(self):
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1"}')
        relay = RelayConfig('https://relay.example', 'id', 'secret', 'old')
        def register(url, name, directory):
            cli.save_private(Path(directory) / 'herd-relay.json', relay.__dict__)
            return relay
        httpx = Mock()
        httpx.Client.return_value.__enter__ = Mock(return_value=httpx)
        httpx.Client.return_value.__exit__ = Mock(return_value=False)
        httpx.post.return_value.json.return_value = {'pair_code': 'replacement'}
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(cli, 'register_bridge', side_effect=register) as registration, \
             patch.dict('sys.modules', {'httpx': httpx}), patch.object(cli, 'show_qr', return_value=None) as qr, patch.object(cli, 'bridge_running', return_value=True):
            cli.pair('https://relay.example', wait=False)
            registration.assert_called_once()
            cli.pair(wait=False)
            self.assertEqual(registration.call_count, 1)
            self.assertEqual(RelayConfig.load(self.root).pair_code, 'replacement')
            self.assertIn('pair=replacement', qr.call_args.args[0])
            self.assertNotIn('secret', qr.call_args.args[0])

    def test_pair_relay_defaults_overrides_opt_out_and_network_fallback(self):
        import httpx
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1"}')
        relay = RelayConfig(cli.DEFAULT_RELAY_URL, 'id', 'secret', 'code')
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(cli, 'register_bridge', return_value=relay) as register, \
             patch.object(cli, 'show_qr', return_value=None) as qr, \
             patch.object(cli, 'bridge_running', return_value=True), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            cli.main(['pair', '--no-wait', '--no-open'])
            self.assertEqual(register.call_args.args[0], cli.DEFAULT_RELAY_URL)
            self.assertIn('relay=', qr.call_args.args[0])
            with patch.dict('os.environ', {'HERD_RELAY_URL': 'https://custom.example'}):
                cli.pair(wait=False)
                self.assertEqual(register.call_args.args[0], 'https://custom.example')
                cli.pair('https://explicit.example', wait=False)
                self.assertEqual(register.call_args.args[0], 'https://explicit.example')
                register.reset_mock()
                cli.save_private(self.root / 'herd-relay.json', relay.__dict__)
                cli.main(['pair', '--no-relay', '--no-wait', '--no-open'])
                register.assert_not_called()
                self.assertNotIn('relay=', qr.call_args.args[0])
                (self.root / 'herd-relay.json').unlink()
            for error in (httpx.ConnectError('private detail'), httpx.ReadTimeout('private detail')):
                register.side_effect = error
                cli.pair(wait=False)
                self.assertNotIn('relay=', qr.call_args.args[0])
                self.assertTrue(json.loads(path.read_text())['token'])
                self.assertFalse((self.root / 'herd-relay.json').exists())
            self.assertEqual(output.getvalue().count('Warning: relay unavailable;'), 2)
            self.assertNotIn('private detail', output.getvalue())

    def test_doctor_relay_default_and_precedence(self):
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1","token":"test"}')
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(cli, 'urlopen') as request, \
             patch.object(cli.socket, 'create_connection'), \
             patch.object(server, 'detect_tailscale', return_value=('127.0.0.1', 'mock')), \
             patch.object(cli.shutil, 'which', return_value='/mock/tool'), \
             patch('sys.stdout', new_callable=io.StringIO):
            request.return_value.__enter__.return_value.status = 200
            cli.doctor()
            request.assert_called_with(cli.DEFAULT_RELAY_URL + '/healthz', timeout=5)
            cli.save_private(self.root / 'herd-relay.json',
                             RelayConfig('https://saved.example', 'id', 'secret', 'code').__dict__)
            cli.doctor()
            request.assert_called_with('https://saved.example/healthz', timeout=5)
            with patch.dict('os.environ', {'HERD_RELAY_URL': 'https://env.example'}):
                cli.doctor()
                request.assert_called_with('https://env.example/healthz', timeout=5)
                cli.main(['doctor', '--relay', 'https://explicit.example'])
                request.assert_called_with('https://explicit.example/healthz', timeout=5)

    def test_pair_url_and_qr_fallback(self):
        relay = RelayConfig('https://relay.example', 'id', 'NEVER-IN-QR', 'p&?')
        url = cli.build_pair_url('100.64.0.1', 8787, 'a+b&c', 'Office PC', relay)
        fields = parse_qs(urlsplit(url).query)
        self.assertEqual(fields['url'], ['http://100.64.0.1:8787'])
        self.assertEqual(fields['token'], ['a+b&c'])
        self.assertEqual(fields['pair'], ['p&?'])
        self.assertNotIn('NEVER-IN-QR', url)
        with patch.dict('sys.modules', {'segno': None}), patch('sys.stdout', new_callable=io.StringIO) as output:
            cli.show_qr(url, self.root, open_image=False)
            self.assertIn(url, output.getvalue())
            self.assertIn('install', output.getvalue())
        fake = Mock()
        with patch.dict('sys.modules', {'segno': fake}), patch('sys.stdout', new_callable=io.StringIO):
            cli.show_qr(url, self.root, open_image=False)
        fake.make.return_value.terminal.assert_called_once_with(compact=True)

    def test_pair_persists_token_without_replacing_config(self):
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1","port":9999}')
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), patch.object(cli, 'show_qr', return_value=None) as qr, patch.object(cli, 'bridge_running', return_value=True):
            cli.pair(wait=False, no_relay=True)
            first = json.loads(path.read_text())
            cli.pair(wait=False, no_relay=True)
            self.assertEqual(json.loads(path.read_text()), first)
            self.assertEqual(first['port'], 9999)
            self.assertTrue(first['token'])
            self.assertIn('9999', qr.call_args.args[0])

    def test_pair_new_token_replaces_token(self):
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1","token":"old"}')
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(cli, 'show_qr', return_value=None), patch.object(cli, 'bridge_running', return_value=True):
            cli.pair(wait=False, no_relay=True)
            self.assertEqual(json.loads(path.read_text())['token'], 'old')
            cli.pair(new_token=True, wait=False, no_relay=True)
            self.assertNotEqual(json.loads(path.read_text())['token'], 'old')

    def test_pair_deletes_qr_image_holding_token(self):
        path = self.root / 'config.json'
        path.write_text('{"host":"127.0.0.1","token":"t"}')
        image = self.root / 'pair-qr.png'
        def fake_show(url, directory, open_image=True):
            image.write_bytes(b'png')
            return image
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(cli, 'show_qr', side_effect=fake_show), patch.object(cli, 'bridge_running', return_value=True), \
             patch('sys.stdout', new_callable=io.StringIO):
            cli.pair(wait=False, no_relay=True)
        self.assertFalse(image.exists())

    def test_bridge_records_phone_and_pair_sees_it(self):
        with patch.object(server, 'agents_snapshot', return_value={'terminals': []}):
            self.request('GET', '/api/agents', token='secret', auth='Bearer secret')
        marker = json.loads((self.root / 'herd-paired.json').read_text())
        self.assertEqual(marker['token'], server.token_fingerprint('secret'))
        self.assertNotIn('secret', json.dumps(marker))
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertTrue(cli.wait_for_phone(self.root, 'secret', timeout=2))
        self.assertIn('connected', output.getvalue())
        with patch('sys.stdout', new_callable=io.StringIO), patch.object(cli.time, 'sleep'):
            self.assertFalse(cli.wait_for_phone(self.root, 'other-token', timeout=0.01))

    def test_token_reloads_when_config_changes(self):
        path = self.root / 'config.json'
        path.write_text('{"token":"first"}')
        with patch.dict('os.environ', {'HERD_CONFIG': str(path)}, clear=True), \
             patch.object(server, 'CONFIG', {'token': 'first'}), patch.object(server, '_CONFIG_MTIME', path.stat().st_mtime_ns):
            self.assertEqual(server.current_token(), 'first')
            path.write_text('{"token":"second"}')
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
            self.assertEqual(server.current_token(), 'second')
            path.write_text('{not json')
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 2_000_000))
            self.assertEqual(server.current_token(), 'second')


if __name__ == '__main__':
    unittest.main()
