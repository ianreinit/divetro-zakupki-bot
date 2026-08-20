"""
Общий код создания и публикации заявки.

Поток:
  1. Сотрудник подаёт потребность (описание, дата, срочность) → publish_need
  2. Закупщик оформляет (поставщик, сумма, наряд, счёт) → process_need
  3. Директор одобряет → кнопка approve
  4. Бухгалтер оплачивает → кнопка pay
  5. Водитель забирает → кнопка ship
  6. Склад принимает → кнопка receive

Административные заявки директора обходят закупщика: publish_request.
"""
import logging
from datetime import datetime
from io import BytesIO

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import BadRequest

import config
import db

log = logging.getLogger("zakupki-core")


# ---------- Разрешение ролей: назначение в БД переопределяет .env ----------

def accountant_ids():
    ids = db.get_users_by_role(config.ROLE_ACCOUNTANT)
    ids2 = db.get_users_by_role(config.ROLE_ACCOUNTANT2)
    result = ids + ids2
    if not result:
        fallback = []
        if config.ACCOUNTANT_ID:
            fallback.append(config.ACCOUNTANT_ID)
        if config.ACCOUNTANT2_ID:
            fallback.append(config.ACCOUNTANT2_ID)
        return fallback
    return result


def buyer_ids():
    ids = db.get_users_by_role(config.ROLE_BUYER)
    ids2 = db.get_users_by_role(config.ROLE_BUYER2)
    result = ids + ids2
    if not result:
        fallback = []
        if config.BUYER_ID:
            fallback.append(config.BUYER_ID)
        if config.BUYER2_ID:
            fallback.append(config.BUYER2_ID)
        return fallback
    return result


def driver_ids():
    ids = db.get_users_by_role(config.ROLE_DRIVER)
    return ids if ids else ([config.DRIVER_ID] if config.DRIVER_ID else [])


def warehouse_ids():
    ids = db.get_users_by_role(config.ROLE_WAREHOUSE)
    return ids if ids else ([config.WAREHOUSE_ID] if config.WAREHOUSE_ID else [])


def director_ids():
    ids = db.get_users_by_role(config.ROLE_DIRECTOR)
    return ids if ids else ([config.DIRECTOR_ID] if config.DIRECTOR_ID else [])


def director_id() -> int:
    ids = director_ids()
    return ids[0] if ids else 0


def is_director(uid: int) -> bool:
    return uid in director_ids()


def is_admin(uid: int) -> bool:
    return bool(config.ADMIN_ID) and uid == config.ADMIN_ID


def is_accountant(uid: int) -> bool:
    return uid in accountant_ids()


def is_buyer(uid: int) -> bool:
    return uid in buyer_ids()


def is_driver(uid: int) -> bool:
    return uid in driver_ids()


def is_warehouse(uid: int) -> bool:
    return uid in warehouse_ids()


def employee_ids():
    return db.get_users_by_role(config.ROLE_EMPLOYEE)


def is_employee(uid: int) -> bool:
    return uid in employee_ids()


# ---------- Форматирование ----------

URGENCY_LABEL = {"обычная": "", "скоро": " ⚡", "срочно": " 🔴"}


def _fmt_dt(iso) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def _fmt_date(iso) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except Exception:
        return iso or ""


def progress_block(req) -> str:
    lines = [f"🔵 Запрос отправлен — {_fmt_dt(req['submitted_at'])}"]
    if req["status"] == "отклонено":
        at = req.get("processed_at") or req.get("approved_at")
        lines.append(f"✖ Отклонено — {_fmt_dt(at)}")
        if req.get("reject_reason"):
            lines.append(f"Причина: {req['reject_reason']}")
        return "\n".join(lines)
    if req.get("processed_at"):
        lines.append(f"📝 Оформлено — {_fmt_dt(req['processed_at'])}")
    if req.get("approved_at"):
        lines.append(f"🟢 Одобрено — {_fmt_dt(req['approved_at'])}")
    if req.get("paid_at"):
        lines.append(f"🟥 Оплачено — {_fmt_dt(req['paid_at'])}")
    if req.get("shipped_at"):
        lines.append(f"🚚 В пути — {_fmt_dt(req['shipped_at'])}")
    if req.get("received_at"):
        lines.append(f"📦 На складе — {_fmt_dt(req['received_at'])}")
    return "\n".join(lines)


def _display_no(req) -> str:
    return req.get("order_no") or req["request_no"]


