"""
Общий код создания и публикации заявки.

Используется двумя точками входа:
  * мастер в личке (main.py, ConversationHandler);
  * форма мини-приложения (webserver.py).

Заявка НЕ публикуется в группу. Вместо этого:
  * сотруднику уходит личная карточка-монитор (статус обновляется на месте);
  * директору уходит карточка с кнопками «Одобрить/Отклонить»;
  * после одобрения бухгалтеру уходит карточка с кнопкой «Оплатить»
    (её отправляет send_accountant_card из обработчика кнопки).
"""
import logging
from datetime import datetime
from io import BytesIO

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import config
import db

log = logging.getLogger("zakupki-core")


# ---------- Разрешение ролей: назначение в БД переопределяет .env ----------

def accountant_ids():
    ids = db.get_users_by_role(config.ROLE_ACCOUNTANT)
    return ids if ids else ([config.ACCOUNTANT_ID] if config.ACCOUNTANT_ID else [])


def driver_ids():
    ids = db.get_users_by_role(config.ROLE_DRIVER)
    return ids if ids else ([config.DRIVER_ID] if config.DRIVER_ID else [])


def warehouse_ids():
    ids = db.get_users_by_role(config.ROLE_WAREHOUSE)
    return ids if ids else ([config.WAREHOUSE_ID] if config.WAREHOUSE_ID else [])


def is_accountant(uid: int) -> bool:
    return uid in accountant_ids()


def is_driver(uid: int) -> bool:
    return uid in driver_ids()


def is_warehouse(uid: int) -> bool:
    return uid in warehouse_ids()


def build_caption(request_no: str, sector: str, supplier: str, amount: float,
                  naryad: str, submitter_name: str) -> str:
    amount_str = f"{amount:,.0f}".replace(",", " ")
    return (
        f"🧾 Наряд {naryad}\n"
        f"Сектор: {sector}\n"
        f"Поставщик: {supplier}\n"
        f"Сумма: {amount_str}\n"
        f"От: {submitter_name}\n"
        f"№ {request_no}"
    )


def _fmt_dt(iso) -> str:
    # Полные дата и время: «11.08.2026 12:42».
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def progress_block(req) -> str:
    """Накопительная история статусов заявки — по строке на этап с датой и временем.

    Одинаковая для ВСЕХ участников (сотрудник/директор/бухгалтер/водитель/склад),
    внизу карточки. Показываются только пройденные этапы.
    """
    lines = [f"🔵 Подано — {_fmt_dt(req['submitted_at'])}"]
    if req["status"] == "отклонено":
        lines.append(f"✖ Отклонено — {_fmt_dt(req['approved_at'])}")
        return "\n".join(lines)
    if req["approved_at"]:
        lines.append(f"🟢 Одобрено — {_fmt_dt(req['approved_at'])}")
    if req["paid_at"]:
        lines.append(f"🟥 Оплачено — {_fmt_dt(req['paid_at'])}")
    if req["shipped_at"]:
        lines.append(f"🚚 В пути — {_fmt_dt(req['shipped_at'])}")
    if req["received_at"]:
        lines.append(f"📦 На складе — {_fmt_dt(req['received_at'])}")
    return "\n".join(lines)


def build_full_caption(req) -> str:
    # Шапка заявки + накопительный блок статусов. Единый вид у всех участников.
    base = build_caption(req["request_no"], req["sector"], req["supplier"],
                         req["amount"], req["naryad"], req["submitted_by_name"])
    return base + "\n\n" + progress_block(req)


# ---------- Кнопки платёжки ----------

