"""Read account quotas from the installed Codex, without creating a model turn."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from datetime import datetime


class Limits:
    def __init__(self, agent_command):
        self.agent_command = agent_command
        self.text = 'Лимиты ещё не запрошены.'
        self.busy = False
        self.lock = threading.Lock()

    def refresh(self, callback=None):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        def work():
            try:
                self.text = format_limits(read_limits(self.agent_command))
            except Exception:
                self.text = 'Не удалось получить лимиты Codex. Проверь вход в аккаунт и подключение к интернету.'
            finally:
                with self.lock:
                    self.busy = False
            if callback:
                callback(self.text)
        threading.Thread(target=work, daemon=True).start()


def read_limits(agent_command):
    candidates = []
    for argument in agent_command:
        path = Path(argument)
        if path.name == 'index.js' and path.parent.name == 'dist':
            modules = path.parent.parent.parent.parent
            candidates.extend(modules.glob('@openai/codex-*/vendor/*/bin/codex.exe'))
    if len(candidates) != 1:
        raise RuntimeError('Installed Codex executable not found unambiguously')
    process = subprocess.Popen([str(candidates[0]), 'app-server'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def reader():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)
    threading.Thread(target=reader, daemon=True).start()
    def send(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()
    def receive(request_id):
        deadline = time.monotonic() + 25
        while True:
            message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Codex stopped')
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError('Codex could not read limits')
                return message['result']
    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'telegram-limits', 'version': '1.0'}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'account/rateLimits/read'})
        return receive(2)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()


def format_limits(result, now=None):
    now = time.time() if now is None else now
    buckets = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
    lines = ['Лимиты Codex · обновлено ' + datetime.fromtimestamp(now).strftime('%d.%m %H:%M')]
    for name, bucket in buckets.items():
        if not bucket:
            continue
        for key in ('primary', 'secondary'):
            window = bucket.get(key)
            if not window:
                continue
            minutes = window.get('windowDurationMins')
            label = '5 часов' if minutes == 300 else 'неделя' if minutes == 10080 else f'{minutes} мин' if minutes else key
            left = max(0, min(100, 100 - window['usedPercent']))
            line = f'{bucket.get("limitName") or name} · {label}: осталось {left:g}%'
            reset = window.get('resetsAt')
            if reset is not None:
                seconds = max(0, int(reset - now))
                line += (' · сброс ' + datetime.fromtimestamp(reset).strftime('%d.%m %H:%M')
                         + f' (через {seconds // 3600} ч {seconds % 3600 // 60} мин)')
                if reset <= now:
                    line += ' · требуется обновление'
            lines.append(line)
    if len(lines) == 1:
        lines.append('Аккаунт не предоставил данные о лимитах.')
    lines.append('Число оставшихся запросов недоступно: расход зависит от объёма задач.')
    return '\n'.join(lines)
