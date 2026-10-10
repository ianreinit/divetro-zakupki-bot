import sqlite3
from contextlib import closing
from datetime import datetime

import config
from config import DB_PATH


def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

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

SCHEMA_AUDIT = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    actor_name TEXT NOT NULL,
    detail TEXT,
    ts TEXT NOT NULL
);
"""


CURRENT_SCHEMA_VERSION = 10


def _add_col(conn, table, column, col_type):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _migrate_to_v1(conn):
    _add_col(conn, "people", "role", "TEXT")
    for col, ctype in [
        ("notify_message_id", "INTEGER"),
        ("is_document", "INTEGER DEFAULT 0"),
        ("director_msg_id", "INTEGER"),
        ("accountant_msg_id", "INTEGER"),
        ("payment_file_id", "TEXT"),
        ("payment_is_document", "INTEGER DEFAULT 0"),
        ("driver_msg_id", "INTEGER"),
        ("warehouse_msg_id", "INTEGER"),
        ("shipped_by", "TEXT"),
        ("shipped_at", "TEXT"),
        ("received_by", "TEXT"),
        ("received_at", "TEXT"),
        ("payment_pending_for", "TEXT"),
        ("admin_msg_id", "INTEGER"),
        ("description", "TEXT"),
        ("needed_by", "TEXT"),
        ("urgency", "TEXT"),
        ("buyer_msg_id", "INTEGER"),
        ("processed_by", "TEXT"),
        ("processed_at", "TEXT"),
        ("accountant2_msg_id", "INTEGER"),
        ("need_photo_file_id", "TEXT"),
        ("need_is_document", "INTEGER DEFAULT 0"),
        ("reject_reason", "TEXT"),
    ]:
        _add_col(conn, "requests", col, ctype)
    conn.execute("UPDATE requests SET status = 'оформлено' WHERE status = 'отправлено'")


def _migrate_to_v2(conn):
    _add_col(conn, "requests", "order_no", "TEXT")


def _migrate_to_v3(conn):
    _add_col(conn, "requests", "buyer2_msg_id", "INTEGER")


def _migrate_to_v4(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS payment_notices (
        request_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        requester TEXT NOT NULL,
        expanded INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, message_id)
    )""")


def _migrate_to_v5(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS accountant_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        accountant_id INTEGER NOT NULL,
        accountant_name TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL,
        director_id INTEGER NOT NULL,
        submission_id TEXT NOT NULL,
        director_message_id INTEGER,
        UNIQUE (accountant_id, submission_id)
    )""")


def _migrate_to_v6(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS request_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        author_role TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        submission_id TEXT NOT NULL,
        UNIQUE (request_id, author_id, submission_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_request ON request_comments(request_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS comment_deliveries (
        comment_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL,
        message_id INTEGER,
        PRIMARY KEY (comment_id, recipient_id)
    )""")


def _migrate_to_v7(conn):
    _add_col(conn, "requests", "expense_category", "TEXT")


def _migrate_to_v8(conn):
    _add_col(conn, "accountant_notifications", "accountant_message_id", "INTEGER")


def _migrate_to_v9(conn):
    _add_col(conn, "requests", "analytics_excluded", "INTEGER NOT NULL DEFAULT 0")


def _migrate_to_v10(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS cabinet_cards (
        request_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        has_media INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (request_id, user_id)
    )""")


_MIGRATIONS = [
    (1, _migrate_to_v1),
    (2, _migrate_to_v2),
    (3, _migrate_to_v3),
    (4, _migrate_to_v4),
    (5, _migrate_to_v5),
    (6, _migrate_to_v6),
    (7, _migrate_to_v7),
    (8, _migrate_to_v8),
    (9, _migrate_to_v9),
    (10, _migrate_to_v10),
]


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(SCHEMA)
        conn.execute(SCHEMA_PEOPLE)
        conn.execute(SCHEMA_AUDIT)

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target, migrate_fn in _MIGRATIONS:
            if version < target:
                migrate_fn(conn)
        if version < CURRENT_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_sector ON requests(sector)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_submitted_by ON requests(submitted_by_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_submitted_at ON requests(submitted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id)")
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
        conn.row_factory = _dict_factory
        cur = conn.execute("SELECT * FROM people WHERE user_id = ?", (user_id,))
        return cur.fetchone()


def list_people():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
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
                       :submitted_by_id, :submitted_by_name, :submitted_at, 'оформлено', :is_document)""",
            kwargs,
        )
        conn.commit()
        return cur.lastrowid


