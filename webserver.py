"""
Внутренний веб-сервер мини-приложения (формы заявки).

Отдаёт страницу формы и принимает её отправку. Личность отправителя берём НЕ из
присланных полей, а из подписанного Телеграмом initData (проверяем HMAC по токену
бота) — подделать нельзя. Файл счёта приходит в том же запросе (multipart).

Сервер слушает локально (config.WEB_HOST:WEB_PORT); наружу по HTTPS его отдаёт
nginx. Запускается в общем событийном цикле рядом с ботом (см. main.py).
"""
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qsl

from aiohttp import web

import config
import core
import db

log = logging.getLogger("zakupki-web")

HERE = os.path.dirname(os.path.abspath(__file__))
FORM_HTML = os.path.join(HERE, "webapp", "form.html")

MAX_FILE_BYTES = 15 * 1024 * 1024  # 15 МБ — с запасом на фото/PDF счёта


def verify_init_data(init_data: str, bot_token: str):
    """Проверяет подпись Telegram WebApp initData. Возвращает dict полей или None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    return pairs


async def handle_form(request: web.Request) -> web.Response:
    # Отдаём саму страницу формы.
    return web.FileResponse(FORM_HTML)


async def handle_config(request: web.Request) -> web.Response:
    # Единый источник правды по секторам — из config.py, чтобы форма не расходилась.
    return web.json_response({"sectors": config.SECTORS})


async def handle_mysector(request: web.Request) -> web.Response:
    # Возвращает закреплённый сектор сотрудника (для авто-подстановки в форме).
    data = await request.post()
    init = verify_init_data(data.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"sector": "", "locked": False})
    try:
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
    except (ValueError, KeyError):
        return web.json_response({"sector": "", "locked": False})
    p = db.get_person(uid)
    sector = p["sector"] if p and p["sector"] else ""
    # Директору форма показывает доп. категорию «Административные».
    admin_sector = config.ADMIN_SECTOR if uid == config.DIRECTOR_ID else ""
    return web.json_response(
        {"sector": sector, "locked": bool(sector), "admin_sector": admin_sector})


async def handle_submit(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    fields = {}
    file_bytes = None
    file_name = None
    file_ctype = None

    async for part in reader:
        if part.name == "file":
            file_name = part.filename or "invoice"
            file_ctype = (part.headers.get("Content-Type") or "").lower()
            buf = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_FILE_BYTES:
                    return web.json_response(
                        {"ok": False, "error": "file_too_big"}, status=413)
            file_bytes = bytes(buf)
        else:
            fields[part.name] = (await part.text()).strip()

    # 1) Проверяем подпись — кто отправил
    init = verify_init_data(fields.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
    try:
        user = json.loads(init.get("user", "{}"))
        submitter_id = int(user["id"])
        submitter_name = " ".join(
            p for p in [user.get("first_name"), user.get("last_name")] if p
        ) or user.get("username") or str(submitter_id)
    except (ValueError, KeyError):
        return web.json_response({"ok": False, "error": "no_user"}, status=403)

    # 2) Регистрируем сотрудника и проверяем право подачи (белый список).
    db.upsert_person(submitter_id, submitter_name)
    person = db.get_person(submitter_id)
    assigned = person["sector"] if person is not None else None
    privileged = submitter_id == config.DIRECTOR_ID or core.is_accountant(submitter_id)
    if not privileged and not assigned:
        return web.json_response({"ok": False, "error": "not_allowed"}, status=403)

    supplier = fields.get("supplier", "")
    naryad = fields.get("naryad", "")
    amount_raw = fields.get("amount", "").replace(" ", "").replace(",", ".")

    # Закреплён за сектором — сектор берём из привязки (только свой сектор),
    # незакреплённый — из формы (свободный выбор).
    if assigned:
        sector = assigned
    else:
        sector = fields.get("sector", "")
        allowed = list(config.SECTORS)
        if submitter_id == config.DIRECTOR_ID:
            allowed.append(config.ADMIN_SECTOR)  # «Административные» — только директору
        if sector not in allowed:
            return web.json_response({"ok": False, "error": "bad_sector"}, status=400)
    if not supplier or not naryad:
        return web.json_response({"ok": False, "error": "empty_fields"}, status=400)
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)
    if not file_bytes:
        return web.json_response({"ok": False, "error": "no_file"}, status=400)

    is_document = "pdf" in file_ctype or (file_name or "").lower().endswith(".pdf")

    # 3) Публикуем заявку
    try:
        request_no = await core.publish_request(
            bot,
            sector=sector, supplier=supplier, amount=amount, naryad=naryad,
            submitter_id=submitter_id, submitter_name=submitter_name,
            file_bytes=file_bytes, file_name=file_name, is_document=is_document,
        )
    except Exception as e:
        log.exception("Не удалось опубликовать заявку из формы: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    # Отдельное подтверждение не шлём: publish_request уже отправил сотруднику
    # личную карточку заявки — она и есть подтверждение (меньше шума).
    return web.json_response({"ok": True, "request_no": request_no})


def build_web_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/form", handle_form)
    app.router.add_get("/config", handle_config)
    app.router.add_post("/mysector", handle_mysector)
    app.router.add_post("/submit", handle_submit)
    return app


async def start_webserver(bot):
    """Поднимает веб-сервер, возвращает AppRunner (для последующего cleanup)."""
    app = build_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    log.info("Веб-сервер формы слушает http://%s:%s (форма: /form, приём: /submit)",
             config.WEB_HOST, config.WEB_PORT)
    return runner
