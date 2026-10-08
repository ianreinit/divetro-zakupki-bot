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

import comments
import config
import core
import db
import webserver


class CommentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db")),
                        patch.object(config, "BOT_TOKEN", "test-token"),
                        patch.object(config, "COMMENTSAPP_URL", "https://example.test/comments"),
                        patch.object(config, "ADMIN_ID", 60),
                        patch.object(core, "director_ids", return_value=[30]),
                        patch.object(core, "accountant_ids", return_value=[20, 21]),
                        patch.object(core, "buyer_ids", return_value=[40]),
                        patch.object(core, "driver_ids", return_value=[50]),
                        patch.object(core, "warehouse_ids", return_value=[70])]
        for p in self.patches:
            p.start()
        db.init_db()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("""INSERT INTO requests
                (id, request_no, sector, supplier, amount, naryad, submitted_by_id, submitted_by_name,
                submitted_at, status, photo_file_id, director_msg_id, accountant_msg_id,
                accountant2_msg_id, driver_msg_id, warehouse_msg_id)
                VALUES (1, 'REQ-1', 'Цех', 'Металл', 12500, '', 10, 'Иван',
                '2026-10-08T09:00:00', 'получено', 'invoice', 300, 200, 210, 500, 700)""")
            conn.execute("UPDATE requests SET notify_message_id = 100")
        for uid, name in [(10, "Иван"), (20, "Анна"), (21, "Мария"), (30, "Директор"), (50, "Водитель")]:
            db.upsert_person(uid, name)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
                                   edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        self.client = TestClient(TestServer(webserver.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def payload(self, uid=20, **kwargs):
        fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "Анна"})}
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
        fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return {"initData": urlencode(fields), "req": 1, "body": "Забрать до 17:00",
                "submission_id": str(uuid4()), "recipients": [], **kwargs}

    async def test_selected_only_sound_and_idempotent(self):
        payload = self.payload(recipients=[20, 30, 50])
        with patch.object(core, "refresh_all_cards", new=AsyncMock()) as refresh:
            response = await self.client.post("/comments_post", json=payload)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["failed"], [])
            refresh.assert_awaited_once()
            response = await self.client.post("/comments_post", json=payload)
            self.assertEqual(response.status, 200)
        self.assertEqual({call.args[0] for call in self.bot.send_message.call_args_list}, {30, 50})
        self.assertEqual(self.bot.send_message.await_count, 2)
        for call in self.bot.send_message.call_args_list:
            self.assertFalse(call.kwargs["disable_notification"])
            self.assertIn("req=1", call.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url)
            self.assertIn("Анна · Бухгалтер", call.args[1])
        self.assertEqual(len(db.list_comments(1)), 1)
        self.assertEqual(db.get_by_id(1)["status"], "получено")

    async def test_without_recipients_updates_cards_silently(self):
        response = await self.client.post("/comments_post", json=self.payload())
        self.assertEqual(response.status, 200)
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(self.bot.edit_message_caption.await_count)
        self.assertIn("Забрать до 17:00", self.bot.edit_message_caption.call_args.kwargs["caption"])
        self.assertEqual(db.comment_summary(1)["total"], 1)

    async def test_admin_privacy_in_history_recipients_and_card_updates(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE requests SET sector = ?, submitted_by_id = 50", (config.ADMIN_SECTOR,))
        for uid in (10, 40, 50, 70, 99):
            response = await self.client.post("/comments_history", json=self.payload(uid))
            self.assertEqual(response.status, 403)
            response = await self.client.post("/comments_post", json=self.payload(uid))
            self.assertEqual(response.status, 403)
        response = await self.client.post("/comments_history", json=self.payload())
        self.assertEqual({p["id"] for p in (await response.json())["recipients"]}, {21, 30, 60})
        response = await self.client.post("/comments_post", json=self.payload(recipients=[50]))
        self.assertEqual(response.status, 403)
        response = await self.client.post("/comments_post", json=self.payload())
        self.assertEqual(response.status, 200)
        self.assertTrue(all(call.kwargs["chat_id"] not in (40, 50, 70)
                            for call in self.bot.edit_message_caption.call_args_list))

    async def test_foreign_access_and_tampered_signature(self):
        for uid in (99,):
            response = await self.client.post("/comments_history", json=self.payload(uid))
            self.assertEqual(response.status, 403)
        payload = self.payload()
        payload["initData"] += "tampered"
        response = await self.client.post("/comments_post", json=payload)
        self.assertEqual(response.status, 403)
        self.assertIsNone(db.comment_summary(1))

    async def test_previous_commenter_can_receive_reply_only_while_authorized(self):
        db.set_person_sector(80, "Цех", "Пётр")
        db.save_comment(1, 80, "Пётр", "Сотрудник", "Комментарий", str(uuid4()), [])
        self.assertIn(80, {person["id"] for person in comments.recipients(db.get_by_id(1), 20)})
        db.clear_person(80)
        self.assertNotIn(80, {person["id"] for person in comments.recipients(db.get_by_id(1), 20)})

    async def test_pagination_and_request_isolation(self):
        for i in range(55):
            db.save_comment(1, 20, "Анна", "Бухгалтер", str(i), str(uuid4()), [])
        db.save_comment(2, 20, "Анна", "Бухгалтер", "Секрет другой заявки", str(uuid4()), [])
        response = await self.client.post("/comments_history", json=self.payload())
        data = await response.json()
        self.assertEqual(len(data["comments"]), 50)
        self.assertEqual(data["comments"][0]["body"], "5")
        response = await self.client.post("/comments_history", json=self.payload(before=data["before"]))
        data = await response.json()
        self.assertEqual([row["body"] for row in data["comments"]], [str(i) for i in range(5)])
        self.assertIsNone(data["before"])

    async def test_failed_recipient_can_retry_without_resending_success(self):
        async def send(uid, *args, **kwargs):
            if uid == 50:
                raise RuntimeError("blocked")
            return SimpleNamespace(message_id=901)
        self.bot.send_message.side_effect = send
        payload = self.payload(recipients=[30, 50])
        response = await self.client.post("/comments_post", json=payload)
        self.assertEqual((await response.json())["failed"], ["Водитель"])
        self.bot.send_message.reset_mock(side_effect=True)
        self.bot.send_message.return_value = SimpleNamespace(message_id=902)
        response = await self.client.post("/comments_post", json=payload)
        self.assertEqual((await response.json())["failed"], [])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_message.call_args.args[0], 50)
        self.assertEqual(db.comment_summary(1)["total"], 1)

    async def test_finished_request_buttons_and_long_caption_preview(self):
        db.save_comment(1, 20, "Анна", "Бухгалтер", "Важный комментарий", str(uuid4()), [])
        req = db.get_by_id(1)
        for keyboard in (core.kb_needpay_or_none(req), core.kb_admin(req), core.kb_warehouse(req), core.kb_buyer(req)):
            self.assertEqual(keyboard.inline_keyboard[-1][0].text, "💬 Комментарии · 1")
        req["description"] = "Длинное описание " * 400
        caption = core._trim_caption(core.build_need_text(req))
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("Важный комментарий", caption)

    async def test_director_approval_buttons_survive_comment_refresh(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE requests SET status = 'оформлено'")
        response = await self.client.post("/comments_post", json=self.payload())
        self.assertEqual(response.status, 200)
        director = next(call.kwargs for call in self.bot.edit_message_caption.call_args_list
                        if call.kwargs["chat_id"] == 30)
        actions = [button.callback_data for row in director["reply_markup"].inline_keyboard for button in row]
        self.assertIn("act:approve:1", actions)
        self.assertIn("act:reject:1", actions)

    async def test_validation_and_migration(self):
        for kwargs in ({"body": " "}, {"body": "a" * 1001}, {"recipients": [99]}, {"recipients": "30"}):
            response = await self.client.post("/comments_post", json=self.payload(**kwargs))
            self.assertIn(response.status, (400, 403))
        self.assertIsNone(db.comment_summary(1))
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE request_comments")
            conn.execute("DROP TABLE comment_deliveries")
            conn.execute("PRAGMA user_version = 5")
        db.init_db()
        self.assertEqual(db.get_by_id(1)["request_no"], "REQ-1")
        self.assertEqual(db.list_comments(1), [])


if __name__ == "__main__":
    unittest.main()
