import json
from pathlib import Path
import tempfile
import time
import unittest

from bridge import Bridge, Session
from new_chat import NewChat
from test_ide_link import eventually


class NewChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.notices, self.agent = [], []
        self.core = Bridge(self.agent.append, lambda _: None,
                           lambda text, buttons=None: self.notices.append(text), 42, 42)
        self.core.new_chat = NewChat(self.folder, self.core)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.folder / name
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value), encoding='utf-8')
        temp.replace(path)

    def test_missing_plugin_does_not_start_an_agent(self):
        self.core.command('/new')
        self.assertIn('Telegram Chat Helper', self.notices[-1])
        self.assertFalse(self.core.creating_chat)
        self.assertFalse(self.agent)

    def test_creation_selects_only_the_session_confirmed_by_intellij(self):
        self.write('idea-helper.local.json', {'version': 1})
        self.core.sessions['old'] = Session()
        self.core.selected = 'old'
        self.core.command('/new')
        request = json.loads((self.folder / 'idea-new-chat.local.json').read_text())
        self.assertEqual(request['command'], 'new-chat')
        self.assertGreater(request['expires'], time.time())
        self.core.command('Must not enter old chat')
        self.assertFalse(self.agent)
        self.write('idea-new-chat-result.local.json', {'id': 'wrong', 'sessionId': 'old'})
        self.core.sessions['unrelated'] = Session()
        self.write('idea-new-chat-result.local.json', {'id': request['id'], 'sessionId': 'new'})
        self.assertEqual(self.core.selected, 'old')
        with self.core.lock:
            self.core.sessions['new'] = Session()
        eventually(lambda: not self.core.creating_chat)
        self.assertEqual(self.core.selected, 'new')
        self.assertIn('создан и выбран', self.notices[-1])

    def test_plugin_error_keeps_the_old_chat_and_allows_retry(self):
        self.write('idea-helper.local.json', {'version': 1})
        self.core.sessions['old'] = Session()
        self.core.selected = 'old'
        self.core.command('/new')
        request = json.loads((self.folder / 'idea-new-chat.local.json').read_text())
        self.write('idea-new-chat-result.local.json', {'id': request['id'], 'error': 'Need login'})
        eventually(lambda: not self.core.creating_chat)
        self.assertEqual(self.core.selected, 'old')
        self.assertIn('Need login', self.notices[-1])

    def test_no_creation_during_a_task_or_hibernation(self):
        self.write('idea-helper.local.json', {'version': 1})
        self.core.sessions['old'] = Session(active='running')
        self.core.command('/new')
        self.assertFalse((self.folder / 'idea-new-chat.local.json').exists())
        self.core.sessions.clear()
        self.core.power_running = True
        self.core.command('/new')
        self.assertFalse((self.folder / 'idea-new-chat.local.json').exists())
