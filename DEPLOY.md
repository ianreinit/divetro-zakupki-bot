# Развёртывание на Hetzner (пошагово)

Цель: бот работает постоянно как служба systemd, форма заявки доступна по HTTPS
рядом с вашим живым проектом. Живой проект мы не трогаем, кроме одного аккуратного
добавления в конфиг nginx (с проверкой перед применением).

> ⚠️ ВАЖНО ПРО КОНФЛИКТ: у одного бот-токена может опрашивать Telegram только ОДИН
> процесс. Пока бот запущен на сервере — на ноутбуке он должен быть выключен,
> иначе оба будут спорить (ошибка `Conflict: terminated by other getUpdates`).

Ниже команды для Ubuntu/Debian (типовой Hetzner). `ВАШ-ДОМЕН` замените на свой.

---

## 0. Подключиться к серверу
```
ssh root@IP-вашего-сервера
```

## 1. Установить системные пакеты
```
sudo apt update
sudo apt install -y python3-venv python3-pip nginx
```

## 2. Создать отдельного пользователя и папку
```
sudo useradd --system --create-home --home-dir /opt/zakupki-bot --shell /usr/sbin/nologin zakupki
```

## 3. Загрузить код на сервер
На НОУТБУКЕ (не на сервере), из папки проекта, отправьте архив:
```
tar --exclude venv --exclude .env --exclude '*.db' --exclude __pycache__ \
    -czf /tmp/zakupki.tgz -C "/Users/yancuxxx/Desktop/Bot.Telegram Bertam" .
scp /tmp/zakupki.tgz root@IP-вашего-сервера:/tmp/
```
На СЕРВЕРЕ распакуйте:
```
sudo tar -xzf /tmp/zakupki.tgz -C /opt/zakupki-bot
sudo chown -R zakupki:zakupki /opt/zakupki-bot
```

## 4. Виртуальное окружение и зависимости
```
cd /opt/zakupki-bot
sudo -u zakupki python3 -m venv venv
sudo -u zakupki venv/bin/pip install --upgrade pip
sudo -u zakupki venv/bin/pip install -r requirements.txt
```

## 5. Файл .env на сервере
```
sudo -u zakupki cp .env.example .env
sudo -u zakupki nano .env
```
Заполните (значения — те же, что уже проверены локально):
```
BOT_TOKEN=...
GROUP_CHAT_ID=-5568811294
DIRECTOR_ID=...
ACCOUNTANT_ID=...
WEBAPP_URL=            # пока пусто, заполним на шаге 8
WEB_HOST=127.0.0.1
WEB_PORT=8080
```

## 6. Служба systemd
```
sudo cp /opt/zakupki-bot/deploy/zakupki-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zakupki-bot
sudo systemctl status zakupki-bot     # должно быть active (running)
sudo journalctl -u zakupki-bot -f     # живой лог (Ctrl+C выйти)
```
На этом бот уже работает (мастер `/new_text`, реакции, отчёт). Форму включим ниже.

## 7. nginx для формы — ВАРИАНТ А (путь на существующем домене, рекомендуется)
Откройте конфиг вашего живого сайта (обычно в `/etc/nginx/sites-available/…`),
найдите `server { ... }`, который слушает `443` для вашего домена, и вставьте туда
блок из `deploy/nginx-location-snippet.conf` (location /zakupki/ ...).
Затем:
```
sudo nginx -t                      # ОБЯЗАТЕЛЬНО: проверка, что не сломали конфиг
sudo systemctl reload nginx        # применяем, только если проверка прошла
```

(Вариант Б — отдельный поддомен `bot.ВАШ-ДОМЕН` — см. `deploy/nginx-subdomain.conf`.)

## 8. Включить форму
```
sudo -u zakupki nano /opt/zakupki-bot/.env
# WEBAPP_URL=https://ВАШ-ДОМЕН/zakupki/form
sudo systemctl restart zakupki-bot
```
Проверка адреса в браузере: `https://ВАШ-ДОМЕН/zakupki/form` — должна открыться форма.

## 9. Проверка в Телеграме
1. Выключите бота на ноутбуке (если ещё запущен).
2. В личке боту `/new` → кнопка «Заполнить заявку» → форма → «Отправить».
3. Карточка появляется в группе, приходит личное подтверждение.

## 10. Резервная копия базы
`zakupki.db` — единственное хранилище. Простой ежедневный бэкап через cron:
```
sudo crontab -u zakupki -e
# добавить строку:
0 3 * * * cp /opt/zakupki-bot/zakupki.db /opt/zakupki-bot/backups/zakupki-$(date +\%F).db
```
(предварительно `sudo -u zakupki mkdir -p /opt/zakupki-bot/backups`)

---

## Обновление кода в будущем
Повторить шаг 3 (архив + scp + распаковать) и:
```
sudo chown -R zakupki:zakupki /opt/zakupki-bot
sudo -u zakupki /opt/zakupki-bot/venv/bin/pip install -r /opt/zakupki-bot/requirements.txt
sudo systemctl restart zakupki-bot
```

## Если что-то не так
- `sudo journalctl -u zakupki-bot -n 100 --no-pager` — последние логи бота.
- Форма не открывается → проверьте `sudo nginx -t`, что блок location добавлен в
  нужный server, и что служба слушает порт: `sudo ss -ltnp | grep 8080`.
- «Conflict … getUpdates» в логе → где-то ещё запущен тот же бот (ноутбук?).
