import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

import config
import core
import db
import webserver


class PaymentCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db")),
                        patch.object(config, "BOT_TOKEN", "test-token"),
                        patch.object(core, "accountant_ids", return_value=[20, 21]),
                        patch.object(core, "refresh_all_cards", new=AsyncMock()),
                        patch.object(core, "notify_paid", new=AsyncMock()),
                        patch.object(core, "send_driver_card", new=AsyncMock())]
        for p in self.patches:
            p.start()
        db.init_db()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("""INSERT INTO requests (id,request_no,sector,supplier,amount,naryad,
                submitted_by_id,submitted_by_name,submitted_at,status)
                VALUES (1,'REQ-1','Закупки','Металл',12500,'125',30,'Директор','2026-10-08','одобрено')""")
        self.bot = SimpleNamespace()
        self.client = TestClient(TestServer(webserver.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def payload(self, uid=20, **kwargs):
        fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "Анна"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return {"initData": urlencode(fields), "req": 1, "mode": "pay", "category": "materials", **kwargs}

    async def test_open_and_close_do_not_pay(self):
        response = await self.client.post('/payment_category_info', json=self.payload())
        self.assertTrue((await response.json())["allowed"])
        self.assertEqual(db.get_by_id(1)["status"], "одобрено")
        self.assertEqual(db.get_audit_log(1), [])

    async def test_choice_pays_and_records_category_once(self):
        response = await self.client.post('/payment_category_submit', json=self.payload())
        self.assertEqual(response.status, 200)
        req = db.get_by_id(1)
        self.assertEqual((req["status"], req["expense_category"]), ("оплачено", "materials"))
        self.assertEqual(len(db.get_audit_log(1)), 1)
        response = await self.client.post('/payment_category_submit', json=self.payload(uid=21, category="rent"))
        self.assertEqual(response.status, 409)
        self.assertEqual(db.get_by_id(1)["expense_category"], "materials")
        core.notify_paid.assert_awaited_once()
        core.send_driver_card.assert_awaited_once()

    async def test_edit_keeps_payment_time_and_logistics(self):
        await self.client.post('/payment_category_submit', json=self.payload())
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE requests SET status = 'получено'")
        original = db.get_by_id(1)
        response = await self.client.post('/payment_category_submit', json=self.payload(mode="edit", category="transport"))
        self.assertEqual(response.status, 200)
        req = db.get_by_id(1)
        self.assertEqual(req["status"], "получено")
        self.assertEqual(req["paid_at"], original["paid_at"])
        self.assertEqual(req["paid_by"], original["paid_by"])
        self.assertEqual(req["expense_category"], "transport")
        self.assertEqual(db.get_audit_log(1)[-1]["action"], "категория")
        core.notify_paid.assert_awaited_once()

    async def test_rejects_non_accountant_invalid_category_and_unapproved(self):
        response = await self.client.post('/payment_category_submit', json=self.payload(uid=30))
        self.assertEqual(response.status, 403)
        response = await self.client.post('/payment_category_submit', json=self.payload(category="invented"))
        self.assertEqual(response.status, 400)
        response = await self.client.post('/payment_category_submit', json=self.payload(mode="edit"))
        self.assertEqual(response.status, 409)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE requests SET status = 'оформлено'")
        response = await self.client.post('/payment_category_submit', json=self.payload())
        self.assertEqual(response.status, 409)
        self.assertIsNone(db.get_by_id(1)["paid_at"])

    async def test_concurrent_accountants_only_one_payment(self):
        def pay(uid):
            return db.categorize_payment(1, 'materials', uid, str(uid), '2026-10-08T12:00:00')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pay, [20, 21]))
        self.assertEqual(sorted(results), ['already_paid', 'updated'])
        self.assertEqual(len(db.get_audit_log(1)), 1)

    async def test_old_paid_requests_can_be_categorized(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE requests SET status = 'оплачено', paid_at = '2026-09-01T10:00:00'")
        response = await self.client.post('/payment_category_submit', json=self.payload(mode="edit", category="other"))
        self.assertEqual(response.status, 200)
        self.assertEqual(db.get_by_id(1)["paid_at"], '2026-09-01T10:00:00')
        core.notify_paid.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
