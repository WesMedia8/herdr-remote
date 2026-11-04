import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.herdr_remote_bridge.herd_push import assistant_summary, StatusWatcher, TokenStore
from bridge.herdr_remote_bridge import server


class SummaryTests(unittest.TestCase):
    def test_markdown(self):
        self.assertEqual(assistant_summary([{'role': 'assistant', 'text':
            '# Done\n> **Built** [the page](https://example.com)\n- `tested`\n* shipped'}]),
            'Done Built the page tested shipped')

    def test_last_assistant(self):
        messages = [{'role': 'assistant', 'text': 'Old'}, {'role': 'user', 'text': 'User'},
                    {'role': 'assistant', 'text': 'Latest'}, {'role': 'tool', 'text': 'Tool'},
                    {'role': 'assistant', 'text': ' \n'}]
        self.assertEqual(assistant_summary(messages), 'Latest')

    def test_empty(self):
        for messages in ([], [{'role': 'user', 'text': 'Hi'}],
                         [{'role': 'assistant', 'text': '  '}],
                         [{'role': 'assistant', 'text': '```'}]):
            self.assertIsNone(assistant_summary(messages))

    def test_first_sentence(self):
        for punctuation in ('.', '!', '?', ':'):
            with self.subTest(punctuation=punctuation):
                text = f'Built the page{punctuation} Tested it too.'
                expected = 'Built the page' + (punctuation if punctuation != ':' else '')
                self.assertEqual(assistant_summary([{'role': 'assistant', 'text': text}]), expected)
        self.assertEqual(assistant_summary([{'role': 'assistant', 'text':
            'OK. Built the page. Tested it too.'}]), 'OK. Built the page.')

    def test_truncation(self):
        self.assertEqual(assistant_summary([{'role': 'assistant', 'text': 'word ' * 40}]),
                         ' '.join(['word'] * 12) + '…')
        text = 'a' * 60
        self.assertEqual(assistant_summary([{'role': 'assistant', 'text': text}]), text)
        self.assertEqual(assistant_summary([{'role': 'assistant', 'text': 'Built the page'}]),
                         'Built the page')

    def test_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / 'tokens.json')
            store.add('ab', 'development')
            for status, result, error, expected in (
                ('done', 'Built the page', None, 'Built the page'),
                ('idle', 'Built the page', None, 'Built the page'),
                ('done', None, None, 'Ready'), ('done', '', None, 'Ready'),
                ('done', None, RuntimeError('oops'), 'Ready'),
                ('blocked', 'Ignored', None, 'Ready')):
                with self.subTest(status=status, result=result, error=error):
                    summarize = Mock(return_value=result, side_effect=error)
                    watcher = StatusWatcher(lambda: [], None, store, 'PC', summarize=summarize)
                    self.addCleanup(watcher.executor.shutdown)
                    term = dict(address='t1', agent='codex', status='working', preview='Ready')
                    watcher.poll([term], 0)
                    term['status'] = status
                    watcher.poll([term], 2)
                    jobs = watcher.poll([term], 6)
                    body = next(j[2]['aps']['alert']['body'] for j in jobs if j[0] == 'alert')
                    self.assertEqual(body, f"Codex {'needs you' if status == 'blocked' else 'finished'}: {expected}")
                    self.assertEqual(summarize.call_count, 0 if status == 'blocked' else 1)

    def test_terminal_summary_mapping(self):
        term = dict(address='t1', agent='codex', cwd='/work', session_file='/transcript')
        with patch.object(server, 'live_terminals', return_value=[term]), \
             patch.object(server, 'agent_home', return_value='/home'), \
             patch.object(server.herd_extras, 'find_transcript', return_value=Path('/transcript')) as find, \
             patch.object(server.herd_extras, 'parse_transcript', return_value=[{'role': 'assistant', 'text': 'Done'}]):
            self.assertEqual(server.terminal_summary(term), 'Done')
            find.assert_called_once_with('codex', cwd='/work', session_file='/transcript', session_id=None, home='/home')
        self.assertIsNone(server.terminal_summary(dict(agent='sh')))

    def test_unavailable_mapping(self):
        term = dict(address='t1', agent='codex', cwd='/work')
        with patch.object(server, 'live_terminals', return_value=[term, dict(cwd='/work')]), \
             patch.object(server, 'chat_source', return_value=None):
            self.assertIsNone(server.terminal_summary(term))

    def test_shared_mapping_order(self):
        term = dict(address='t1', agent='claude-code', cwd='/work')
        mapped = dict(session_id='session', cwd='/mapped', home='/home')
        with patch.object(server, 'chat_source', return_value=mapped) as lookup, \
             patch.object(server, 'agent_home', return_value='/home'):
            self.assertEqual(server.transcript_source(term, [term]), (mapped, True))
            explicit = dict(term, session_id='explicit')
            lookup.reset_mock()
            self.assertEqual(server.transcript_source(explicit, [explicit]),
                             (dict(cwd='/work', session_file=None, session_id='explicit', home='/home'), False))
            lookup.assert_not_called()
            lookup.return_value = None
            self.assertEqual(server.transcript_source(term, [term]),
                             (dict(cwd='/work', session_file=None, session_id=None, home='/home'), False))
            lookup.side_effect = RuntimeError('unavailable')
            self.assertEqual(server.transcript_source(term, [term, dict(cwd='/work')]), (None, False))

    def test_missing_transcript(self):
        term = dict(address='t1', agent='codex', session_file='/missing')
        with patch.object(server, 'live_terminals', return_value=[term]), \
             patch.object(server, 'agent_home', return_value='/home'), \
             patch.object(server.herd_extras, 'find_transcript', side_effect=FileNotFoundError):
            self.assertIsNone(server.terminal_summary(term))
