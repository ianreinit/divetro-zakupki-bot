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

SECTOR, SUPPLIER, AMOUNT, NARYAD, PHOTO = range(5)


# ---------- Мастер подачи заявки (личка сотрудника с ботом) ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existed = db.get_person(user.id) is not None
    db.upsert_person(user.id, user.full_name)
    person = db.get_person(user.id)
    await apply_menu(context.bot, user.id)

    role = person["role"] if person else None
    if is_privileged(user.id) or (person is not None and person["sector"]):
        # Закреплён за сектором / привилегирован — сразу к делу.
        sector_line = f"Ваш сектор: {person['sector']}\n" if (person and person["sector"]) else ""
        kb = None
        if config.WEBAPP_URL and may_submit(user.id):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "📝 Заполнить заявку", web_app=WebAppInfo(config.WEBAPP_URL))]])
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я бот закупок.\n{sector_line}\n"
            "📝 /new — подать заявку\n"
            "📋 /list — заявки\n"
            "📋 /my — мои заявки",
            reply_markup=kb,
        )
    elif role:
        # Водитель / склад — работают только по кнопкам на приходящих карточках.
        await update.message.reply_text(
            f"Привет, {user.first_name}! Вы в системе как «{role}».\n\n"
            "Задачи будут приходить карточками — нажимайте кнопки прямо на них.\n\n"
            f"Ваш ID: {user.id}"
        )
    else:
        # Новый / незакреплённый — ждёт директора.
        await update.message.reply_text(
            f"Привет, {user.first_name}! Вы зарегистрированы в боте закупок.\n\n"
            "Осталось, чтобы директор закрепил вас за сектором — после этого "
            "сможете подавать заявки. Обычно это быстро.\n\n"
            f"Ваш ID: {user.id}"
        )
        # Директору — единоразовый пинг при первом появлении нового человека.
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
    # Показать свой Telegram ID — для настройки DIRECTOR_ID / ACCOUNTANT_ID.
    u = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: {u.id}\nИмя: {u.full_name}")


async def new_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пошаговый мастер (запасной путь / команда /new_text).
    db.upsert_person(update.effective_user.id, update.effective_user.full_name)
    if not may_submit(update.effective_user.id):
        await update.message.reply_text(NOT_ALLOWED_MSG)
        return ConversationHandler.END
    sectors = list(config.SECTORS)
    if core.is_director(update.effective_user.id):
        sectors.append(config.ADMIN_SECTOR)   # категория «Административные» — только директору
    keyboard = [[InlineKeyboardButton(s, callback_data=f"sector:{s}")] for s in sectors]
    await update.message.reply_text("Выберите сектор:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SECTOR


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /new: если настроена форма (WEBAPP_URL) — показываем кнопку с формой, без диалога.
    # Иначе откатываемся на пошаговый мастер.
    db.upsert_person(update.effective_user.id, update.effective_user.full_name)
    if not may_submit(update.effective_user.id):
        await update.message.reply_text(NOT_ALLOWED_MSG)
        return ConversationHandler.END
    if config.WEBAPP_URL:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Заполнить заявку", web_app=WebAppInfo(config.WEBAPP_URL))
        ]])
        await update.message.reply_text(
            "Нажмите кнопку, чтобы заполнить заявку одной формой:", reply_markup=kb)
        return ConversationHandler.END
    return await new_wizard(update, context)


async def sector_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sector = query.data.split(":", 1)[1]
    # «Административные» доступны только директору (на случай подделки callback_data).
    if sector == config.ADMIN_SECTOR and not core.is_director(query.from_user.id):
        await query.edit_message_text("Эта категория доступна только директору.")
        return ConversationHandler.END
    context.user_data["sector"] = sector
    await query.edit_message_text(f"Сектор: {sector}\n\nПоставщик?")
    return SUPPLIER


async def supplier_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["supplier"] = update.message.text.strip()
    await update.message.reply_text("Сумма?")
    return AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Не понял сумму, введите числом, например 42500")
        return AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text("Номер наряда / проекта?")
    return NARYAD


