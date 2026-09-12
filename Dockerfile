# python:3.11-slim — компактная база, все зависимости из requirements.txt
# ставятся отдельным слоем ДО копирования кода: правки в app/ не пересобирают pip install.
FROM python:3.11-slim

WORKDIR /srv/app

# --- Зависимости (кэшируемый слой) ---
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# --- Код и исходные данные ---
# data/ — заводские faq/schedule из репозитория. Приложение само копирует их
# в $DATA_DIR при первом запуске, если том пуст (config.ensure_dirs + сидинг).
COPY app/ ./app/
COPY web/ ./web/
COPY data/ ./data/

# Непривилегированный пользователь. /data создаём и отдаём ему заранее: Docker
# при первой инициализации именованного тома копирует содержимое и права
# каталога-точки монтирования из образа — так том сразу будет писать без root.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv/app /data

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

EXPOSE 8000

# Том с данными: faq/, schedule/, app.db, cache/, outbox/ — переживает пересборку образа.
VOLUME ["/data"]

# curl в slim-образе нет и специально не ставим ради размера — health-check средствами stdlib.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request as u; import sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)" || exit 1

# `python -m uvicorn` (а не голая команда uvicorn) добавляет /srv/app в sys.path —
# иначе локальный пакет app/ не найдётся при запуске из установленного entry point.
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
