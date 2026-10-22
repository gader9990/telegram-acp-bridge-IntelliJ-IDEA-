# Telegram ↔ IntelliJ IDEA

[English version](#english)

Мост подключает Telegram к Codex ACP в IntelliJ IDEA. Проверен с IntelliJ IDEA 2026.2.3 (build 262.10968.223) и JetBrains AI Assistant.

Исходники можно быстро адаптировать под себя: попросите Codex добавить нужную функцию, а затем просмотрите изменения в IntelliJ.

## Быстрый старт

Нужны Windows, Python 3.11+ с Tkinter, IntelliJ IDEA с установленным и включённым JetBrains AI Assistant, настроенный Codex ACP и Telegram-бот. Внешние Python-пакеты не нужны.

1. Из папки репозитория выполните `python setup.py`. Следуйте подсказкам, чтобы создать локальную конфигурацию и привязать бота.
2. Добавьте содержимое созданного `acp-entry.local.json` в `agent_servers` файла `~/.jetbrains/acp.json`, сохранив остальные записи.
3. Установите `idea-helper/dist/telegram-chat-helper.zip` через **Settings → Plugins → ⚙ → Install Plugin from Disk** и перезапустите IntelliJ.
4. Запустите `start_bot.cmd`, оставьте проект открытым и нажмите **«Новый чат»** в Telegram или окне бота. Подтвердите подключение в IntelliJ и отправьте задачу.

Помощник Telegram Chat Helper нужен для создания чатов. Сам AI-чат заранее открывать не нужно. Плагин проверен с IDEA build `262.10968.223` и использует внутренний API AI Assistant; с другими сборками совместимость не гарантируется.

## Команды

- `/new`, `/sessions`, `/use ID` — создать или выбрать чат IntelliJ.
- Текст или `/ask ТЕКСТ` — отправить задачу; `/cancel` — остановить её.
- `/queue ТЕКСТ`, `/queue`, `/clearqueue`, `/resumequeue` — управлять очередью.
- `/models`, `/efforts` — выбрать модель и уровень рассуждения, если их предоставляет агент.
- `/pending` — открыть ожидающие подтверждения и вопросы.
- `/notes`, `/note ТЕКСТ`, `/screenshot`, `/compact`, `/limits` — заметки, снимок экрана, сжатие контекста и лимиты Codex.
- `/sleep 08:30`, `/sleep_after 08:30`, `/nosleep` — планировать или отменить гибернацию Windows.

Полный список действий доступен через `/menu`. Команды и подтверждения принимает только привязанный владелец в личном чате.

## Доработка

Попросите Codex реализовать недостающую функцию в этом проекте. Например: «Добавь команду `/foo`, которая делает X; добавь кнопку в меню и обнови README». Укажите, где она нужна — в Telegram, окне бота или в обоих местах. Просмотрите diff в IntelliJ; после изменения кода перезапустите бота.

## Важно

- Первое подключение и разрешения подтверждаются в IntelliJ. Если чат не появился за две минуты, проверьте `/sessions` перед повтором: запросы автоматически не повторяются.
- Кнопка **«Завершить бота»** останавливает процесс и может прервать текущую задачу. Обычное закрытие AI-чата оставляет бота запущенным.
- Для команд сна нужны разрешённая гибернация и таймеры пробуждения Windows; окно бота должно оставаться запущенным.
- Не публикуйте `bot-token.txt`, `*.local.json`, `*.local.tmp` и `notes.local.json`: там секреты, локальные настройки и личные заметки.

## Проверка

Из корня проекта запустите:

```powershell
python -m unittest discover -s . -p "test_*.py"
```

## English

This bridge connects Telegram to Codex ACP in IntelliJ IDEA. It has been tested with IntelliJ IDEA 2026.2.3 (build 262.10968.223) and JetBrains AI Assistant.

You can adapt the source to your needs: ask Codex to add a feature, then review the changes in IntelliJ.

### Quick start

You need Windows, Python 3.11+ with Tkinter, IntelliJ IDEA with JetBrains AI Assistant installed and enabled, Codex ACP configured, and a Telegram bot. No extra Python packages are required.

1. From the repository root, run `python setup.py`. Follow the prompts to create the local configuration and pair your bot.
2. Merge the generated `acp-entry.local.json` into `agent_servers` in `~/.jetbrains/acp.json`, preserving other entries.
3. Install `idea-helper/dist/telegram-chat-helper.zip` using **Settings → Plugins → ⚙ → Install Plugin from Disk**, then restart IntelliJ.
4. Run `start_bot.cmd`, keep the project open, and select **New chat** in Telegram or the bot window. Confirm the connection in IntelliJ, then send your task.

The Telegram Chat Helper plugin is required to create chats. You do not need to open an AI chat first. The plugin was tested with IDEA build `262.10968.223` and uses an internal AI Assistant API; compatibility with other builds is not guaranteed.

### Commands

- `/new`, `/sessions`, `/use ID` — create or select an IntelliJ chat.
- Send text or use `/ask TEXT` to start a task; `/cancel` stops it.
- `/queue TEXT`, `/queue`, `/clearqueue`, `/resumequeue` — manage the task queue.
- `/models`, `/efforts` — choose a model and reasoning level when offered by the agent.
- `/pending` — review pending approvals and questions.
- `/notes`, `/note TEXT`, `/screenshot`, `/compact`, `/limits` — notes, screenshot, context compaction, and Codex limits.
- `/sleep 08:30`, `/sleep_after 08:30`, `/nosleep` — schedule or cancel Windows hibernation.

Use `/menu` to see all actions. Only the paired owner can send commands or respond to approvals in the private chat.

### Customize

Ask Codex to implement a missing feature in this project. For example: “Add a `/foo` command that does X, add a menu button, and update the README.” Say whether it should be available in Telegram, the bot window, or both. Review the diff in IntelliJ and restart the bot after code changes.

### Notes

- Confirm the first connection and agent permissions in IntelliJ. If the chat does not appear within two minutes, check `/sessions` before trying again; requests are not retried automatically.
- **Stop bot** terminates the process and may interrupt the current task. Closing an AI chat leaves the bot running.
- Sleep commands require Windows hibernation and wake timers to be enabled. Keep the bot window running.
- Do not publish `bot-token.txt`, `*.local.json`, `*.local.tmp`, or `notes.local.json`; they may contain credentials, local settings, or private notes.

### Tests

From the repository root, run:

```powershell
python -m unittest discover -s . -p "test_*.py"
```
