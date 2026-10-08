"""Request discussion: access control, history, and explicit notifications."""
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

from aiohttp import web

import config
import core
import db

log = logging.getLogger("zakupki-comments")


def can_access(uid, req):
    privileged = core.is_admin(uid) or core.is_director(uid) or core.is_accountant(uid)
    if req["sector"] == config.ADMIN_SECTOR:
        return privileged
    if privileged or core.is_buyer(uid) or uid == req["submitted_by_id"]:
        return True
    if core.is_driver(uid) and req.get("driver_msg_id"):
        return True
    if core.is_warehouse(uid) and req.get("warehouse_msg_id"):
        return True
    person = db.get_person(uid)
    return bool(person and person["sector"] == req["sector"])


def access_checker(uid):
    """Snapshot access once per read request; never cache across users or requests."""
    if core.is_admin(uid) or core.is_director(uid) or core.is_accountant(uid):
        return lambda req: True
    buyer = core.is_buyer(uid)
    driver = core.is_driver(uid)
    warehouse = core.is_warehouse(uid)
    person = db.get_person(uid)
    sector = person['sector'] if person else None
    def check(req):
        if req['sector'] == config.ADMIN_SECTOR:
            return False
        return bool(buyer or uid == req['submitted_by_id'] or
                    (driver and req.get('driver_msg_id')) or
                    (warehouse and req.get('warehouse_msg_id')) or
                    (sector and sector == req['sector']))
    return check


def user_role(uid):
    for check, label in [(core.is_admin, "Администратор"), (core.is_director, "Директор"),
                         (core.is_accountant, "Бухгалтер"), (core.is_buyer, "Закупщик"),
                         (core.is_driver, "Водитель"), (core.is_warehouse, "Склад")]:
        if check(uid):
            return label
    return "Сотрудник"


def recipients(req, author_id):
    ids = [req["submitted_by_id"], *core.director_ids(), *core.accountant_ids(), *core.buyer_ids()]
    ids.extend(db.comment_author_ids(req["id"]))
    if config.ADMIN_ID:
        ids.append(config.ADMIN_ID)
    if req.get("driver_msg_id"):
        ids.extend(core.driver_ids())
    if req.get("warehouse_msg_id"):
        ids.extend(core.warehouse_ids())
    result = []
    for uid in dict.fromkeys(ids):
        if uid == author_id or not can_access(uid, req):
            continue
        person = db.get_person(uid)
        role = user_role(uid)
        name = person["name"] if person else None
        if not name and uid == req["submitted_by_id"]:
            name = req["submitted_by_name"]
        result.append({"id": uid, "name": name or role, "role": role})
    return result


def error(code, status):
    return web.json_response({"ok": False, "error": code}, status=status)


async def authenticate(request):
    try:
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get("initData"), str):
            raise ValueError
        init = request.app["comments_verify"](data["initData"], config.BOT_TOKEN)
        if init is None:
            return None, None, None, error("auth_failed", 403)
        user = json.loads(init.get("user", "{}"))
        uid = int(user["id"])
        req = db.get_by_id(int(data["req"]))
    except (ValueError, TypeError, KeyError):
        return None, None, None, error("bad_request", 400)
    if req is None:
        return None, None, None, error("not_found", 404)
    if not can_access(uid, req):
        return None, None, None, error("not_allowed", 403)
    return data, user, req, None


def public_comment(row):
    return {key: row[key] for key in ("id", "author_name", "author_role", "body", "created_at")}


async def handle_page(request):
    return web.FileResponse(Path(__file__).parent / "webapp" / "comments.html")


async def handle_history(request):
    data, user, req, failure = await authenticate(request)
    if failure is not None:
        return failure
    try:
        before = int(data["before"]) if data.get("before") is not None else None
    except (TypeError, ValueError):
        return error("bad_request", 400)
    rows = db.list_comments(req["id"], before=before, limit=51)
    more = len(rows) > 50
    rows = rows[-50:]
    return web.json_response({
        "ok": True,
        "request": {"number": core._display_no(req), "supplier": req.get("supplier") or "—",
                    "amount": req.get("amount") or 0, "status": req["status"]},
        "comments": [public_comment(row) for row in rows],
        "before": rows[0]["id"] if more else None,
        "recipients": recipients(req, int(user["id"]))})


async def handle_post(request):
    data, user, req, failure = await authenticate(request)
    if failure is not None:
        return failure
    uid = int(user["id"])
    body = data.get("body")
    if not isinstance(body, str) or not body.strip() or len(body.strip()) > 1000:
        return error("invalid_body", 400)
    body = body.strip()
    try:
        submission_id = str(UUID(data.get("submission_id", "")))
        selected = data.get("recipients", [])
        if not isinstance(selected, list) or any(type(item) is not int for item in selected):
            raise ValueError
        selected = set(selected) - {uid}
    except (ValueError, TypeError, AttributeError):
        return error("bad_request", 400)
    available = {person["id"]: person for person in recipients(req, uid)}
    if not selected.issubset(available):
        return error("invalid_recipients", 403)
    name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p) or str(uid)
    async with request.app["comments_lock"]:
        comment = db.save_comment(req["id"], uid, name, user_role(uid), body, submission_id, selected)
        deliveries = db.comment_deliveries(comment["id"])
        if comment["body"] != body or {d["recipient_id"] for d in deliveries} != selected:
            return error("changed_request", 409)
        # Edits are silent. An unavailable old card must not prevent notifications.
        try:
            await core.refresh_all_cards(request.app["bot"], req)
        except Exception:
            log.exception("Не удалось обновить карточки заявки %s", req["id"])
        failed = []
        text = (f"💬 Комментарий к заявке {core._display_no(req)}\n"
                f"Поставщик: {req.get('supplier') or '—'}\n"
                f"Сумма: {req.get('amount') or 0:,.0f}\n\n"
                f"{comment['author_name']} · {comment['author_role']}:\n{comment['body']}")
        for delivery in deliveries:
            if delivery["message_id"]:
                continue
            recipient_id = delivery["recipient_id"]
            try:
                message = await request.app["bot"].send_message(
                    recipient_id, text, disable_notification=False,
                    reply_markup=core.comments_kb(req["id"], "💬 Открыть комментарии"))
                db.mark_comment_delivered(comment["id"], recipient_id, message.message_id)
            except Exception:
                log.warning("Не доставлен комментарий %s получателю %s", comment["id"], recipient_id)
                failed.append(available[recipient_id]["name"])
    return web.json_response({"ok": True, "comment": public_comment(comment), "failed": failed})


def register(app, verify_init_data):
    app["comments_verify"] = verify_init_data
    app["comments_lock"] = asyncio.Lock()
    app.router.add_get("/comments", handle_page)
    app.router.add_post("/comments_history", handle_history)
    app.router.add_post("/comments_post", handle_post)