async def naryad_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["naryad"] = update.message.text.strip()
    await update.message.reply_text(
        "Прикрепите фото или PDF счёта — без него заявку отправить нельзя."
    )
    return PHOTO


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        is_document = False
    elif update.message.document:
        file_id = update.message.document.file_id
        # PDF (и прочие не-картинки) отправляем как документ, а не как фото.
        is_document = (update.message.document.mime_type or "") != "" and \
            not (update.message.document.mime_type or "").startswith("image/")
    else:
        await update.message.reply_text("Это не похоже на фото или файл. Прикрепите счёт.")
        return PHOTO

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

    # Отдельное «Готово» не шлём — личная карточка заявки (её отправляет
    # publish_request) сама служит подтверждением. Меньше шума.
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ---------- Кнопки: одобрение директора / оплата / логистика / платёжка ----------

def _requester_label(uid: int, req) -> str:
    # Кто запросил платёжку — для уведомления бухгалтеру.
    if core.is_driver(uid):
        return "Водитель"
    if core.is_director(uid):
        return "Директор"
    p = db.get_person(uid)
    name = (p["name"] if p else None) or str(uid)
    return f"{name} · {req['sector']}"


async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, req_id_s = query.data.split(":")
    req_id = int(req_id_s)
    req = db.get_by_id(req_id)
    if req is None:
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    uid = query.from_user.id
    now_dt = datetime.now(config.TZ)
    now = now_dt.isoformat(timespec="seconds")
    ts = now_dt.strftime("%d.%m %H:%M")

    if action in ("approve", "reject"):
        if not core.is_director(uid):
            await query.answer("Подтверждать может только директор.", show_alert=True)
            return
        if req["status"] != "отправлено":
            await query.answer("Заявка уже обработана.", show_alert=True)
            return
        await query.answer()
        if action == "approve":
            db.set_status(req_id, "одобрено", "approved_by", query.from_user.full_name, "approved_at", now)
            req = db.get_by_id(req_id)
            await core.refresh_all_cards(context.bot, req)                  # у всех — прогресс на месте
            await core.send_accountant_card(context.bot, req)              # бухгалтеру — карточка с «Оплатить»
        else:
            db.set_status(req_id, "отклонено", "approved_by", query.from_user.full_name, "approved_at", now)
            req = db.get_by_id(req_id)
            await core.refresh_all_cards(context.bot, req)
            await notify(context, req["submitted_by_id"], f"✖ Наряд {req['naryad']} — заявка отклонена.")

    elif action == "pay":
        # Оплата в один тап — без обязательной платёжки. Платёжка теперь по запросу
        # (кнопка «Запросить платёжку») или проактивно кнопкой «Прикрепить платёжку».
        if not core.is_accountant(uid):
            await query.answer("Оплатить может только бухгалтер.", show_alert=True)
            return
        if req["status"] != "одобрено":
            await query.answer("Заявка ещё не одобрена или уже оплачена.", show_alert=True)
            return
        await query.answer("Оплачено")
        db.set_status(req_id, "оплачено", "paid_by", query.from_user.full_name, "paid_at", now)
        req = db.get_by_id(req_id)
        await core.refresh_all_cards(context.bot, req)     # у всех — прогресс + кнопки по статусу
        await core.notify_paid(context.bot, req)           # пуш сотруднику: оплачено
        await core.send_driver_card(context.bot, req)      # логистика: водителю

    elif action == "needpay":
        # Запросить платёжку по заявке. Кнопка у водителя, сотрудника-подателя, директора.
        allowed = (uid == req["submitted_by_id"] or core.is_driver(uid)
                   or core.is_director(uid))
        if not allowed:
            await query.answer("Недоступно.", show_alert=True)
            return
        if req["payment_file_id"]:
            # Платёжка уже прикреплена — сразу отправляем запросившему, бухгалтера не трогаем.
            await core.send_payment_file_to(context.bot, req, uid)
            await query.answer("📄 Платёжка отправлена вам.")
            return
        already = db.get_payment_pending(req_id)
        if uid in already:
            await query.answer("⏳ Уже запрошено — ждём бухгалтера.", show_alert=True)
            return
        db.add_payment_pending(req_id, uid)
        await query.answer("✅ Запрос отправлен бухгалтеру.", show_alert=True)
        # Пинг бухгалтеру — только на ПЕРВЫЙ запрос по заявке (очередь была пуста).
        # Последующие запросы (в т.ч. от других людей) молча встают в очередь. FR-7/T5.
        if already:
            return
        label = _requester_label(uid, req)
        for aid in core.accountant_ids():
            try:
                await context.bot.send_message(
                    aid,
                    f"📄 По наряду {req['naryad']} нужна платёжка.\nЗапросил: {label}.",
                    reply_markup=core.attach_kb(req_id))
            except Exception as e:
                log.warning("Не уведомить бухгалтера %s о запросе платёжки: %s", aid, e)

    elif action == "attach":
        # Бухгалтер прикрепляет платёжку к конкретной заявке (следующий файл — сюда).
        if not core.is_accountant(uid):
            await query.answer("Только бухгалтер.", show_alert=True)
            return
        await query.answer()
        context.user_data["awaiting_payment_for"] = req_id
        prompt = await context.bot.send_message(
            uid,
            f"Пришлите фото или PDF платёжки по наряду {req['naryad']} — "
            f"прикреплю к заявке и отправлю тем, кто её запросил.")
        context.user_data["payment_prompt_msg_id"] = prompt.message_id

    elif action == "ship":
        # Водитель поехал за товаром → складу приходит карточка «Принял на складе».
        if not core.is_driver(uid):
            await query.answer("Отметить может только водитель.", show_alert=True)
            return
        if req["status"] != "оплачено":
            await query.answer("Заявка ещё не оплачена или уже в работе.", show_alert=True)
            return
        await query.answer()
        db.set_status(req_id, "в_пути", "shipped_by", query.from_user.full_name, "shipped_at", now)
        req = db.get_by_id(req_id)
        await core.refresh_all_cards(context.bot, req)                     # у всех — прогресс на месте
        await core.send_warehouse_card(context.bot, req)                   # складу — кнопка «Принял»

    elif action == "receive":
        # Склад/цех принял товар → заявка закрыта, сотруднику финальный пинг.
        if not core.is_warehouse(uid):
            await query.answer("Отметить может только склад.", show_alert=True)
            return
        if req["status"] != "в_пути":
            await query.answer("Товар ещё не в пути или уже принят.", show_alert=True)
            return
        await query.answer()
        db.set_status(req_id, "получено", "received_by", query.from_user.full_name, "received_at", now)
        req = db.get_by_id(req_id)
        await core.refresh_all_cards(context.bot, req)                     # у всех — прогресс на месте
        await notify(context, req["submitted_by_id"],
                     f"📦 Наряд {req['naryad']} — товар принят на складе.")


