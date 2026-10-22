"""ACP stdio proxy with a private Telegram control channel (Python 3.11+).

stdout belongs exclusively to ACP. No shell commands are accepted from Telegram;
ordinary text is submitted to the agent with its existing permissions.
"""
from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import tempfile
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from power import parse_wake
from datetime import datetime


MENU_ACTIONS = {
    "📉 Лимиты Codex": "/limits",
    "✨ Новый чат": "/new",
    "🤖 Модель": "/models", "🧠 Рассуждение": "/efforts",
    "📒 Заметки": "/notes", "✍ Добавить заметку": "input:note",
    "🧹 Сжать контекст": "/compact",
    "📷 Скриншот": "/screenshot", "📊 Статус": "/status", "💬 Выбрать чат": "/sessions",
    "📝 Новая задача": "input:ask", "➕ В очередь": "input:queue", "📋 Очередь": "/queue",
    "⏹ Остановить": "/cancel", "📨 Последний ответ": "/last", "✅ Подтверждения": "/pending",
    "🌙 Уснуть до…": "input:sleep", "💤 Уснуть после задач…": "input:sleep_after",
    "☀ Отменить сон": "/nosleep", "▶ Продолжить очередь": "/resumequeue",
    "🗑 Очистить очередь": "/clearqueue", "❓ Справка": "/help", "↩ Отмена ввода": "/menu",
}
MAIN_KEYBOARD = {"keyboard": [
    ["📉 Лимиты Codex"],
    ["✨ Новый чат", "💬 Выбрать чат"],
    ["🤖 Модель", "🧠 Рассуждение"],
    ["📒 Заметки", "✍ Добавить заметку"],
    ["📷 Скриншот", "📊 Статус", "💬 Выбрать чат"],
    ["📝 Новая задача", "➕ В очередь", "📋 Очередь"],
    ["⏹ Остановить", "📨 Последний ответ", "✅ Подтверждения"],
    ["🌙 Уснуть до…", "💤 Уснуть после задач…", "☀ Отменить сон"],
    ["▶ Продолжить очередь", "🗑 Очистить очередь"],
    ["🧹 Сжать контекст", "❓ Справка", "↩ Отмена ввода"],
], "resize_keyboard": True, "is_persistent": True,
   "input_field_placeholder": "Выбери кнопку или напиши задачу"}


def log(message: str) -> None:
    print(f"[telegram-acp] {message}", file=sys.stderr, flush=True)


def chunks(text: str, size: int = 3500):
    # Telegram limits characters; 3500 UTF-16 units also fits astral emoji.
    block, length = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if length + width > size:
            yield "".join(block)
            block, length = [], 0
        block.append(char)
        length += width
    if block:
        yield "".join(block)


def error_response(request_id, message: str, code: int = -32000) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


@dataclass
class Session:
    cwd: str = ""
    title: str = ""
    active: str | None = None
    source: str = ""
    text: str = ""
    last_flush: float = 0
    last_tool: str = ""
    current_reply: str = ""
    last_reply: str = ""
    config_options: list = field(default_factory=list)
    control: str | None = None


@dataclass
class Pending:
    message: dict
    sid: str
    expires: float


HELP = """/compact — сжать историю, сохранив краткое изложение (не полный сброс)
/models — модель текущего чата IntelliJ; /efforts — глубина рассуждения
/sessions — чаты, открытые через посредник
/new — создать и выбрать новый чат IntelliJ (нужен Telegram Chat Helper)
/menu — показать кнопки; /screenshot — снимок основного экрана ПК
/use ID — выбрать чат для Telegram
/release — вернуть подтверждения в IDE
/status — состояние
/limits — остаток лимитов Codex и время сброса
/cancel — остановить текущую задачу
/pending — повторить ожидающие подтверждения и вопросы
/answer КОД {\"поле\":\"ответ\"} — ответить на форму
/decline КОД — отклонить вопрос
/ask ТЕКСТ — отправить задачу (в том числе со слеш-командой)
/queue ТЕКСТ — добавить задачу в очередь; /queue — показать очередь
/clearqueue — очистить очередь; /resumequeue — продолжить после ошибки/отмены
/last — последний ответ
/sleep 08:30 — гибернация сейчас, пробуждение в 08:30
/sleep_after 08:30 — гибернация после всех задач и очереди
/nosleep — отменить запланированную гибернацию

Обычный текст тоже отправляется агенту. Во время работы новые задачи
отклоняются; для следующей задачи используй /queue ТЕКСТ.
Время — по часам компьютера. Также принимается дата: 2026-10-25 08:30."""


