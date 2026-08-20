import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo("Europe/Chisinau")


def _int_env(name: str) -> int:
    # Пустая строка в .env (напр. GROUP_CHAT_ID=) не должна ронять запуск —
    # трактуем её как 0, а не как ошибку int("").
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else 0


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_CHAT_ID = _int_env("GROUP_CHAT_ID")
# Супер-админ. Назначает всех через /assign, ВКЛЮЧАЯ директора, и видит всё.
# Задаётся ТОЛЬКО в .env (якорь: через бота его сменить/снять нельзя — чтобы не
# потерять доступ). Обычно это владелец. Директора и роли админ раздаёт сам.
ADMIN_ID = _int_env("ADMIN_ID")
# Директор — стартовое значение из .env. Может быть переопределён: админ назначает
# роль «Директор» через /assign (см. core.director_ids — БД важнее .env).
DIRECTOR_ID = _int_env("DIRECTOR_ID")
ACCOUNTANT_ID = _int_env("ACCOUNTANT_ID")
ACCOUNTANT2_ID = _int_env("ACCOUNTANT2_ID")
BUYER_ID = _int_env("BUYER_ID")
BUYER2_ID = _int_env("BUYER2_ID")
# Логистика (общая на все секторы). Оба — личные чаты с тем же ботом.
# Пусто/0 — этап логистики выключен (цикл заканчивается на «оплачено»).
DRIVER_ID = _int_env("DRIVER_ID")        # водитель: кнопка «🚚 Еду за товаром»
WAREHOUSE_ID = _int_env("WAREHOUSE_ID")  # склад/цех: кнопка «📦 Принял на складе»

# TODO(Диветро): подставьте реальные направления/отделы компании и их префиксы
# номеров заявок. Пока — одна заглушка «Закупки» (у Диветро один закупщик).
# У Бертама было: Покраска/Лазер-Гибка/Конструкции/Цех.
SECTORS = ["Закупки"]

# Категория для платежей не по производству (аренда, коммуналка и т.п.).
# Доступна ТОЛЬКО директору, логистику (водитель/склад) не запускает.
ADMIN_SECTOR = "Административные"

SECTOR_PREFIX = {
    "Закупки": "ЗАК",
    ADMIN_SECTOR: "АДМ",
}

# Полный набор направлений для обзора и пайплайна (списки, отчёты, номера заявок).
# SECTORS — только производственные (мастер сотрудника, /assign);
# ALL_SECTORS — с «Административными» (директору/бухгалтеру в /list и /report).
ALL_SECTORS = SECTORS + [ADMIN_SECTOR]

# Роли, назначаемые через /assign (хранятся в people.role). Назначение в БД
# переопределяет соответствующий ID из .env; если роль никому не назначена —
# работает ACCOUNTANT_ID/DRIVER_ID/WAREHOUSE_ID из .env. Директор — только в .env.
ROLE_DIRECTOR = "Директор"    # назначается ТОЛЬКО админом; переопределяет DIRECTOR_ID из .env
ROLE_ACCOUNTANT = "Бухгалтер"
ROLE_ACCOUNTANT2 = "Бухгалтер 2"
ROLE_BUYER = "Закупщик"
ROLE_BUYER2 = "Закупщик 2"
ROLE_DRIVER = "Водитель"
ROLE_WAREHOUSE = "Склад"
ROLE_EMPLOYEE = "Сотрудник"
# Роли, которые может раздавать директор. «Директор» сюда НЕ входит — его назначает
# только админ (см. ADMIN_ROLES и проверки в main.assign_*).
ROLES = [ROLE_EMPLOYEE, ROLE_ACCOUNTANT, ROLE_ACCOUNTANT2, ROLE_BUYER, ROLE_BUYER2, ROLE_DRIVER, ROLE_WAREHOUSE]
# Полный набор ролей, доступных админу для назначения (директор + всё остальное).
ADMIN_ROLES = ROLES + [ROLE_DIRECTOR]

# Реакции, которые бот распознаёт как действие. ВАЖНО: Telegram принимает как
# реакции только эмодзи из своего фиксированного набора. 🟢 ✖ ✅ в него НЕ входят
# (Reaction_invalid) — поэтому используем валидные. Проверенные рабочие варианты:
# 👍 👎 🔥 🎉 💯 👌. В настройках группы: Реакции → «Все» (или явно разрешить эти).
REACTION_APPROVE = "👍"
REACTION_REJECT = "👎"
REACTION_PAID = "🎉"

DB_PATH = os.getenv("DB_PATH", "zakupki.db")

# ---------- Мини-приложение (форма заявки) ----------
# Публичный HTTPS-адрес страницы формы (form.html). Telegram открывает Web App
# только по https. Напр.: https://bot.mydomain.ru/form
# Локально (без туннеля) можно оставить пустым — тогда /new покажет старый мастер.
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Публичный HTTPS-адрес окна загрузки платёжки (pay.html) — бухгалтер прикрепляет
# платёжку к заявке через окно, а не отдельным сообщением со скрепкой.
# По умолчанию выводим из WEBAPP_URL (.../form → .../pay) — отдельно задавать не нужно.
# Пусто — кнопка «Прикрепить платёжку» работает через запасной путь (callback).
PAYAPP_URL = os.getenv("PAYAPP_URL", "") or (
    WEBAPP_URL.replace("/form", "/pay") if WEBAPP_URL else "")

BUYERAPP_URL = os.getenv("BUYERAPP_URL", "") or (
    WEBAPP_URL.replace("/form", "/buyer_form") if WEBAPP_URL else "")

BUYER_REQUEST_URL = os.getenv("BUYER_REQUEST_URL", "") or (
    WEBAPP_URL.replace("/form", "/buyer_request_form") if WEBAPP_URL else "")

# Где поднимать внутренний веб-сервер формы. На сервере он слушает локально,
# а наружу его отдаёт nginx с TLS. WEB_PORT — порт за прокси.
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = _int_env("WEB_PORT") or 8080