async def accountant_payment_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Бухгалтер прислал платёжку после нажатия «Прикрепить» — цепляем к заявке и
    # рассылаем тем, кто запросил. Оплата к этому моменту уже проставлена (один тап).
    req_id = context.user_data.get("awaiting_payment_for")
    if not req_id:
        return  # файл прислан вне режима прикрепления — не наш случай
    if update.message.photo:
        file_id, is_document = update.message.photo[-1].file_id, 0
    elif update.message.document:
        mime = update.message.document.mime_type or ""
        file_id = update.message.document.file_id
        is_document = 0 if mime.startswith("image/") else 1
    else:
        await update.message.reply_text("Пришлите фото или PDF платёжки.")
        return

    req = db.get_by_id(req_id)
    if req is None:
        context.user_data.pop("awaiting_payment_for", None)
        await update.message.reply_text("Заявка не найдена.")
        return

    db.set_payment_file(req_id, file_id, is_document)
    context.user_data.pop("awaiting_payment_for", None)
    req = db.get_by_id(req_id)  # свежая версия, уже с платёжкой

    sent_to = await core.deliver_payment_to_pending(context.bot, req)   # всем, кто запросил
    # Прогресс-блок на карточках не меняется — платёжка не статус; карточки не трогаем.

    # Подтверждение бухгалтеру (правкой промпта, без лишнего сообщения).
    who = f" — отправлена запросившим ({len(sent_to)})" if sent_to else " — сохранена (запросов пока нет)"
    prompt_id = context.user_data.pop("payment_prompt_msg_id", None)
    text = f"✅ Платёжка по наряду {req['naryad']} прикреплена{who}."
    if prompt_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_user.id, message_id=prompt_id, text=text)
        except Exception:
            await update.message.reply_text(text)
    else:
        await update.message.reply_text(text)


