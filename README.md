# Р-26-1 Info Bot 2.1.1

Telegram-инфо-бот для группы Р-26-1.

## Запуск
1. Python 3.11+.
2. Скопировать `.env.example` в `.env`.
3. Заполнить `BOT_TOKEN` и `ADMIN_IDS` (ID через запятую).
4. `pip install -r requirements.txt`
5. `python main.py`

База SQLite лежит в `data/bot.db`. При старте выполняется безопасная миграция: существующие данные не удаляются.

Часовой пояс: Europe/Moscow (UTC+3).

Команды:
- `/start` — главное меню
- `/info` — информация о боте и Telegram ID
- `/admin` — админ-панель (только ADMIN_IDS)

Для Bothost точкой запуска используйте `python main.py`.
