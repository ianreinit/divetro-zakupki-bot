import hashlib
import hmac
import itertools
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import config
import core
import db
import main
import webserver


class AdminWithoutInvoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db")),
                        patch.object(config, "BOT_TOKEN", "test-token"),
                        patch.object(config, "ADMIN_ID", 60),
                        patch.object(config, "COMMENTSAPP_URL", "https://example.test/comments"),
                        patch.object(config, "ADMIN_REQUEST_URL", "https://example.test/admin_request"),
                        patch.object(core, "director_ids", return_value=[30]),
                        patch.object(core, "accountant_ids", return_value=[20, 21]),
                        patch.object(core, "buyer_ids", return_value=[40]),
                        patch.object(core, "driver_ids", return_value=[50])]
        for p in self.patches:
            p.start()
        db.init_db()
        ids = itertools.count(100)
        async def message(*args, **kwargs):
            return SimpleNamespace(message_id=next(ids))
        async def photo(*args, **kwargs):
            return SimpleNamespace(message_id=next(ids), photo=[SimpleNamespace(file_id="invoice-photo")], document=None)
        async def document(*args, **kwargs):
            return SimpleNamespace(message_id=next(ids), document=SimpleNamespace(file_id="invoice-pdf"))
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=message), send_photo=AsyncMock(side_effect=photo),
            send_document=AsyncMock(side_effect=document), edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        self.client = TestClient(TestServer(webserver.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def form(self, uid=30, amount="3000", supplier="Иван Петров", purpose="", file=None):
        fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "Директор"})}
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        form = FormData()
        form.add_field("initData", urlencode(fields), content_type="text/plain")
        form.add_field("supplier", supplier)
        form.add_field("amount", amount)
        form.add_field("naryad", purpose)
        if file:
            form.add_field("file", b"test", filename=file[0], content_type=file[1])
        return form

    async def create_request(self):
        response = await self.client.post("/admin_request_submit", data=self.form())
        self.assertEqual(response.status, 200, await response.text())
        return db.get_by_id(1)

    async def test_supplier_and_amount_only_delivers_text_to_both_accountants(self):
        req = await self.create_request()
        self.assertEqual(req["status"], "одобрено")
        self.assertEqual(req["naryad"], "")
        self.assertIsNone(req["photo_file_id"])
        self.assertTrue(req["accountant_msg_id"])
        self.assertTrue(req["accountant2_msg_id"])
        self.assertTrue(req["notify_message_id"])
        self.assertEqual({call.args[0] for call in self.bot.send_message.call_args_list}, {20, 21, 30, 60})
        for call in self.bot.send_message.call_args_list:
            self.assertIn("Счёт не приложен", call.args[1])
            self.assertIn("Одобрено", call.args[1])
            if call.args[0] in (20, 21):
                self.assertEqual(call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "act:pay:1")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_payment_receipt_and_comments_refresh_text_cards(self):
        req = await self.create_request()
        query = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(full_name="Анна"))
        context = SimpleNamespace(bot=self.bot, user_data={"attach_req_id": 1})
        await main._act_pay(query, context, req, 1, 20, "2026-10-08T12:00:00")
        req = db.get_by_id(1)
        self.assertEqual(req["status"], "оплачено")
        self.assertIn("Без платёжки", self.bot.edit_message_text.call_args.kwargs["text"])
        update = SimpleNamespace(message=SimpleNamespace(photo=[], document=SimpleNamespace(
            file_id="receipt", mime_type="application/pdf"), reply_text=AsyncMock()),
            effective_user=SimpleNamespace(id=20, full_name="Анна"))
        await main.attach_file_received(update, context)
        db.save_comment(1, 20, "Анна", "Бухгалтер", "Оплата подтверждена", "test-comment", [])
        await core.refresh_all_cards(self.bot, db.get_by_id(1))
        texts = [call.kwargs["text"] for call in self.bot.edit_message_text.call_args_list[-4:]]
        self.assertTrue(all("Платёжка прикреплена" in text and "Оплата подтверждена" in text for text in texts))
        self.bot.edit_message_caption.assert_not_awaited()
        self.assertIsNone(db.get_by_id(1)["driver_msg_id"])

    async def test_optional_invoice_still_delivered_as_media(self):
        for file, expected, sender in [(("invoice.pdf", "application/pdf"), "invoice-pdf", self.bot.send_document),
                                       (("invoice.png", "image/png"), "invoice-photo", self.bot.send_photo)]:
            response = await self.client.post("/admin_request_submit", data=self.form(file=file, purpose="Аренда"))
            self.assertEqual(response.status, 200)
            with sqlite3.connect(db.DB_PATH) as conn:
                req_id = conn.execute("SELECT MAX(id) FROM requests").fetchone()[0]
            self.assertEqual(db.get_by_id(req_id)["photo_file_id"], expected)
            for call in sender.call_args_list:
                self.assertNotIn("Счёт не приложен", call.kwargs["caption"])
                self.assertIn("Назначение: Аренда", call.kwargs["caption"])

    async def test_invalid_amounts_and_unauthorized_roles_rejected(self):
        for amount in ("0", "-1", "nan", "inf", "abc"):
            response = await self.client.post("/admin_request_submit", data=self.form(amount=amount))
            self.assertEqual(response.status, 400)
        for uid in (20, 40, 50, 60, 99):
            response = await self.client.post("/admin_request_submit", data=self.form(uid=uid))
            self.assertEqual(response.status, 403)
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(db.get_by_id(1))

    async def test_other_requests_still_require_invoice(self):
        for uid, sector in [(50, config.ADMIN_SECTOR), (30, config.SECTORS[0])]:
            with self.assertRaises(ValueError):
                await core.publish_request(self.bot, sector=sector, supplier="Supplier", amount=100,
                    naryad="", submitter_id=uid, submitter_name="Name")
        self.assertIsNone(db.get_by_id(1))

    async def test_accountant_can_create_regular_request_for_director_approval(self):
        response = await self.client.post("/buyer_request_submit", data=self.form(
            uid=20, file=("invoice.pdf", "application/pdf"), purpose="Наряд 125"))
        self.assertEqual(response.status, 200, await response.text())
        req = db.get_by_id(1)
        self.assertEqual(req["submitted_by_id"], 20)
        self.assertEqual(req["status"], "оформлено")
        self.assertTrue(req["director_msg_id"])
        self.assertIsNone(req["accountant_msg_id"])

    async def test_director_menu_and_wizard_skip(self):
        user = SimpleNamespace(id=30, full_name="Директор")
        update = SimpleNamespace(effective_user=user, message=SimpleNamespace(reply_text=AsyncMock(), text="3000"))
        context = SimpleNamespace(bot=self.bot, user_data={"sector": config.ADMIN_SECTOR, "supplier": "Иван"})
        await main.new_request(update, context)
        buttons = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[-1][0].web_app.url, config.ADMIN_REQUEST_URL)
        self.assertEqual(await main.adm_amount_received(update, context), main.ADM_NARYAD)
        query = SimpleNamespace(from_user=user, answer=AsyncMock(), edit_message_text=AsyncMock())
        update.callback_query = query
        self.assertEqual(await main.adm_skip_purpose(update, context), main.ADM_PHOTO)
        self.assertEqual(await main.adm_skip_invoice(update, context), -1)
        self.assertEqual(db.get_by_id(1)["naryad"], "")
        self.assertEqual(db.get_by_id(1)["status"], "одобрено")


if __name__ == "__main__":
    unittest.main()
