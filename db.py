import sqlite3
from contextlib import closing
from datetime import datetime

import config
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT UNIQUE NOT NULL,
    sector TEXT NOT NULL,
    supplier TEXT NOT NULL,
    amount REAL NOT NULL,
    naryad TEXT NOT NULL,
    photo_file_id TEXT,
    submitted_by_id INTEGER NOT NULL,
    submitted_by_name TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    group_chat_id INTEGER,
    group_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'отправлено',
    approved_by TEXT,
    approved_at TEXT,
    paid_by TEXT,
    paid_at TEXT,
    notify_message_id INTEGER,
    is_document INTEGER DEFAULT 0,
    director_msg_id INTEGER
);
"""

# Реестр людей: кто взаимодействовал с ботом и как назначен.
# sector = NULL и role = NULL — человек известен боту (нажал /start), но не назначен.
# Назначение взаимоисключимо: либо sector (производственный сектор), либо role
# (Бухгалтер/Водитель/Склад). Роль в БД переопределяет соответствующий ID из .env.
SCHEMA_PEOPLE = """
CREATE TABLE IF NOT EXISTS people (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    sector TEXT,
    role TEXT
);
"""


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(SCHEMA)
        conn.execute(SCHEMA_PEOPLE)
        # Миграция для старых баз: колонка роли в реестре людей.
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(people)").fetchall()]
        if "role" not in pcols:
            conn.execute("ALTER TABLE people ADD COLUMN role TEXT")
        # Миграция для старых баз: добавить колонку карточки в личке, если её нет.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
        if "notify_message_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN notify_message_id INTEGER")
        if "is_document" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN is_document INTEGER DEFAULT 0")
        if "director_msg_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN director_msg_id INTEGER")
        # Карточка у бухгалтера (чтобы дописать итог, когда оплата завершается
        # отдельным сообщением с платёжкой, а не нажатием кнопки) и сама платёжка.
        if "accountant_msg_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN accountant_msg_id INTEGER")
        if "payment_file_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN payment_file_id TEXT")
        if "payment_is_document" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN payment_is_document INTEGER DEFAULT 0")
        # Логистика: карточки у водителя и склада + кто/когда отметил этапы.
        if "driver_msg_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN driver_msg_id INTEGER")
        if "warehouse_msg_id" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN warehouse_msg_id INTEGER")
        if "shipped_by" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN shipped_by TEXT")
        if "shipped_at" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN shipped_at TEXT")
        if "received_by" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN received_by TEXT")
        if "received_at" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN received_at TEXT")
        # Кто запросил платёжку и ждёт её (CSV из user_id). Когда платёжка
        # прикрепляется — рассылается всем из списка, список очищается.
        if "payment_pending_for" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN payment_pending_for TEXT")
        conn.commit()


# ---------- Реестр людей ----------

def upsert_person(user_id: int, name: str):
    # Запоминаем/обновляем имя, сектор не трогаем (его ставит директор).
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT INTO people (user_id, name) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET name = excluded.name""",
            (user_id, name),
        )
        conn.commit()


def set_person_sector(user_id: int, sector, name: str = None):
    # Назначить сектор. Роль при этом снимается (назначение взаимоисключимо).
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT INTO people (user_id, name, sector, role) VALUES (?, ?, ?, NULL)
               ON CONFLICT(user_id) DO UPDATE SET sector = excluded.sector, role = NULL""",
            (user_id, name, sector),
        )
        conn.commit()


def set_person_role(user_id: int, role, name: str = None):
    # Назначить роль (Бухгалтер/Водитель/Склад). Один держатель на роль: снимаем её
    # с остальных. Сектор при этом снимается (назначение взаимоисключимо).
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE people SET role = NULL WHERE role = ? AND user_id != ?",
                     (role, user_id))
        conn.execute(
            """INSERT INTO people (user_id, name, sector, role) VALUES (?, ?, NULL, ?)
               ON CONFLICT(user_id) DO UPDATE SET role = excluded.role, sector = NULL""",
            (user_id, name, role),
        )
        conn.commit()


def clear_person(user_id: int, name: str = None):
    # Снять любое назначение (и сектор, и роль).
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT INTO people (user_id, name, sector, role) VALUES (?, ?, NULL, NULL)
               ON CONFLICT(user_id) DO UPDATE SET sector = NULL, role = NULL""",
            (user_id, name),
        )
        conn.commit()