class Bridge:
    """Routing state. All entry points are serialized by a reentrant lock.

    Outbound callbacks must be non-blocking (the runtime uses queues).
    Request IDs are translated in both directions, including cancellation.
    """

    def __init__(self, send_agent: Callable, send_ide: Callable,
                 notify: Callable, owner_id: int, chat_id: int,
                 permission_timeout: float = 600, clock=time.monotonic, power=None,
                 screenshot=None, send_photo=None, notes=None):
        self.send_agent, self.send_ide, self.notify = send_agent, send_ide, notify
        self.owner_id, self.chat_id = owner_id, chat_id
        self.permission_timeout, self.clock = permission_timeout, clock
        self.lock = threading.RLock()
        self.prefix = secrets.token_hex(6)
        self.counter = 0
        self.ready = False
        self.alive = True
        self.sessions: dict[str, Session] = {}
        self.selected: str | None = None
        self.to_agent: dict[str, dict] = {}
        self.to_ide: dict[str, dict] = {}
        self.pending: dict[str, Pending] = {}
        self.jobs: list[tuple[str, str]] = []
        self.queue_paused = False
        self.power = power
        self.sleep_plan = None
        self.power_running = False
        self.screenshot = screenshot
        self.send_photo = send_photo
        self.screenshot_busy = False
        self.last_screenshot = float('-inf')
        self.input_mode = None
        self.session_buttons = {}
        self.config_buttons = {}
        self.notes = notes
        self.last_notice = ''
        self.creating_chat = False
        self.new_chat = None
        self.limits = None

    def new_id(self, kind: str) -> str:
        self.counter += 1
        return f"{self.prefix}:{kind}:{self.counter}"

    def say(self, text: str, buttons=None):
        self.last_notice = text
        return self.notify(text, buttons)

    def from_ide(self, message: dict):
        with self.lock:
            method, params = message.get("method"), message.get("params") or {}
            if not method:
                original = self.to_ide.pop(message.get("id"), None)
                if original is not None:
                    self.send_agent({**message, "id": original["id"]})
                return
            if method == "$/cancel_request":
                original_id = params.get("requestId")
                for key, req in self.to_agent.items():
                    if req["source"] == "ide" and req["message"].get("id") == original_id:
                        self.send_agent({**message, "params": {**params, "requestId": key}})
                        return
                return
            sid = params.get("sessionId")
            session = self.sessions.get(sid)
            if method == "session/cancel":
                self.cancel_pending(sid)
            if "id" not in message:
                self.send_agent(message)
                return
            if not self.alive:
                self.send_ide(error_response(message["id"], "ACP agent is offline"))
                return
            if self.power_running:
                self.send_ide(error_response(message["id"], "Hibernation in progress; retry after resume"))
                return
            if session and (session.active or session.control) and method in {
                "session/prompt", "session/load", "session/resume", "session/close",
                "session/delete", "session/set_mode", "session/set_config_option",
            }:
                self.send_ide(error_response(message["id"],
                    "Session is busy; wait or cancel the active turn first"))
                return
            wire_id = self.new_id("ide")
            self.to_agent[wire_id] = {"source": "ide", "message": message}
            if method == "session/prompt" and session:
                self.cancel_countdown()
                self.begin(session, wire_id, "ide")
            self.send_agent({**message, "id": wire_id})

    def begin(self, session: Session, wire_id: str, source: str):
        session.active, session.source = wire_id, source
        session.text, session.last_tool = "", ""
        session.current_reply = ""
        session.last_flush = self.clock()

    def from_agent(self, message: dict):
        with self.lock:
            method, params = message.get("method"), message.get("params") or {}
            if not method:
                self.agent_response(message)
                return
            if method == "$/cancel_request":
                request_id = params.get("requestId")
                for key, item in list(self.pending.items()):
                    if item.message["id"] == request_id:
                        self.resolve(key, cancelled=True)
                        return
                for key, original in self.to_ide.items():
                    if original["id"] == request_id:
                        self.send_ide({**message, "params": {**params, "requestId": key}})
                        return
                return
            if method == "elicitation/complete":
                eid = params.get("elicitationId")
                for key, item in list(self.pending.items()):
                    if eid and item.message["params"].get("elicitationId") == eid:
                        self.resolve(key, cancelled=True)
            if "id" in message:
                sid = params.get("sessionId")
                if sid and sid == self.selected and method in {
                    "session/request_permission", "elicitation/create"
                }:
                    if method == "elicitation/create" and not supported_form(params):
                        self.say("Агент запросил форму или авторизацию, которую прототип "
                                 "не поддерживает. Запрос передан в IntelliJ; можно /cancel.")
                    else:
                        key = secrets.token_hex(5)
                        self.pending[key] = Pending(copy.deepcopy(message), sid,
                                                    self.clock() + self.permission_timeout)
                        self.show_pending(key)
                        return
                wire_id = self.new_id("agent")
                self.to_ide[wire_id] = message
                forwarded = {**message, "id": wire_id}
                # Authentication elicitation can refer to the parent IDE request.
                parent = self.to_agent.get(params.get("requestId"))
                if parent and parent["source"] == "ide":
                    forwarded["params"] = {**params, "requestId": parent["message"]["id"]}
                self.send_ide(forwarded)
                return
            if method == "session/update":
                sid, update = params.get("sessionId"), params.get("update") or {}
                session = self.sessions.get(sid)
                if session:
                    kind = update.get("sessionUpdate")
                    if kind == 'config_option_update':
                        session.config_options = copy.deepcopy(update.get('configOptions') or [])
                    if kind == "session_info_update" and update.get("title"):
                        session.title = update["title"]
                    if sid == self.selected and session.active:
                        if kind == "agent_message_chunk":
                            content = update.get("content") or {}
                            if content.get("type") == "text":
                                session.text += content.get("text", "")
                                session.current_reply = (session.current_reply + content.get("text", ""))[-64000:]
                                if len(session.text) >= 3000:
                                    self.flush(session)
                        elif kind in {"tool_call", "tool_call_update"}:
                            if update.get("title"):
                                session.last_tool = update["title"]
                # History replay and thoughts are forwarded only to the IDE.
            self.send_ide(message)

    def agent_response(self, message: dict):
        wire_id = message.get("id")
        request = self.to_agent.pop(wire_id, None)
        if request is None:
            return
        original, source = request["message"], request["source"]
        method, params = original["method"], original.get("params") or {}
        result = message.get("result") or {}
        sid = params.get("sessionId")
        if "error" not in message:
            if method == "initialize":
                self.ready = True
            if method in {"session/new", "session/load", "session/resume", "session/fork"}:
                sid = result.get("sessionId", sid)
                if sid:
                    session = self.sessions.setdefault(sid, Session())
                    session.cwd = params.get("cwd", session.cwd)
            if method in {"session/close", "session/delete"}:
                self.cancel_pending(sid)
                self.sessions.pop(sid, None)
                self.jobs = [job for job in self.jobs if job[0] != sid]
                if sid == self.selected:
                    self.selected = None
                    self.say("Выбранный чат закрыт. Выбери другой через /sessions и /use ID.")
        session = self.sessions.get(sid)
        if session and 'error' not in message and isinstance(result.get('configOptions'), list):
            session.config_options = copy.deepcopy(result['configOptions'])
        if source == 'telegram_config' and session:
            session.control = None
            if 'error' in message:
                self.say('Агент не применил настройку. Повтори выбор модели/рассуждения.')
            else:
                self.send_ide({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                    'sessionId': sid, 'update': {'sessionUpdate': 'config_option_update',
                                               'configOptions': session.config_options}}})
                self.say('Настройка применена к чату IntelliJ.')
        if method == "session/prompt" and session and session.active == wire_id:
            self.cancel_pending(sid)
            session.last_reply = session.current_reply
            if "error" in message or result.get("stopReason") != "end_turn":
                self.queue_paused = True
                if self.sleep_plan:
                    self.sleep_plan = None
                    self.say("План гибернации снят: задача завершилась ошибкой, отменой или ограничением.")
            if sid == self.selected:
                self.flush(session)
                if "error" in message:
                    self.say("Ошибка агента: " + str(message["error"].get("message", "Unknown")))
                else:
                    self.say("Задача завершена: " + str(result.get("stopReason", "done")))
                if self.jobs and self.queue_paused:
                    self.say("Очередь приостановлена. /resumequeue — продолжить, /clearqueue — очистить.")
            session.active, session.source = None, ""
        if source == "ide":
            self.send_ide({**message, "id": original["id"]})

    def flush(self, session: Session):
        if session.text:
            self.say(session.text)
            session.text = ""
        session.last_flush = self.clock()

    def authorized(self, user, chat) -> bool:
        return (user.get("id") == self.owner_id and chat.get("id") == self.chat_id
                and chat.get("type") == "private")

    def telegram_update(self, update: dict):
        with self.lock:
            callback = update.get("callback_query")
            if callback:
                msg = callback.get("message") or {}
                if not self.authorized(callback.get("from") or {}, msg.get("chat") or {}):
                    return False
                data = callback.get("data", "").split(":")
                if len(data) == 3 and data[0] == "perm":
                    self.resolve(data[1], choice=data[2])
                elif len(data) == 3 and data[:2] == ['ui', 'use']:
                    selection = self.session_buttons.get(data[2])
                    if selection and self.clock() < selection[1]:
                        self.input_mode = None
                        self.command('/use ' + selection[0])
                    else:
                        self.say('Кнопка выбора чата устарела. Нажми «💬 Выбрать чат».')
                elif len(data) == 3 and data[:2] == ['ui', 'config']:
                    choice = self.config_buttons.pop(data[2], None)
                    if choice and choice[0] == self.selected and self.clock() < choice[3]:
                        self.set_config(choice[1], choice[2])
                    else:
                        self.say('Кнопка устарела или чат сменился. Открой выбор настройки заново.')
                return True
            msg = update.get("message") or {}
            if not self.authorized(msg.get("from") or {}, msg.get("chat") or {}):
                return False
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                self.say("В прототипе поддерживаются текстовые сообщения.")
                return True
            self.handle_text(text.strip())
            return True

    def handle_text(self, text):
        action = MENU_ACTIONS.get(text)
        if action:
            self.input_mode = None
            if action.startswith('input:'):
                mode = action.split(':', 1)[1]
                if mode in {'ask', 'queue'} and not self.selected:
                    self.say('Сначала нажми «💬 Выбрать чат» и выбери чат кнопкой.')
                    return
                self.input_mode = (mode, self.clock() + 300, self.selected)
                hint = ('Напиши заметку следующим сообщением. Она сохранится на ПК, без отправки агенту.' if mode == 'note' else
                        'Напиши текст задачи следующим сообщением.' if mode in {'ask', 'queue'} else
                        f'Напиши время пробуждения, например 08:30, или дату: 2026-10-25 08:30.\n'
                        f'Сейчас на ПК {datetime.now().astimezone():%Y-%m-%d %H:%M %z}.')
                self.say(hint + '\n«↩ Отмена ввода» — вернуться в меню. Ожидаю 5 минут.')
                return
            self.command(action)
        elif text.startswith('/'):
            self.input_mode = None
            self.command(text)
        elif self.input_mode:
            mode, deadline, sid = self.input_mode
            self.input_mode = None
            if self.clock() >= deadline or (mode in {'ask', 'queue'} and sid != self.selected):
                self.say('Ожидание ввода истекло или чат сменился. Нажми нужную кнопку заново; текст не отправлен.')
                return
            self.command('/' + mode + ' ' + text)
        else:
            self.command(text)

    def command(self, text: str):
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0], arg.strip()
        if self.creating_chat and cmd in {'/use', '/release', '/queue', '/resumequeue', '/sleep', '/sleep_after'}:
            self.say('Дождись подключения нового чата.')
            return
        if cmd in {"/start", "/help", "/menu"}:
            self.input_mode = None
            self.say('Выбери действие кнопкой ниже.\n\n' + HELP, MAIN_KEYBOARD)
        elif cmd == '/limits':
            if self.limits:
                self.say('Запрашиваю лимиты Codex…')
                self.limits.refresh(self.say)
            else:
                self.say('Просмотр лимитов доступен в окне бота: запусти start_bot.cmd.')
        elif cmd == '/screenshot':
            self.take_screenshot()
        elif cmd == '/new':
            if self.new_chat:
                self.new_chat.start()
            else:
                self.say('Создание чатов доступно в окне бота: запусти start_bot.cmd.')
        elif cmd == '/compact':
            self.prompt('/compact')
        elif cmd in {'/models', '/efforts'}:
            self.show_config('model' if cmd == '/models' else 'thought_level')
        elif cmd in {'/notes', '/note'}:
            if self.notes is None:
                self.say('Хранилище заметок недоступно.')
                return
            try:
                if cmd == '/note':
                    if not arg:
                        self.say('Нажми «✍ Добавить заметку» и напиши текст.')
                        return
                    self.notes.add(arg)
                    self.say('Заметка сохранена. Посмотреть: «📒 Заметки».')
                else:
                    items = self.notes.read()
                    self.say('\n\n'.join(f'{i}. {n["date"]}\n{n["text"]}'
                             for i, n in list(enumerate(items, 1))[-20:])
                             or 'Заметок пока нет. Нажми «✍ Добавить заметку».')
                    if len(items) > 20:
                        self.say('Показаны последние 20 заметок. Все сохранены в notes.local.json на ПК.')
            except (OSError, ValueError):
                self.say('Не удалось прочитать/сохранить заметки. Проверь notes.local.json и права записи. '
                         'Лимит: 500 заметок, 8000 символов на заметку.')
        elif cmd == "/sessions":
            self.session_buttons.clear()
            if not self.sessions:
                self.say("Нет открытых чатов. Создай чат с агентом-посредником в IntelliJ.")
            for sid, s in list(self.sessions.items())[:30]:
                token = secrets.token_hex(5)
                self.session_buttons[token] = (sid, self.clock() + 300)
                self.say(f"{s.title or '(без названия)'}\n{s.cwd}\n{sid}",
                         [[{'text': 'Выбрать этот чат', 'callback_data': f'ui:use:{token}'}]])
        elif cmd == "/use":
            if self.power_running or self.jobs or self.pending or any(s.active or s.control for s in self.sessions.values()):
                self.say("Сначала дождись завершения текущих задач или отмени их.")
            elif arg not in self.sessions:
                self.say("Неизвестный ID. Скопируй полный ID из /sessions.")
            else:
                self.selected = arg
                self.say(f"Выбран чат {arg}. Ответы и поддерживаемые подтверждения идут сюда.")
        elif cmd == "/release":
            if self.power_running or self.jobs or self.pending or (self.selected and (
                    self.sessions[self.selected].active or self.sessions[self.selected].control)):
                self.say("Сначала заверши или отмени текущую задачу.")
            else:
                self.selected = None
                self.say("Telegram отключён от чата. Подтверждения снова обрабатывает IDE.")
        elif cmd == "/status":
            session = self.sessions.get(self.selected)
            state = "работает" if session and session.active else "свободен"
            if self.pending:
                state = "ждёт подтверждения/ответа: /pending"
            self.say(f"Агент: {'онлайн' if self.alive else 'отключён'}\n"
                     f"Чат: {self.selected or 'не выбран'}\nСостояние: {state}"
                     + (f"\nДействие: {session.last_tool}" if session and session.last_tool else "")
                     + f"\nОчередь: {len(self.jobs)} ({'пауза' if self.queue_paused else 'активна'})"
                     + (f"\nПробуждение: {self.sleep_plan['wake']:%Y-%m-%d %H:%M %z}"
                        if self.sleep_plan else ""))
        elif cmd == "/last":
            session = self.sessions.get(self.selected)
            self.say((session.last_reply if session else "") or "Сохранённого ответа пока нет.")
        elif cmd == "/queue":
            if not arg:
                self.say("\n".join(f"{i}. {text[:200]}" for i, (_, text) in enumerate(self.jobs, 1))
                         or "Очередь пуста.")
            elif not self.selected or not self.alive or self.power_running:
                self.say("Сначала выбери доступный чат через /use ID.")
            elif len(self.jobs) >= 20:
                self.say("Очередь заполнена (20 задач).")
            else:
                self.cancel_countdown()
                self.jobs.append((self.selected, arg))
                self.say(f"Добавлено в очередь: №{len(self.jobs)}."
                         + (" Очередь на паузе: /resumequeue." if self.queue_paused else ""))
        elif cmd == "/clearqueue":
            self.jobs.clear()
            self.queue_paused = False
            self.sleep_plan = None
            self.say("Очередь и план гибернации очищены. Текущая задача продолжает работу.")
        elif cmd == "/resumequeue":
            self.queue_paused = False
            self.say("Очередь продолжится после завершения текущей задачи.")
        elif cmd in {"/sleep", "/sleep_after"}:
            self.plan_sleep(arg, after=cmd == "/sleep_after")
        elif cmd == "/nosleep":
            if self.power_running:
                self.say("Windows уже выполняет переход в гибернацию.")
            else:
                self.sleep_plan = None
                self.say("План гибернации отменён.")
        elif cmd == "/pending":
            if not self.pending:
                self.say("Ожидающих запросов нет.")
            for key in self.pending:
                self.show_pending(key)
        elif cmd == "/cancel":
            self.queue_paused = True
            self.sleep_plan = None
            session = self.sessions.get(self.selected)
            if not session or not session.active:
                self.say("Активной задачи нет.")
            else:
                self.cancel_pending(self.selected)
                self.send_agent({"jsonrpc": "2.0", "method": "session/cancel",
                                 "params": {"sessionId": self.selected}})
                self.say("Отмена отправлена. Жду завершения задачи.")
        elif cmd == "/decline":
            self.resolve(arg, cancelled=True)
        elif cmd == "/answer":
            key, _, raw = arg.partition(" ")
            try:
                content = json.loads(raw)
                self.resolve(key, content=content)
            except (ValueError, TypeError):
                self.say('Формат: /answer КОД {"поле":"ответ"}')
        elif cmd == "/ask":
            self.prompt(arg)
        elif text.startswith("/"):
            self.say("Неизвестная команда. /help — справка; /ask /команда — передать агенту.")
        else:
            self.prompt(text)

    def prompt(self, text: str):
        if self.creating_chat:
            self.say('Дождись подключения нового чата, затем отправь задачу.')
            return
        session = self.sessions.get(self.selected)
        if not self.alive or not self.ready or not session:
            self.say("Открой чат через посредник в IntelliJ, затем /sessions и /use ID.")
            return
        if session.active or session.control or self.pending:
            self.say("Агент занят. Сообщение не отправлено. Дождись ответа или /cancel.")
            return
        if self.power_running:
            self.say("Переход в гибернацию уже начался. Повтори после пробуждения.")
            return
        if not text:
            self.say("Укажи текст задачи.")
            return
        wire_id = self.new_id("telegram")
        self.cancel_countdown()
        message = {"jsonrpc": "2.0", "id": wire_id, "method": "session/prompt",
                   "params": {"sessionId": self.selected,
                              "prompt": [{"type": "text", "text": text}]}}
        self.to_agent[wire_id] = {"source": "telegram", "message": message}
        self.begin(session, wire_id, "telegram")
        # Standard ACP notification; actual IDE rendering still needs live QA.
        self.send_ide({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": self.selected, "update": {"sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": text}}}})
        self.send_agent(message)
        self.say("Задача отправлена агенту.")

    def busy(self):
        return bool(self.creating_chat or self.screenshot_busy or self.jobs or self.pending or self.to_agent or self.to_ide
                    or any(s.active for s in self.sessions.values()))

    def take_screenshot(self):
        if not self.screenshot or not self.send_photo or self.power_running:
            self.say('Скриншот сейчас недоступен.')
            return
        if self.screenshot_busy or self.clock() - self.last_screenshot < 5:
            self.say('Подожди несколько секунд перед следующим снимком.')
            return
        self.cancel_countdown()
        self.last_screenshot = self.clock()
        self.screenshot_busy = True
        self.say('Снимаю основной экран ПК…')
        def capture():
            try:
                png = self.screenshot()
                self.send_photo(png, f'Основной экран ПК · {datetime.now().astimezone():%H:%M:%S %z}')
            except Exception as exc:
                self.say(f'Скриншот не получился: {exc}')
            finally:
                with self.lock:
                    self.screenshot_busy = False
        threading.Thread(target=capture, daemon=True).start()

    @staticmethod
    def config_choices(option):
        choices = []
        for item in option.get('options') or []:
            if isinstance(item, dict):
                if isinstance(item.get('value'), str):
                    choices.append(item)
                elif 'options' in item:
                    choices.extend(Bridge.config_choices(item))
        return choices

    def show_config(self, category):
        session = self.sessions.get(self.selected)
        if not session or not self.ready:
            self.say('Открой чат Codex via Telegram в IntelliJ и выбери его через «💬 Выбрать чат».')
            return
        option = next((o for o in session.config_options if o.get('category') == category
                       and o.get('type') == 'select'), None)
        if not option:
            self.say('Агент не предоставил эту настройку. Проверь выбор модели в IntelliJ.')
            return
        self.config_buttons.clear()
        buttons = []
        for item in self.config_choices(option):
            key = secrets.token_hex(5)
            self.config_buttons[key] = (self.selected, option['id'], item['value'], self.clock() + 300)
            label = ('✓ ' if item['value'] == option.get('currentValue') else '') + item.get('name', item['value'])
            buttons.append([{'text': label[:60], 'callback_data': 'ui:config:' + key}])
        self.say('Настройка текущего чата IntelliJ: ' + option.get('name', category), buttons)

    def set_config(self, config_id, value):
        session = self.sessions.get(self.selected)
        if not session or not self.alive or not self.ready:
            self.say('Сначала подключи и выбери чат IntelliJ.')
            return
        if session.active or session.control or self.jobs or self.pending or self.power_running:
            self.say('Сначала дождись завершения задачи и очереди.')
            return
        option = next((o for o in session.config_options if o.get('id') == config_id), {})
        if value not in {x['value'] for x in self.config_choices(option)}:
            self.say('Настройка больше не доступна. Обнови список.')
            return
        self.cancel_countdown()
        wire_id = self.new_id('config')
        message = {'jsonrpc': '2.0', 'id': wire_id, 'method': 'session/set_config_option',
                   'params': {'sessionId': self.selected, 'configId': config_id, 'value': value}}
        self.to_agent[wire_id] = {'source': 'telegram_config', 'message': message}
        session.control = wire_id
        self.send_agent(message)

    def cancel_countdown(self):
        if self.sleep_plan and self.sleep_plan['stage'] != 'waiting':
            self.sleep_plan = None
            self.say("Отсчёт до гибернации отменён: поступила новая задача.")

    def plan_sleep(self, text, after):
        if not self.power or self.power_running:
            self.say("Управление питанием сейчас недоступно.")
            return
        if not after and self.busy():
            self.say("Есть активные задачи или очередь. Используй /sleep_after ВРЕМЯ.")
            return
        try:
            wake = parse_wake(text)
            self.power.preflight()
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.say(str(exc))
            return
        self.sleep_plan = {"wake": wake, "stage": "waiting"}
        self.say(f"План: гибернация {'после задач и очереди' if after else 'при простое'}, "
                 f"пробуждение {wake:%Y-%m-%d %H:%M %z}. /nosleep — отмена.")

    def power_tick(self):
        plan = self.sleep_plan
        if not plan or self.power_running:
            return
        if plan['wake'].timestamp() - time.time() < 90:
            self.sleep_plan = None
            self.say("Время пробуждения уже близко. План гибернации отменён.")
            return
        if self.busy():
            return
        if plan['stage'] == 'waiting':
            plan['receipt'] = self.say(f"Через 15 секунд после доставки этого сообщения усну до "
                                       f"{plan['wake']:%Y-%m-%d %H:%M %z}. /nosleep — отмена.")
            plan['stage'] = 'delivery'
        elif plan['stage'] == 'delivery':
            if plan['receipt'] is not None and plan['receipt'].is_set():
                plan['deadline'] = self.clock() + 15
                plan['stage'] = 'countdown'
        elif self.clock() >= plan['deadline']:
            self.power_running = True
            def suspend():
                try:
                    self.power.hibernate_until(plan['wake'])
                    self.say("Windows вернулась из гибернации. Бот снова готов к работе.")
                except Exception as exc:
                    self.say(f"Гибернация не выполнена: {exc}")
                finally:
                    with self.lock:
                        self.sleep_plan = None
                        self.power_running = False
            threading.Thread(target=suspend, daemon=True).start()

    def show_pending(self, key: str):
        item = self.pending[key]
        params = item.message["params"]
        if item.message["method"] == "session/request_permission":
            call = params.get("toolCall") or {}
            details = json.dumps({k: v for k, v in call.items()
                                  if k in {"title", "kind", "rawInput", "content", "locations"}},
                                 ensure_ascii=False, indent=2)
            # Send the FULL operation before its buttons, even if long.
            self.say(f"Codex просит разрешение [{key}]:\n{details}")
            buttons = [[{"text": option.get("name", option["optionId"])[:60],
                         "callback_data": f"perm:{key}:{index}"}]
                       for index, option in enumerate(params.get("options", []))]
            buttons.append([{"text": "Отменить запрос", "callback_data": f"perm:{key}:cancel"}])
            self.say(f"Решение для запроса {key}:", buttons)
        else:
            schema = params["requestedSchema"]
            self.say(f"Вопрос Codex [{key}]: {params.get('message', '')}\n"
                     + json.dumps(schema, ensure_ascii=False, indent=2)
                     + f'\nОтвет: /answer {key} {{"поле":"ответ"}}\nОтказ: /decline {key}')

    def resolve(self, key: str, choice: str | None = None,
                content: Any = None, cancelled: bool = False):
        item = self.pending.get(key)
        if not item:
            self.say("Этот запрос уже закрыт или устарел.")
            return
        if self.clock() >= item.expires:
            cancelled = True
        params = item.message["params"]
        if item.message["method"] == "session/request_permission":
            outcome = {"outcome": "cancelled"}
            if not cancelled and choice != "cancel":
                options = params.get("options", [])
                if choice is None or not choice.isdecimal() or int(choice) >= len(options):
                    self.say("Выбери кнопку у запроса разрешения.")
                    return
                outcome = {"outcome": "selected", "optionId": options[int(choice)]["optionId"]}
            result = {"outcome": outcome}
        else:
            if cancelled:
                result = {"action": "cancel"}
            elif not validate_form(params["requestedSchema"], content):
                self.say("Ответ не соответствует полям/вариантам формы. Проверь /pending.")
                return
            else:
                result = {"action": "accept", "content": content}
        del self.pending[key]
        self.send_agent({"jsonrpc": "2.0", "id": item.message["id"], "result": result})
        self.say(f"Запрос {key}: {'отменён' if cancelled or choice == 'cancel' else 'ответ отправлен'}.")

    def cancel_pending(self, sid):
        for key, item in list(self.pending.items()):
            if item.sid == sid:
                self.resolve(key, cancelled=True)

    def tick(self):
        with self.lock:
            for key, item in list(self.pending.items()):
                if self.clock() >= item.expires:
                    self.resolve(key, cancelled=True)
            session = self.sessions.get(self.selected)
            if session and session.active and self.clock() - session.last_flush >= 3:
                self.flush(session)
            if self.alive and self.ready and self.jobs and not self.queue_paused and not self.power_running:
                sid, text = self.jobs[0]
                target = self.sessions.get(sid)
                if target and sid == self.selected and not target.active and not target.control and not self.pending:
                    self.jobs.pop(0)
                    self.prompt(text)
            self.power_tick()

    def disconnected(self):
        with self.lock:
            if not self.alive:
                return
            self.alive, self.ready = False, False
            self.sleep_plan = None
            self.jobs.clear()
            for req in self.to_agent.values():
                if req["source"] == "ide":
                    self.send_ide(error_response(req["message"]["id"], "ACP agent disconnected"))
            self.to_agent.clear()
            self.pending.clear()
            for session in self.sessions.values():
                self.flush(session)
                session.active = None
                session.control = None
            if not self.creating_chat:
                self.say("Связь с агентом закрыта. Для продолжения запусти посредник из IntelliJ.")


# Deliberately limited: unsupported forms continue to the IDE unchanged.
def supported_form(params: dict) -> bool:
    schema = params.get("requestedSchema")
    if params.get("mode") != "form" or not isinstance(schema, dict):
        return False
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return False
    if set(schema) - {"type", "properties", "required", "additionalProperties", "title", "description"}:
        return False
    if schema.get("additionalProperties", False) not in (True, False):
        return False
    for prop in schema["properties"].values():
        if not isinstance(prop, dict) or prop.get("type") not in {"string", "boolean", "integer", "number"}:
            return False
        if set(prop) - {"type", "title", "description", "enum", "oneOf", "default", "_meta"}:
            return False
        if prop.get("_meta", {}).get("codex", {}).get("isSecret"):
            return False
        if "oneOf" in prop and not all(isinstance(x, dict) and "const" in x
                                        and not set(x) - {"const", "title", "description"}
                                        for x in prop["oneOf"]):
            return False
    return True


def validate_form(schema: dict, content: Any) -> bool:
    if not isinstance(content, dict):
        return False
    properties = schema["properties"]
    if set(content) - set(properties) or set(schema.get("required", [])) - set(content):
        return False
    types = {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float)}
    for key, value in content.items():
        prop = properties[key]
        if type(value) not in types[prop["type"]]:
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if "enum" in prop and value not in prop["enum"]:
            return False
        if "oneOf" in prop and value not in [option["const"] for option in prop["oneOf"]]:
            return False
    return True


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=5, detail=""):
        # Never include request URLs: Telegram embeds the token in them.
        super().__init__(f"Telegram API error {code}" + (f": {detail}" if detail else ""))
        self.code, self.retry_after = code, retry_after