def create_need(**kwargs) -> int:
    kwargs.setdefault("need_photo_file_id", None)
    kwargs.setdefault("need_is_document", 0)
    kwargs.setdefault("order_no", None)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """INSERT INTO requests
               (request_no, sector, supplier, amount, naryad,
                submitted_by_id, submitted_by_name, submitted_at, status,
                description, needed_by, urgency, need_photo_file_id, need_is_document,
                order_no)
               VALUES (:request_no, :sector, '', 0, '',
                       :submitted_by_id, :submitted_by_name, :submitted_at, 'потребность',
                       :description, :needed_by, :urgency, :need_photo_file_id, :need_is_document,
                       :order_no)""",
            kwargs,
        )
        conn.commit()
        return cur.lastrowid


def update_need_to_request(request_id: int, *, supplier: str, amount: float,
                           naryad: str, photo_file_id: str, is_document: int,
                           processed_by: str, processed_at: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """UPDATE requests
               SET supplier = ?, amount = ?, naryad = ?, photo_file_id = ?,
                   is_document = ?, processed_by = ?, processed_at = ?,
                   status = 'оформлено'
               WHERE id = ?""",
            (supplier, amount, naryad, photo_file_id, is_document,
             processed_by, processed_at, request_id),
        )
        conn.commit()


