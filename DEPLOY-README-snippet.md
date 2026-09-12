## Запуск

**Локально (разработка):**
```bash
cp .env.example .env   # заполнить GROQ_API_KEY и остальное
./run.sh                # venv, зависимости, uvicorn --reload на http://localhost:8000
./run.sh test            # прогон тестов (pytest)
```

**В контейнере:**
```bash
cp .env.example .env    # заполнить перед первым запуском
docker compose up -d --build   # http://localhost:8000/docs
```
Данные (faq/schedule/app.db/cache/outbox) живут в томе `shorthack_data` и переживают пересборку образа.

**На сервере (демо):** `./deploy.sh` — идемпотентно заливает код и поднимает `docker compose -p shorthack`
на VPS без затрагивания чужих контейнеров. Публичный адрес и `.env` на сервере — см. `deploy.sh`.
