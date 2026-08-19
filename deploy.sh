#!/usr/bin/env bash
# Выкатить Диветро-бота: коммит → push → обновить сервер.
# Использование:  ./deploy.sh "что изменил"
set -e
SERVER="root@bertam.online"
REMOTE_DIR="/opt/divetro-zakupki-bot"
SERVICE="divetro-bot"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

MSG="${1:-update}"

if [ -x venv/bin/python ]; then
  venv/bin/python -m py_compile main.py core.py webserver.py db.py config.py
fi

git add -u
git commit -m "$MSG" || echo "→ нечего коммитить (изменений нет)"
git push origin main

echo "→ Обновляю сервер…"
ssh -o BatchMode=yes "$SERVER" "cd $REMOTE_DIR && git pull origin main && $REMOTE_DIR/venv/bin/pip install -q -r requirements.txt && systemctl restart $SERVICE && sleep 2 && echo \"служба: \$(systemctl is-active $SERVICE) | код: \$(git rev-parse --short HEAD)\""
echo "✓ Готово. Логи: ./logs.sh"
