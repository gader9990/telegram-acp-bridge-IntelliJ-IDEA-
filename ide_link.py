"""ACP transport owned by IntelliJ: preserves IDE initialization and MCP verbatim."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from multiprocessing import AuthenticationError

from ipc import connect, receive, send


def read_acp(stream, deliver, stopped):
    # Read the descriptor directly: an idle daemon holding sys.stdin's buffered
    # reader lock causes a fatal Python shutdown when the desktop closes first.
    pending = bytearray()
    try:
        while not stopped.is_set():
            block = os.read(stream.fileno(), 65536)
            if not block:
                break
            pending.extend(block)
            while (end := pending.find(b'\n')) >= 0:
                line = bytes(pending[:end])
                del pending[:end + 1]
                if line.strip():
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError('ACP object expected')
                    deliver(message)
            if len(pending) > 32 * 1024 * 1024:
                raise ValueError('ACP message too large')
    except (OSError, ValueError):
        pass
    finally:
        stopped.set()


def write_acp(stream, messages, stopped):
    try:
        while True:
            message = messages.get()
            if message is None:
                return
            data = memoryview((json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'))
            while data:
                written = os.write(stream.fileno(), data)
                if not written:
                    raise OSError('ACP stream closed')
                data = data[written:]
    except (OSError, ValueError):
        stopped.set()


def connect_or_start(config_path):
    try:
        return connect(config_path)
    except (OSError, ValueError, KeyError, EOFError, AuthenticationError):
        pass
    # Starting concurrently is safe: the desktop owns the existing instance lock.
    subprocess.Popen([sys.executable, str(Path(__file__).with_name('standalone.py')),
                      '--config', str(config_path), '--auto-start'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return connect(config_path)
        except (OSError, ValueError, KeyError, EOFError, AuthenticationError):
            time.sleep(.15)
    raise RuntimeError('Окно бота не запустилось. Запусти start_bot.cmd и повтори подключение IntelliJ.')


def stop_child(child):
    try:
        child.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            child.terminate()
            child.wait(timeout=5)


def run(config_path):
    from bridge import load_config
    config, _ = load_config(config_path, require_token=False)
    connection = connect_or_start(config_path)
    try:
        child = subprocess.Popen(config['agent_command'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        connection.close()
        raise
    stopped = threading.Event()
    outgoing = queue.Queue()
    targets = {'agent': queue.Queue(), 'ide': queue.Queue()}

    def read_stream(stream, source):
        read_acp(stream, lambda message: outgoing.put({'source': source, 'message': message}), stopped)

    def write_transport():
        try:
            while True:
                packet = outgoing.get()
                if packet is None:
                    return
                send(connection, packet)
        except (OSError, EOFError):
            stopped.set()

    def read_transport():
        try:
            while not stopped.is_set():
                packet = receive(connection)
                target = targets.get(packet.get('target'))
                if target is None or not isinstance(packet.get('message'), dict):
                    raise ValueError('Invalid transport packet')
                target.put(packet['message'])
        except (OSError, EOFError, ValueError):
            stopped.set()

    for target, args in [(read_stream, (sys.stdin.buffer, 'ide')), (read_stream, (child.stdout, 'agent')),
                         (write_transport, ()), (read_transport, ()),
                         (write_acp, (sys.stdout.buffer, targets['ide'], stopped)),
                         (write_acp, (child.stdin, targets['agent'], stopped))]:
        threading.Thread(target=target, args=args, daemon=True).start()
    try:
        stopped.wait()
    finally:
        outgoing.put(None)
        for target in targets.values():
            target.put(None)
        stop_child(child)
        connection.close()


def run_diagnostic(config_path):
    """Direct stdio harness for routing tests; no Telegram, UI, or local listener."""
    from bridge import Bridge, load_config
    config, _ = load_config(config_path, require_token=False)
    stopped = threading.Event()
    to_agent, to_ide = queue.Queue(), queue.Queue()
    core = Bridge(to_agent.put, to_ide.put, lambda *args: None, config['owner_id'], config['chat_id'])
    child = subprocess.Popen(config['agent_command'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for target, args in [(read_acp, (sys.stdin.buffer, core.from_ide, stopped)),
                         (read_acp, (child.stdout, core.from_agent, stopped)),
                         (write_acp, (sys.stdout.buffer, to_ide, stopped)),
                         (write_acp, (child.stdin, to_agent, stopped))]:
        threading.Thread(target=target, args=args, daemon=True).start()
    try:
        stopped.wait()
    finally:
        to_agent.put(None)
        to_ide.put(None)
        stop_child(child)