def build_need_text(req) -> str:
    no = _display_no(req)
    lines = []
    if req["status"] == "потребность":
        lines.append(f"📋 Потребность {no}")
    else:
        lines.append(f"📋 Заявка {no}")
    if req.get("description"):
        lines.append(f"Что нужно: {req['description']}")
    if req.get("needed_by"):
        urg = URGENCY_LABEL.get(req.get("urgency") or "", "")
        lines.append(f"Нужно к: {_fmt_date(req['needed_by'])}{urg}")
    if req.get("supplier"):
        lines.append(f"Поставщик: {req['supplier']}")
    if req.get("amount") and req["amount"] > 0:
        lines.append(f"Сумма: {req['amount']:,.0f}".replace(",", " "))
    if req.get("naryad"):
        lines.append(f"Наряд: {req['naryad']}")
    lines.append(f"От: {req['submitted_by_name']}")
    if req.get("processed_by"):
        lines.append(f"Оформил: {req['processed_by']}")
    lines.append("")
    lines.append(progress_block(req))
    return "\n".join(lines)


def build_full_caption(req) -> str:
    no = _display_no(req)
    amount_str = f"{req['amount']:,.0f}".replace(",", " ") if req.get("amount") and req["amount"] > 0 else "—"
    lines = [f"🧾 {no}"]
    if req.get("supplier"):
        lines.append(f"Поставщик: {req['supplier']}")
    if req["amount"] and req["amount"] > 0:
        lines.append(f"Сумма: {amount_str}")
    if req.get("naryad"):
        lines.append(f"Наряд: {req['naryad']}")
    if req.get("description"):
        lines.append(f"Что нужно: {req['description']}")
    lines.append(f"От: {req['submitted_by_name']}")
    if req.get("processed_by"):
        lines.append(f"Оформил: {req['processed_by']}")
    lines.append("")
    lines.append(progress_block(req))
    return "\n".join(lines)


# ---------- Кнопки платёжки ----------

def needpay_kb(req_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Запросить платёжку", callback_data=f"act:needpay:{req_id}")
    ]])


def attach_kb(req_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📎 Прикрепить платёжку", callback_data=f"act:attach:{req_id}")
    ]])


# ---------- Клавиатуры по ролям ----------

def kb_needpay_or_none(req):
    if req["status"] in ("получено", "отклонено"):
        return None
    if req["status"] == "потребность":
        return None
    return needpay_kb(req["id"])


def kb_buyer(req):
    if req["status"] != "потребность":
        return None
    if config.BUYERAPP_URL:
        process_btn = InlineKeyboardButton(
            "📝 Оформить заявку",
            web_app=WebAppInfo(f"{config.BUYERAPP_URL}?req={req['id']}"))
    else:
        process_btn = InlineKeyboardButton(
            "📝 Оформить заявку", callback_data=f"act:process:{req['id']}")
    return InlineKeyboardMarkup([
        [process_btn],
        [InlineKeyboardButton("✖ Отклонить", callback_data=f"act:buyreject:{req['id']}")],
    ])


def kb_accountant(req):
    if req["status"] == "одобрено":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Оплатить", callback_data=f"act:pay:{req['id']}")]])
    return attach_kb(req["id"])


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


def kb_director_approve(req_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Одобрить", callback_data=f"act:approve:{req_id}"),
        InlineKeyboardButton("✖ Отклонить", callback_data=f"act:reject:{req_id}"),
    ]])


def kb_buyer_rejected(req):
    if req["status"] != "отклонено":
        return None
    if not req.get("naryad"):
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить заявку", callback_data=f"act:editreq:{req['id']}")],
        [InlineKeyboardButton("🔄 Перенаправить", callback_data=f"act:resubmit:{req['id']}")],
    ])