def set_photo_file_id(request_id: int, file_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET photo_file_id = ? WHERE id = ?", (file_id, request_id))
        conn.commit()


def get_by_id(request_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        return cur.fetchone()


def attach_group_message(request_id: int, chat_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET group_chat_id = ?, group_message_id = ? WHERE id = ?",
            (chat_id, message_id, request_id),
        )
        conn.commit()


_MSG_COLUMNS = frozenset({
    "director_msg_id", "accountant_msg_id", "accountant2_msg_id",
    "driver_msg_id", "warehouse_msg_id", "admin_msg_id",
    "buyer_msg_id", "buyer2_msg_id",
})


def set_msg(request_id: int, column: str, message_id: int):
    if column not in _MSG_COLUMNS:
        raise ValueError(f"Invalid msg column: {column}")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            f"UPDATE requests SET {column} = ? WHERE id = ?", (message_id, request_id))
        conn.commit()


def set_director_msg(request_id: int, message_id: int):
    set_msg(request_id, "director_msg_id", message_id)


def set_accountant_msg(request_id: int, message_id: int):
    set_msg(request_id, "accountant_msg_id", message_id)


def set_accountant2_msg(request_id: int, message_id: int):
    set_msg(request_id, "accountant2_msg_id", message_id)


def set_driver_msg(request_id: int, message_id: int):
    set_msg(request_id, "driver_msg_id", message_id)


def set_warehouse_msg(request_id: int, message_id: int):
    set_msg(request_id, "warehouse_msg_id", message_id)


def set_admin_msg(request_id: int, message_id: int):
    set_msg(request_id, "admin_msg_id", message_id)


def set_buyer_msg(request_id: int, message_id: int):
    set_msg(request_id, "buyer_msg_id", message_id)


def set_buyer2_msg(request_id: int, message_id: int):
    set_msg(request_id, "buyer2_msg_id", message_id)


def set_need_photo(request_id: int, file_id: str, is_document: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET need_photo_file_id = ?, need_is_document = ? WHERE id = ?",
            (file_id, is_document, request_id))
        conn.commit()


def comment_summary(request_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("""SELECT *,
            (SELECT COUNT(*) FROM request_comments WHERE request_id = ?) AS total
            FROM request_comments WHERE request_id = ? ORDER BY id DESC LIMIT 1""",
            (request_id, request_id)).fetchone()
        return dict(row) if row else None


def comment_author_ids(request_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT author_id FROM request_comments WHERE request_id = ?", (request_id,))]


def list_comments(request_id, before=None, limit=50):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT * FROM request_comments
            WHERE request_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?""",
            (request_id, before, before, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]


def save_comment(request_id, author_id, author_name, author_role, body, submission_id, recipients):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""SELECT * FROM request_comments
                WHERE request_id = ? AND author_id = ? AND submission_id = ?""",
                (request_id, author_id, submission_id)).fetchone()
            if existing:
                return dict(existing)
            cursor = conn.execute("""INSERT INTO request_comments
                (request_id, author_id, author_name, author_role, body, created_at, submission_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (request_id, author_id, author_name, author_role,
                body, datetime.now(config.TZ).isoformat(timespec="seconds"), submission_id))
            comment_id = cursor.lastrowid
            conn.executemany("INSERT INTO comment_deliveries (comment_id, recipient_id) VALUES (?, ?)",
                             [(comment_id, uid) for uid in set(recipients) if uid != author_id])
            return dict(conn.execute("SELECT * FROM request_comments WHERE id = ?", (comment_id,)).fetchone())


def comment_deliveries(comment_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM comment_deliveries WHERE comment_id = ?", (comment_id,))]


def mark_comment_delivered(comment_id, recipient_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE comment_deliveries SET message_id = ? WHERE comment_id = ? AND recipient_id = ?",
                     (message_id, comment_id, recipient_id))
        conn.commit()


def create_accountant_notification(accountant_id, accountant_name, description,
                                   director_id, submission_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""INSERT OR IGNORE INTO accountant_notifications
            (accountant_id, accountant_name, description, created_at, director_id, submission_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (accountant_id, accountant_name, description,
             datetime.now(config.TZ).isoformat(timespec="seconds"), director_id, submission_id))
        conn.commit()
        return dict(conn.execute("""SELECT * FROM accountant_notifications
            WHERE accountant_id = ? AND submission_id = ?""",
            (accountant_id, submission_id)).fetchone())


def mark_accountant_notification_sent(notification_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE accountant_notifications SET director_message_id = ? WHERE id = ?",
                     (message_id, notification_id))
        conn.commit()


def save_payment_notice(request_id, chat_id, message_id, requester):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO payment_notices (request_id, chat_id, message_id, requester) VALUES (?, ?, ?, ?)",
            (request_id, chat_id, message_id, requester))
        conn.commit()


def get_payment_notices(request_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM payment_notices WHERE request_id = ?", (request_id,))]


def expand_payment_notice(request_id, chat_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE payment_notices SET expanded = 1 WHERE request_id = ? AND chat_id = ? AND message_id = ?",
            (request_id, chat_id, message_id))
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
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM requests WHERE submitted_by_id = ? ORDER BY submitted_at DESC LIMIT ?",
            (user_id, limit),
        )
        return cur.fetchall()


def list_by_sector(sector: str, limit: int = 20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM requests WHERE sector = ? ORDER BY submitted_at DESC LIMIT ?",
            (sector, limit),
        )
        return cur.fetchall()


def list_all_requests(limit: int = 20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM requests ORDER BY submitted_at DESC LIMIT ?", (limit,))
        return cur.fetchall()


def find_by_message(chat_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM requests WHERE group_chat_id = ? AND group_message_id = ?",
            (chat_id, message_id),
        )
        return cur.fetchone()


_STATUS_FIELDS = frozenset({
    "approved_by", "approved_at", "paid_by", "paid_at",
    "shipped_by", "shipped_at", "received_by", "received_at",
    "processed_by", "processed_at",
})


def categorize_payment(request_id, category, actor_id, actor_name, when, *, edit=False):
    if category not in config.EXPENSE_CATEGORIES:
        raise ValueError("Invalid expense category")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            req = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if req is None:
                return "not_found"
            if edit:
                if not req["paid_at"] or req["status"] not in ("оплачено", "в_пути", "получено"):
                    return "not_paid"
                if req["expense_category"] == category:
                    return "unchanged"
                conn.execute("UPDATE requests SET expense_category = ? WHERE id = ?", (category, request_id))
                action = "категория"
                old = config.EXPENSE_CATEGORIES.get(req["expense_category"], "Не распределено")
                detail = f"{old} → {config.EXPENSE_CATEGORIES[category]}"
            else:
                if req["status"] != "одобрено" or req["paid_at"]:
                    return "already_paid" if req["paid_at"] else "not_approved"
                conn.execute("""UPDATE requests SET status = 'оплачено', paid_by = ?, paid_at = ?,
                    expense_category = ? WHERE id = ?""", (actor_name, when, category, request_id))
                action = "оплачено"
                detail = f"Категория: {config.EXPENSE_CATEGORIES[category]}"
            conn.execute("INSERT INTO audit_log (request_id, action, actor_id, actor_name, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
                         (request_id, action, actor_id, actor_name, detail, when))
    return "updated"


def set_status(request_id: int, status: str, actor_field: str, actor_name: str,
               time_field: str, when: str):
    if actor_field not in _STATUS_FIELDS or time_field not in _STATUS_FIELDS:
        raise ValueError(f"Invalid field name: {actor_field}/{time_field}")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            f"UPDATE requests SET status = ?, {actor_field} = ?, {time_field} = ? WHERE id = ?",
            (status, actor_name, when, request_id),
        )
        conn.commit()


def reset_rejected(request_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """UPDATE requests SET status = 'оформлено', approved_by = NULL, approved_at = NULL,
               director_msg_id = NULL, reject_reason = NULL
               WHERE id = ? AND status = 'отклонено'""",
            (request_id,))
        conn.commit()


def set_reject_reason(request_id: int, reason: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE requests SET reject_reason = ? WHERE id = ?",
            (reason, request_id))
        conn.commit()


def log_action(request_id: int, action: str, actor_id: int, actor_name: str,
               detail: str = None):
    ts = datetime.now(config.TZ).isoformat(timespec="seconds")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO audit_log (request_id, action, actor_id, actor_name, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, action, actor_id, actor_name, detail, ts))
        conn.commit()


def get_audit_log(request_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM audit_log WHERE request_id = ? ORDER BY ts", (request_id,))
        return cur.fetchall()


def recent_audit(limit: int = 20):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        cur = conn.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,))
        return cur.fetchall()


def report(sector, period_start: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
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


def mark_accountant_copy_sent(notification_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE accountant_notifications SET accountant_message_id = ? WHERE id = ?",
                     (message_id, notification_id))
        conn.commit()


def accountant_notification_history(uid, offset=0):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        return conn.execute("""SELECT id, description, created_at, director_message_id,
            accountant_message_id FROM accountant_notifications WHERE accountant_id = ?
            ORDER BY id DESC LIMIT 41 OFFSET ?""", (uid, offset)).fetchall()


def set_analytics_excluded(request_id, excluded, actor_id, actor_name):
    """Change reporting scope and its audit entry in one transaction."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT analytics_excluded FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return "not_found"
        if bool(row[0]) == excluded:
            return "unchanged"
        conn.execute("UPDATE requests SET analytics_excluded = ? WHERE id = ?", (int(excluded), request_id))
        conn.execute("""INSERT INTO audit_log (request_id,action,actor_id,actor_name,detail,ts)
            VALUES (?,?,?,?,?,?)""", (request_id,
            "исключено_из_аналитики" if excluded else "восстановлено_в_аналитике", actor_id, actor_name,
            "Тестовая заявка" if excluded else "Возвращена в расчёты",
            datetime.now(config.TZ).isoformat(timespec="seconds")))
        conn.commit()
        return "updated"


def get_cabinet_card(request_id, user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = _dict_factory
        return conn.execute(
            "SELECT message_id, has_media FROM cabinet_cards WHERE request_id = ? AND user_id = ?",
            (request_id, user_id),
        ).fetchone()


def set_cabinet_card(request_id, user_id, message_id, has_media):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""INSERT INTO cabinet_cards (request_id,user_id,message_id,has_media)
            VALUES (?,?,?,?) ON CONFLICT(request_id,user_id) DO UPDATE SET
            message_id=excluded.message_id, has_media=excluded.has_media""",
            (request_id, user_id, message_id, int(has_media)))
        conn.commit()


def clear_cabinet_card(request_id, user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM cabinet_cards WHERE request_id = ? AND user_id = ?",
                     (request_id, user_id))
        conn.commit()


def bind_accountant_card(request_id, slot, chat_id, message_id):
    column = ("accountant_msg_id", "accountant2_msg_id")[slot]
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(f"UPDATE requests SET {column} = ? WHERE id = ?", (message_id, request_id))
        conn.execute("DELETE FROM payment_notices WHERE request_id = ? AND chat_id = ? AND message_id = ?",
                     (request_id, chat_id, message_id))
        conn.commit()


def remove_payment_notice(request_id, chat_id, message_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM payment_notices WHERE request_id = ? AND chat_id = ? AND message_id = ?",
                     (request_id, chat_id, message_id))
        conn.commit()
