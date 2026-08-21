import asyncio
import logging
import os
import signal
import sqlite3
from contextlib import closing
from datetime import datetime, time as dtime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    BotCommand, MenuButtonWebApp, MenuButtonCommands,
)
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import config
import core
import db
import webserver

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zakupki-bot")

# Состояния мастера сотрудника (потребность)
ORDER_NO, DESCRIPTION, NEEDED_BY, URGENCY, NEED_PHOTO = range(5)
# Состояния мастера сотрудника (админ-заявка директора — полный набор полей)
ADM_SUPPLIER, ADM_AMOUNT, ADM_NARYAD, ADM_PHOTO = range(5, 9)
# Состояния мастера закупщика (оформление потребности в заявку)
B_SUPPLIER, B_AMOUNT, B_NARYAD, B_PHOTO = range(4)
# Состояния мастера редактирования отклонённой заявки (закупщик)
E_SUPPLIER, E_AMOUNT, E_NARYAD, E_PHOTO = range(9, 13)
# Состояние ожидания причины отклонения (директор)
REJECT_REASON = 13
# Состояние ожидания файла платёжки (бухгалтер)
PAYMENT_FILE = 14


# ---------- /start ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existed = db.get_person(user.id) is not None
    db.upsert_person(user.id, user.full_name)
    person = db.get_person(user.id)
    await apply_menu(context.bot, user.id)

    role = person["role"] if person else None
    if may_submit(user.id):
        sector_line = f"Ваш сектор: {person['sector']}\n" if (person and person["sector"]) else ""
        role_line = f"Ваша роль: {role}\n" if (role and not person.get("sector")) else ""
        kb = None
        if config.WEBAPP_URL and not core.is_buyer(user.id):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "📋 Подать потребность", web_app=WebAppInfo(config.WEBAPP_URL))]])
        new_hint = "потребность или заявка" if core.is_buyer(user.id) else "подать потребность"
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я бот закупок.\n{sector_line}{role_line}\n"
            f"📋 /new — {new_hint}\n"
            "📋 /list — заявки\n"
            "📋 /my — мои заявки",
            reply_markup=kb,
        )
    elif role:
        await update.message.reply_text(
            f"Привет, {user.first_name}! Вы в системе как «{role}».\n\n"
            "Задачи будут приходить карточками — нажимайте кнопки прямо на них.\n\n"
            f"Ваш ID: {user.id}"
        )
    else:
        await update.message.reply_text(
            f"Привет, {user.first_name}! Вы зарегистрированы в боте закупок.\n\n"
            "Осталось, чтобы директор закрепил вас за сектором — после этого "
            "сможете подавать потребности. Обычно это быстро.\n\n"
            f"Ваш ID: {user.id}"
        )
        did = core.director_id()
        if not existed and did and not is_privileged(user.id):
            try:
                await context.bot.send_message(
                    did,
                    f"🆕 {user.full_name} запустил(а) бота и ждёт закрепления за сектором.\n"
                    f"Закрепите командой /assign",
                )
            except Exception:
                pass


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: {u.id}\nИмя: {u.full_name}")


# ---------- Мастер подачи потребности (сотрудник) ----------

async def new_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.upsert_person(update.effective_user.id, update.effective_user.full_name)
    if not may_submit(update.effective_user.id):
        await update.message.reply_text(NOT_ALLOWED_MSG)
        return ConversationHandler.END

    if core.is_director(update.effective_user.id):
        keyboard = [
            [InlineKeyboardButton("📋 Потребность (закупка)", callback_data="wiz:need")],
            [InlineKeyboardButton("📝 Административный платёж", callback_data="wiz:adm")],
        ]
        await update.message.reply_text("Что подаёте?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ORDER_NO

    if core.is_buyer(update.effective_user.id):
        buttons = [
            [InlineKeyboardButton("📋 Потребность", callback_data="wiz:need")],
        ]
        if config.BUYER_REQUEST_URL:
            buttons.append([InlineKeyboardButton(
                "📝 Новая заявка", web_app=WebAppInfo(config.BUYER_REQUEST_URL))])
        else:
            buttons.append([InlineKeyboardButton(
                "📝 Новая заявка", callback_data="wiz:buyreq")])
        await update.message.reply_text("Что подаёте?", reply_markup=InlineKeyboardMarkup(buttons))
        return ORDER_NO

    context.user_data["sector"] = config.SECTORS[0]
    await update.message.reply_text("Введите сток или номер заказа:")
    return ORDER_NO


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.upsert_person(update.effective_user.id, update.effective_user.full_name)
    if not may_submit(update.effective_user.id):
        await update.message.reply_text(NOT_ALLOWED_MSG)
        return ConversationHandler.END

    if core.is_buyer(update.effective_user.id):
        buttons = []
        if config.WEBAPP_URL:
            buttons.append([InlineKeyboardButton(
                "📋 Потребность", web_app=WebAppInfo(config.WEBAPP_URL))])
        else:
            buttons.append([InlineKeyboardButton(
                "📋 Потребность", callback_data="wiz:need")])
        if config.BUYER_REQUEST_URL:
            buttons.append([InlineKeyboardButton(
                "📝 Новая заявка", web_app=WebAppInfo(config.BUYER_REQUEST_URL))])
        else:
            buttons.append([InlineKeyboardButton(
                "📝 Новая заявка", callback_data="wiz:buyreq")])
        await update.message.reply_text(
            "Что подаёте?", reply_markup=InlineKeyboardMarkup(buttons))
        return ORDER_NO

    if core.is_director(update.effective_user.id):
        buttons = []
        if config.WEBAPP_URL:
            buttons.append([InlineKeyboardButton(
                "📋 Потребность (закупка)", web_app=WebAppInfo(config.WEBAPP_URL))])
        else:
            buttons.append([InlineKeyboardButton(
                "📋 Потребность (закупка)", callback_data="wiz:need")])
        buttons.append([InlineKeyboardButton(
            "📝 Административный платёж", callback_data="wiz:adm")])
        await update.message.reply_text(
            "Что подаёте?", reply_markup=InlineKeyboardMarkup(buttons))
        return ORDER_NO

    if config.WEBAPP_URL:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Подать потребность", web_app=WebAppInfo(config.WEBAPP_URL))
        ]])
        await update.message.reply_text(
            "Нажмите кнопку, чтобы заполнить потребность:", reply_markup=kb)
        return ConversationHandler.END
    return await new_wizard(update, context)


