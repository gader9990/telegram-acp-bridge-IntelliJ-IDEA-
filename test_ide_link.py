import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bridge import Bridge
from ipc import Hub, connect, receive, send


FAKE_AGENT = '''import json,sys
prompt=None
def send(m): print(json.dumps(m),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 method=m.get('method')
 if method=='initialize':
  send({'id':m['id'],'result':{'protocolVersion':1,'echo':m['params']}})
 elif method=='session/new':
  send({'id':m['id'],'result':{'sessionId':'real-ide-session','echo':m['params']}})
 elif method=='session/prompt':
  prompt=m['id']
  send({'id':'fs-request','method':'fs/read_text_file','params':{'sessionId':'real-ide-session','path':'project.txt'}})
 elif m.get('id')=='fs-request':
  send({'method':'session/update','params':{'sessionId':'real-ide-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':m['result']['content']}}}})
  send({'id':prompt,'result':{'stopReason':'end_turn'}})
'''


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('State did not converge')
        threading.Event().wait(.02)


class IdeLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.config = folder / 'config.json'
        agent = folder / 'agent.py'
        agent.write_text(FAKE_AGENT, encoding='utf-8')
        self.config.write_text(json.dumps({'owner_id': 42, 'chat_id': 42,
                              'agent_command': [sys.executable, str(agent)]}), encoding='utf-8')
        self.notices = []
        self.core = Bridge(lambda _: None, lambda _: None,
                           lambda text, buttons=None: self.notices.append(text), 42, 42)
        self.hub = Hub(self.config, self.core)
        self.hub.start()
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=12)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.hub.close()
        self.temp.cleanup()

    def launch(self):
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('bridge.py')),
                                    '--config', str(self.config)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        messages = queue.Queue()
        def reader():
            for line in process.stdout:
                messages.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        return process, messages

    @staticmethod
    def write(process, message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()

    def test_real_relay_preserves_ide_mcp_and_client_tool_roundtrip(self):
        process, messages = self.launch()
        initialize = {'protocolVersion': 1, 'clientCapabilities': {'fs': {'readTextFile': True}},
                      '_meta': {'ide-owned': True}}
        self.write(process, {'id': 1, 'method': 'initialize', 'params': initialize})
        self.assertEqual(messages.get(timeout=10)['result']['echo'], initialize)
        params = {'cwd': self.temp.name, 'mcpServers': [
            {'name': 'idea', 'type': 'http', 'url': 'http://localhost:12345/mcp',
             'headers': [{'name': 'Authorization', 'value': 'synthetic-test-secret'}]}]}
        self.write(process, {'id': 2, 'method': 'session/new', 'params': params})
        self.assertEqual(messages.get(timeout=5)['result']['echo'], params)
        with self.core.lock:
            self.core.command('/use real-ide-session')
            self.core.command('Read through IntelliJ')
        user_chunk = messages.get(timeout=5)
        self.assertEqual(user_chunk['method'], 'session/update')
        request = messages.get(timeout=5)
        self.assertEqual(request['method'], 'fs/read_text_file')
        self.write(process, {'id': request['id'], 'result': {'content': 'Answer from IntelliJ'}})
        eventually(lambda: any('Answer from IntelliJ' in n for n in self.notices))
        self.assertNotIn('synthetic-test-secret', str(self.notices))
        self.assertIsNone(self.core.sessions['real-ide-session'].active)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=12), 0)
        eventually(lambda: not self.core.alive)
        self.assertFalse(self.hub.stop.is_set())
        self.assertFalse(self.core.sessions)
        self.core.command('/menu')
        self.assertIn('Выбери действие', self.notices[-1])
        # The same bot host accepts a new IDE process after EOF.
        second, replies = self.launch()
        self.write(second, {'id': 3, 'method': 'initialize', 'params': initialize})
        self.assertEqual(replies.get(timeout=5)['id'], 3)
        self.assertTrue(self.core.ready)

    def test_two_ide_connections_keep_rpc_and_sessions_separate(self):
        first, second = connect(self.config), connect(self.config)
        def exchange(connection, sid):
            send(connection, {'source': 'ide', 'message': {
                'id': 1, 'method': 'session/new', 'params': {'cwd': sid}}})
            request = receive(connection)['message']
            send(connection, {'source': 'agent', 'message': {
                'id': request['id'], 'result': {'sessionId': sid}}})
            result = receive(connection)
            self.assertEqual(result['target'], 'ide')
            self.assertEqual(result['message']['id'], 1)
        try:
            exchange(first, 'first')
            exchange(second, 'second')
            self.assertEqual(set(self.core.sessions), {'first', 'second'})
            send(first, {'source': 'ide', 'message': {
                'id': 5, 'method': 'session/prompt', 'params': {'sessionId': 'second'}}})
            self.assertIn('error', receive(first)['message'])
            self.assertFalse(second.poll(.1))
            self.core.ready = True
            self.core.command('/use second')
            self.core.command('hello')
            self.assertEqual(receive(second)['target'], 'ide')
            prompt = receive(second)['message']
            self.assertEqual(prompt['params']['sessionId'], 'second')
            self.assertFalse(first.poll(.1))
            first.close()
            eventually(lambda: 'first' not in self.core.sessions)
            self.assertIn('second', self.core.sessions)
            self.assertTrue(self.core.alive)
            send(second, {'source': 'agent', 'message': {
                'id': 'same-fs-id', 'method': 'fs/read_text_file',
                'params': {'sessionId': 'second', 'path': 'file'}}})
            request = receive(second)['message']
            send(second, {'source': 'ide', 'message': {
                'id': request['id'], 'result': {'content': 'correct chat'}}})
            reply = receive(second)['message']
            self.assertEqual(reply['id'], 'same-fs-id')
            self.assertEqual(reply['result']['content'], 'correct chat')
        finally:
            first.close()
            second.close()
        eventually(lambda: not self.core.alive)

    def test_closing_host_disconnects_ide_and_stops_its_adapter(self):
        process, messages = self.launch()
        self.write(process, {'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(messages.get(timeout=5)['id'], 1)
        self.hub.close()
        code = process.wait(timeout=10)
        self.assertEqual(code, 0, process.stderr.read().decode(errors='replace'))

    def test_bad_authentication_does_not_steal_the_connection(self):
        from multiprocessing.connection import Client
        from multiprocessing import AuthenticationError
        with self.assertRaises(AuthenticationError):
            Client(self.hub.listener.address, authkey=b'incorrect')
        connection = connect(self.config)
        self.assertTrue(self.core.alive)
        connection.close()
        eventually(lambda: not self.core.alive)

    def test_bot_works_before_any_ide_connects(self):
        self.assertFalse(self.core.alive)
        self.core.command('/menu')
        self.assertTrue(self.notices)
        self.assertFalse(self.hub.stop.is_set())
        self.core.command('do not launch a different agent')
        self.assertFalse(self.core.to_agent)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.ide, self.notices = [], [], []
        self.core = Bridge(self.agent.append, self.ide.append,
                           lambda text, buttons=None: self.notices.append(text), 42, 42)
        self.option = {'id': 'model', 'name': 'Model', 'type': 'select', 'category': 'model',
                       'currentValue': 'old', 'options': [{'value': 'old', 'name': 'Old'},
                                                         {'value': 'new', 'name': 'New'}]}
        for method, result in [('initialize', {}), ('session/new', {'sessionId': 's1', 'configOptions': [self.option]})]:
            self.core.from_ide({'id': 1, 'method': method, 'params': {}})
            self.core.from_agent({'id': self.agent[-1]['id'], 'result': result})
        self.core.command('/use s1')

    def test_changes_use_the_same_ide_session_and_notify_ide_on_ack(self):
        self.core.set_config('model', 'new')
        request = self.agent[-1]
        self.assertEqual(request['params'], {'sessionId': 's1', 'configId': 'model', 'value': 'new'})
        count = len(self.agent)
        self.core.command('wait until setting applied')
        self.assertEqual(len(self.agent), count)
        self.core.from_agent({'id': request['id'], 'result': {
            'configOptions': [{**self.option, 'currentValue': 'new'}]}})
        self.assertEqual(self.ide[-1]['params']['update']['sessionUpdate'], 'config_option_update')
        self.assertIsNone(self.core.sessions['s1'].control)
        self.assertEqual(self.core.sessions['s1'].config_options[0]['currentValue'], 'new')

    def test_unadvertised_model_and_foreign_callback_are_rejected(self):
        before = len(self.agent)
        self.core.set_config('model', 'invented')
        self.assertEqual(len(self.agent), before)
        self.core.command('/models')
        token = next(iter(self.core.config_buttons))
        self.assertFalse(self.core.telegram_update({'callback_query': {
            'data': 'ui:config:' + token, 'from': {'id': 77},
            'message': {'chat': {'id': 42, 'type': 'private'}}}}))
        self.assertEqual(len(self.agent), before)
