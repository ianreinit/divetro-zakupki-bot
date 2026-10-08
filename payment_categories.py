"""Accountant-only payment category selection."""
from pathlib import Path
from aiohttp import web

import comments
import config
import core


async def page(request):
    return web.FileResponse(Path(__file__).parent / "webapp" / "payment_category.html")


async def authorized(request):
    data, user, req, failure = await comments.authenticate(request)
    if failure is not None:
        return data, user, req, failure
    if not core.is_accountant(int(user["id"])):
        return data, user, req, comments.error("not_allowed", 403)
    if data.get("mode", "pay") not in ("pay", "edit"):
        return data, user, req, comments.error("bad_request", 400)
    return data, user, req, None


async def info(request):
    data, user, req, failure = await authorized(request)
    if failure is not None:
        return failure
    edit = data.get("mode") == "edit"
    allowed = (bool(req.get("paid_at")) and req["status"] in ("оплачено", "в_пути", "получено")) if edit else req["status"] == "одобрено"
    return web.json_response({"ok": True, "allowed": allowed,
        "request": {"number": core._display_no(req), "supplier": req.get("supplier") or "—",
                    "amount": req.get("amount") or 0, "category": req.get("expense_category"),
                    "status": req["status"]},
        "categories": config.EXPENSE_CATEGORIES})


async def submit(request):
    data, user, req, failure = await authorized(request)
    if failure is not None:
        return failure
    category = data.get("category")
    if not isinstance(category, str) or category not in config.EXPENSE_CATEGORIES:
        return comments.error("invalid_category", 400)
    name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p) or str(user["id"])
    result = await core.apply_categorized_payment(request.app["bot"], req["id"], category,
        int(user["id"]), name, edit=data.get("mode") == "edit")
    if result not in ("updated", "unchanged"):
        return comments.error(result, 409 if result != "not_allowed" else 403)
    return web.json_response({"ok": True, "category": config.EXPENSE_CATEGORIES[category]})


def register(app):
    app.router.add_get("/payment_category", page)
    app.router.add_post("/payment_category_info", info)
    app.router.add_post("/payment_category_submit", submit)
