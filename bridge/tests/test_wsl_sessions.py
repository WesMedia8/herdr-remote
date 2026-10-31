"""Offline tests: Windows folders through WSL (/api/dirs, /api/new) and transcript mapping (/api/chat)."""
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge.herdr_remote_bridge import server, sessions
from bridge.tests.test_server import Socket


class FakeDrive:
    """Maps C:\\... to a temp folder standing in for /mnt/c, like wslpath does."""
    def __init__(self, root):
        self.root = Path(root)

    def wslpath(self, path, to):
        if to == 'u':
            if not server.WINDOWS_PATH.match(path):
                raise ValueError('not a Windows path')
            win = PureWindowsPath(path)
            return str(self.root.joinpath(*win.parts[1:]))
        rel = Path(path).relative_to(self.root)
        return 'C:\\' + '\\'.join(rel.parts)


class WindowsDirsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.drive = FakeDrive(Path(temp.name).resolve())
        home = self.drive.root / 'Users' / 'me'
        for name in ('Projects/site', 'AppData/Roaming', 'My Documents', '.git', 'Desktop'):
            (home / name).mkdir(parents=True)
        (home / 'notes.txt').write_text('x')
        patches = [patch.object(server, 'wslpath', side_effect=self.drive.wslpath),
                   patch.object(server, 'windows_home', return_value='C:\\Users\\me')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_home_listing_uses_windows_paths_and_hides_system_folders(self):
        status, result = server.windows_dirs({})
        self.assertEqual(status, 200)
        self.assertEqual(result['path'], 'C:\\Users\\me')
        self.assertEqual(result['parent'], 'C:\\Users')
        self.assertEqual(result['dirs'], ['Desktop', 'Projects'])

    def test_browse_into_windows_path_and_back(self):
        status, result = server.windows_dirs({'path': 'C:\\Users\\me\\Projects'})
        self.assertEqual((status, result['path'], result['dirs']), (200, 'C:\\Users\\me\\Projects', ['site']))
        self.assertEqual(server.windows_dirs({'path': result['parent']})[1]['path'], 'C:\\Users\\me')

    def test_missing_or_bad_folder_is_400(self):
        self.assertEqual(server.windows_dirs({'path': 'C:\\Users\\me\\Nope'})[0], 400)
        self.assertEqual(server.windows_dirs({'path': 'not a path'})[0], 400)

    def test_new_hands_herdr_a_windows_path(self):
        target = self.drive.root / 'Users' / 'me' / 'Projects'
        calls = []
        def app(*args, **kwargs):
            calls.append(args)
            return 0, json.dumps({'terminal': 't5'}), ''
        body = json.dumps({'space': 'Site', 'agent': 'sh', 'cwd': str(target)}).encode()
        sock = Socket(f'POST /api/new HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        runtime = server.Runtime(self.drive.root)
        with patch.object(server, 'windows_host', return_value=True), patch.object(server, 'CONFIG', {}), \
             patch.object(server, 'app_spaces', return_value=[]), patch.object(server, 'app', side_effect=app), \
             patch.object(server, 'live_terminals', return_value=[{'address': 't5', 'where': 'Site / 1'}]):
            server.Handler(sock, ('127.0.0.1', 1), SimpleNamespace(runtime=runtime))
        self.assertIn(b'"t5"', sock.output.getvalue())
        args = json.loads(calls[0][2])
        self.assertEqual(args['folder'], 'C:\\Users\\me\\Projects')


class SessionMappingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / '.claude' / 'sessions').mkdir(parents=True)
        self.now = int(time.time() * 1000)

    def session(self, pid, session_id, cwd='C:\\Users\\me'):
        (self.home / '.claude' / 'sessions' / f'{pid}.json').write_text(
            json.dumps({'pid': pid, 'sessionId': session_id, 'cwd': cwd}))

    def test_claude_session_follows_each_terminals_process_tree(self):
        # Two terminals in the same folder: shell 100 -> claude 200, shell 300 -> claude 400.
        procs = {100: (1, 'powershell.exe', 1000), 200: (100, 'claude.exe', 2000),
                 300: (1, 'powershell.exe', 1500), 400: (300, 'claude.exe', 2500)}
        self.session(200, 'aaa')
        self.session(400, 'bbb')
        self.session(999, 'stale')  # a process that no longer exists
        self.assertEqual(sessions.claude_session([100], self.home, procs)['sessionId'], 'aaa')
        self.assertEqual(sessions.claude_session([300, None], self.home, procs)['sessionId'], 'bbb')
        self.assertIsNone(sessions.claude_session([555], self.home, procs))

    def test_outermost_claude_wins_over_one_it_started(self):
        procs = {100: (1, 'sh', 1000), 200: (100, 'claude', 2000), 250: (200, 'claude', 3000)}
        self.session(200, 'outer')
        self.session(250, 'inner')
        self.assertEqual(sessions.claude_session([100], self.home, procs)['sessionId'], 'outer')

    def rollout(self, name, began_ms, cwd):
        folder = self.home / '.codex' / 'sessions' / '2026' / '10' / '31'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(began_ms / 1000)) + '.000Z'
        path = folder / f'rollout-{name}.jsonl'
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': name, 'timestamp': stamp, 'cwd': cwd}}) + '\n')
        return path

    def test_codex_rollout_matches_folder_and_start_time(self):
        start, later = self.now - 600_000, self.now - 300_000
        procs = {10: (1, 'pwsh.exe', start - 1000), 11: (10, 'codex.exe', start),
                 20: (1, 'pwsh.exe', later - 1000), 21: (20, 'codex.exe', later)}
        self.rollout('before', start - 60_000, 'C:\\proj')           # before this codex started
        self.rollout('first', start + 2_000, 'C:\\proj')
        mine = self.rollout('after-new', start + 100_000, 'c:/proj/')  # same folder, other spelling
        self.rollout('other-folder', start + 120_000, 'C:\\elsewhere')
        theirs = self.rollout('second-codex', later + 3_000, 'C:\\proj')
        self.assertEqual(sessions.codex_rollout([10], self.home, 'C:\\proj', procs), mine)
        self.assertEqual(sessions.codex_rollout([20], self.home, 'C:\\proj', procs), theirs)
        self.assertIsNone(sessions.codex_rollout([555], self.home, 'C:\\proj', procs))

    def test_codex_folder_match_survives_different_spellings_of_same_folder(self):
        start = self.now - 600_000
        procs = {10: (1, 'pwsh.exe', start - 1000), 11: (10, 'codex.exe', start)}
        real = self.home / 'proj'
        real.mkdir()
        alias = self.home / 'alias'
        try:
            alias.symlink_to(real, target_is_directory=True)  # stands in for a Windows short name
        except OSError:
            self.skipTest('symlinks unavailable')
        mine = self.rollout('long-name', start + 2_000, str(real))
        self.assertEqual(sessions.codex_rollout([10], self.home, str(alias), procs), mine)

    def test_injected_context_is_not_shown_as_chat(self):
        from bridge.herdr_remote_bridge.herd_extras import parse_transcript
        path = self.home / 't.jsonl'
        path.write_text('\n'.join(json.dumps(x) for x in [
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'e',
             'content': [{'type': 'input_text', 'text': '<environment_context>\n  <cwd>C:\\x</cwd>\n</environment_context>'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'q',
             'content': [{'type': 'input_text', 'text': 'Reply with: pineapple'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'id': 'a',
             'content': [{'type': 'output_text', 'text': 'pineapple'}]}},
            {'type': 'user', 'uuid': 'm', 'isMeta': True, 'message': {'role': 'user', 'content': 'Caveat: ...'}},
        ]) + '\n')
        self.assertEqual([m['text'] for m in parse_transcript(path)], ['Reply with: pineapple', 'pineapple'])

    def test_etime_parsing(self):
        self.assertEqual(sessions._parse_etime('05:07'), 307)
        self.assertEqual(sessions._parse_etime('02:00:01'), 7201)
        self.assertEqual(sessions._parse_etime('3-00:00:10'), 3 * 86400 + 10)

    def test_chat_returns_mapped_claude_transcript(self):
        procs = {100: (1, 'powershell.exe', 1000), 200: (100, 'claude.exe', 2000)}
        self.session(200, 'sess-1')
        project = self.home / '.claude' / 'projects' / 'C--Users-me'
        project.mkdir(parents=True)
        (project / 'sess-1.jsonl').write_text('\n'.join(json.dumps(x) for x in [
            {'type': 'user', 'uuid': 'u1', 'message': {'role': 'user', 'content': 'hello'}},
            {'type': 'assistant', 'uuid': 'a1', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi there'}]}},
        ]) + '\n')
        (project / 'someone-else.jsonl').write_text(json.dumps(
            {'type': 'user', 'uuid': 'x', 'message': {'role': 'user', 'content': 'not mine'}}) + '\n')
        terminals = [{'address': 't3', 'agent': 'claude-code', 'cwd': 'C:\\Users\\me'},
                     {'address': 't4', 'agent': 'claude-code', 'cwd': 'C:\\Users\\me'}]
        sock = Socket(b'GET /api/chat?t=t3 HTTP/1.0\r\n\r\n')
        with patch.object(server, 'CONFIG', {}), patch.object(server, 'live_terminals', return_value=terminals), \
             patch.object(server, 'terminal_info', return_value={'pid': 100, 'foreground': {'pid': 100}}), \
             patch.object(server.sessions, 'processes', return_value=procs), \
             patch.object(server, 'agent_home', return_value=self.home):
            server.Handler(sock, ('127.0.0.1', 1), SimpleNamespace(runtime=server.Runtime(self.home)))
        head, payload = sock.output.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b' 200 ', head)
        texts = [m['text'] for m in json.loads(payload)['messages']]
        self.assertEqual(texts, ['hello', 'hi there'])

    def test_chat_is_empty_not_missing_before_first_message(self):
        procs = {100: (1, 'powershell.exe', 1000), 200: (100, 'claude.exe', 2000)}
        self.session(200, 'brand-new')  # Claude running, transcript file not written yet
        terminals = [{'address': 't3', 'agent': 'claude-code', 'cwd': 'C:\\Users\\me'}]
        sock = Socket(b'GET /api/chat?t=t3 HTTP/1.0\r\n\r\n')
        with patch.object(server, 'CONFIG', {}), patch.object(server, 'live_terminals', return_value=terminals), \
             patch.object(server, 'terminal_info', return_value={'pid': 100}), \
             patch.object(server.sessions, 'processes', return_value=procs), \
             patch.object(server, 'agent_home', return_value=self.home):
            server.Handler(sock, ('127.0.0.1', 1), SimpleNamespace(runtime=server.Runtime(self.home)))
        head, payload = sock.output.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b' 200 ', head)
        self.assertEqual(json.loads(payload), {'messages': []})


if __name__ == '__main__':
    unittest.main()