async def notify(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        log.warning("Не удалось отправить личное уведомление %s: %s", user_id, e)


# ---------- Отчёт по кнопке ----------

async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # «Административные» в отчёте видит только директор/бухгалтер, сотрудники — нет.
    sectors = config.ALL_SECTORS if is_privileged(update.effective_user.id) else config.SECTORS
    buttons = [InlineKeyboardButton(s, callback_data=f"report:{s}") for s in sectors]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("Все направления", callback_data="report:Все")])
    await update.message.reply_text(
        "📊 Отчёт по закупкам — выберите направление",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


STATUS_MARK = {"отправлено": "🔵", "одобрено": "🟢", "оплачено": "🟥",
               "в_пути": "🚚", "получено": "📦", "отклонено": "✖"}


async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sector = query.data.split(":", 1)[1]
    priv = is_privileged(query.from_user.id)
    # Административные — только директору/бухгалтеру: и как отдельный фильтр, и внутри «Все».
    if sector == config.ADMIN_SECTOR and not priv:
        await query.edit_message_text("Раздел доступен только директору.")
        return

    month_start = datetime.now(config.TZ).replace(day=1, hour=0, minute=0, second=0).isoformat(timespec="seconds")
    rows = db.report(sector, month_start)
    if not priv:
        rows = [r for r in rows if r["sector"] != config.ADMIN_SECTOR]

    total = sum(r["amount"] for r in rows)
    pending = sum(1 for r in rows if r["status"] in ("отправлено", "одобрено"))

    lines = [
        f"{sector} · {datetime.now(config.TZ):%m.%Y}", "",
        f"Заявок: {len(rows)}  ·  Сумма: {total:,.0f}  ·  В ожидании: {pending}".replace(",", " "),
        "",
    ]
    for r in rows[:10]:
        amount_str = f"{r['amount']:,.0f}".replace(",", " ")
        lines.append(f"{STATUS_MARK[r['status']]} наряд {r['naryad']} · {r['supplier']} · {amount_str}")

    if not rows:
        lines.append("Заявок за этот месяц пока нет.")

    await query.edit_message_text("\n".join(lines))


# ---------- Мои заявки (монитор для сотрудника, в личке) ----------

STATUS_SHORT = {
    "отправлено": "отправлено",
    "одобрено": "ждёт оплаты",
    "оплачено": "оплачено",
    "в_пути": "в пути",
    "получено": "на складе",
    "отклонено": "отклонено",
}


def is_privileged(user_id: int) -> bool:
    # Полный доступ (видят все заявки, /list, /report): админ, директор, бухгалтер.
    # Директор — из .env ИЛИ назначенный админом; бухгалтер — из .env ИЛИ /assign.
    return (core.is_admin(user_id) or core.is_director(user_id)
            or core.is_accountant(user_id))


NOT_ALLOWED_MSG = (
    "Вы пока не закреплены за сектором, поэтому не можете подавать заявки.\n"
    "Обратитесь к директору — он добавит вас командой."
)


def may_submit(user_id: int) -> bool:
    # Подавать заявки может закреплённый за сектором сотрудник или директор/бухгалтер.
    if is_privileged(user_id):
        return True
    p = db.get_person(user_id)
    return bool(p is not None and p["sector"])


async def apply_menu(bot, user_id: int):
    # Кнопка-меню (≡) ведёт на форму только у тех, кому можно подавать; у остальных
    # — обычное меню команд (форму им открывать незачем).
    try:
        if config.WEBAPP_URL and may_submit(user_id):
            await bot.set_chat_menu_button(
                chat_id=user_id,
                menu_button=MenuButtonWebApp(text="Заявка", web_app=WebAppInfo(config.WEBAPP_URL)))
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
        amount_str = f"{r['amount']:,.0f}".replace(",", " ")
        mark = STATUS_MARK.get(r["status"], "•")
        short = STATUS_SHORT.get(r["status"], r["status"])
        sec = f"[{r['sector']}] " if show_sector else ""
        lines.append(f"{mark} {sec}наряд {r['naryad']} · {r['supplier']} · {amount_str} — {short}")
    return "\n".join(lines)


async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_by_submitter(update.effective_user.id, limit=15)
    await update.message.reply_text(format_requests(rows, "📋 Ваши заявки"))


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_privileged(uid):
        buttons = [InlineKeyboardButton(s, callback_data=f"lst:{i}")
                   for i, s in enumerate(config.ALL_SECTORS)]
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
    if not is_privileged(query.from_user.id):
        return
    key = query.data.split(":", 1)[1]
    if key == "all":
        rows = db.list_all_requests(limit=25)
        text = format_requests(rows, "📋 Все заявки", show_sector=True)
    else:
        sector = config.ALL_SECTORS[int(key)]
        rows = db.list_by_sector(sector, limit=25)
        text = format_requests(rows, f"📋 Заявки сектора «{sector}»")
    await query.edit_message_text(text)


# ---------- Назначение сотрудников за секторами (только директор) ----------

def _can_assign(user_id: int) -> bool:
    # Назначать может директор (секторы + Бухгалтер/Водитель/Склад) и админ
    # (дополнительно роль «Директор»).
    return core.is_director(user_id) or core.is_admin(user_id)


def _assign_roles_for(actor_id: int):
    # Директор раздаёт ROLES; админ — ещё и роль «Директор» (ADMIN_ROLES).
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


# Подписи кнопок ролей в /assign (значение в БД — из config.ROLES, без эмодзи).
ROLE_LABEL = {
    config.ROLE_DIRECTOR: "👑 Директор",
    config.ROLE_ACCOUNTANT: "💰 Бухгалтер",
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
    # Кнопки-цели: сначала производственные секторы, затем роли. Индекс в callback —
    # позиция в общем списке SECTORS + roles (тот же порядок в assign_set).
    sec_btns = [InlineKeyboardButton(s, callback_data=f"asgset:{uid}:{i}")
                for i, s in enumerate(config.SECTORS)]
    role_btns = [InlineKeyboardButton(ROLE_LABEL[r], callback_data=f"asgset:{uid}:{len(config.SECTORS)+i}")
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
    # Набор целей зависит от актора: у админа в конце добавлена роль «Директор».
    roles = _assign_roles_for(actor)
    targets = list(config.SECTORS) + roles
    if idx == -1:
        db.clear_person(uid, name)
        await apply_menu(context.bot, uid)
        await query.edit_message_text(f"Готово: у «{name}» назначение снято.")
        return
    if idx < 0 or idx >= len(targets):
        # Индекс вне диапазона (напр. директор нажал устаревшую кнопку «Директор»).
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
    log.info("Бэкап отправлен админу (%s, %.0f КБ)", filename, size_kb)


# ---------- Запуск ----------

def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_request),
            CommandHandler("new_text", new_wizard),
        ],
        states={
            SECTOR: [CallbackQueryHandler(sector_chosen, pattern=r"^sector:")],
            SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, supplier_received)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
            NARYAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, naryad_received)],
            PHOTO: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, photo_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(conv)
    app.add_handler(CommandHandler("my", my_requests))
    app.add_handler(CommandHandler("list", list_requests))
    app.add_handler(CommandHandler("assign", assign_cmd))
    app.add_handler(CommandHandler("employees", employees_cmd))
    app.add_handler(CommandHandler("report", report_menu))
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^report:"))
    app.add_handler(CallbackQueryHandler(list_callback, pattern=r"^lst:"))
    app.add_handler(CallbackQueryHandler(assign_pick_user, pattern=r"^asg:"))
    app.add_handler(CallbackQueryHandler(assign_set, pattern=r"^asgset:"))
    app.add_handler(CallbackQueryHandler(action_callback, pattern=r"^act:"))
    # Платёжка от бухгалтера (фото/PDF) — только после нажатия «Оплатить».
    # Бухгалтер может быть задан и в .env, и назначен через /assign, поэтому фильтр
    # по конкретному User не ставим: ловим фото/файл в личке и обрабатываем лишь при
    # выставленном awaiting_payment_for (его выставляет только нажатие «Оплатить»).
    # Регистрируем после ConversationHandler, чтобы не перехватывать шаг мастера.
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.ChatType.PRIVATE & ~filters.COMMAND,
        accountant_payment_received))

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

        # Подсказки команд и кнопка-меню (форма всегда под рукой).
        try:
            await app.bot.set_my_commands([
                BotCommand("new", "Подать заявку"),
                BotCommand("list", "Заявки"),
                BotCommand("my", "Мои заявки"),
            ])
            # Глобально по умолчанию — обычное меню команд. Форму на кнопке-меню
            # включаем персонально закреплённым (см. apply_menu).
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
                pass  # Windows
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
