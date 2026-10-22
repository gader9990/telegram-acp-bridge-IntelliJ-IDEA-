"""Persistent Telegram/Tkinter host. The agent and tools remain owned by IntelliJ."""
import argparse
import os
from pathlib import Path
import threading

from bridge import Bridge, InstanceLock, Telegram, TelegramWorker, load_config, log
from ipc import Hub
from notes import Notes
from new_chat import NewChat
from power import WindowsPower
from screenshot import capture_screen
from limits import Limits


def run(config_path):
    config, token = load_config(config_path)
    try:
        instance = InstanceLock(config_path.with_suffix('.lock'))
    except OSError:
        raise RuntimeError('Окно бота уже запущено. Если работает старая версия, заверши её перед обновлением.') from None
    stop = threading.Event()
    hub = None
    try:
        worker = TelegramWorker(Telegram(token), None, config['chat_id'], stop)
        core = Bridge(lambda _: None, lambda _: None, worker.notify,
            config['owner_id'], config['chat_id'],
            permission_timeout=config.get('permission_timeout_seconds', 600),
            power=WindowsPower() if os.name == 'nt' else None,
            notes=Notes(config_path.parent / 'notes.local.json'),
            screenshot=capture_screen if os.name == 'nt' else None, send_photo=worker.photo)
        worker.core = core
        core.limits = Limits(config['agent_command'])
        core.limits.refresh()
        core.new_chat = NewChat(config_path.parent, core)
        hub = Hub(config_path, core)
        hub.start()
        for target in (worker.send_loop, worker.poll_loop, lambda: run_ticks(core, stop)):
            threading.Thread(target=target, daemon=True).start()
        from dashboard import run_dashboard
        run_dashboard(core, worker, stop)
    finally:
        stop.set()
        if hub:
            hub.close()
        instance.close()


def run_ticks(core, stop):
    failed = False
    try:
        while not stop.wait(.5):
            if core.power:
                try:
                    core.power.keep_awake(not core.power_running)
                    failed = False
                except OSError:
                    if not failed:
                        log('Could not prevent automatic sleep.')
                        failed = True
            core.tick()
    finally:
        if core.power:
            try:
                core.power.keep_awake(False)
            except OSError:
                pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.local.json'))
    parser.add_argument('--auto-start', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        run(args.config.resolve())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else (
            f'Не удалось запустить бота ({type(exc).__name__}). Проверь настройки и установку Python.')
        log(message)
        if not args.auto_start:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror('Telegram-бот', message)
                root.destroy()
            except Exception:
                pass
        raise SystemExit(1)
