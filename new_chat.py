"""Local mailbox for the IntelliJ helper; no network or separate agent process."""
import json
import secrets
import threading
import time
from pathlib import Path


class NewChat:
    def __init__(self, folder, core):
        self.folder, self.core = Path(folder), core

    def start(self):
        core = self.core
        with core.lock:
            if core.power_running or core.creating_chat or core.busy():
                core.say('Сначала заверши текущие задачи и очередь.')
                return
            try:
                heartbeat = self.folder / 'idea-helper.local.json'
                data = json.loads(heartbeat.read_text(encoding='utf-8'))
                if data.get('version') != 1 or time.time() - heartbeat.stat().st_mtime > 15:
                    raise ValueError()
            except (OSError, ValueError):
                core.say('Открой проект в IntelliJ с плагином Telegram Chat Helper. '
                         'Установка: Settings → Plugins → ⚙ → Install Plugin from Disk → '
                         'tools/telegram-acp-bridge/idea-helper/dist/telegram-chat-helper.zip.')
                return
            request_id = secrets.token_hex(16)
            request = {'id': request_id, 'expires': time.time() + 120, 'command': 'new-chat'}
            try:
                path = self.folder / 'idea-new-chat.local.json'
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(request), encoding='utf-8')
                temporary.replace(path)
            except OSError:
                core.say('Не удалось передать запрос IntelliJ: проверь права на папку бота.')
                return
            core.creating_chat = True
            core.cancel_countdown()
            core.say('Создаю чат в IntelliJ и подключаю инструменты…')
            threading.Thread(target=self.wait, args=(request_id,), daemon=True).start()

    def wait(self, request_id):
        core = self.core
        response_path = self.folder / 'idea-new-chat-result.local.json'
        deadline = time.monotonic() + 125
        try:
            while time.monotonic() < deadline:
                try:
                    response = json.loads(response_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    response = {}
                if isinstance(response, dict) and response.get('id') == request_id:
                    if response.get('error'):
                        with core.lock:
                            core.say('IntelliJ не подключила новый чат: ' + str(response['error'])[:600])
                        return
                    sid = response.get('sessionId')
                    with core.lock:
                        if sid and sid in core.sessions:
                            if core.pending or any(s.active or s.control for s in core.sessions.values()):
                                core.say('Новый чат создан. Сейчас выполняется другая задача; '
                                         'после её завершения выбери новый чат через /sessions.')
                                return
                            core.selected = sid
                            core.input_mode = None
                            from bridge import MAIN_KEYBOARD
                            core.say('Новый чат IntelliJ создан и выбран. Можно отправлять задачу.', MAIN_KEYBOARD)
                            return
                time.sleep(.25)
            with core.lock:
                core.say('IntelliJ не подтвердила подключение за 2 минуты. Проверь AI Assistant: '
                         'возможно, требуется вход или подтверждение. Перед повтором посмотри /sessions, '
                         'чтобы не создать лишний чат.')
        finally:
            with core.lock:
                core.creating_chat = False
