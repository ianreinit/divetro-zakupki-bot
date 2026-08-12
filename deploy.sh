#!/usr/bin/env bash
# Выкатить бота: закоммитить изменения, отправить в GitHub и сразу обновить сервер.
#
# Использование:  ./deploy.sh "что изменил"
#
# Сервер сам подтягивает код из GitHub (systemd-таймер каждые ~2 минуты),
# так что можно просто закоммитить и запушить из любого места — эта команда
# лишь делает деплой мгновенным (не ждать таймер). Секреты (.env, база, venv)
# на сервере не трогаются — они не в git.
set -e
SERVER="root@bertam.online"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

MSG="${1:-update}"

# Проверка синтаксиса перед отправкой (если есть локальный venv)
if [ -x venv/bin/python ]; then
  venv/bin/python -m py_compile main.py core.py webserver.py db.py config.py
fi

git add -A
git commit -m "$MSG" || echo "→ нечего коммитить (изменений нет)"
git push origin main

echo "→ Обновляю сервер (мгновенно, не жду таймер)…"
ssh -o BatchMode=yes "$SERVER" \
  'systemctl start zakupki-autopull.service && sleep 5 && echo "служба: $(systemctl is-active zakupki-bot) | код: $(cd /opt/zakupki-bot && git rev-parse --short HEAD)"'
echo "✓ Готово. Логи: ./logs.sh"