async def wiz_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "adm":
        if not core.is_director(query.from_user.id):
            await query.edit_message_text("Эта категория доступна только директору.")
            return ConversationHandler.END
        context.user_data["sector"] = config.ADMIN_SECTOR
        await query.edit_message_text("Административный платёж\n\nПоставщик?")
        return ADM_SUPPLIER

    if choice == "buyreq":
        if not core.is_buyer(query.from_user.id):
            await query.edit_message_text("Эта функция доступна только закупщику.")
            return ConversationHandler.END
        context.user_data["sector"] = config.SECTORS[0]
        await query.edit_message_text("📝 Новая заявка\n\nПоставщик?")
        return ADM_SUPPLIER

    context.user_data["sector"] = config.SECTORS[0]
    await query.edit_message_text("Введите сток или номер заказа:")
    return ORDER_NO


# --- Упрощённый путь: потребность ---

MAX_DESCRIPTION = 500
MAX_SUPPLIER = 200
MAX_NARYAD = 100
MAX_REJECT_REASON = 300


MAX_ORDER_NO = 100


async def order_no_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Укажите сток или номер заказа.")
        return ORDER_NO
    if len(text) > MAX_ORDER_NO:
        await update.message.reply_text(f"Слишком длинный номер (макс. {MAX_ORDER_NO} символов).")
        return ORDER_NO
    context.user_data["order_no"] = text
    await update.message.reply_text("Что нужно? (опишите)")
    return DESCRIPTION


async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_DESCRIPTION:
        await update.message.reply_text(f"Слишком длинное описание (макс. {MAX_DESCRIPTION} символов).")
        return DESCRIPTION
    context.user_data["description"] = text
    await update.message.reply_text(
        "К какой дате нужно? (ДД.ММ, например 25.08)")
    return NEEDED_BY


async def needed_by_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    # Разбираем ДД.ММ или ДД.ММ.ГГГГ
    try:
        parts = raw.split(".")
        day = int(parts[0])
        month = int(parts[1])
        year = int(parts[2]) if len(parts) > 2 else datetime.now(config.TZ).year
        if year < 100:
            year += 2000
        dt = datetime(year, month, day)
        context.user_data["needed_by"] = dt.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        await update.message.reply_text("Не понял дату. Введите в формате ДД.ММ, например 25.08")
        return NEEDED_BY

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Обычная", callback_data="urgency:обычная"),
        InlineKeyboardButton("⚡ Скоро", callback_data="urgency:скоро"),
        InlineKeyboardButton("🔴 Срочно", callback_data="urgency:срочно"),
    ]])
    await update.message.reply_text("Срочность:", reply_markup=kb)
    return URGENCY


async def urgency_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["urgency"] = query.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Пропустить", callback_data="skipphoto")
    ]])
    await query.edit_message_text(
        "Прикрепите фото или файл (необязательно) — или нажмите «Пропустить».",
        reply_markup=kb)
    return NEED_PHOTO


