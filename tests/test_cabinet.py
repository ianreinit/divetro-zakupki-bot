import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
import cabinet
import comments
import config
import core
import db
import main
import webserver


class CabinetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, 'DB_PATH', str(Path(self.tmp.name)/'db.sqlite')),
            patch.object(config, 'BOT_TOKEN', 'test-token'), patch.object(config, 'ADMIN_ID', 10)]
        for name, ids in [('director_ids',[11]),('accountant_ids',[20]),('buyer_ids',[30]),
                          ('driver_ids',[40]),('warehouse_ids',[50]),('employee_ids',[60])]:
            self.patches.append(patch.object(core, name, return_value=ids))
        for p in self.patches: p.start()
        db.init_db()
        now = datetime.now(config.TZ).isoformat()
        with sqlite3.connect(db.DB_PATH) as c:
            for i,sector,status,amount,owner in [(1,'Закупки','одобрено',100,60),
                (2,config.ADMIN_SECTOR,'оплачено',200,11),(3,'Закупки','получено',300,60),
                (4,'Другой сектор','оформлено',400,70),(5,'Закупки','отклонено',500,60)]:
                c.execute('''INSERT INTO requests(id,request_no,sector,status,amount,supplier,naryad,
                    submitted_by_id,submitted_by_name,submitted_at,paid_at,order_no,expense_category)
                    VALUES(?,?,?,?,?,'Металл','Наряд',?,'Автор',?,?,?,?)''',
                    (i,f'REQ-{i}',sector,status,amount,owner,'2025-01-01',now if i in (2,3,5) else None,
                     '125' if i in (1,3) else None, 'materials' if i==3 else None))
            c.execute("UPDATE requests SET payment_pending_for='60' WHERE id=3")
        self.bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)),
            edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        self.client=TestClient(TestServer(webserver.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def payload(self, uid=10, **extra):
        fields={'auth_date':str(int(time.time())), 'user':json.dumps({'id':uid})}
        key=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
        check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
        fields['hash']=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        return {'initData':urlencode(fields),**extra}

    async def get(self, uid=10, **extra):
        r=await self.client.post('/cabinet_data',json=self.payload(uid,**extra))
        self.assertEqual(r.status,200)
        return await r.json()

    async def test_authentication_and_unassigned_users(self):
        for payload in [{'initData':'fake'},self.payload(999)]:
            r=await self.client.post('/cabinet_data',json=payload)
            self.assertEqual(r.status,403)
        r=await self.client.post('/cabinet_data',json={'initData':None})
        self.assertEqual(r.status,400)

    async def test_roles_scope_counts_and_details(self):
        admin=await self.get(view='search')
        self.assertEqual(admin['count'],5)
        buyer=await self.get(30,view='search')
        self.assertEqual(buyer['count'],4)
        employee=await self.get(60,view='search')
        self.assertEqual({r['id'] for r in employee['items']},{1,3,5})
        for path in ['/cabinet_detail','/cabinet_open']:
            r=await self.client.post(path,json=self.payload(60,req=2))
            self.assertEqual(r.status,403)
        self.bot.send_message.assert_not_awaited()

    async def test_paid_date_not_created_date_and_rejected_excluded(self):
        d=await self.get(view='analytics',period='month')
        self.assertEqual(d['total'],500)
        self.assertEqual({g['name']:g['amount'] for g in d['groups']},
                         {'Не распределено':200,'Материалы':300})
        self.assertEqual((await self.get(60,view='analytics'))['total'],300)
        d=await self.get(view='analytics',drill='Материалы')
        self.assertEqual([r['id'] for r in d['items']],[3])
        self.assertTrue(cabinet.paid_in_period({'status':'оплачено','paid_at':'2026-10-01T00:00:00+03:00'},
            datetime(2026,10,1,tzinfo=config.TZ),datetime(2026,10,8,tzinfo=config.TZ)))
        self.assertFalse(cabinet.paid_in_period({'status':'оплачено','paid_at':'broken'},
            datetime.now(config.TZ)-timedelta(days=10),datetime.now(config.TZ)))

    async def test_search_order_and_supplier_totals(self):
        d=await self.get(view='search',query='125',field='order')
        self.assertEqual((d['count'],d['paid_total'],d['waiting_total']),(2,300,100))
        d=await self.get(view='search',query='металл',field='supplier')
        self.assertEqual(d['count'],5)

    async def test_role_tasks_and_only_accountant_gets_payment_link(self):
        d=await self.get(20,task='receipts')
        self.assertEqual([r['id'] for r in d['items']],[3])
        d=await self.get(11,task='approve')
        self.assertEqual([r['id'] for r in d['items']],[4])
        for uid,expect in [(20,True),(10,False),(60,False)]:
            r=await self.client.post('/cabinet_detail',json=self.payload(uid,req=1))
            self.assertEqual(r.status,200)
            links=(await r.json())['links']
            self.assertEqual(any('payment_category' in l['url'] for l in links),expect)
        self.bot.send_message.assert_not_awaited()

    async def test_open_card_only_sends_to_authenticated_caller(self):
        r=await self.client.post('/cabinet_open',json=self.payload(11,req=4,target_uid=999))
        self.assertEqual(r.status,200)
        self.assertFalse((await r.json())['reused'])
        self.assertEqual(self.bot.send_message.call_args.args[0],11)
        kb=self.bot.send_message.call_args.kwargs['reply_markup']
        self.assertEqual(kb.inline_keyboard[0][0].callback_data,'act:approve:4')
        r=await self.client.post('/cabinet_open',json=self.payload(11,req=4))
        self.assertTrue((await r.json())['reused'])
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'],8)

    async def test_pagination_keeps_full_analytics_total(self):
        with sqlite3.connect(db.DB_PATH) as c:
            for i in range(10, 61):
                c.execute("""INSERT INTO requests(request_no,sector,status,amount,supplier,naryad,
                    submitted_by_id,submitted_by_name,submitted_at,paid_at)
                    VALUES(?, 'Закупки','оплачено',10,'Тест','',60,'Автор',?,?)""",
                    (f'PAGE-{i}', datetime.now(config.TZ).isoformat(), datetime.now(config.TZ).isoformat()))
        first = await self.get(view='analytics')
        self.assertEqual(first['total'], 1010)
        self.assertEqual(len(first['items']), 40)
        self.assertEqual(first['next_offset'], 40)
        second = await self.get(view='analytics', offset=40)
        self.assertEqual(second['total'], 1010)
        self.assertEqual(len(second['items']), 13)
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})

    def test_calendar_periods_and_timezone_boundary(self):
        now = datetime(2026, 10, 8, 12, tzinfo=config.TZ)
        self.assertEqual(cabinet.period_start('week',now).day, 5)
        self.assertEqual(cabinet.period_start('month',now).day, 1)
        self.assertEqual(cabinet.period_start('year',now).month, 1)
        start = cabinet.period_start('month', now)
        self.assertTrue(cabinet.paid_in_period({'status':'оплачено', 'paid_at':'2026-09-30T21:30:00+00:00'}, start, now))
        self.assertFalse(cabinet.paid_in_period({'status':'оплачено', 'paid_at':'2026-09-30T20:30:00+00:00'}, start, now))

    async def visibility(self, uid=11, req=3, excluded=True, **extra):
        return await self.client.post('/cabinet_analytics_visibility', json=self.payload(
            uid, req=req, excluded=excluded, confirmed=True, **extra))

    async def test_only_director_can_manage_analytics_including_not_admin(self):
        for uid in (10,20,30,40,50,60):
            response = await self.visibility(uid)
            self.assertEqual(response.status,403)
            response = await self.client.post('/cabinet_data',json=self.payload(uid,view='excluded'))
            self.assertEqual(response.status,403)
        self.assertFalse(db.get_by_id(3)['analytics_excluded'])
        self.assertEqual(db.get_audit_log(3),[])
        for uid, expected in [(11,True),(10,False),(20,False)]:
            response=await self.client.post('/cabinet_detail',json=self.payload(uid,req=3))
            self.assertEqual((await response.json())['manage_analytics'],expected)

    async def test_exclusion_restore_and_audit_preserve_payment_and_history(self):
        original=db.get_by_id(3)
        self.assertEqual((await self.visibility()).status,200)
        self.assertEqual((await self.visibility()).status,200)
        changed=db.get_by_id(3)
        self.assertTrue(changed['analytics_excluded'])
        for key in ('paid_at','paid_by','status','amount','payment_pending_for','expense_category'):
            self.assertEqual(changed[key],original[key])
        self.assertEqual(len(db.get_audit_log(3)),1)
        self.assertEqual(db.get_audit_log(3)[0]['actor_id'],11)
        for group in ('category','supplier','order'):
            d=await self.get(view='analytics',group=group)
            self.assertEqual(d['total'],200)
            self.assertNotIn(3,[r['id'] for r in d['items']])
        # Search still finds the original request, but its amount is not an expense.
        d=await self.get(view='search',field='order',query='125')
        self.assertEqual(d['count'],2)
        self.assertEqual(d['paid_total'],0)
        self.assertEqual((await self.get(11,view='excluded'))['count'],1)
        self.assertEqual((await self.visibility(excluded=False)).status,200)
        self.assertEqual((await self.get(view='analytics'))['total'],500)
        self.assertEqual((await self.get(11,view='excluded'))['count'],0)
        self.assertEqual(len(db.get_audit_log(3)),2)
        self.bot.send_message.assert_not_awaited()

    async def test_excluded_pending_request_is_not_an_actionable_task_for_any_role(self):
        self.assertEqual((await self.get(11,task='pay'))['count'],1)
        self.assertEqual((await self.get(10,task='pay'))['count'],1)
        self.assertEqual((await self.visibility(req=1)).status,200)
        for uid in (10,11,20,30):
            result=await self.get(uid,task='pay')
            self.assertEqual(result['count'],0)
            self.assertEqual(result['items'],[])
        # It remains discoverable as history and can be restored by the director.
        self.assertIn(1,[r['id'] for r in (await self.get(11,view='search'))['items']])
        self.assertEqual((await self.get(11,view='excluded'))['count'],1)

    async def test_visibility_requires_confirmation_and_valid_request(self):
        for extra in ({'excluded':'false','confirmed':True},{'excluded':True,'confirmed':False},
                      {'excluded':True}):
            response=await self.client.post('/cabinet_analytics_visibility',json=self.payload(11,req=3,**extra))
            self.assertEqual(response.status,400)
        self.assertEqual((await self.visibility(req=999)).status,404)
        self.assertFalse(db.get_by_id(3)['analytics_excluded'])

    async def test_v9_migration_preserves_existing_requests(self):
        with sqlite3.connect(db.DB_PATH) as c:
            c.execute('ALTER TABLE requests DROP COLUMN analytics_excluded')
            c.execute('PRAGMA user_version=8')
        db.init_db()
        self.assertEqual(len(db.list_all_requests()),5)
        self.assertFalse(db.get_by_id(3)['analytics_excluded'])
        self.assertEqual((await self.get(view='analytics'))['total'],500)

    async def test_access_snapshot_matches_existing_rules_and_refreshes_roles(self):
        rows=db.list_all_requests(-1)
        for uid in (10,11,20,30,40,50,60,999):
            check=comments.access_checker(uid)
            self.assertEqual([check(r) for r in rows], [comments.can_access(uid,r) for r in rows])
        original=sqlite3.connect
        with patch('sqlite3.connect',side_effect=original) as connections:
            check=comments.access_checker(60)
            for r in rows*200: check(r)
            self.assertLess(connections.call_count,15)
        with patch.object(core,'director_ids',return_value=[60]):
            self.assertTrue(comments.access_checker(60)(db.get_by_id(2)))
        self.assertFalse(comments.access_checker(60)(db.get_by_id(2)))

    async def test_form_bootstrap_served_and_html_does_not_block_on_external_sdk(self):
        response=await self.client.get('/telegram_bootstrap.js')
        self.assertEqual(response.status,200)
        self.assertIn('max-age',response.headers['Cache-Control'])
        for path in ('/cabinet','/comments','/payment_category','/notify','/form','/pay',
                     '/buyer_form','/buyer_request_form','/admin_request'):
            response=await self.client.get(path)
            html=await response.text()
            self.assertIn('defer src="telegram_bootstrap.js?v=1"',html)
            self.assertIn('await window.telegramReady',html)
            self.assertNotIn('<script src="https://telegram.org',html)

    async def test_director_can_open_both_creation_forms_from_cabinet(self):
        for view in ('tasks','search','analytics'):
            result=await self.get(11,view=view)
            self.assertEqual([a['url'] for a in result['create_actions']],['form','admin_request'])
        result=await self.get(60)
        self.assertEqual(result['create_actions'],[])

    async def test_menu_available_to_admin_and_accountant(self):
        bot=SimpleNamespace(set_chat_menu_button=AsyncMock(),delete_my_commands=AsyncMock(),set_my_commands=AsyncMock())
        with patch.object(config,'CABINET_URL','https://example.com/cabinet'),patch.object(config,'NOTIFYAPP_URL','https://example.com/notify'):
            await main.apply_menu(bot,10)
            self.assertEqual(bot.set_chat_menu_button.call_args.kwargs['menu_button'].text,'Личный кабинет')
            await main.apply_menu(bot,20)
            self.assertIn('cabinet',[c.command for c in bot.set_my_commands.call_args.args[0]])
            self.assertEqual(bot.set_chat_menu_button.call_args.kwargs['menu_button'].text,'Уведомить')
