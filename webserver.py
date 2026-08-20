"""
Внутренний веб-сервер мини-приложения (формы потребности).

Отдаёт страницу формы и принимает её отправку. Личность отправителя берём НЕ из
присланных полей, а из подписанного Телеграмом initData (проверяем HMAC по токену
бота) — подделать нельзя. Файл (фото/чертёж) приходит в том же запросе (multipart),
но теперь необязателен (сотрудник подаёт потребность, а не готовую заявку).

Сервер слушает локально (config.WEB_HOST:WEB_PORT); наружу по HTTPS его отдаёт
nginx. Запускается в общем событийном цикле рядом с ботом (см. main.py).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from io import BytesIO
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import InputFile

import config
import core
import db

log = logging.getLogger("zakupki-web")

HERE = os.path.dirname(os.path.abspath(__file__))
FORM_HTML = os.path.join(HERE, "webapp", "form.html")
PAY_HTML = os.path.join(HERE, "webapp", "pay.html")
BUYER_FORM_HTML = os.path.join(HERE, "webapp", "buyer_form.html")
BUYER_REQUEST_FORM_HTML = os.path.join(HERE, "webapp", "buyer_request_form.html")

MAX_FILE_BYTES = 15 * 1024 * 1024


class FileTooLarge(Exception):
    pass


async def parse_multipart(request):
    try:
        reader = await request.multipart()
    except Exception:
        return None, None, None, None

    fields = {}
    file_bytes = None
    file_name = None
    file_ctype = None

    async for part in reader:
        if part.name == "file":
            file_name = part.filename or "attachment"
            file_ctype = (part.headers.get("Content-Type") or "").lower()
            buf = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_FILE_BYTES:
                    raise FileTooLarge
            file_bytes = bytes(buf)
        else:
            fields[part.name] = (await part.text()).strip()

    return fields, file_bytes, file_name, file_ctype


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    auth_date = int(pairs.get("auth_date", "0"))
    if auth_date and time.time() - auth_date > max_age:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    return pairs


async def handle_form(request: web.Request) -> web.Response:
    return web.FileResponse(FORM_HTML)


async def handle_config(request: web.Request) -> web.Response:
    return web.json_response({"sectors": config.SECTORS})


async def handle_mysector(request: web.Request) -> web.Response:
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
    if not sector and len(config.SECTORS) == 1:
        if (core.is_employee(uid) or core.is_driver(uid) or core.is_warehouse(uid)
                or core.is_director(uid) or core.is_accountant(uid) or core.is_buyer(uid)
                or core.is_admin(uid)):
            sector = config.SECTORS[0]
    admin_sector = config.ADMIN_SECTOR if core.is_director(uid) else ""
    return web.json_response(
        {"sector": sector, "locked": bool(sector), "admin_sector": admin_sector})


async def handle_submit(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        fields, file_bytes, file_name, file_ctype = await parse_multipart(request)
    except FileTooLarge:
        return web.json_response({"ok": False, "error": "file_too_big"}, status=413)
    if fields is None:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    # 1) Проверяем подпись
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

    # 2) Белый список
    db.upsert_person(submitter_id, submitter_name)
    person = db.get_person(submitter_id)
    assigned = person["sector"] if person is not None else None
    privileged = (core.is_director(submitter_id) or core.is_accountant(submitter_id)
                  or core.is_buyer(submitter_id) or core.is_admin(submitter_id))
    has_role = (core.is_employee(submitter_id) or core.is_driver(submitter_id)
                or core.is_warehouse(submitter_id))
    if not privileged and not has_role and not assigned:
        return web.json_response({"ok": False, "error": "not_allowed"}, status=403)

    # 3) Поля потребности
    order_no = fields.get("order_no", "").strip()
    description = fields.get("description", "")
    needed_by = fields.get("needed_by", "")
    urgency = fields.get("urgency", "обычная")

    if assigned:
        sector = assigned
    else:
        sector = fields.get("sector", "")
        if sector not in config.SECTORS:
            return web.json_response({"ok": False, "error": "bad_sector"}, status=400)

    if not order_no:
        return web.json_response({"ok": False, "error": "no_order_no"}, status=400)
    if len(order_no) > 100:
        return web.json_response({"ok": False, "error": "order_no_too_long"}, status=400)
    if not description:
        return web.json_response({"ok": False, "error": "no_description"}, status=400)
    if len(description) > 500:
        return web.json_response({"ok": False, "error": "description_too_long"}, status=400)
    if not needed_by:
        return web.json_response({"ok": False, "error": "bad_date"}, status=400)

    is_document = False
    if file_bytes:
        is_document = "pdf" in (file_ctype or "") or (file_name or "").lower().endswith(".pdf")

    try:
        request_no = await core.publish_need(
            bot, sector=sector, description=description, needed_by=needed_by,
            urgency=urgency, submitter_id=submitter_id, submitter_name=submitter_name,
            order_no=order_no,
            file_bytes=file_bytes, file_name=file_name, is_document=is_document,
        )
    except Exception as e:
        log.exception("Не удалось опубликовать потребность из формы: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    return web.json_response({"ok": True, "request_no": request_no})


async def handle_buyer_form(request: web.Request) -> web.Response:
    return web.FileResponse(BUYER_FORM_HTML)


async def handle_buyer_need_info(request: web.Request) -> web.Response:
    data = await request.post()
    init = verify_init_data(data.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
    try:
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
    except (ValueError, KeyError):
        return web.json_response({"ok": False, "error": "no_user"}, status=403)

    if not core.is_buyer(uid):
        return web.json_response({"ok": False, "error": "not_buyer"}, status=403)

    try:
        req_id = int(data.get("req", ""))
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    req = db.get_by_id(req_id)
    if req is None:
        return web.json_response({"ok": False, "error": "not_found"}, status=404)
    if req["status"] != "потребность":
        return web.json_response({"ok": False, "error": "already_processed"}, status=400)

    return web.json_response({
        "ok": True,
        "request_no": req.get("request_no", ""),
        "order_no": req.get("order_no", ""),
        "description": req.get("description", ""),
        "needed_by": req.get("needed_by", ""),
        "urgency": req.get("urgency", ""),
        "submitted_by_name": req.get("submitted_by_name", ""),
    })


async def handle_buyer_submit(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        fields, file_bytes, file_name, file_ctype = await parse_multipart(request)
    except FileTooLarge:
        return web.json_response({"ok": False, "error": "file_too_big"}, status=413)
    if fields is None:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    init = verify_init_data(fields.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
    try:
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
        actor_name = " ".join(
            p for p in [user.get("first_name"), user.get("last_name")] if p
        ) or user.get("username") or str(uid)
    except (ValueError, KeyError):
        return web.json_response({"ok": False, "error": "no_user"}, status=403)

    if not core.is_buyer(uid):
        return web.json_response({"ok": False, "error": "not_buyer"}, status=403)

    try:
        req_id = int(fields.get("req", ""))
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    req = db.get_by_id(req_id)
    if req is None:
        return web.json_response({"ok": False, "error": "not_found"}, status=404)
    if req["status"] != "потребность":
        return web.json_response({"ok": False, "error": "already_processed"}, status=400)

    supplier = fields.get("supplier", "").strip()
    naryad = fields.get("naryad", "").strip()
    if not supplier:
        return web.json_response({"ok": False, "error": "no_supplier"}, status=400)
    if len(supplier) > 200:
        return web.json_response({"ok": False, "error": "no_supplier"}, status=400)
    if not naryad:
        return web.json_response({"ok": False, "error": "no_naryad"}, status=400)
    if len(naryad) > 100:
        return web.json_response({"ok": False, "error": "no_naryad"}, status=400)

    try:
        amount = float(fields.get("amount", "0").replace(" ", "").replace(",", "."))
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)
    if amount <= 0:
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)

    if not file_bytes:
        return web.json_response({"ok": False, "error": "no_file"}, status=400)

    is_document = "pdf" in (file_ctype or "") or (file_name or "").lower().endswith(".pdf")

    try:
        media = InputFile(BytesIO(file_bytes), filename=file_name or "invoice")
        no = core._display_no(req)
        caption = f"📝 Заявка {no} оформлена и отправлена на одобрение."
        _, file_id = await core._send_card(bot, uid, media, caption, is_document)
    except Exception as e:
        log.exception("buyer_submit: не удалось загрузить счёт: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    try:
        await core.process_need(
            bot, req_id,
            supplier=supplier, amount=amount, naryad=naryad,
            photo_file_id=file_id, is_document=is_document,
            processed_by_id=uid, processed_by_name=actor_name,
        )
    except Exception as e:
        log.exception("buyer_submit: не удалось оформить заявку: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    return web.json_response({"ok": True, "request_no": req.get("request_no", "")})


async def handle_buyer_request_form(request: web.Request) -> web.Response:
    return web.FileResponse(BUYER_REQUEST_FORM_HTML)


async def handle_buyer_request_submit(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        fields, file_bytes, file_name, file_ctype = await parse_multipart(request)
    except FileTooLarge:
        return web.json_response({"ok": False, "error": "file_too_big"}, status=413)
    if fields is None:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    init = verify_init_data(fields.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
    try:
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
        actor_name = " ".join(
            p for p in [user.get("first_name"), user.get("last_name")] if p
        ) or user.get("username") or str(uid)
    except (ValueError, KeyError):
        return web.json_response({"ok": False, "error": "no_user"}, status=403)

    if not core.is_buyer(uid):
        return web.json_response({"ok": False, "error": "not_buyer"}, status=403)

    supplier = fields.get("supplier", "").strip()
    naryad = fields.get("naryad", "").strip()
    if not supplier:
        return web.json_response({"ok": False, "error": "no_supplier"}, status=400)
    if len(supplier) > 200:
        return web.json_response({"ok": False, "error": "no_supplier"}, status=400)
    if not naryad:
        return web.json_response({"ok": False, "error": "no_naryad"}, status=400)
    if len(naryad) > 100:
        return web.json_response({"ok": False, "error": "no_naryad"}, status=400)

    try:
        amount = float(fields.get("amount", "0").replace(" ", "").replace(",", "."))
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)
    if amount <= 0:
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)

    if not file_bytes:
        return web.json_response({"ok": False, "error": "no_file"}, status=400)

    is_document = "pdf" in (file_ctype or "") or (file_name or "").lower().endswith(".pdf")

    try:
        request_no = await core.publish_request(
            bot,
            sector=config.SECTORS[0],
            supplier=supplier,
            amount=amount,
            naryad=naryad,
            submitter_id=uid,
            submitter_name=actor_name,
            file_bytes=file_bytes,
            file_name=file_name or "invoice",
            is_document=is_document,
        )
    except Exception as e:
        log.exception("buyer_request_submit: не удалось создать заявку: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    return web.json_response({"ok": True, "request_no": request_no})


async def handle_pay(request: web.Request) -> web.Response:
    return web.FileResponse(PAY_HTML)


async def handle_attach_payment(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        fields, file_bytes, file_name, file_ctype = await parse_multipart(request)
    except FileTooLarge:
        return web.json_response({"ok": False, "error": "file_too_big"}, status=413)
    if fields is None:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    init = verify_init_data(fields.get("initData", ""), config.BOT_TOKEN)
    if init is None:
        return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
    try:
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
    except (ValueError, KeyError):
        return web.json_response({"ok": False, "error": "no_user"}, status=403)

    if not core.is_accountant(uid):
        return web.json_response({"ok": False, "error": "not_allowed"}, status=403)

    try:
        req_id = int(fields.get("req", ""))
    except ValueError:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    req = db.get_by_id(req_id)
    if req is None:
        return web.json_response({"ok": False, "error": "not_found"}, status=404)
    if not file_bytes:
        return web.json_response({"ok": False, "error": "no_file"}, status=400)

    is_document = "pdf" in file_ctype or (file_name or "").lower().endswith(".pdf")

    try:
        caption = f"✅ Платёжка по {core._display_no(req)} прикреплена."
        media = InputFile(BytesIO(file_bytes), filename=file_name or "payment")
        _, file_id = await core._send_card(bot, uid, media, caption, is_document)
    except Exception as e:
        log.exception("attach_payment: не удалось отправить платёжку бухгалтеру: %s", e)
        return web.json_response({"ok": False, "error": "publish_failed"}, status=500)

    db.set_payment_file(req_id, file_id, 1 if is_document else 0)
    user = json.loads(init.get("user", "{}"))
    actor_name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p) or str(uid)
    db.log_action(req_id, "платёжка", uid, actor_name)
    req = db.get_by_id(req_id)
    await core.deliver_payment_to_pending(bot, req)
    return web.json_response({"ok": True})


def build_web_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/form", handle_form)
    app.router.add_get("/config", handle_config)
    app.router.add_post("/mysector", handle_mysector)
    app.router.add_post("/submit", handle_submit)
    app.router.add_get("/buyer_form", handle_buyer_form)
    app.router.add_post("/buyer_need_info", handle_buyer_need_info)
    app.router.add_post("/buyer_submit", handle_buyer_submit)
    app.router.add_get("/buyer_request_form", handle_buyer_request_form)
    app.router.add_post("/buyer_request_submit", handle_buyer_request_submit)
    app.router.add_get("/pay", handle_pay)
    app.router.add_post("/attach_payment", handle_attach_payment)
    return app


async def start_webserver(bot):
    app = build_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    log.info("Веб-сервер формы слушает http://%s:%s (форма: /form, приём: /submit, "
             "платёжка: /pay → /attach_payment)", config.WEB_HOST, config.WEB_PORT)
    return runner
