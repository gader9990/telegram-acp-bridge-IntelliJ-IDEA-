"""Authenticated local transport between the desktop bot and IntelliJ's ACP process.

Only JSON bytes cross the connection (never pickle). No model process lives here.
"""
import hashlib
import json
from multiprocessing.connection import Listener, Client
from multiprocessing import AuthenticationError
import os
from pathlib import Path
import queue
import secrets
import threading


MAX_MESSAGE = 32 * 1024 * 1024


def receive(connection):
    value = json.loads(connection.recv_bytes(MAX_MESSAGE))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def send(connection, message):
    connection.send_bytes(json.dumps(message, ensure_ascii=False).encode('utf-8'))


def endpoint_path(config_path):
    return Path(config_path).with_name('ide-endpoint.local.json')


def connect(config_path):
    data = json.loads(endpoint_path(config_path).read_text(encoding='utf-8'))
    family, address = data['family'], data['address']
    if family == 'AF_INET':
        if not isinstance(address, list) or address[0] != '127.0.0.1':
            raise ValueError('Only local addresses are accepted')
        address = tuple(address)
    elif family != 'AF_PIPE' or not address.startswith('\\\\.\\pipe\\telegram-acp-'):
        raise ValueError('Invalid local pipe address')
    connection = Client(address, family=family, authkey=bytes.fromhex(data['key']))
    try:
        send(connection, {'kind': 'attach', 'version': 1})
        reply = receive(connection)
        if reply.get('kind') != 'attached':
            raise RuntimeError('К боту уже подключён другой процесс IntelliJ.')
        return connection
    except Exception:
        connection.close()
        raise