def get_users_by_role(role: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT user_id FROM people WHERE role = ?", (role,))
        return [r[0] for r in cur.fetchall()]


def get_person(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM people WHERE user_id = ?", (user_id,))
        return cur.fetchone()


def list_people():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM people ORDER BY sector IS NULL DESC, name")
        return cur.fetchall()


def next_request_no(sector: str, prefix: str) -> str:
    today = datetime.now(config.TZ).strftime("%d%m%y")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE sector = ? AND request_no LIKE ?",
            (sector, f"{prefix}-{today}-%"),
        )
        count = cur.fetchone()[0] + 1
    return f"{prefix}-{today}-{count:02d}"


def create_request(**kwargs) -> int:
    kwargs.setdefault("photo_file_id", None)
    kwargs.setdefault("is_document", 0)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """INSERT INTO requests
               (request_no, sector, supplier, amount, naryad, photo_file_id,
                submitted_by_id, submitted_by_name, submitted_at, status, is_document)
               VALUES (:request_no, :sector, :supplier, :amount, :naryad, :photo_file_id,
                       :submitted_by_id, :submitted_by_name, :submitted_at, 'отправлено', :is_document)""",
            kwargs,
        )
        conn.commit()
        return cur.lastrowid


def set_photo_file_id(request_id: int, file_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET photo_file_id = ? WHERE id = ?", (file_id, request_id))
        conn.commit()


def get_by_id(request_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        return cur.fetchone()


def attach_group_message(request_id: int, chat_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET group_chat_id = ?, group_message_id = ? WHERE id = ?",
            (chat_id, message_id, request_id),
        )
        conn.commit()


def set_director_msg(request_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET director_msg_id = ? WHERE id = ?", (message_id, request_id))
        conn.commit()


def set_accountant_msg(request_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET accountant_msg_id = ? WHERE id = ?", (message_id, request_id))
        conn.commit()


def set_driver_msg(request_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET driver_msg_id = ? WHERE id = ?", (message_id, request_id))
        conn.commit()


def set_warehouse_msg(request_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET warehouse_msg_id = ? WHERE id = ?", (message_id, request_id))
        conn.commit()


def get_payment_pending(request_id: int):
    # Список user_id, кто запросил платёжку и ждёт её.
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT payment_pending_for FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row or not row[0]:
        return []
    return [int(x) for x in row[0].split(",") if x]


def add_payment_pending(request_id: int, user_id: int):
    # Добавляет запросившего в список ожидающих (без дублей).
    ids = get_payment_pending(request_id)
    if user_id in ids:
        return
    ids.append(user_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE requests SET payment_pending_for = ? WHERE id = ?",
                     (",".join(str(i) for i in ids), request_id))
        conn.commit()


def clear_payment_pending(request_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE requests SET payment_pending_for = NULL WHERE id = ?",
                     (request_id,))
        conn.commit()


def set_payment_file(request_id: int, file_id: str, is_document: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET payment_file_id = ?, payment_is_document = ? WHERE id = ?",
            (file_id, is_document, request_id))
        conn.commit()


def attach_notify_message(request_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET notify_message_id = ? WHERE id = ?",
            (message_id, request_id),
        )
        conn.commit()


def list_by_submitter(user_id: int, limit: int = 15):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM requests WHERE submitted_by_id = ? ORDER BY submitted_at DESC LIMIT ?",
            (user_id, limit),
        )
        return cur.fetchall()


def list_by_sector(sector: str, limit: int = 20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM requests WHERE sector = ? ORDER BY submitted_at DESC LIMIT ?",
            (sector, limit),
        )
        return cur.fetchall()


def list_all_requests(limit: int = 20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM requests ORDER BY submitted_at DESC LIMIT ?", (limit,))
        return cur.fetchall()


def find_by_message(chat_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM requests WHERE group_chat_id = ? AND group_message_id = ?",
            (chat_id, message_id),
        )
        return cur.fetchone()


def set_status(request_id: int, status: str, actor_field: str, actor_name: str,
               time_field: str, when: str):
    # actor_field/time_field приходят из кода бота (не от пользователя), не из ввода —
    # поэтому подстановка имени колонки здесь безопасна.
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            f"UPDATE requests SET status = ?, {actor_field} = ?, {time_field} = ? WHERE id = ?",
            (status, actor_name, when, request_id),
        )
        conn.commit()


def report(sector, period_start: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        if sector and sector != "Все":
            cur = conn.execute(
                "SELECT * FROM requests WHERE sector = ? AND submitted_at >= ? ORDER BY submitted_at DESC",
                (sector, period_start),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM requests WHERE submitted_at >= ? ORDER BY submitted_at DESC",
                (period_start,),
            )
        return cur.fetchall()
