#!/usr/bin/env bash
# Локальный запуск для разработки — то, на чём реально идёт работа все шесть часов.
# Использование:
#   ./run.sh          — venv, зависимости, uvicorn app.api:app --reload
#   ./run.sh test     — прогон pytest (аргументы после test пробрасываются в pytest)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    echo "Не найден .env — скопируйте .env.example в .env и заполните переменные (GROQ_API_KEY и т.д.)." >&2
    exit 1
fi

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Создаю виртуальное окружение $VENV_DIR..."
    python -m venv "$VENV_DIR"
fi

# Windows/Git Bash кладёт скрипты активации в Scripts/, POSIX — в bin/.
if [ -f "$VENV_DIR/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/Scripts/activate"
else
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

pip install --quiet --disable-pip-version-check -r requirements.txt

if [ "${1:-}" = "test" ]; then
    shift
    exec python -m pytest "$@"
fi

# Хост/порт не парсим из .env руками (рискованно из-за спецсимволов в значениях) —
# берём их из app/config.py, единственной точки чтения окружения в проекте.
APP_HOST="$(python -c 'import app.config as c; print(c.APP_HOST)')"
APP_PORT="$(python -c 'import app.config as c; print(c.APP_PORT)')"

exec python -m uvicorn app.api:app --reload --host "$APP_HOST" --port "$APP_PORT"
