# Railway Telegram Relay

Мини-сервис relay для отправки сообщений в Telegram из Railway.

## Что делает

- Принимает POST `/notify`
- Проверяет `relay_token`
- Отправляет `text` в Telegram (`sendMessage`)

## Переменные окружения в Railway

- `RELAY_TOKEN` — секрет для проверки входящих запросов
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота
- `TELEGRAM_DEFAULT_CHAT_ID` — чат по умолчанию (опционально)

## Локальный запуск

```bash
cd railway-relay
python -m venv .venv
# windows: .venv\Scripts\activate
# linux/mac: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Деплой на Railway

1. Запушьте репозиторий в GitHub.
2. Railway → New Project → Deploy from GitHub Repo.
3. **Обязательно:** в настройках сервиса (**Settings → Source** или **Service → Settings**) укажите **Root Directory** = `railway-relay`.  
   Если оставить корень репозитория, Railpack не найдёт приложение и будет ошибка **«Error creating build plan with Railpack»**.
4. Сборка идёт по **`Dockerfile`** внутри `railway-relay` (Python 3.11 + uvicorn).
5. Добавьте переменные окружения: `RELAY_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`.
6. В **Networking** сгенерируйте домен, порт укажите **8080** (как в Dockerfile / Procfile).
7. Проверьте `GET https://<ваш-домен>/health` → `{"status":"ok"}`.

### Если билд падает с Railpack

- Проверьте **Root Directory** = `railway-relay`.
- Убедитесь, что в GitHub попали файлы `railway-relay/Dockerfile` и `railway-relay/requirements.txt`.
- Сделайте **Redeploy** после смены Root Directory.

## Настройка monitor.py

В `monitor.py` выставьте:

- `TELEGRAM_RELAY_URL = "https://<your-railway-domain>/notify"`
- `TELEGRAM_RELAY_TOKEN = "<RELAY_TOKEN>"`

После этого уведомления пойдут через Railway relay.