def kb_admin(req):
    if req["status"] in ("получено", "отклонено"):
        return None
    rows = []
    if req["status"] == "оплачено":
        rows.append([InlineKeyboardButton("🚚 Еду за товаром", callback_data=f"act:ship:{req['id']}")])
    if req["status"] == "в_пути":
        rows.append([InlineKeyboardButton("📦 Принял на складе", callback_data=f"act:receive:{req['id']}")])
    if req["status"] not in ("потребность",):
        rows.append([InlineKeyboardButton("📄 Запросить платёжку", callback_data=f"act:needpay:{req['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


# ---------- Отправка / редактирование ----------

MAX_CAPTION = 1024


def _trim_caption(text: str) -> str:
    if len(text) <= MAX_CAPTION:
        return text
    return text[:MAX_CAPTION - 1] + "…"


async def _edit_caption(bot, chat_id, msg_id, caption, reply_markup):
    if not chat_id or not msg_id:
        return
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=msg_id, caption=_trim_caption(caption),
            reply_markup=reply_markup)
    except BadRequest:
        pass
    except Exception as e:
        log.warning("edit_caption failed %s/%s: %s", chat_id, msg_id, e)


async def _edit_text(bot, chat_id, msg_id, text, reply_markup):
    if not chat_id or not msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text, reply_markup=reply_markup)
    except BadRequest:
        pass
    except Exception as e:
        log.warning("edit_text failed %s/%s: %s", chat_id, msg_id, e)


async def refresh_all_cards(bot, req):
    text = build_need_text(req)
    npkb = kb_needpay_or_none(req)
    has_need_photo = bool(req.get("need_photo_file_id"))

    # Карточка сотрудника (фото или текст)
    if has_need_photo and req.get("notify_message_id"):
        await _edit_caption(bot, req["submitted_by_id"], req["notify_message_id"], text, npkb)
    else:
        await _edit_text(bot, req["submitted_by_id"], req["notify_message_id"], text, npkb)

    # Карточки закупщиков (фото или текст)
    bids = buyer_ids()
    buyer_msg_cols = ["buyer_msg_id", "buyer2_msg_id"]
    for i, bid in enumerate(bids):
        col = buyer_msg_cols[i] if i < len(buyer_msg_cols) else None
        msg_id = req.get(col) if col else None
        if msg_id:
            if has_need_photo:
                await _edit_caption(bot, bid, msg_id, text, kb_buyer(req))
            else:
                await _edit_text(bot, bid, msg_id, text, kb_buyer(req))

    # Фото-карточки (существуют только после оформления закупщиком)
    if req.get("photo_file_id"):
        cap = build_full_caption(req)
        did = director_id()
        if did and req.get("director_msg_id"):
            await _edit_caption(bot, did, req["director_msg_id"], cap, npkb)

        acc_ids = accountant_ids()
        if req.get("accountant_msg_id") and len(acc_ids) > 0:
            await _edit_caption(bot, acc_ids[0], req["accountant_msg_id"], cap, kb_accountant(req))
        if req.get("accountant2_msg_id") and len(acc_ids) > 1:
            await _edit_caption(bot, acc_ids[1], req["accountant2_msg_id"], cap, kb_accountant(req))

        if req.get("driver_msg_id"):
            drv = driver_ids()
            if drv:
                await _edit_caption(bot, drv[0], req["driver_msg_id"], cap, kb_driver(req))
        if req.get("warehouse_msg_id"):
            wh = warehouse_ids()
            if wh:
                await _edit_caption(bot, wh[0], req["warehouse_msg_id"], cap, kb_warehouse(req))

    if req.get("admin_msg_id") and config.ADMIN_ID:
        if req.get("photo_file_id") or has_need_photo:
            cap = build_full_caption(req) if req.get("photo_file_id") else text
            await _edit_caption(bot, config.ADMIN_ID, req["admin_msg_id"], cap, kb_admin(req))
        else:
            await _edit_text(bot, config.ADMIN_ID, req["admin_msg_id"], text, kb_admin(req))


async def _send_card(bot, chat_id, media, caption, is_document, reply_markup=None,
                     reply_to_message_id=None):
    caption = _trim_caption(caption)
    extra = {}
    if reply_to_message_id:
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


# ---------- Публикация потребности (сотрудник → закупщик) ----------

async def publish_need(bot, *, sector: str, description: str, needed_by: str,
                       urgency: str, submitter_id: int, submitter_name: str,
                       order_no: str = "",
                       photo_file_id: str = None, is_document: bool = False,
                       file_bytes: bytes = None, file_name: str = None) -> str:
    prefix = config.SECTOR_PREFIX.get(sector, "REQ")
    request_no = db.next_request_no(sector, prefix)
    now = datetime.now(config.TZ).isoformat(timespec="seconds")

    request_id = db.create_need(
        request_no=request_no, sector=sector,
        submitted_by_id=submitter_id, submitted_by_name=submitter_name,
        submitted_at=now, description=description,
        needed_by=needed_by, urgency=urgency,
        need_photo_file_id=photo_file_id,
        need_is_document=1 if is_document else 0,
        order_no=order_no or None,
    )
    db.log_action(request_id, "потребность", submitter_id, submitter_name)

    req = db.get_by_id(request_id)
    text = build_need_text(req)

    media = photo_file_id
    if not media and file_bytes:
        media = InputFile(BytesIO(file_bytes), filename=file_name or "attachment")

    def _remember_fid(fid):
        nonlocal media
        if fid and not db.get_by_id(request_id)["need_photo_file_id"]:
            db.set_need_photo(request_id, fid, 1 if is_document else 0)
            media = fid

    bids = buyer_ids()
    for i, bid in enumerate(bids):
        try:
            if media:
                m, fid = await _send_card(bot, bid, media, text,
                                          is_document, reply_markup=kb_buyer(req))
                _remember_fid(fid)
            else:
                m = await bot.send_message(bid, text, reply_markup=kb_buyer(req))
            if i == 0:
                db.set_buyer_msg(request_id, m.message_id)
            else:
                db.set_buyer2_msg(request_id, m.message_id)
        except Exception as e:
            log.warning("Не удалось отправить потребность закупщику %s: %s", bid, e)

    try:
        if media:
            m, fid = await _send_card(bot, submitter_id, media, text, is_document)
            _remember_fid(fid)
        else:
            m = await bot.send_message(submitter_id, text)
        db.attach_notify_message(request_id, m.message_id)
    except Exception:
        pass

    if config.ADMIN_ID and submitter_id != config.ADMIN_ID:
        try:
            if media:
                m, fid = await _send_card(bot, config.ADMIN_ID, media, text, is_document)
                _remember_fid(fid)
            else:
                m = await bot.send_message(config.ADMIN_ID, text)
            db.set_admin_msg(request_id, m.message_id)
        except Exception:
            pass

    return request_no


# ---------- Оформление закупщиком (потребность → заявка) ----------

async def process_need(bot, request_id: int, *, supplier: str, amount: float,
                       naryad: str, photo_file_id: str, is_document: bool,
                       processed_by_id: int = 0, processed_by_name: str) -> None:
    now = datetime.now(config.TZ).isoformat(timespec="seconds")
    db.update_need_to_request(
        request_id, supplier=supplier, amount=amount, naryad=naryad,
        photo_file_id=photo_file_id, is_document=1 if is_document else 0,
        processed_by=processed_by_name, processed_at=now,
    )
    db.log_action(request_id, "оформлено", processed_by_id, processed_by_name)
    req = db.get_by_id(request_id)

    auto_approve = is_director(req["submitted_by_id"])
    if auto_approve:
        db.set_status(request_id, "одобрено", "approved_by", processed_by_name, "approved_at", now)
        req = db.get_by_id(request_id)
        await refresh_all_cards(bot, req)
        await send_accountant_card(bot, req)
        await send_admin_card(bot, req)
        return

    cap = build_full_caption(req)
    kb = kb_director_approve(request_id)
    did = director_id()
    if did:
        try:
            dm, _ = await _send_card(bot, did, photo_file_id, cap, is_document, reply_markup=kb)
            db.set_director_msg(request_id, dm.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку директору: %s", e)

    await refresh_all_cards(bot, req)
    await send_admin_card(bot, req)


# ---------- Публикация административной заявки (директор, без закупщика) ----------

async def publish_request(bot, *, sector: str, supplier: str, amount: float,
                          naryad: str, submitter_id: int, submitter_name: str,
                          photo_file_id: str = None,
                          file_bytes: bytes = None, file_name: str = None,
                          is_document: bool = False) -> str:
    prefix = config.SECTOR_PREFIX[sector]
    request_no = db.next_request_no(sector, prefix)
    now_dt = datetime.now(config.TZ)
    now = now_dt.isoformat(timespec="seconds")

    request_id = db.create_request(
        request_no=request_no, sector=sector, supplier=supplier, amount=amount,
        naryad=naryad, photo_file_id=photo_file_id, submitted_by_id=submitter_id,
        submitted_by_name=submitter_name, submitted_at=now,
        is_document=1 if is_document else 0,
    )
    db.update_need_to_request(
        request_id, supplier=supplier, amount=amount, naryad=naryad,
        photo_file_id=photo_file_id, is_document=1 if is_document else 0,
        processed_by=submitter_name, processed_at=now,
    )
    db.log_action(request_id, "адм_заявка", submitter_id, submitter_name)

    stored = {"file_id": photo_file_id}

    def media():
        if stored["file_id"]:
            return stored["file_id"]
        return InputFile(BytesIO(file_bytes), filename=file_name or "invoice")

    def remember(fid):
        if fid and not stored["file_id"]:
            stored["file_id"] = fid
            db.set_photo_file_id(request_id, fid)

    auto_approve = is_director(submitter_id)

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
        afid = await send_admin_card(bot, db.get_by_id(request_id), media())
        remember(afid)
        return request_no

    full_caption = build_full_caption(db.get_by_id(request_id))
    kb = kb_director_approve(request_id)
    did = director_id()
    if did:
        try:
            dm, fid = await _send_card(bot, did, media(), full_caption,
                                       is_document, reply_markup=kb)
            remember(fid)
            db.set_director_msg(request_id, dm.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку директору: %s", e)

    try:
        card, fid = await _send_card(bot, submitter_id, media(), full_caption,
                                     is_document, reply_markup=needpay_kb(request_id))
        remember(fid)
        db.attach_notify_message(request_id, card.message_id)
    except Exception:
        pass

    afid = await send_admin_card(bot, db.get_by_id(request_id), media())
    remember(afid)
    return request_no


# ---------- Отправка карточек по ролям ----------

async def send_accountant_card(bot, req):
    ids = accountant_ids()
    if not ids or not req["photo_file_id"]:
        return
    caption = build_full_caption(req)
    kb = kb_accountant(req)
    for i, aid in enumerate(ids):
        try:
            m, _ = await _send_card(bot, aid, req["photo_file_id"], caption,
                                    bool(req["is_document"]), reply_markup=kb)
            if i == 0:
                db.set_accountant_msg(req["id"], m.message_id)
            else:
                db.set_accountant2_msg(req["id"], m.message_id)
        except Exception as e:
            log.warning("Не удалось отправить карточку бухгалтеру %s: %s", aid, e)


async def notify_paid(bot, req):
    no = _display_no(req)
    text = f"🧾 {no} — оплачено, можно ехать за материалом."
    try:
        await bot.send_message(req["submitted_by_id"], text)
    except Exception as e:
        log.warning("Не удалось уведомить сотрудника об оплате: %s", e)


async def send_payment_file_to(bot, req, chat_id: int):
    if not req["payment_file_id"]:
        return False
    no = _display_no(req)
    caption = f"🧾 {no} — платёжка по закупке."
    try:
        await _send_card(bot, chat_id, req["payment_file_id"], caption,
                         bool(req["payment_is_document"]))
        return True
    except Exception as e:
        log.warning("Не удалось отправить платёжку %s: %s", chat_id, e)
        return False


async def deliver_payment_to_pending(bot, req):
    pending = db.get_payment_pending(req["id"])
    for uid in pending:
        await send_payment_file_to(bot, req, uid)
    db.clear_payment_pending(req["id"])
    return pending


async def send_driver_card(bot, req):
    ids = driver_ids()
    if not ids or not req["photo_file_id"]:
        return
    if req["sector"] == config.ADMIN_SECTOR:
        return
    caption = build_full_caption(req)
    kb = kb_driver(req)
    for did in ids:
        try:
            m, _ = await _send_card(bot, did, req["photo_file_id"], caption,
                                    bool(req["is_document"]), reply_markup=kb)
            db.set_driver_msg(req["id"], m.message_id)
            if req["payment_file_id"]:
                await send_payment_file_to(bot, req, did)
        except Exception as e:
            log.warning("Не удалось отправить карточку водителю %s: %s", did, e)


async def send_warehouse_card(bot, req):
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


async def notify_buyer_rejected(bot, req):
    kb = kb_buyer_rejected(req)
    if not kb:
        return
    amount_str = f"{req['amount']:,.0f}".replace(",", " ")
    reason_line = f"\nПричина: {req['reject_reason']}" if req.get("reject_reason") else ""
    no = _display_no(req)
    text = (
        f"✖ Заявка {no} отклонена директором.\n"
        f"Наряд: {req['naryad']}\n"
        f"Поставщик: {req['supplier']}\n"
        f"Сумма: {amount_str}"
        f"{reason_line}\n\n"
        "Вы можете изменить заявку или перенаправить её повторно."
    )
    for bid in buyer_ids():
        try:
            await bot.send_message(bid, text, reply_markup=kb)
        except Exception as e:
            log.warning("Не удалось уведомить закупщика об отклонении: %s", e)


async def send_admin_card(bot, req, file_id_or_input=None):
    if not config.ADMIN_ID or not req["photo_file_id"]:
        return None
    if req["submitted_by_id"] == config.ADMIN_ID:
        return None
    caption = build_full_caption(req)
    kb = kb_admin(req)
    media = file_id_or_input or req["photo_file_id"]
    try:
        m, fid = await _send_card(bot, config.ADMIN_ID, media, caption,
                                  bool(req["is_document"]), reply_markup=kb)
        db.set_admin_msg(req["id"], m.message_id)
        return fid
    except Exception as e:
        log.warning("Не удалось отправить карточку админу: %s", e)
        return None
