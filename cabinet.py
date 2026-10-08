"""Role-scoped employee cabinet. No client-supplied roles or financial totals."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from aiohttp import web

import comments
import config
import core
import db

PAID = {'оплачено', 'в_пути', 'получено'}
TASKS = {
    'approve': 'На одобрение', 'pay': 'Ожидают оплаты',
    'receipts': 'Платёжки по запросу', 'need': 'Оформить заявки',
    'collect': 'Можно забирать', 'receive': 'Принять на склад',
    'active': 'В работе', 'mine': 'Мои заявки', 'uncategorized': 'Без категории',
}


def allowed(uid):
    person = db.get_person(uid)
    return any(check(uid) for check in (core.is_admin, core.is_director, core.is_accountant,
        core.is_buyer, core.is_driver, core.is_warehouse, core.is_employee)) or bool(person and person['sector'])


def task_keys(uid):
    if core.is_admin(uid):
        return list(TASKS)
    keys = []
    for check, tasks in [(core.is_director, ['approve', 'pay']),
        (core.is_accountant, ['pay', 'receipts', 'uncategorized']),
        (core.is_buyer, ['need', 'approve', 'pay', 'collect']),
        (core.is_driver, ['collect', 'receive']), (core.is_warehouse, ['receive'])]:
        if check(uid):
            keys.extend(tasks)
    return list(dict.fromkeys(keys + ['active', 'mine']))


def matches(req, key, uid):
    status = req['status']
    if key == 'approve': return status in ('оформлено', 'отправлено')
    if key == 'pay': return status == 'одобрено' and not req.get('paid_at')
    if key == 'receipts': return bool(req.get('payment_pending_for')) and not req.get('payment_file_id')
    if key == 'need': return status == 'потребность'
    if key == 'collect': return status == 'оплачено' and req['sector'] != config.ADMIN_SECTOR
    if key == 'receive': return status == 'в_пути' and req['sector'] != config.ADMIN_SECTOR
    if key == 'uncategorized': return is_paid(req) and not req.get('expense_category')
    if key == 'mine': return req['submitted_by_id'] == uid
    return status not in ('получено', 'отклонено') and not (req['sector'] == config.ADMIN_SECTOR and is_paid(req))


def is_paid(req):
    return bool(req.get('paid_at')) and req['status'] in PAID


def total(rows):
    return float(sum((Decimal(str(r['amount'] or 0)) for r in rows), Decimal(0)))


def period_start(period, now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week': return today - timedelta(days=today.weekday())
    if period == 'month': return today.replace(day=1)
    if period == 'year': return today.replace(month=1, day=1)
    raise ValueError('period')


def paid_in_period(req, start, now):
    if not is_paid(req): return False
    try:
        when = datetime.fromisoformat(req['paid_at'])
        if when.tzinfo is None: when = when.replace(tzinfo=config.TZ)
        return start <= when <= now
    except (ValueError, TypeError):
        return False


def public(req):
    return {**{k: req.get(k) for k in ('id', 'sector', 'supplier', 'amount', 'order_no', 'naryad',
        'description', 'status', 'submitted_at', 'needed_by', 'paid_at', 'submitted_by_name')},
        'number': req['request_no'],
        'analytics_excluded': bool(req.get('analytics_excluded')),
        'category': config.EXPENSE_CATEGORIES.get(req.get('expense_category'), 'Не распределено'),
        'receipt': 'Платёжка прикреплена' if req.get('payment_file_id') else
            'Платёжка запрошена' if req.get('payment_pending_for') else 'Без платёжки'}


async def authenticate(request):
    try:
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get('initData'), str): raise ValueError
        init = request.app['comments_verify'](data['initData'], config.BOT_TOKEN)
        if init is None: return None, None, comments.error('auth_failed', 403)
        user = json.loads(init.get('user', '{}'))
        uid = int(user['id'])
    except (ValueError, TypeError, KeyError):
        return None, None, comments.error('bad_request', 400)
    if not allowed(uid): return None, None, comments.error('not_allowed', 403)
    return data, uid, None


async def page(request):
    return web.FileResponse(Path(__file__).parent / 'webapp' / 'cabinet.html')


async def data_view(request):
    data, uid, failure = await authenticate(request)
    if failure is not None: return failure
    view = data.get('view', 'tasks')
    if view == 'notifications':
        if not core.is_accountant(uid): return comments.error('not_allowed', 403)
        try:
            offset = int(data.get('offset', 0))
            if offset < 0: raise ValueError
        except (ValueError, TypeError): return comments.error('bad_request', 400)
        rows = db.accountant_notification_history(uid, offset)
        return web.json_response({'ok': True, 'items': [
            {'id': r['id'], 'description': r['description'], 'created_at': r['created_at'],
             'sent': bool(r['director_message_id'])} for r in rows[:40]],
            'next_offset': offset + 40 if len(rows) > 40 else None})
    query = data.get('query', '')
    field = data.get('field', 'all')
    if not isinstance(query, str) or len(query) > 200 or field not in ('all', 'order', 'supplier'):
        return comments.error('bad_request', 400)
    try:
        offset = int(data.get('offset', 0))
        if offset < 0: raise ValueError
    except (ValueError, TypeError): return comments.error('bad_request', 400)
    # Filter access before counting, searching or aggregating. Never truncate financial totals.
    can_access = comments.access_checker(uid)
    rows = [r for r in db.list_all_requests(limit=-1) if can_access(r)]
    tasks = [{'key': k, 'label': TASKS[k], 'count': sum(matches(r, k, uid) for r in rows)} for k in task_keys(uid)]
    response = {'ok': True, 'role': comments.user_role(uid), 'tasks': tasks, 'notifications': core.is_accountant(uid), 'manage_analytics': core.is_director(uid)}
    if view == 'tasks':
        key = data.get('task') or tasks[0]['key']
        if key not in task_keys(uid): return comments.error('bad_request', 400)
        rows = [r for r in rows if matches(r, key, uid)]
        response['task'] = key
    elif view == 'excluded':
        if not core.is_director(uid): return comments.error('not_allowed', 403)
        rows = [r for r in rows if r.get('analytics_excluded')]
    elif view not in ('search', 'analytics'): return comments.error('bad_request', 400)
    if view != "tasks" and query.strip():
        q = query.strip().casefold()
        fields = {'order': ['order_no'], 'supplier': ['supplier'],
                  'all': ['request_no', 'order_no', 'supplier', 'naryad', 'description']}[field]
        rows = [r for r in rows if any(q in str(r.get(k) or '').casefold() for k in fields)]
    if view == 'analytics':
        now = datetime.now(config.TZ)
        try: start = period_start(data.get('period', 'month'), now)
        except ValueError: return comments.error('bad_request', 400)
        rows = [r for r in rows if not r.get('analytics_excluded') and paid_in_period(r, start, now)]
        group = data.get('group', 'category')
        if group not in ('category', 'supplier', 'order'): return comments.error('bad_request', 400)
        def group_name(r):
            if group == 'category': return config.EXPENSE_CATEGORIES.get(r.get('expense_category'), 'Не распределено')
            return str(r.get('supplier' if group == 'supplier' else 'order_no') or ('Без поставщика' if group == 'supplier' else 'Без заказа')).strip()
        groups = {}
        for r in rows: groups.setdefault(group_name(r), []).append(r)
        response.update(start=start.isoformat(), end=now.isoformat(), total=total(rows),
            groups=sorted([{'name': name, 'amount': total(items), 'count': len(items)} for name, items in groups.items()], key=lambda g: -g['amount']))
        if data.get('drill') is not None:
            if not isinstance(data['drill'], str): return comments.error('bad_request', 400)
            rows = groups.get(data['drill'], [])
    response.update(count=len(rows), paid_total=total([r for r in rows if is_paid(r) and not r.get('analytics_excluded')]),
        waiting_total=total([r for r in rows if matches(r, 'pay', uid) and not r.get('analytics_excluded')]),
        items=[public(r) for r in rows[offset:offset+40]],
        next_offset=offset+40 if offset+40 < len(rows) else None)
    return web.json_response(response)


def links(uid, req):
    result = [{'label': 'Комментарии', 'url': f'comments?req={req["id"]}'}]
    if core.is_accountant(uid):
        if req['status'] == 'одобрено': result.insert(0, {'label': 'Оплатить · выбрать категорию', 'url': f'payment_category?req={req["id"]}&mode=pay'})
        if is_paid(req):
            result.insert(0, {'label': 'Прикрепить платёжку', 'url': f'pay?req={req["id"]}'})
            result.append({'label': 'Изменить категорию', 'url': f'payment_category?req={req["id"]}&mode=edit'})
    if core.is_buyer(uid) and req['status'] == 'потребность':
        result.insert(0, {'label': 'Оформить заявку', 'url': f'buyer_form?req={req["id"]}'})
    return result


async def detail(request):
    data, uid, failure = await authenticate(request)
    if failure is not None: return failure
    try: req = db.get_by_id(int(data['req']))
    except (ValueError, TypeError, KeyError): return comments.error('bad_request', 400)
    if req is None: return comments.error('not_found', 404)
    if not comments.can_access(uid, req): return comments.error('not_allowed', 403)
    return web.json_response({'ok': True, 'request': public(req), 'progress': core.progress_block(req),
        'links': links(uid, req), 'manage_analytics': core.is_director(uid)})


async def open_card(request):
    """Only an explicit button press sends a current actionable card to its caller."""
    data, uid, failure = await authenticate(request)
    if failure is not None: return failure
    try: req = db.get_by_id(int(data['req']))
    except (ValueError, TypeError, KeyError): return comments.error('bad_request', 400)
    if req is None: return comments.error('not_found', 404)
    if not comments.can_access(uid, req): return comments.error('not_allowed', 403)
    kb = core.kb_needpay_or_none(req)
    if core.is_admin(uid): kb = core.kb_admin(req)
    if core.is_director(uid) and req['status'] == 'оформлено': kb = core.kb_director_approve(req['id'])
    if core.is_accountant(uid) and (req['status'] == 'одобрено' or is_paid(req)): kb = core.kb_accountant(req)
    if core.is_buyer(uid): kb = core.kb_buyer_rejected(req) if req['status'] == 'отклонено' else core.kb_buyer(req)
    if core.is_driver(uid) and req['sector'] != config.ADMIN_SECTOR: kb = core.kb_driver(req)
    if core.is_warehouse(uid) and req['sector'] != config.ADMIN_SECTOR: kb = core.kb_warehouse(req)
    media = req.get('photo_file_id') or req.get('need_photo_file_id')
    document = req.get('is_document') if req.get('photo_file_id') else req.get('need_is_document')
    try:
        await core._send_card(request.app['bot'], uid, media, core.build_full_caption(req), bool(document), reply_markup=kb)
    except Exception:
        return comments.error('send_failed', 502)
    return web.json_response({'ok': True})


async def analytics_visibility(request):
    data, uid, failure = await authenticate(request)
    if failure is not None: return failure
    if not core.is_director(uid): return comments.error('not_allowed', 403)
    if type(data.get('excluded')) is not bool or data.get('confirmed') is not True:
        return comments.error('bad_request', 400)
    try: req_id = int(data['req'])
    except (ValueError, TypeError, KeyError): return comments.error('bad_request', 400)
    # Use the signed identity, never a client-supplied actor or role.
    init = request.app['comments_verify'](data['initData'], config.BOT_TOKEN)
    user = json.loads(init['user'])
    name = ' '.join(p for p in [user.get('first_name'), user.get('last_name')] if p) or str(uid)
    result = db.set_analytics_excluded(req_id, data['excluded'], uid, name)
    if result == 'not_found': return comments.error('not_found', 404)
    return web.json_response({'ok': True, 'excluded': data['excluded'], 'result': result})


def register(app):
    app.router.add_get('/cabinet', page)
    app.router.add_post('/cabinet_analytics_visibility', analytics_visibility)
    app.router.add_post('/cabinet_data', data_view)
    app.router.add_post('/cabinet_detail', detail)
    app.router.add_post('/cabinet_open', open_card)