def network_error(reason):
    if isinstance(reason, ssl.SSLCertVerificationError):
        detail = "TLS: Python не доверяет сертификату сервера; проверь сертификаты VPN/антивируса."
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        detail = "Истекло время соединения с Telegram; проверь VPN и интернет."
    elif isinstance(reason, socket.gaierror):
        detail = "DNS: не удалось найти api.telegram.org."
    elif isinstance(reason, ssl.SSLError):
        detail = "Сбой защищённого соединения TLS с Telegram."
    else:
        detail = "Не удалось соединиться с Telegram; проверь VPN, прокси и интернет."
    # Do not stringify exceptions: they can contain a bot URL or proxy credentials.
    return TelegramError(detail=detail)


def curl_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def multipart_photo(params):
    boundary = 'telegram_' + secrets.token_hex(16)
    parts = []
    for name, value in params.items():
        if name == '_photo':
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      f'{value}\r\n').encode('utf-8'))
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                  'filename="screenshot.png"\r\nContent-Type: image/png\r\n\r\n').encode('ascii'))
    parts.extend([params['_photo'], f'\r\n--{boundary}--\r\n'.encode('ascii')])
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.curl_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/curl.exe"
        self.use_curl = False

    def call_curl(self, method, params):
        if '_photo' in params:
            body, content_type = multipart_photo(params)
            with tempfile.TemporaryDirectory(prefix='telegram-photo-') as folder:
                path = Path(folder) / 'upload.bin'
                path.write_bytes(body)
                config = ('url = ' + curl_quote(self.base + method) + '\n'
                          + 'header = ' + curl_quote('Content-Type: ' + content_type) + '\n'
                          + 'data-binary = ' + curl_quote('@' + str(path)) + '\n')
                return self.run_curl(config)
        # Windows' Schannel uses native certificate validation. Never use --insecure.
        # Token and body go through stdin, not process arguments or temporary files.
        config = ("url = " + curl_quote(self.base + method) + "\n"
                  + 'header = "Content-Type: application/json"\n'
                  + "data-raw = " + curl_quote(json.dumps(params, ensure_ascii=False)) + "\n")
        return self.run_curl(config)

    def run_curl(self, config):
        try:
            completed = subprocess.run([
                str(self.curl_path), "--disable", "--silent", "--show-error",
                "--connect-timeout", "15", "--max-time", "40", "--proto", "=https",
                "--config", "-", "--write-out", "\\n%{http_code}"],
                input=config.encode("utf-8"), capture_output=True, timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise network_error(TimeoutError()) from None
        except OSError:
            raise TelegramError(detail="Не удалось запустить системный curl Windows.") from None
        if completed.returncode:
            errors = {5: "Не найден прокси.", 6: "DNS: не найден api.telegram.org.",
                      7: "Не удалось подключиться к Telegram.", 28: "Истекло время соединения.",
                      35: "Ошибка TLS.", 60: "Windows не доверяет сертификату сервера."}
            raise TelegramError(detail=errors.get(completed.returncode,
                                f"Сетевой сбой curl, код {completed.returncode}."))
        try:
            body, status = completed.stdout.rsplit(b"\n", 1)
            status = int(status)
            result = json.loads(body)
        except (ValueError, TypeError):
            raise TelegramError(detail="Telegram вернул некорректный ответ вместо JSON.") from None
        return self.parse_result(result, status)

    @staticmethod
    def parse_result(result, status=200):
        if not isinstance(result, dict):
            raise TelegramError(detail="Telegram вернул некорректную структуру JSON.")
        if not result.get("ok") or status >= 400:
            code = result.get("error_code", status if status >= 400 else 0)
            hints = {401: "Неверный или отозванный токен.",
                     404: "Бот или метод не найден; проверь токен.",
                     409: "Другой процесс получает сообщения или настроен webhook.",
                     429: "Telegram ограничил частоту запросов."}
            raise TelegramError(code, (result.get("parameters") or {}).get("retry_after", 5),
                                hints.get(code, "Telegram отклонил запрос."))
        if "result" not in result:
            raise TelegramError(detail="В ответе Telegram отсутствует result.")
        return result["result"]

    def call(self, method: str, **params):
        if self.use_curl:
            return self.call_curl(method, params)
        if '_photo' in params:
            body, content_type = multipart_photo(params)
        else:
            body, content_type = json.dumps(params, ensure_ascii=False).encode('utf-8'), 'application/json'
        request = urllib.request.Request(self.base + method,
            data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError, http.client.HTTPException):
                result = {}
            return self.parse_result(result, exc.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError) and os.name == "nt" and self.curl_path.is_file():
                self.use_curl = True
                log("Python TLS verification failed; using Windows curl with certificate validation.")
                return self.call_curl(method, params)
            raise network_error(reason) from None
        except ValueError:
            raise TelegramError(detail="Telegram вернул некорректный ответ вместо JSON.") from None
        return self.parse_result(result)


class TelegramWorker:
    def __init__(self, api, core, chat_id, stop):
        self.api, self.core, self.chat_id, self.stop = api, core, chat_id, stop
        self.outbox = queue.Queue()
        self.started = int(time.time())
        self.connection_status = 'подключается'

    def notify(self, text, buttons=None):
        parts = list(chunks(text))
        delivered = threading.Event()
        for index, part in enumerate(parts):
            params = {"chat_id": self.chat_id, "text": part,
                      "link_preview_options": {"is_disabled": True}}
            if index == len(parts) - 1:
                params["reply_markup"] = (buttons if isinstance(buttons, dict) else
                                          {"inline_keyboard": buttons} if buttons else MAIN_KEYBOARD)
            self.outbox.put(("sendMessage", params, delivered if index == len(parts) - 1 else None))
        return delivered

    def photo(self, png, caption):
        self.outbox.put(('sendPhoto', {'chat_id': self.chat_id, '_photo': png,
                                     'caption': caption, 'reply_markup': MAIN_KEYBOARD}, None))

    def send_loop(self):
        while not self.stop.is_set():
            try:
                method, params, delivered = self.outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            while not self.stop.is_set():
                try:
                    self.api.call(method, **params)
                    if delivered is not None:
                        delivered.set()
                    break
                except TelegramError as exc:
                    log(str(exc))
                    if exc.code and exc.code != 429 and exc.code < 500:
                        if method == 'sendPhoto':
                            self.notify('Не удалось отправить скриншот. Попробуй ещё раз: ' + str(exc))
                        break
                    if self.stop.wait(max(1, min(exc.retry_after, 60))):
                        return
            # Keep within Telegram's per-chat sending rate.
            self.stop.wait(1.1)

    def poll_loop(self):
        offset = 0
        while not self.stop.is_set():
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=25,
                                        allowed_updates=["message", "callback_query"])
                self.connection_status = 'соединение работает'
                for update in updates:
                    uid = update["update_id"]
                    if uid < offset:
                        continue
                    offset = uid + 1
                    msg = update.get("message")
                    # After a restart never replay offline tasks or binding commands.
                    if msg and msg.get("date", 0) < self.started:
                        continue
                    accepted = self.core.telegram_update(update)
                    callback = update.get("callback_query")
                    if callback and accepted:
                        self.outbox.put(("answerCallbackQuery", {"callback_query_id": callback["id"]}, None))
            except TelegramError as exc:
                self.connection_status = str(exc)
                log(str(exc))
                if exc.code == 401:
                    log("Telegram polling stopped: check token.")
                    return
                if exc.code == 409:
                    # A pre-suspend long poll may still be alive on Telegram's side.
                    # Retry without launching a second polling thread.
                    if self.stop.wait(30):
                        return
                    continue
                self.stop.wait(max(1, min(exc.retry_after, 60)))


class InstanceLock:
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        self.file.close()


def load_config(path: Path, require_token=True):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    command = config.get("agent_command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("agent_command must be a non-empty argument array")
    for key in ("owner_id", "chat_id"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive private Telegram ID; run setup.py")
    if not require_token:
        return config, ""
    if config.get("paired") is not True:
        raise ValueError("Run setup.py to pair the Telegram owner first")
    token_file = path.parent / config.get("token_file", "bot-token.txt")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or token_file.read_text(encoding="utf-8-sig").strip()
    if not token or ":" not in token or any(c.isspace() for c in token):
        raise ValueError("Invalid Telegram bot token")
    return config, token


def run(config_path: Path, stdio_only=False):
    from ide_link import run as run_link, run_diagnostic
    return run_diagnostic(config_path) if stdio_only else run_link(config_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stdio-only", action="store_true", help="Disable Telegram for ACP diagnostics")
    args = parser.parse_args()
    try:
        run(args.config.resolve(), args.stdio_only)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log(f"Startup failed ({type(exc).__name__}); check config, paths, token and instance lock.")
        sys.exit(1)