class Hub:
    """Multiplex IDE-owned adapters without mixing their RPC IDs or sessions."""
    def __init__(self, config_path, core, family=None):
        self.path = endpoint_path(config_path)
        self.core = core
        self.stop = threading.Event()
        self.links = {}
        self.owners = {}
        self.requests = {'agent': {}, 'ide': {}}
        self.context = threading.local()
        self.key = secrets.token_bytes(32)
        family = family or ('AF_PIPE' if os.name == 'nt' else 'AF_INET')
        digest = hashlib.sha256(str(Path(config_path).resolve()).lower().encode()).hexdigest()[:20]
        address = r'\\.\pipe\telegram-acp-' + digest if family == 'AF_PIPE' else ('127.0.0.1', 0)
        self.listener = Listener(address, family=family, authkey=self.key)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'family': family, 'address': self.listener.address,
                                         'key': self.key.hex()}), encoding='utf-8')
        temporary.replace(self.path)
        self.core.send_agent = lambda message: self.route('agent', message)
        self.core.send_ide = lambda message: self.route('ide', message)
        self.core.alive = self.core.ready = False

    @property
    def active(self):
        # Kept for diagnostics; routing never relies on a global active adapter.
        return next(iter(self.links.values()), None)

    def route(self, target, message):
        with self.core.lock:
            sid = (message.get('params') or {}).get('sessionId')
            current = self.owners.get(sid) or getattr(self.context, 'current', None)
            packet = dict(message)
            request_id = packet.get('id')
            if isinstance(request_id, str) and request_id.startswith('link:'):
                _, link_id, side, raw = request_id.split(':', 3)
                current = self.links.get(link_id)
                if side != target:
                    raise ValueError('Wrong RPC destination')
                packet['id'] = json.loads(raw)
            if current is None or current['id'] not in self.links:
                return
            if 'method' in message and 'id' in message:
                self.requests[target][message['id']] = current
            params = packet.get('params') or {}
            ref = params.get('requestId')
            if isinstance(ref, str) and ref.startswith('link:' + current['id'] + ':' + target + ':'):
                packet['params'] = {**params, 'requestId': json.loads(ref.split(':', 3)[3])}
            current['outbox'].put({'target': target, 'message': packet})

    def start(self):
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while not self.stop.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                break
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()

    def inbound(self, current, source, message):
        with self.core.lock:
            self.context.current = current
            try:
                packet = dict(message)
                sid = (packet.get('params') or {}).get('sessionId')
                owner = self.owners.get(sid)
                if owner is not None and owner is not current:
                    if 'method' in packet and 'id' in packet:
                        current['outbox'].put({'target': source, 'message': {
                            'jsonrpc': '2.0', 'id': packet['id'], 'error': {
                                'code': -32000, 'message': 'Session belongs to another IDE connection'}}})
                    return
                if 'method' in message:
                    prefix = 'link:' + current['id'] + ':' + source + ':'
                    if 'id' in packet:
                        packet['id'] = prefix + json.dumps(packet['id'])
                    params = packet.get('params') or {}
                    if 'requestId' in params:
                        packet['params'] = {**params, 'requestId': prefix + json.dumps(params['requestId'])}
                else:
                    owner = self.requests[source].get(packet.get('id'))
                    if owner is not current:
                        return  # An adapter cannot answer another adapter's request.
                    self.requests[source].pop(packet.get('id'), None)
                    if source == 'agent':
                        pending = self.core.to_agent.get(packet.get('id'), {})
                        method = pending.get('message', {}).get('method')
                        if 'error' not in packet:
                            if method == 'initialize':
                                current['ready'] = True
                            if method in {'session/new', 'session/load', 'session/resume', 'session/fork'}:
                                sid = (packet.get('result') or {}).get('sessionId') or (
                                    pending.get('message', {}).get('params') or {}).get('sessionId')
                                if sid:
                                    previous = self.owners.get(sid)
                                    if previous and previous is not current:
                                        packet = {'id': packet['id'], 'error': {
                                            'code': -32000, 'message': 'Session already connected in another IDE chat'}}
                                    else:
                                        self.owners[sid] = current
                if source == 'ide':
                    self.core.from_ide(packet)
                else:
                    self.core.from_agent(packet)
                self.core.ready = any(link['ready'] for link in self.links.values())
            finally:
                self.context.current = None

    def serve(self, connection):
        current = {'id': secrets.token_hex(12), 'connection': connection,
                   'outbox': queue.Queue(), 'ready': False}
        attached = False
        try:
            if not connection.poll(5) or receive(connection) != {'kind': 'attach', 'version': 1}:
                return
            with self.core.lock:
                if self.stop.is_set():
                    return
                self.links[current['id']] = current
                attached = True
                self.core.alive = True
                send(connection, {'kind': 'attached'})
            threading.Thread(target=self.write_loop, args=(current,), daemon=True).start()
            while not self.stop.is_set():
                packet = receive(connection)
                message, source = packet.get('message'), packet.get('source')
                if not isinstance(message, dict) or source not in {'ide', 'agent'}:
                    raise ValueError('Invalid ACP packet')
                self.inbound(current, source, message)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            if attached:
                self.detach(current)
            connection.close()

    def detach(self, current):
        with self.core.lock:
            self.context.current = current
            try:
                lost = {sid for sid, owner in self.owners.items() if owner is current}
                for sid in lost:
                    self.core.cancel_pending(sid)
                    self.core.sessions.pop(sid, None)
                    self.owners.pop(sid, None)
                self.core.jobs = [job for job in self.core.jobs if job[0] not in lost]
                if lost and self.core.sleep_plan:
                    self.core.sleep_plan = None
                    self.core.say('План сна отменён: один из чатов IntelliJ отключился.')
                for target, pending in self.requests.items():
                    state = self.core.to_agent if target == 'agent' else self.core.to_ide
                    for key, owner in list(pending.items()):
                        if owner is current:
                            pending.pop(key, None)
                            state.pop(key, None)
                self.links.pop(current['id'], None)
                if self.core.selected in lost:
                    self.core.selected = None
                    self.core.input_mode = None
                    self.core.sleep_plan = None
                    if self.links and not self.core.creating_chat:
                        self.core.say('Выбранный чат IntelliJ отключён. Выбери другой через /sessions.')
                if not self.links:
                    self.core.disconnected()
                    self.core.sessions.clear()
                    self.core.selected = None
                    self.core.to_ide.clear()
                else:
                    self.core.ready = any(link['ready'] for link in self.links.values())
            finally:
                self.context.current = None
                current['outbox'].put(None)

    def write_loop(self, current):
        try:
            while True:
                packet = current['outbox'].get()
                if packet is None:
                    return
                send(current['connection'], packet)
        except (OSError, EOFError):
            current['connection'].close()

    def close(self):
        self.stop.set()
        self.listener.close()
        with self.core.lock:
            for current in list(self.links.values()):
                current['outbox'].put(None)
                current['connection'].close()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