async def need_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        is_doc = False
    elif update.message.document:
        file_id = update.message.document.file_id
        is_doc = (update.message.document.mime_type or "") != "" and \
            not (update.message.document.mime_type or "").startswith("image/")
    else:
        await update.message.reply_text("Это не похоже на фото или файл. Прикрепите или нажмите «Пропустить».")
        return NEED_PHOTO

    data = context.user_data
    await core.publish_need(
        context.bot,
        sector=data["sector"],
        description=data["description"],
        needed_by=data["needed_by"],
        urgency=data["urgency"],
        submitter_id=update.effective_user.id,
        submitter_name=update.effective_user.full_name,
        order_no=data.get("order_no", ""),
        photo_file_id=file_id,
        is_document=is_doc,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def need_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data
    await core.publish_need(
        context.bot,
        sector=data["sector"],
        description=data["description"],
        needed_by=data["needed_by"],
        urgency=data["urgency"],
        submitter_id=query.from_user.id,
        submitter_name=query.from_user.full_name,
        order_no=data.get("order_no", ""),
    )
    await query.edit_message_text("Потребность отправлена закупщику.")
    context.user_data.clear()
    return ConversationHandler.END


# --- Админ-путь (директор, полная заявка) ---

async def adm_supplier_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_SUPPLIER:
        await update.message.reply_text(f"Слишком длинное название (макс. {MAX_SUPPLIER} символов).")
        return ADM_SUPPLIER
    context.user_data["supplier"] = text
    await update.message.reply_text("Сумма?")
    return ADM_AMOUNT


async def adm_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Не понял сумму, введите числом, например 42500")
        return ADM_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text("Номер наряда / проекта?")
    return ADM_NARYAD


async def adm_naryad_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_NARYAD:
        await update.message.reply_text(f"Слишком длинный наряд (макс. {MAX_NARYAD} символов).")
        return ADM_NARYAD
    context.user_data["naryad"] = text
    await update.message.reply_text(
        "Прикрепите фото или PDF счёта — без него заявку отправить нельзя."
    )
    return ADM_PHOTO


async def adm_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        is_document = False
    elif update.message.document:
        file_id = update.message.document.file_id
        is_document = (update.message.document.mime_type or "") != "" and \
            not (update.message.document.mime_type or "").startswith("image/")
    else:
        await update.message.reply_text("Это не похоже на фото или файл. Прикрепите счёт.")
        return ADM_PHOTO

    data = context.user_data
    await core.publish_request(
        context.bot,
        sector=data["sector"],
        supplier=data["supplier"],
        amount=data["amount"],
        naryad=data["naryad"],
        submitter_id=update.effective_user.id,
        submitter_name=update.effective_user.full_name,
        photo_file_id=file_id,
        is_document=is_document,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ---------- Мастер закупщика (оформление потребности → заявка) ----------

async def buyer_start_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)

    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ConversationHandler.END
    if not core.is_buyer(query.from_user.id):
        await query.answer("Оформить может только закупщик.", show_alert=True)
        return ConversationHandler.END
    if req["status"] != "потребность":
        await query.answer("Потребность уже обработана.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["buyer_req_id"] = req_id
    context.user_data["buyer_data"] = {}

    desc = req.get("description") or "—"
    no = core._display_no(req)
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass
    await context.bot.send_message(
        query.from_user.id,
        f"📝 Оформление потребности {no}\n"
        f"Что нужно: {desc}\n\n"
        f"Введите поставщика:")
    return B_SUPPLIER


async def buyer_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_SUPPLIER:
        await update.message.reply_text(f"Слишком длинное название (макс. {MAX_SUPPLIER} символов).")
        return B_SUPPLIER
    context.user_data["buyer_data"]["supplier"] = text
    await update.message.reply_text("Сумма?")
    return B_AMOUNT


async def buyer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Не понял сумму, введите числом, например 42500")
        return B_AMOUNT
    context.user_data["buyer_data"]["amount"] = amount
    await update.message.reply_text("Номер наряда / проекта?")
    return B_NARYAD


async def buyer_naryad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return B_NARYAD
    text = update.message.text.strip()
    if len(text) > MAX_NARYAD:
        await update.message.reply_text(f"Слишком длинный наряд (макс. {MAX_NARYAD} символов).")
        return B_NARYAD
    context.user_data["buyer_data"]["naryad"] = text
    await update.message.reply_text(
        "Прикрепите фото или PDF счёта.")
    return B_PHOTO


async def buyer_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        is_document = False
    elif update.message.document:
        file_id = update.message.document.file_id
        is_document = (update.message.document.mime_type or "") != "" and \
            not (update.message.document.mime_type or "").startswith("image/")
    else:
        await update.message.reply_text("Это не похоже на фото или файл. Прикрепите счёт.")
        return B_PHOTO

    req_id = context.user_data["buyer_req_id"]
    data = context.user_data["buyer_data"]

    await core.process_need(
        context.bot,
        req_id,
        supplier=data["supplier"],
        amount=data["amount"],
        naryad=data["naryad"],
        photo_file_id=file_id,
        is_document=is_document,
        processed_by_id=update.effective_user.id,
        processed_by_name=update.effective_user.full_name,
    )

    req = db.get_by_id(req_id)
    await update.message.reply_text(
        f"Заявка {core._display_no(req)} оформлена и отправлена на одобрение директору.")
    context.user_data.pop("buyer_req_id", None)
    context.user_data.pop("buyer_data", None)
    return ConversationHandler.END


async def buyer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("buyer_req_id", None)
    context.user_data.pop("buyer_data", None)
    await update.message.reply_text("Оформление отменено.")
    return ConversationHandler.END


# ---------- Мастер редактирования отклонённой заявки (закупщик) ----------

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)

    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ConversationHandler.END
    if not core.is_buyer(query.from_user.id):
        await query.answer("Изменить может только закупщик.", show_alert=True)
        return ConversationHandler.END
    if req["status"] != "отклонено":
        await query.answer("Заявку можно изменить только после отклонения.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["edit_req_id"] = req_id
    context.user_data["edit_data"] = {}

    amount_str = f"{req['amount']:,.0f}".replace(",", " ")
    no = core._display_no(req)
    await query.edit_message_text(
        f"Редактирование заявки {no}\n"
        f"Текущий поставщик: {req['supplier']}\n"
        f"Текущая сумма: {amount_str}\n"
        f"Текущий наряд: {req['naryad']}\n\n"
        f"Введите поставщика:")
    return E_SUPPLIER


async def edit_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_SUPPLIER:
        await update.message.reply_text(f"Слишком длинное название (макс. {MAX_SUPPLIER} символов).")
        return E_SUPPLIER
    context.user_data["edit_data"]["supplier"] = text
    await update.message.reply_text("Сумма?")
    return E_AMOUNT


async def edit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Не понял сумму, введите числом, например 42500")
        return E_AMOUNT
    context.user_data["edit_data"]["amount"] = amount
    await update.message.reply_text("Номер наряда / проекта?")
    return E_NARYAD


async def edit_naryad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > MAX_NARYAD:
        await update.message.reply_text(f"Слишком длинный наряд (макс. {MAX_NARYAD} символов).")
        return E_NARYAD
    context.user_data["edit_data"]["naryad"] = text
    await update.message.reply_text("Прикрепите фото или PDF счёта.")
    return E_PHOTO


async def edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        is_document = False
    elif update.message.document:
        file_id = update.message.document.file_id
        is_document = (update.message.document.mime_type or "") != "" and \
            not (update.message.document.mime_type or "").startswith("image/")
    else:
        await update.message.reply_text("Это не похоже на фото или файл. Прикрепите счёт.")
        return E_PHOTO

    req_id = context.user_data["edit_req_id"]
    data = context.user_data["edit_data"]
    now = datetime.now(config.TZ).isoformat(timespec="seconds")

    db.reset_rejected(req_id)
    db.update_need_to_request(
        req_id,
        supplier=data["supplier"],
        amount=data["amount"],
        naryad=data["naryad"],
        photo_file_id=file_id,
        is_document=1 if is_document else 0,
        processed_by=update.effective_user.full_name,
        processed_at=now,
    )
    req = db.get_by_id(req_id)

    cap = core.build_full_caption(req)
    kb = core.kb_director_approve(req_id)
    did = core.director_id()
    if did:
        try:
            dm, _ = await core._send_card(context.bot, did, file_id, cap,
                                          is_document, reply_markup=kb)
            db.set_director_msg(req_id, dm.message_id)
        except Exception as e:
            log.warning("Не удалось отправить изменённую карточку директору: %s", e)

    await core.refresh_all_cards(context.bot, req)
    await update.message.reply_text(
        f"Заявка {core._display_no(req)} изменена и отправлена директору на повторное рассмотрение.")
    context.user_data.pop("edit_req_id", None)
    context.user_data.pop("edit_data", None)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit_req_id", None)
    context.user_data.pop("edit_data", None)
    await update.message.reply_text("Редактирование отменено.")
    return ConversationHandler.END


# ---------- Кнопки: одобрение / оплата / логистика / платёжка ----------

def _requester_label(uid: int, req) -> str:
    if core.is_driver(uid):
        return "Водитель"
    if core.is_director(uid):
        return "Директор"
    p = db.get_person(uid)
    name = (p["name"] if p else None) or str(uid)
    return f"{name} · {req['sector']}"


async def _act_buyreject(query, context, req, req_id, uid, now):
    if not core.is_buyer(uid):
        await query.answer("Отклонить может только закупщик.", show_alert=True)
        return
    if req["status"] != "потребность":
        await query.answer("Потребность уже обработана.", show_alert=True)
        return
    await query.answer()
    db.set_status(req_id, "отклонено", "processed_by", query.from_user.full_name,
                  "processed_at", now)
    db.log_action(req_id, "отклонено_закупщиком", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    await notify(context, req["submitted_by_id"],
                 f"✖ Потребность {core._display_no(req)} — отклонена закупщиком.")


async def _act_approve(query, context, req, req_id, uid, now):
    if not core.is_director(uid):
        await query.answer("Подтверждать может только директор.", show_alert=True)
        return
    if req["status"] != "оформлено":
        await query.answer("Заявка уже обработана.", show_alert=True)
        return
    await query.answer()
    db.set_status(req_id, "одобрено", "approved_by", query.from_user.full_name, "approved_at", now)
    db.log_action(req_id, "одобрено", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    await core.send_accountant_card(context.bot, req)


async def reject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)

    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ConversationHandler.END
    if not core.is_director(query.from_user.id):
        await query.answer("Подтверждать может только директор.", show_alert=True)
        return ConversationHandler.END
    if req["status"] != "оформлено":
        await query.answer("Заявка уже обработана.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["reject_req_id"] = req_id
    no = core._display_no(req)
    prompt = f"✖ Отклонение заявки ({no})\n\nУкажите причину отклонения:"
    try:
        await query.edit_message_caption(caption=prompt)
    except Exception:
        await query.edit_message_text(text=prompt)
    return REJECT_REASON


async def reject_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req_id = context.user_data.pop("reject_req_id", None)
    if not req_id:
        await update.message.reply_text("Нет активного отклонения.")
        return ConversationHandler.END

    reason = update.message.text.strip()
    if len(reason) > MAX_REJECT_REASON:
        await update.message.reply_text(f"Слишком длинная причина (макс. {MAX_REJECT_REASON} символов).")
        context.user_data["reject_req_id"] = req_id
        return REJECT_REASON
    now = datetime.now(config.TZ).isoformat(timespec="seconds")

    db.set_status(req_id, "отклонено", "approved_by", update.effective_user.full_name, "approved_at", now)
    db.set_reject_reason(req_id, reason)
    db.log_action(req_id, "отклонено", update.effective_user.id, update.effective_user.full_name, reason)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    no = core._display_no(req)
    await notify(context, req["submitted_by_id"],
                 f"✖ {no} — заявка отклонена.\nПричина: {reason}")
    await core.notify_buyer_rejected(context.bot, req)
    await update.message.reply_text(f"Заявка {no} отклонена.")
    return ConversationHandler.END


async def reject_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reject_req_id", None)
    await update.message.reply_text("Отклонение отменено.")
    return ConversationHandler.END


async def _act_resubmit(query, context, req, req_id, uid, now):
    if not core.is_buyer(uid):
        await query.answer("Перенаправить может только закупщик.", show_alert=True)
        return
    if req["status"] != "отклонено":
        await query.answer("Заявка не в статусе «отклонено».", show_alert=True)
        return
    await query.answer()
    db.reset_rejected(req_id)
    db.log_action(req_id, "перенаправлено", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    cap = core.build_full_caption(req)
    kb = core.kb_director_approve(req_id)
    did = core.director_id()
    if did and req.get("photo_file_id"):
        try:
            dm, _ = await core._send_card(context.bot, did, req["photo_file_id"], cap,
                                          bool(req["is_document"]), reply_markup=kb)
            db.set_director_msg(req_id, dm.message_id)
        except Exception as e:
            log.warning("Не удалось перенаправить карточку директору: %s", e)
    await core.refresh_all_cards(context.bot, req)
    try:
        await query.edit_message_text(
            f"🔄 Заявка {core._display_no(req)} перенаправлена директору на повторное рассмотрение.")
    except Exception:
        pass


async def _act_pay(query, context, req, req_id, uid, now):
    if not core.is_accountant(uid):
        await query.answer("Оплатить может только бухгалтер.", show_alert=True)
        return
    if req["status"] != "одобрено":
        await query.answer("Заявка ещё не одобрена или уже оплачена.", show_alert=True)
        return
    await query.answer("Оплачено")
    db.set_status(req_id, "оплачено", "paid_by", query.from_user.full_name, "paid_at", now)
    db.log_action(req_id, "оплачено", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    await core.notify_paid(context.bot, req)
    if req["sector"] != config.ADMIN_SECTOR:
        await core.send_driver_card(context.bot, req)
    no = core._display_no(req)
    for aid in core.accountant_ids():
        try:
            await context.bot.send_message(
                aid,
                f"📎 По {no} можно прикрепить платёжку.",
                reply_markup=core.attach_kb(req_id))
        except Exception as e:
            log.warning("Не удалось отправить кнопку платёжки бухгалтеру %s: %s", aid, e)


async def _act_needpay(query, context, req, req_id, uid, now):
    allowed = (uid == req["submitted_by_id"] or core.is_driver(uid)
               or core.is_director(uid) or core.is_admin(uid))
    if not allowed:
        await query.answer("Недоступно.", show_alert=True)
        return
    if req["payment_file_id"]:
        await core.send_payment_file_to(context.bot, req, uid)
        await query.answer("📄 Платёжка отправлена вам.")
        return
    already = db.get_payment_pending(req_id)
    if uid in already:
        await query.answer("⏳ Уже запрошено — ждём бухгалтера.", show_alert=True)
        return
    db.add_payment_pending(req_id, uid)
    await query.answer("✅ Запрос отправлен бухгалтеру.", show_alert=True)
    if already:
        return
    label = _requester_label(uid, req)
    for aid in core.accountant_ids():
        try:
            await context.bot.send_message(
                aid,
                f"📄 По {core._display_no(req)} нужна платёжка.\nЗапросил: {label}.",
                reply_markup=core.attach_kb(req_id))
        except Exception as e:
            log.warning("Не уведомить бухгалтера %s о запросе платёжки: %s", aid, e)


async def attach_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)

    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ConversationHandler.END
    if not core.is_accountant(query.from_user.id):
        await query.answer("Только бухгалтер.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["attach_req_id"] = req_id
    no = core._display_no(req)
    supplier = req.get("supplier") or "—"
    amount_str = f"{req['amount']:,.0f}".replace(",", " ") if req.get("amount") and req["amount"] > 0 else "—"
    prompt = await context.bot.send_message(
        query.from_user.id,
        f"📎 Прикрепить платёжку\n"
        f"Заявка: {no}\n"
        f"Поставщик: {supplier}\n"
        f"Сумма: {amount_str}\n\n"
        f"Пришлите фото или PDF платёжки.\n"
        f"/cancel — отмена")
    context.user_data["payment_prompt_msg_id"] = prompt.message_id
    return PAYMENT_FILE


async def attach_file_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req_id = context.user_data.pop("attach_req_id", None)
    if not req_id:
        await update.message.reply_text("Нет активного запроса на платёжку.")
        return ConversationHandler.END

    if update.message.photo:
        file_id, is_document = update.message.photo[-1].file_id, 0
    elif update.message.document:
        mime = update.message.document.mime_type or ""
        file_id = update.message.document.file_id
        is_document = 0 if mime.startswith("image/") else 1
    else:
        await update.message.reply_text("Пришлите фото или PDF платёжки.")
        context.user_data["attach_req_id"] = req_id
        return PAYMENT_FILE

    req = db.get_by_id(req_id)
    if req is None:
        await update.message.reply_text("Заявка не найдена.")
        context.user_data.pop("payment_prompt_msg_id", None)
        return ConversationHandler.END

    db.set_payment_file(req_id, file_id, is_document)
    db.log_action(req_id, "платёжка", update.effective_user.id, update.effective_user.full_name)
    req = db.get_by_id(req_id)
    sent_to = await core.deliver_payment_to_pending(context.bot, req)

    who = f"\nОтправлена запросившим ({len(sent_to)})" if sent_to else "\nСохранена (запросов пока нет)"
    no = core._display_no(req)
    supplier = req.get("supplier") or "—"
    amount_str = f"{req['amount']:,.0f}".replace(",", " ") if req.get("amount") and req["amount"] > 0 else "—"
    prompt_id = context.user_data.pop("payment_prompt_msg_id", None)
    text = (
        f"✅ Платёжка прикреплена\n"
        f"Заявка: {no}\n"
        f"Поставщик: {supplier}\n"
        f"Сумма: {amount_str}"
        f"{who}"
    )
    if prompt_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_user.id, message_id=prompt_id, text=text)
        except Exception:
            await update.message.reply_text(text)
    else:
        await update.message.reply_text(text)
    return ConversationHandler.END


async def attach_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("attach_req_id", None)
    context.user_data.pop("payment_prompt_msg_id", None)
    await update.message.reply_text("Прикрепление платёжки отменено.")
    return ConversationHandler.END


async def _act_ship(query, context, req, req_id, uid, now):
    if not core.is_driver(uid) and not core.is_admin(uid):
        await query.answer("Отметить может только водитель.", show_alert=True)
        return
    if req["sector"] == config.ADMIN_SECTOR:
        await query.answer("У административной заявки нет этапа логистики.", show_alert=True)
        return
    if req["status"] != "оплачено":
        await query.answer("Заявка ещё не оплачена или уже в работе.", show_alert=True)
        return
    await query.answer()
    db.set_status(req_id, "в_пути", "shipped_by", query.from_user.full_name, "shipped_at", now)
    db.log_action(req_id, "в_пути", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    await core.send_warehouse_card(context.bot, req)


async def _act_receive(query, context, req, req_id, uid, now):
    if not core.is_warehouse(uid) and not core.is_admin(uid):
        await query.answer("Отметить может только склад.", show_alert=True)
        return
    if req["sector"] == config.ADMIN_SECTOR:
        await query.answer("У административной заявки нет этапа логистики.", show_alert=True)
        return
    if req["status"] != "в_пути":
        await query.answer("Товар ещё не в пути или уже принят.", show_alert=True)
        return
    await query.answer()
    db.set_status(req_id, "получено", "received_by", query.from_user.full_name, "received_at", now)
    db.log_action(req_id, "получено", uid, query.from_user.full_name)
    req = db.get_by_id(req_id)
    await core.refresh_all_cards(context.bot, req)
    await notify(context, req["submitted_by_id"],
                 f"📦 {core._display_no(req)} — товар принят на складе.")


_ACTION_HANDLERS = {
    "buyreject": _act_buyreject,
    "approve": _act_approve,
    "resubmit": _act_resubmit,
    "pay": _act_pay,
    "needpay": _act_needpay,
    "ship": _act_ship,
    "receive": _act_receive,
}


async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)
    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    handler = _ACTION_HANDLERS.get(action)
    if not handler:
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    uid = query.from_user.id
    now = datetime.now(config.TZ).isoformat(timespec="seconds")
    await handler(query, context, req, req_id, uid, now)



async def notify(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        log.warning("Не удалось отправить личное уведомление %s: %s", user_id, e)


# ---------- Отчёт ----------

async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_privileged(uid):
        sectors = [s for s in config.ALL_SECTORS
                   if s != config.ADMIN_SECTOR or can_see_admin_sector(uid)]
    else:
        sectors = list(config.SECTORS)
    buttons = [InlineKeyboardButton(s, callback_data=f"report:{s}") for s in sectors]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("Все направления", callback_data="report:Все")])
    await update.message.reply_text(
        "📊 Отчёт по закупкам — выберите направление",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


STATUS_MARK = {"потребность": "📋", "оформлено": "📝", "одобрено": "🟢", "оплачено": "🟥",
               "в_пути": "🚚", "получено": "📦", "отклонено": "✖"}


async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sector = query.data.split(":", 1)[1]
    uid = query.from_user.id
    if sector == config.ADMIN_SECTOR and not can_see_admin_sector(uid):
        await query.edit_message_text("Раздел доступен только директору, бухгалтеру или админу.")
        return

    month_start = datetime.now(config.TZ).replace(day=1, hour=0, minute=0, second=0).isoformat(timespec="seconds")
    rows = db.report(sector, month_start)
    if not can_see_admin_sector(uid):
        rows = [r for r in rows if r["sector"] != config.ADMIN_SECTOR]

    total = sum(r["amount"] for r in rows)
    pending = sum(1 for r in rows if r["status"] in ("потребность", "оформлено", "одобрено"))

    lines = [
        f"{sector} · {datetime.now(config.TZ):%m.%Y}", "",
        f"Заявок: {len(rows)}  ·  Сумма: {total:,.0f}  ·  В ожидании: {pending}".replace(",", " "),
        "",
    ]
    for r in rows[:10]:
        amount_str = f"{r['amount']:,.0f}".replace(",", " ") if r["amount"] > 0 else "—"
        mark = STATUS_MARK.get(r["status"], "•")
        supplier = r["supplier"] or "—"
        label = r.get("order_no") or r.get("naryad") or (r.get("description") or "—")[:20]
        lines.append(f"{mark} {label} · {supplier} · {amount_str}")

    if not rows:
        lines.append("Заявок за этот месяц пока нет.")

    await query.edit_message_text("\n".join(lines))


# ---------- Аудит-лог ----------

ACTION_LABEL = {
    "потребность": "📋 Подана потребность",
    "оформлено": "📝 Оформлена заявка",
    "одобрено": "🟢 Одобрено",
    "отклонено": "✖ Отклонено",
    "отклонено_закупщиком": "✖ Отклонено закупщиком",
    "оплачено": "🟥 Оплачено",
    "в_пути": "🚚 В пути",
    "получено": "📦 Получено",
    "перенаправлено": "🔄 Перенаправлено",
    "адм_заявка": "📝 Адм. заявка",
    "платёжка": "📄 Платёжка прикреплена",
}


async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_privileged(uid):
        await update.message.reply_text("Команда доступна только директору, бухгалтеру или закупщику.")
        return

    args = context.args
    if args:
        req = db.get_by_id(int(args[0])) if args[0].isdigit() else None
        if req is None:
            await update.message.reply_text("Заявка не найдена. Используйте: /audit <ID заявки>")
            return
        rows = db.get_audit_log(req["id"])
        if not rows:
            await update.message.reply_text(f"По заявке {core._display_no(req)} действий пока нет.")
            return
        lines = [f"📜 История заявки {core._display_no(req)}", ""]
        for r in rows:
            label = ACTION_LABEL.get(r["action"], r["action"])
            ts = r["ts"][5:16].replace("T", " ")
            detail = f" — {r['detail']}" if r["detail"] else ""
            lines.append(f"{label}\n  {r['actor_name']} · {ts}{detail}")
        await update.message.reply_text("\n".join(lines))
    else:
        rows = db.recent_audit(15)
        if not rows:
            await update.message.reply_text("Действий пока нет.")
            return
        lines = ["📜 Последние действия", ""]
        for r in rows:
            label = ACTION_LABEL.get(r["action"], r["action"])
            ts = r["ts"][5:16].replace("T", " ")
            lines.append(f"{label} · #{r['request_id']} · {r['actor_name']} · {ts}")
        await update.message.reply_text("\n".join(lines))


# ---------- Мои заявки ----------

STATUS_SHORT = {
    "потребность": "у закупщика",
    "оформлено": "ждёт одобрения",
    "одобрено": "ждёт оплаты",
    "оплачено": "оплачено",
    "в_пути": "в пути",
    "получено": "на складе",
    "отклонено": "отклонено",
}


def is_privileged(user_id: int) -> bool:
    return (core.is_admin(user_id) or core.is_director(user_id)
            or core.is_accountant(user_id) or core.is_buyer(user_id))


def can_see_admin_sector(user_id: int) -> bool:
    return core.is_admin(user_id) or core.is_director(user_id) or core.is_accountant(user_id)


NOT_ALLOWED_MSG = (
    "Вы пока не закреплены за сектором, поэтому не можете подавать потребности.\n"
    "Обратитесь к директору — он добавит вас командой."
)


def may_submit(user_id: int) -> bool:
    if core.is_admin(user_id) or core.is_director(user_id) or core.is_accountant(user_id) or core.is_buyer(user_id):
        return True
    if core.is_employee(user_id) or core.is_driver(user_id) or core.is_warehouse(user_id):
        return True
    p = db.get_person(user_id)
    return bool(p is not None and p["sector"])


async def apply_menu(bot, user_id: int):
    try:
        if config.WEBAPP_URL and may_submit(user_id):
            await bot.set_chat_menu_button(
                chat_id=user_id,
                menu_button=MenuButtonWebApp(text="Потребность", web_app=WebAppInfo(config.WEBAPP_URL)))
        else:
            await bot.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonCommands())
    except Exception:
        pass


def format_requests(rows, title: str, show_sector: bool = False) -> str:
    lines = [title, ""]
    if not rows:
        lines.append("Заявок пока нет.")
        return "\n".join(lines)
    for r in rows:
        amount_str = f"{r['amount']:,.0f}".replace(",", " ") if r["amount"] > 0 else "—"
        mark = STATUS_MARK.get(r["status"], "•")
        short = STATUS_SHORT.get(r["status"], r["status"])
        sec = f"[{r['sector']}] " if show_sector else ""
        label = r.get("order_no") or r.get("naryad") or (r.get("description") or "—")[:25]
        lines.append(f"{mark} {sec}{label} · {amount_str} — {short}")
    return "\n".join(lines)


async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_by_submitter(update.effective_user.id, limit=15)
    await update.message.reply_text(format_requests(rows, "📋 Ваши заявки"))


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_privileged(uid):
        visible = [s for s in config.ALL_SECTORS
                   if s != config.ADMIN_SECTOR or can_see_admin_sector(uid)]
        buttons = [InlineKeyboardButton(s, callback_data=f"lst:{config.ALL_SECTORS.index(s)}")
                   for s in visible]
        keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        keyboard.append([InlineKeyboardButton("Все секторы", callback_data="lst:all")])
        await update.message.reply_text(
            "📋 Заявки — выберите сектор:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    person = db.get_person(uid)
    if person is not None and person["sector"]:
        rows = db.list_by_sector(person["sector"])
        await update.message.reply_text(
            format_requests(rows, f"📋 Заявки сектора «{person['sector']}»"))
    else:
        rows = db.list_by_submitter(uid, limit=15)
        await update.message.reply_text(
            format_requests(rows, "📋 Ваши заявки")
            + "\n\nВы не закреплены за сектором — попросите директора, "
              "чтобы видеть все заявки своего сектора.")


async def list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_privileged(uid):
        return
    key = query.data.split(":", 1)[1]
    if key == "all":
        rows = db.list_all_requests(limit=25)
        if not can_see_admin_sector(uid):
            rows = [r for r in rows if r["sector"] != config.ADMIN_SECTOR]
        text = format_requests(rows, "📋 Все заявки", show_sector=True)
    else:
        sector = config.ALL_SECTORS[int(key)]
        if sector == config.ADMIN_SECTOR and not can_see_admin_sector(uid):
            await query.edit_message_text("Раздел доступен только директору, бухгалтеру или админу.")
            return
        rows = db.list_by_sector(sector, limit=25)
        text = format_requests(rows, f"📋 Заявки сектора «{sector}»")
    await query.edit_message_text(text)


# ---------- Назначение сотрудников ----------

def _can_assign(user_id: int) -> bool:
    return core.is_director(user_id) or core.is_admin(user_id)


def _assign_roles_for(actor_id: int):
    return config.ADMIN_ROLES if core.is_admin(actor_id) else config.ROLES


async def assign_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _can_assign(update.effective_user.id):
        await update.message.reply_text("Команда доступна только директору или админу.")
        return
    people = db.list_people()
    if not people:
        await update.message.reply_text(
            "Пока некого закреплять — сотрудники ещё не запускали бота (/start).")
        return
    buttons = []
    for p in people:
        assigned = p["sector"] or p["role"]
        tag = f" · {assigned}" if assigned else " · не назначен"
        label = f"{p['name'] or p['user_id']}{tag}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"asg:{p['user_id']}")])
    await update.message.reply_text(
        "Кого назначить? (сектор или роль)", reply_markup=InlineKeyboardMarkup(buttons))


ROLE_LABEL = {
    config.ROLE_DIRECTOR: "👑 Директор",
    config.ROLE_EMPLOYEE: "👷 Сотрудник",
    config.ROLE_ACCOUNTANT: "💰 Бухгалтер",
    config.ROLE_ACCOUNTANT2: "💰 Бухгалтер 2",
    config.ROLE_BUYER: "🛒 Закупщик",
    config.ROLE_BUYER2: "🛒 Закупщик 2",
    config.ROLE_DRIVER: "🚚 Водитель",
    config.ROLE_WAREHOUSE: "📦 Склад",
}


async def assign_pick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    actor = query.from_user.id
    if not _can_assign(actor):
        return
    uid = int(query.data.split(":", 1)[1])
    p = db.get_person(uid)
    name = (p["name"] if p else None) or str(uid)
    roles = _assign_roles_for(actor)
    sec_btns = [InlineKeyboardButton(s, callback_data=f"asgset:{uid}:{i}")
                for i, s in enumerate(config.SECTORS)]
    role_btns = [InlineKeyboardButton(ROLE_LABEL.get(r, r), callback_data=f"asgset:{uid}:{len(config.SECTORS)+i}")
                 for i, r in enumerate(roles)]
    keyboard = [sec_btns[i:i + 2] for i in range(0, len(sec_btns), 2)]
    keyboard += [role_btns[i:i + 2] for i in range(0, len(role_btns), 2)]
    keyboard.append([InlineKeyboardButton("— убрать назначение —", callback_data=f"asgset:{uid}:-1")])
    await query.edit_message_text(
        f"Назначить «{name}» — сектор или роль:", reply_markup=InlineKeyboardMarkup(keyboard))


async def assign_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    actor = query.from_user.id
    if not _can_assign(actor):
        return
    _, uid_s, idx_s = query.data.split(":")
    uid, idx = int(uid_s), int(idx_s)
    p = db.get_person(uid)
    name = (p["name"] if p else None) or str(uid)
    roles = _assign_roles_for(actor)
    targets = list(config.SECTORS) + roles
    if idx == -1:
        db.clear_person(uid, name)
        await apply_menu(context.bot, uid)
        await query.edit_message_text(f"Готово: у «{name}» назначение снято.")
        return
    if idx < 0 or idx >= len(targets):
        await query.edit_message_text("Недоступно.")
        return
    target = targets[idx]
    if target in roles:
        db.set_person_role(uid, target, name)
        await apply_menu(context.bot, uid)
        extra = ("\n⚠️ Прежний директор потерял права директора."
                 if target == config.ROLE_DIRECTOR else "")
        await query.edit_message_text(f"Готово: «{name}» → роль «{target}».{extra}")
    else:
        db.set_person_sector(uid, target, name)
        await apply_menu(context.bot, uid)
        await query.edit_message_text(f"Готово: «{name}» → сектор «{target}».")


async def employees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_privileged(update.effective_user.id):
        return
    people = db.list_people()
    if not people:
        await update.message.reply_text("Реестр пуст — никто ещё не запускал бота.")
        return
    lines = ["👥 Сотрудники", ""]
    for p in people:
        assigned = p["sector"] or p["role"] or "не назначен"
        lines.append(f"• {p['name'] or p['user_id']} — {assigned}")
    await update.message.reply_text("\n".join(lines))


# ---------- Бэкап базы ----------

async def daily_backup(context: ContextTypes.DEFAULT_TYPE):
    if not config.ADMIN_ID:
        return
    db_path = os.path.abspath(config.DB_PATH)
    if not os.path.isfile(db_path):
        return
    now = datetime.now(config.TZ)
    today = now.strftime("%Y-%m-%d")
    last = context.bot_data.get("last_backup_date")
    if last == today:
        log.info("Бэкап за %s уже отправлен — пропускаю.", today)
        return
    size_kb = os.path.getsize(db_path) / 1024
    with closing(sqlite3.connect(db_path)) as conn:
        req_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        ppl_count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    caption = (
        f"💾 Бэкап базы данных\n"
        f"📅 {now:%d.%m.%Y %H:%M}\n"
        f"📊 Заявок: {req_count} · Сотрудников: {ppl_count}\n"
        f"📦 Размер: {size_kb:.0f} КБ"
    )
    filename = f"zakupki_{now:%Y-%m-%d}.db"
    with open(db_path, "rb") as f:
        await context.bot.send_document(
            config.ADMIN_ID, document=f, filename=filename, caption=caption)
    context.bot_data["last_backup_date"] = today
    log.info("Бэкап отправлен админу (%s, %.0f КБ)", filename, size_kb)


# ---------- Запуск ----------

def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Мастер сотрудника (потребность + админ-заявка директора)
    employee_conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_request),
            CommandHandler("new_text", new_wizard),
        ],
        states={
            ORDER_NO: [
                CallbackQueryHandler(wiz_type_chosen, pattern=r"^wiz:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_no_received),
            ],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_received)],
            NEEDED_BY: [MessageHandler(filters.TEXT & ~filters.COMMAND, needed_by_received)],
            URGENCY: [CallbackQueryHandler(urgency_chosen, pattern=r"^urgency:")],
            NEED_PHOTO: [
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, need_photo_received),
                CallbackQueryHandler(need_skip_photo, pattern=r"^skipphoto$"),
            ],
            ADM_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_supplier_received)],
            ADM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_amount_received)],
            ADM_NARYAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_naryad_received)],
            ADM_PHOTO: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, adm_photo_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )

    # Мастер закупщика (оформление потребности → заявка)
    buyer_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buyer_start_process, pattern=r"^act:process:")],
        states={
            B_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, buyer_supplier)],
            B_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buyer_amount)],
            B_NARYAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, buyer_naryad)],
            B_PHOTO: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, buyer_photo)],
        },
        fallbacks=[CommandHandler("cancel", buyer_cancel)],
        allow_reentry=True,
        conversation_timeout=600,
    )

    # Мастер редактирования отклонённой заявки (закупщик)
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_start, pattern=r"^act:editreq:")],
        states={
            E_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_supplier)],
            E_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_amount)],
            E_NARYAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_naryad)],
            E_PHOTO: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, edit_photo)],
        },
        fallbacks=[CommandHandler("cancel", edit_cancel)],
        allow_reentry=True,
        conversation_timeout=600,
    )

    # Мастер отклонения заявки (директор вводит причину)
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(reject_start, pattern=r"^act:reject:")],
        states={
            REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, reject_reason_received)],
        },
        fallbacks=[CommandHandler("cancel", reject_cancel)],
        conversation_timeout=600,
    )

    # Мастер прикрепления платёжки (бухгалтер)
    attach_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(attach_start, pattern=r"^act:attach:")],
        states={
            PAYMENT_FILE: [
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, attach_file_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", attach_cancel)],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(employee_conv)
    app.add_handler(buyer_conv)
    app.add_handler(edit_conv)
    app.add_handler(reject_conv)
    app.add_handler(attach_conv)
    app.add_handler(CommandHandler("my", my_requests))
    app.add_handler(CommandHandler("list", list_requests))
    app.add_handler(CommandHandler("assign", assign_cmd))
    app.add_handler(CommandHandler("employees", employees_cmd))
    app.add_handler(CommandHandler("report", report_menu))
    app.add_handler(CommandHandler("audit", audit_cmd))
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^report:"))
    app.add_handler(CallbackQueryHandler(list_callback, pattern=r"^lst:"))
    app.add_handler(CallbackQueryHandler(assign_pick_user, pattern=r"^asg:"))
    app.add_handler(CallbackQueryHandler(assign_set, pattern=r"^asgset:"))
    app.add_handler(CallbackQueryHandler(action_callback, pattern=r"^act:"))

    return app


async def run_all():
    db.init_db()
    app = build_application()

    web_runner = None
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        try:
            web_runner = await webserver.start_webserver(app.bot)
        except Exception as e:
            log.warning("Веб-сервер формы не запущен (%s). Бот работает без формы.", e)

        try:
            await app.bot.set_my_commands([
                BotCommand("new", "Подать потребность"),
                BotCommand("list", "Заявки"),
                BotCommand("my", "Мои заявки"),
            ])
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            log.warning("Не удалось настроить меню/команды: %s", e)

        if config.ADMIN_ID:
            app.job_queue.run_daily(
                daily_backup, time=dtime(18, 30, tzinfo=config.TZ))
            log.info("Бэкап базы запланирован на 18:30 (%s)", config.TZ)

        log.info("Бот запущен")
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await stop_event.wait()

        log.info("Останавливаюсь…")
        await app.updater.stop()
        await app.stop()

    if web_runner is not None:
        await web_runner.cleanup()


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env по образцу .env.example")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
