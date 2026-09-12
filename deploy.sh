#!/usr/bin/env bash
# Идемпотентный деплой на VPS. Можно запускать сколько угодно раз подряд.
#
# ВАЖНО ДЛЯ ВСЕХ, КТО БУДЕТ ПРАВИТЬ ЭТОТ СКРИПТ:
# На сервере уже крутятся ЧУЖИЕ контейнеры (automessage_*, btc_*, spectra_*,
# tl_world_bot) — их нельзя трогать ни при каких условиях. Поэтому здесь
# НАВСЕГДА ЗАПРЕЩЕНЫ любые глобальные/массовые docker-команды:
#   docker system prune, docker volume prune, docker network prune,
#   docker stop $(docker ps -q), docker rm $(docker ps -aq),
#   docker compose down (без -p shorthack — иначе можно попасть в чужой проект).
# Разрешены ТОЛЬКО адресные операции над проектом shorthack (см. docker-compose.yml,
# там name: shorthack — все ресурсы с префиксом shorthack_).
#
# Что делает скрипт:
#   1. читает DEPLOY_HOST / DEPLOY_SSH_KEY / DEPLOY_DIR / DEPLOY_PORT из локального .env;
#   2. заливает на сервер app/, data/, web/, requirements.txt, Dockerfile, docker-compose.yml
#      (через tar по ssh — без .git, .env, var/, __pycache__, т.к. просто их не архивируем);
#   3. на сервере: docker compose -p shorthack up -d --build;
#   4. .env на сервер НЕ копируется — проверяем, что он там уже лежит, иначе останавливаемся;
#   5. проверяет /api/health и печатает публичный адрес.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    echo "Не найден локальный .env — в нём должны быть DEPLOY_HOST, DEPLOY_SSH_KEY, DEPLOY_DIR, DEPLOY_PORT." >&2
    exit 1
fi

# Достаём только нужные ключи по имени, а не `source .env` целиком: в .env есть
# и другие значения (например GROQ_API_KEY), сорсить их в этот шелл незачем и небезопасно.
env_val() {
    local key="$1"
    grep -E "^${key}=" .env | tail -n1 | cut -d '=' -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

DEPLOY_HOST="$(env_val DEPLOY_HOST)"
DEPLOY_SSH_KEY="$(env_val DEPLOY_SSH_KEY)"
DEPLOY_DIR="$(env_val DEPLOY_DIR)"
DEPLOY_PORT="$(env_val DEPLOY_PORT)"

for var in DEPLOY_HOST DEPLOY_SSH_KEY DEPLOY_DIR DEPLOY_PORT; do
    if [ -z "${!var}" ]; then
        echo "В .env не задан $var — заполните его и запустите деплой заново." >&2
        exit 1
    fi
done

SSH="ssh -i ${DEPLOY_SSH_KEY} -o StrictHostKeyChecking=accept-new"

echo "==> Проверяю .env на сервере (он туда не копируется и должен быть заведён вручную)..."
if ! $SSH "$DEPLOY_HOST" "test -f '${DEPLOY_DIR}/.env'"; then
    echo "На сервере нет ${DEPLOY_DIR}/.env — без него контейнер не получит секреты (GROQ_API_KEY и т.д.)." >&2
    echo "Заведите его на сервере вручную (он не переезжает автодеплоем) и запустите деплой заново." >&2
    exit 1
fi

echo "==> Заливаю app/, data/, web/, requirements.txt, Dockerfile, docker-compose.yml..."
# Явно перечисляем, что кладём в архив — так .git, .env, var/, __pycache__
# просто никогда туда не попадают, без хрупких --exclude по всему дереву.
tar -czf - \
    --exclude='__pycache__' --exclude='*.pyc' \
    app data web requirements.txt Dockerfile docker-compose.yml \
    | $SSH "$DEPLOY_HOST" "mkdir -p '${DEPLOY_DIR}' && tar -xzf - -C '${DEPLOY_DIR}'"

echo "==> Собираю и поднимаю контейнер на сервере (docker compose -p shorthack)..."
# -p shorthack — на случай, если top-level `name:` в compose-файле по какой-то
# причине не подхватится; двойная страховка от попадания в чужой проект.
$SSH "$DEPLOY_HOST" "cd '${DEPLOY_DIR}' && docker compose -p shorthack up -d --build"

echo "==> Проверяю здоровье приложения..."
if $SSH "$DEPLOY_HOST" "curl -fsS http://localhost:${DEPLOY_PORT}/api/health"; then
    echo
    echo "OK: приложение отвечает."
else
    echo
    echo "Health-check не прошёл — смотрите логи: ssh -i ${DEPLOY_SSH_KEY} ${DEPLOY_HOST} 'cd ${DEPLOY_DIR} && docker compose -p shorthack logs --tail=100'" >&2
    exit 1
fi

HOST_ONLY="${DEPLOY_HOST#*@}"
echo "==> Публичный адрес: http://${HOST_ONLY}:${DEPLOY_PORT}"
