"""One desktop window for the persistent bot and its actual IntelliJ sessions."""
import time


def run_dashboard(core, worker, stop, test_close_ms=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title('Telegram ↔ IntelliJ IDEA')
    root.geometry('860x650')
    root.minsize(640, 440)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Telegram ↔ IntelliJ IDEA', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, justify='left', wraplength=700).pack(anchor='w', pady=12)
    limits_text = tk.StringVar()
    ttk.Label(frame, textvariable=limits_text, justify='left', wraplength=800).pack(anchor='w', pady=6)
    limits_button = ttk.Button(frame, text='Обновить лимиты Codex',
                              command=lambda: core.limits.refresh() if core.limits else None)
    limits_button.pack(anchor='w')

    ttk.Label(frame, text='Чат, открытый в IntelliJ:').pack(anchor='w')
    chats = ttk.Combobox(frame, state='readonly', width=80)
    chats.pack(fill='x', pady=5)
    chat_ids = []

    def choose_chat(_):
        index = chats.current()
        if 0 <= index < len(chat_ids):
            with core.lock:
                core.command('/use ' + chat_ids[index])
    chats.bind('<<ComboboxSelected>>', choose_chat)

    settings = ttk.Frame(frame)
    settings.pack(fill='x', pady=8)
    selectors = {}
    config_ids = {}
    for category, title, width in [('model', 'Модель', 28), ('thought_level', 'Рассуждение', 15)]:
        ttk.Label(settings, text=title + ':').pack(side='left', padx=(0, 5))
        selector = ttk.Combobox(settings, state='disabled', width=width)
        selector.pack(side='left', padx=(0, 14))
        selectors[category] = selector
        def choose(_, category=category):
            with core.lock:
                config_id = config_ids.get(category)
                if config_id:
                    core.set_config(config_id, selectors[category].get())
        selector.bind('<<ComboboxSelected>>', choose)

    ttk.Label(frame, text='Создай чат кнопкой «Новый чат» (нужен плагин Telegram Chat Helper).\n'
              'Подключение и инструменты IDE передаются автоматически.\n'
              'Без AI-чата доступны меню, заметки, скриншоты и управление питанием.',
              wraplength=700).pack(anchor='w', pady=10)
    notice = tk.StringVar()
    ttk.Label(frame, textvariable=notice, justify='left', wraplength=700).pack(anchor='w', pady=6)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', side='bottom')

    def show_menu():
        with core.lock:
            core.command('/menu')

    def finish():
        with core.lock:
            busy = bool(core.pending or any(s.active or s.control for s in core.sessions.values()))
        if busy and not messagebox.askyesno('Завершить бота?',
                'Есть активная задача. Завершение бота прервёт связь с агентом IntelliJ.', parent=root):
            return
        stop.set()
        root.destroy()

    ttk.Button(buttons, text='Показать меню в Telegram', command=show_menu).pack(side='left')
    def new_chat():
        with core.lock:
            core.command('/new')
    ttk.Button(buttons, text='Новый чат', command=new_chat).pack(side='left', padx=8)
    ttk.Button(buttons, text='Завершить бота', command=finish).pack(side='right')
    ttk.Button(buttons, text='Свернуть', command=root.iconify).pack(side='right', padx=8)
    root.protocol('WM_DELETE_WINDOW', root.iconify)

    def refresh():
        nonlocal chat_ids
        if core.limits:
            limits_text.set(core.limits.text)
            limits_button['state'] = 'disabled' if core.limits.busy else 'normal'
        if stop.is_set():
            root.destroy()
            return
        with core.lock:
            session = core.sessions.get(core.selected)
            connection = ('подключена' if core.ready else 'подключается') if core.alive else 'ожидается чат Codex via Telegram'
            state = ('ждёт подтверждения' if core.pending else 'выполняет задачу' if session and session.active
                     else 'применяет настройку' if session and session.control else 'ожидает задачи')
            status.set(f'Бот работает · Telegram: {worker.connection_status}\nIntelliJ: {connection}\n'
                       f'Состояние: {state} · Очередь: {len(core.jobs)} · {time.strftime("%H:%M:%S")}')
            notice.set(core.last_notice[-350:])
            chat_ids = list(core.sessions)
            labels = [f'{s.title or sid} · {s.cwd}' for sid, s in core.sessions.items()]
            chats['values'] = labels
            if core.selected in chat_ids:
                chats.current(chat_ids.index(core.selected))
            else:
                chats.set('Выбери чат после подключения IntelliJ')
            for category, selector in selectors.items():
                option = next((o for o in session.config_options if o.get('category') == category
                               and o.get('type') == 'select'), None) if session else None
                if option:
                    config_ids[category] = option['id']
                    selector['values'] = [x['value'] for x in core.config_choices(option)]
                    selector.set(option.get('currentValue', ''))
                    selector['state'] = 'disabled' if session.active or session.control else 'readonly'
                else:
                    config_ids.pop(category, None)
                    selector['values'] = []
                    selector.set('')
                    selector['state'] = 'disabled'
        root.after(1000, refresh)

    if test_close_ms is not None:
        root.after(test_close_ms, lambda: (stop.set(), root.destroy()))
    refresh()
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
