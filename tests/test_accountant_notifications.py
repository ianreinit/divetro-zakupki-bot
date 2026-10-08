import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

import config
import core
import db
import main
import webserver


class AccountantNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db")),
                        patch.object(config, "BOT_TOKEN", "test-token"),
                        patch.object(core, "is_accountant", side_effect=lambda uid: uid == 20),
                        patch.object(core, "director_id", return_value=30)]
        for p in self.patches:
            p.start()
        db.init_db()
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
                                   set_chat_menu_button=AsyncMock(), set_my_commands=AsyncMock(),
                                   delete_my_commands=AsyncMock())
        self.client = TestClient(TestServer(webserver.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def payload(self, uid=20):
        fields = {"auth_date": str(int(time.time())),
                  "user": json.dumps({"id": uid, "first_name": "Анна"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return {"initData": urlencode(fields), "description": "Orange — 1 250 лей, интернет",
                "submission_id": str(uuid4())}

    async def test_delivery_saved_without_creating_procurement_request(self):
        payload = self.payload()
        response = await self.client.post("/notify_payment", json=payload)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        call = self.bot.send_message.call_args_list[0]
        self.assertEqual(call.args[0], 30)
        self.assertIn("🟥 Оплачено\nБухгалтер: Анна", call.args[1])
        self.assertIn(payload["description"], call.args[1])
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
            row = conn.execute("SELECT accountant_id, director_message_id, description FROM accountant_notifications").fetchone()
            self.assertEqual(row, (20, 100, payload["description"]))
        response = await self.client.post("/notify_payment", json=payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bot.send_message.call_args_list[1].args[0], 20)

    async def test_copy_retry_does_not_resend_to_director(self):
        payload = self.payload()
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=101), RuntimeError('offline')]
        response = await self.client.post('/notify_payment', json=payload)
        self.assertEqual((await response.json())['error'], 'copy_failed')
        self.bot.send_message.side_effect = None
        response = await self.client.post('/notify_payment', json=payload)
        self.assertEqual(response.status, 200)
        self.assertEqual([c.args[0] for c in self.bot.send_message.call_args_list], [30,20,20])
        response = await self.client.post('/cabinet_data', json={**payload, 'view':'notifications'})
        history = await response.json()
        self.assertEqual(len(history['items']),1)
        self.assertTrue(history['items'][0]['sent'])
        response = await self.client.post('/cabinet_data', json={**self.payload(99), 'view':'notifications'})
        self.assertEqual(response.status,403)

    async def test_history_includes_old_records_and_excludes_other_accountants(self):
        mine = db.create_accountant_notification(20, 'Анна', 'Ранее отправлено', 30, str(uuid4()))
        db.mark_accountant_notification_sent(mine['id'], 88)
        db.create_accountant_notification(21, 'Другой', 'Чужое', 30, str(uuid4()))
        response = await self.client.post('/cabinet_data', json={**self.payload(), 'view':'notifications'})
        items = (await response.json())['items']
        self.assertEqual([r['description'] for r in items], ['Ранее отправлено'])
        self.assertTrue(items[0]['sent'])
        self.bot.send_message.assert_not_awaited()

    async def test_rejects_non_accountant_and_invalid_signature(self):
        response = await self.client.post("/notify_payment", json=self.payload(uid=99))
        self.assertEqual(response.status, 403)
        payload = self.payload()
        payload["initData"] += "tampered"
        response = await self.client.post("/notify_payment", json=payload)
        self.assertEqual(response.status, 403)
        self.bot.send_message.assert_not_awaited()

    async def test_validation_and_missing_director(self):
        for description in ["  ", "a" * 501, None]:
            payload = self.payload()
            payload["description"] = description
            response = await self.client.post("/notify_payment", json=payload)
            self.assertEqual(response.status, 400)
        with patch.object(core, "director_id", return_value=0):
            response = await self.client.post("/notify_payment", json=self.payload())
            self.assertEqual(response.status, 409)
        self.bot.send_message.assert_not_awaited()

    async def test_failed_send_does_not_report_success_and_can_retry(self):
        self.bot.send_message.side_effect = RuntimeError("Unavailable")
        payload = self.payload()
        response = await self.client.post("/notify_payment", json=payload)
        self.assertEqual(response.status, 502)
        self.assertFalse((await response.json())["ok"])
        self.bot.send_message.side_effect = None
        response = await self.client.post("/notify_payment", json=payload)
        self.assertEqual(response.status, 200)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accountant_notifications").fetchone()[0], 1)

    async def test_menu_only_changes_for_accountant(self):
        with patch.object(config, "NOTIFYAPP_URL", "https://example.test/divetro/notify"), \
                patch.object(config, "WEBAPP_URL", "https://example.test/divetro/form"), \
                patch.object(main.cabinet, "allowed", return_value=True), \
                patch.object(config, "CABINET_URL", "https://example.test/divetro/cabinet"):
            await main.apply_menu(self.bot, 20)
            menu = self.bot.set_chat_menu_button.call_args.kwargs["menu_button"]
            self.assertEqual(menu.text, "Уведомить")
            self.assertEqual(menu.web_app.url, "https://example.test/divetro/notify")
            await main.apply_menu(self.bot, 99)
            self.assertEqual(self.bot.set_chat_menu_button.call_args.kwargs["menu_button"].text, "Личный кабинет")

    async def test_form_is_available(self):
        response = await self.client.get("/notify")
        self.assertEqual(response.status, 200)
        self.assertIn("Отправить директору", await response.text())

    async def test_accountant_start_and_new_offer_only_notification_and_request(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=20, full_name="Анна"),
                                 message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(bot=self.bot, user_data={})
        with patch.object(config, "NOTIFYAPP_URL", "https://example.test/notify"), \
                patch.object(config, "BUYER_REQUEST_URL", "https://example.test/buyer_request_form"), \
                patch.object(config, "CABINET_URL", "https://example.test/cabinet"):
            for handler in (main.start, main.new_request, main.new_wizard):
                await handler(update, context)
                kb = update.message.reply_text.call_args.kwargs["reply_markup"]
                self.assertEqual([row[0].text for row in kb.inline_keyboard], ["Уведомить", "📝 Новая заявка", "Личный кабинет"])
                self.assertEqual(kb.inline_keyboard[1][0].web_app.url, config.BUYER_REQUEST_URL)

    async def test_migration_from_v4_preserves_existing_notices(self):
        db.save_payment_notice(1, 20, 50, "Иван")
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE accountant_notifications")
            conn.execute("PRAGMA user_version = 4")
        db.init_db()
        self.assertEqual(db.get_payment_notices(1)[0]["message_id"], 50)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.CURRENT_SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accountant_notifications").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