def needpay_kb(req_id):
    """Кнопка «Запросить платёжку» (для водителя/сотрудника/директора)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Запросить платёжку", callback_data=f"act:needpay:{req_id}")
    ]])


def attach_kb(req_id):
    """Кнопка «Прикрепить платёжку» (для бухгалтера).

    Есть PAYAPP_URL → открывает окно загрузки (Web App) с ?req=<id>: бухгалтер
    выбирает файл в окне, тот цепляется к заявке (FR-1, FR-2).
    Пусто → запасной путь через callback: бот попросит прислать файл сообщением (FR-8).
    """
    if config.PAYAPP_URL:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📎 Прикрепить платёжку",
                web_app=WebAppInfo(f"{config.PAYAPP_URL}?req={req_id}"))
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📎 Прикрепить платёжку", callback_data=f"act:attach:{req_id}")
    ]])


# ---------- Клавиатуры по ролям (зависят от текущего статуса) ----------

def kb_needpay_or_none(req):
    # Сотрудник/директор: кнопка запроса платёжки, пока заявка не закрыта.
    if req["status"] in ("получено", "отклонено"):
        return None
    return needpay_kb(req["id"])


def kb_accountant(req):
    if req["status"] == "одобрено":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Оплатить", callback_data=f"act:pay:{req['id']}")]])
    return attach_kb(req["id"])  # оплачено и дальше — прикрепить платёжку


def kb_driver(req):
    rows = []
    if req["status"] == "оплачено":
        rows.append([InlineKeyboardButton("🚚 Еду за товаром", callback_data=f"act:ship:{req['id']}")])
    rows.append([InlineKeyboardButton("📄 Запросить платёжку", callback_data=f"act:needpay:{req['id']}")])
    return InlineKeyboardMarkup(rows)


def kb_warehouse(req):
    if req["status"] == "в_пути":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📦 Принял на складе", callback_data=f"act:receive:{req['id']}")]])
    return None


async def _edit_caption(bot, chat_id, msg_id, caption, reply_markup):
    if not chat_id or not msg_id:
        return
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=msg_id, caption=caption, reply_markup=reply_markup)
    except Exception:
        pass  # сообщение недоступно / без изменений — не критично


async def refresh_all_cards(bot, req):
    """Обновляет накопительный блок статусов на КАРТОЧКАХ ВСЕХ участников (на месте).

    У каждого своя клавиатура по текущему статусу; редактируются только те карточки,
    что уже существуют (по сохранённым message_id).
    """
    cap = build_full_caption(req)
    npkb = kb_needpay_or_none(req)
    await _edit_caption(bot, req["submitted_by_id"], req["notify_message_id"], cap, npkb)
    if config.DIRECTOR_ID:
        await _edit_caption(bot, config.DIRECTOR_ID, req["director_msg_id"], cap, npkb)
    if req["accountant_msg_id"]:
        await _edit_caption(bot, next(iter(accountant_ids()), None),
                            req["accountant_msg_id"], cap, kb_accountant(req))
    if req["driver_msg_id"]:
        await _edit_caption(bot, next(iter(driver_ids()), None),
                            req["driver_msg_id"], cap, kb_driver(req))
    if req["warehouse_msg_id"]:
        await _edit_caption(bot, next(iter(warehouse_ids()), None),
                            req["warehouse_msg_id"], cap, kb_warehouse(req))


async def _send_card(bot, chat_id, media, caption, is_document, reply_markup=None,
                     reply_to_message_id=None):
    """Отправляет карточку (фото или документ). Возвращает (message, file_id)."""
    extra = {}
    if reply_to_message_id:
        # Платёжка уходит ответом на карточку-монитор сотрудника (появится с цитатой
        # заявки). Если карточку удалили — шлём без привязки, а не роняем с ошибкой.
        extra = {"reply_to_message_id": reply_to_message_id,
                 "allow_sending_without_reply": True}
    if is_document:
        m = await bot.send_document(chat_id, document=media, caption=caption,
                                    reply_markup=reply_markup, **extra)
        fid = m.document.file_id if m.document else None
    else:
        m = await bot.send_photo(chat_id, photo=media, caption=caption,
                                 reply_markup=reply_markup, **extra)
        fid = m.photo[-1].file_id if m.photo else None
    return m, fid


async def send_accountant_card(bot, req):
    """Отправляет бухгалтеру(-ам) карточку одобренной заявки с кнопкой «Оплатить»."""
    ids = accountant_ids()
    if not ids or not req["photo_file_id"]:
        return
    caption = build_full_caption(req)
    kb = kb_accountant(req)
    for aid in ids:
        try:
            m, _ = await _send_card(bot, aid, req["photo_file_id"], caption,
                                    bool(req["is_document"]), reply_markup=kb)
            db.set_accountant_msg(req["id"], m.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку бухгалтеру %s: %s", aid, e)


async def notify_paid(bot, req):
    """Оплата: короткое уведомление сотруднику (без платёжки — она теперь по запросу)."""
    text = f"🧾 Наряд {req['naryad']} — оплачено, можно ехать за материалом."
    try:
        await bot.send_message(req["submitted_by_id"], text)
    except Exception as e:
        log.warning("Не удалось уведомить сотрудника об оплате: %s", e)


async def send_payment_file_to(bot, req, chat_id: int):
    """Отправляет прикреплённую платёжку одному адресату (кто запросил)."""
    if not req["payment_file_id"]:
        return False
    caption = f"🧾 Наряд {req['naryad']} — платёжка по закупке."
    try:
        await _send_card(bot, chat_id, req["payment_file_id"], caption,
                         bool(req["payment_is_document"]))
        return True
    except Exception as e:
        log.warning("Не удалось отправить платёжку %s: %s", chat_id, e)
        return False


async def deliver_payment_to_pending(bot, req):
    """Рассылает прикреплённую платёжку всем, кто её запросил, и очищает список."""
    pending = db.get_payment_pending(req["id"])
    for uid in pending:
        await send_payment_file_to(bot, req, uid)
    db.clear_payment_pending(req["id"])
    return pending


async def send_driver_card(bot, req):
    """После оплаты — водителю карточку заявки с кнопкой «🚚 Еду за товаром».

    Отдельным сообщением докидываем платёжку (если есть) — водитель показывает её
    при получении товара. Заявка (счёт) и есть первый файл с кнопкой.
    """
    ids = driver_ids()
    if not ids or not req["photo_file_id"]:
        return
    if req["sector"] == config.ADMIN_SECTOR:
        return  # административные платежи (аренда и т.п.) — без логистики
    caption = build_full_caption(req)
    kb = kb_driver(req)
    for did in ids:
        try:
            m, _ = await _send_card(bot, did, req["photo_file_id"], caption,
                                    bool(req["is_document"]), reply_markup=kb)
            db.set_driver_msg(req["id"], m.message_id)
            # Если платёжка уже приложена (бухгалтер прикрепил заранее) — сразу шлём.
            if req["payment_file_id"]:
                await send_payment_file_to(bot, req, did)
        except Exception as e:
            log.warning("Не удалось отправить карточку водителю %s: %s", did, e)


async def send_warehouse_card(bot, req):
    """После «Еду за товаром» — складу карточку с кнопкой «📦 Принял на складе»."""
    ids = warehouse_ids()
    if not ids or not req["photo_file_id"]:
        return
    caption = build_full_caption(req)
    kb = kb_warehouse(req)
    for wid in ids:
        try:
            m, _ = await _send_card(bot, wid, req["photo_file_id"], caption,
                                    bool(req["is_document"]), reply_markup=kb)
            db.set_warehouse_msg(req["id"], m.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку складу %s: %s", wid, e)


async def publish_request(bot, *, sector: str, supplier: str, amount: float,
                          naryad: str, submitter_id: int, submitter_name: str,
                          photo_file_id: str = None,
                          file_bytes: bytes = None, file_name: str = None,
                          is_document: bool = False) -> str:
    """Создаёт заявку в БД и рассылает карточки (директору с кнопками + сотруднику).

    Файл счёта: либо photo_file_id (уже в Telegram), либо file_bytes (+file_name).
    Возвращает номер заявки (напр. 'ПОК-310726-03').
    """
    prefix = config.SECTOR_PREFIX[sector]
    request_no = db.next_request_no(sector, prefix)
    now_dt = datetime.now()
    now = now_dt.isoformat(timespec="seconds")

    request_id = db.create_request(
        request_no=request_no, sector=sector, supplier=supplier, amount=amount,
        naryad=naryad, photo_file_id=photo_file_id, submitted_by_id=submitter_id,
        submitted_by_name=submitter_name, submitted_at=now,
        is_document=1 if is_document else 0,
    )

    stored = {"file_id": photo_file_id}

    def media():
        # Пока нет постоянного file_id — шлём байты; потом переиспользуем file_id.
        if stored["file_id"]:
            return stored["file_id"]
        return InputFile(BytesIO(file_bytes), filename=file_name or "invoice")

    def remember(fid):
        if fid and not stored["file_id"]:
            stored["file_id"] = fid
            db.set_photo_file_id(request_id, fid)

    # Заявка самого директора — сразу одобрена, идёт бухгалтеру (без «одобрить себя»).
    auto_approve = bool(config.DIRECTOR_ID) and submitter_id == config.DIRECTOR_ID

    if auto_approve:
        db.set_status(request_id, "одобрено", "approved_by", submitter_name, "approved_at", now)
        card_caption = build_full_caption(db.get_by_id(request_id))
        try:
            card, fid = await _send_card(bot, submitter_id, media(), card_caption,
                                         is_document, reply_markup=needpay_kb(request_id))
            remember(fid)
            db.attach_notify_message(request_id, card.message_id)
        except Exception:
            pass
        await send_accountant_card(bot, db.get_by_id(request_id))
        return request_no

    full_caption = build_full_caption(db.get_by_id(request_id))

    # 1) Директору — карточка с кнопками одобрения. Заодно получаем file_id.
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Одобрить", callback_data=f"act:approve:{request_id}"),
        InlineKeyboardButton("✖ Отклонить", callback_data=f"act:reject:{request_id}"),
    ]])
    if config.DIRECTOR_ID:
        try:
            dm, fid = await _send_card(bot, config.DIRECTOR_ID, media(), full_caption,
                                       is_document, reply_markup=kb)
            remember(fid)
            db.set_director_msg(request_id, dm.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку директору: %s", e)

    # 2) Сотруднику — личная карточка-монитор (она же подтверждение подачи).
    card_caption = full_caption
    try:
        card, fid = await _send_card(bot, submitter_id, media(), card_caption,
                                     is_document, reply_markup=needpay_kb(request_id))
        remember(fid)
        db.attach_notify_message(request_id, card.message_id)
    except Exception:
        pass  # сотрудник не нажимал /start — личную карточку отправить нельзя

    return request_no
