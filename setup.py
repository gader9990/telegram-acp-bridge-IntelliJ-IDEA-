"""Prepare local files, then pair a private Telegram bot using a one-time code."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

from bridge import Telegram, TelegramError

HERE = Path(__file__).resolve().parent


def version_key(path):
    return tuple(int(n) for n in re.findall(r"\d+", str(path)))


def discover_command() -> list[str]:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "JetBrains"
    adapters = sorted(root.glob(
        "*/acp-agents/codex-acp/*/node_modules/@agentclientprotocol/codex-acp/dist/index.js"),
        key=version_key, reverse=True)
    for adapter in adapters:
        agents_root = adapter.parents[6]
        nodes = sorted((agents_root / ".runtimes/node").glob("*/node.exe"),
                       key=version_key, reverse=True)
        if nodes:
            return [str(nodes[0]), str(adapter)]
    raise RuntimeError("Не найден установленный codex-acp и Node.js в каталоге JetBrains.")


def write_json(path: Path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare():
    config_path = HERE / "config.local.json"
    if not config_path.exists():
        write_json(config_path, {
            "agent_command": discover_command(),
            "owner_id": 1,
            "chat_id": 1,
            "paired": False,
            "token_file": "bot-token.txt",
            "permission_timeout_seconds": 600,
        })
    entry = {"agent_servers": {"Codex via Telegram": {
        "command": sys.executable,
        "args": [str(HERE / "bridge.py"), "--config", str(config_path)],
        "env": {},
        "use_idea_mcp": True,
        "use_custom_mcp": True,
    }}}
    write_json(HERE / "acp-entry.local.json", entry)
    return config_path


def pair(config_path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("paired"):
        raise RuntimeError("Бот уже привязан. Для перепривязки вручную поставь paired: false в config.local.json.")
    token = input("Токен нового бота от @BotFather: ").strip()
    if ":" not in token or any(c.isspace() for c in token):
        raise RuntimeError("Неверный формат токена.")
    api = Telegram(token)
    me = api.call("getMe")
    info = api.call("getWebhookInfo")
    if info.get("url"):
        raise RuntimeError("У этого бота уже есть webhook. Используй отдельного нового бота.")
    code = secrets.token_hex(8)
    print(f"Открой https://t.me/{me['username']} и отправь в личный чат:")
    print(f"/pair {code}\nОжидаю до 5 минут. Только обладатель этого кода сможет привязаться.")
    started, offset = int(time.time()), 0
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        for update in api.call("getUpdates", offset=offset, timeout=25,
                               allowed_updates=["message", "callback_query"]):
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            if (msg.get("text") == f"/pair {code}" and msg.get("date", 0) >= started
                    and msg.get("chat", {}).get("type") == "private"
                    and not msg.get("from", {}).get("is_bot", True)):
                config.update(owner_id=msg["from"]["id"], chat_id=msg["chat"]["id"], paired=True)
                (HERE / config["token_file"]).write_text(token, encoding="utf-8")
                write_json(config_path, config)
                api.call("sendMessage", chat_id=config["chat_id"],
                         text="Привязка готова. Подключи Codex via Telegram в IntelliJ, затем /sessions.")
                print("Привязка сохранена. Токен записан только в bot-token.txt.")
                return
    raise RuntimeError("Время привязки истекло. Запусти setup.py ещё раз.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Only generate local configuration, no network")
    args = parser.parse_args()
    path = prepare()
    if not args.prepare:
        pair(path)
    print("Создан acp-entry.local.json — это фрагмент настройки, не установленный плагин.")
    print("Добавь запись Codex via Telegram в agent_servers файла ~/.jetbrains/acp.json.")
    print("Подробная инструкция: README.md")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TelegramError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as exc:
        print(f"Настройка не завершена ({type(exc).__name__}). Проверь локальные файлы и сеть.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Привязка отменена.", file=sys.stderr)
        sys.exit(1)
