import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import core
from telegram.error import BadRequest, TimedOut
import db
import main


class PaymentNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.path_patch.start()
        db.init_db()
        self.req = dict(id=1, request_no="АДМ-071026-03", sector="Закупки",
                        supplier="Orange", amount=1250, naryad="Интернет",
                        description="Интернет за октябрь", status="оплачено",
                        submitted_at="2026-10-07T09:00:00", paid_at="2026-10-07T14:30:00",
                        submitted_by_id=10, submitted_by_name="Иван", payment_file_id=None,
                        photo_file_id="invoice", is_document=1,
                        accountant_msg_id=100, accountant2_msg_id=200)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=300)),
                                   edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot)

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    async def test_v3_migration_preserves_saved_notice_on_restart(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE payment_notices")
            conn.execute("PRAGMA user_version = 3")
        db.init_db()
        db.save_payment_notice(1, 20, 300, "Иван")
        db.expand_payment_notice(1, 20, 300)
        db.init_db()
        self.assertEqual(db.get_payment_notices(1)[0]["expanded"], 1)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.CURRENT_SCHEMA_VERSION)

    async def test_status_follows_payment_and_survives_logistics(self):
        self.assertIn("🟥 Оплачено — 07.10.2026 14:30\nКатегория: Не распределено\n   📄 Без платёжки", core.progress_block(self.req))
        self.req.update(payment_file_id="receipt", status="получено",
                        received_at="2026-10-08T10:00:00")
        text = core.progress_block(self.req)
        self.assertIn("📄 Платёжка прикреплена", text)
        self.assertLess(text.index("Платёжка"), text.index("На складе"))
        self.req["paid_at"] = None
        self.assertNotIn("Платёжка", core.progress_block(self.req))

    async def test_first_request_has_context_and_reply_for_both_accountants(self):
        query = SimpleNamespace(answer=AsyncMock())
        with patch.object(db, "get_payment_pending", return_value=[]), \
                patch.object(db, "add_payment_pending"), \
                patch.object(core, "accountant_ids", return_value=[20, 21]):
            await main._act_needpay(query, self.context, self.req, 1, 10, "now")
        calls = self.bot.send_message.call_args_list
        self.assertEqual(len(calls), 2)
        for call, original in zip(calls, [100, 200]):
            self.assertIn("Orange", call.args[1])
            self.assertIn("1 250", call.args[1])
            self.assertIn("Интернет за октябрь", call.args[1])
            self.assertEqual(call.kwargs["reply_to_message_id"], original)
        self.assertEqual(len(db.get_payment_notices(1)), 2)
        self.bot.send_message.reset_mock()
        with patch.object(db, "get_payment_pending", return_value=[10]):
            await main._act_needpay(query, self.context, self.req, 1, 10, "now")
        self.bot.send_message.assert_not_awaited()

    async def test_open_updates_same_message_and_rejects_non_accountant(self):
        db.save_payment_notice(1, 20, 300, "Иван")
        query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(message_id=300),
                                edit_message_media=AsyncMock())
        with patch.object(core, "is_accountant", return_value=False):
            await main._act_openpay(query, self.context, self.req, 1, 20, "now")
        query.edit_message_media.assert_not_awaited()
        with patch.object(core, "is_accountant", return_value=True):
            await main._act_openpay(query, self.context, self.req, 1, 20, "now")
        self.assertEqual(query.edit_message_media.call_args.kwargs["media"].media, "invoice")
        self.assertEqual(db.get_payment_notices(1)[0]["expanded"], 1)
        self.bot.send_message.assert_not_awaited()

    async def test_attachment_updates_both_notices_and_removes_attach_button(self):
        db.save_payment_notice(1, 20, 300, "Иван")
        db.save_payment_notice(1, 21, 301, "Иван")
        db.expand_payment_notice(1, 21, 301)
        self.req["payment_file_id"] = "receipt"
        with patch.object(core, "is_accountant", return_value=True):
            await core.refresh_payment_notices(self.bot, self.req)
        text_call = self.bot.edit_message_text.call_args.kwargs
        caption_call = self.bot.edit_message_caption.call_args.kwargs
        self.assertEqual(text_call["message_id"], 300)
        self.assertEqual(caption_call["message_id"], 301)
        self.assertIn("✅ Платёжка прикреплена", text_call["text"])
        self.assertIn("📄 Платёжка прикреплена", caption_call["caption"])
        self.assertEqual(text_call["reply_markup"].inline_keyboard[0][0].callback_data, "act:openpay:1")
        self.assertIsNone(caption_call["reply_markup"])
        self.bot.send_message.assert_not_awaited()

    def prepare_approval(self, media=False):
        with sqlite3.connect(db.DB_PATH) as c:
            c.execute("""INSERT INTO requests(id,request_no,sector,supplier,amount,naryad,
                submitted_by_id,submitted_by_name,submitted_at,status,photo_file_id,is_document)
                VALUES(1,'АДМ-081026-02',?,'Metancor',3457,'Расчёт',10,'Автор',
                '2026-10-08T12:00:00','одобрено',?,?)""",
                (config.ADMIN_SECTOR, 'invoice' if media else None, int(media)))
        for uid, mid in [(20,300),(21,301)]:
            db.save_payment_notice(1,uid,mid,'Автор')
            db.expand_payment_notice(1,uid,mid)
        self.bot.edit_message_media=AsyncMock()
        self.bot.send_document=AsyncMock(return_value=SimpleNamespace(message_id=400,document=SimpleNamespace(file_id='invoice')))

    async def test_approval_reuses_expanded_notices_for_both_accountants(self):
        self.prepare_approval()
        with patch.object(core,'accountant_ids',return_value=[20,21]):
            await core.send_accountant_card(self.bot,db.get_by_id(1))
            req=db.get_by_id(1)
            self.assertEqual((req['accountant_msg_id'],req['accountant2_msg_id']),(300,301))
            self.assertEqual(db.get_payment_notices(1),[])
            # A subsequent refresh must not replace the primary keyboard with notice buttons.
            await core.refresh_all_cards(self.bot,req)
            await core.send_accountant_card(self.bot,db.get_by_id(1))
        self.bot.send_message.assert_not_awaited()
        self.assertIn('Оплатить', self.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text)

    async def test_media_notice_is_promoted_without_resending_invoice(self):
        self.prepare_approval(media=True)
        with patch.object(core,'accountant_ids',return_value=[20,21]):
            await core.send_accountant_card(self.bot,db.get_by_id(1))
        self.assertEqual(self.bot.edit_message_media.await_count,2)
        self.bot.send_document.assert_not_awaited()
        self.assertEqual(db.get_by_id(1)['accountant_msg_id'],300)

    async def test_missing_notice_falls_back_but_timeout_does_not_duplicate(self):
        self.prepare_approval()
        self.bot.edit_message_text.side_effect=[BadRequest('Message to edit not found'),TimedOut()]
        with patch.object(core,'accountant_ids',return_value=[20,21]):
            await core.send_accountant_card(self.bot,db.get_by_id(1))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.args[0],20)
        self.assertIsNone(db.get_by_id(1)['accountant2_msg_id'])

    async def test_unchanged_card_is_bound_without_sending_new_message(self):
        self.prepare_approval()
        self.bot.edit_message_text.side_effect=BadRequest('Message is not modified')
        with patch.object(core,'accountant_ids',return_value=[20,21]):
            await core.send_accountant_card(self.bot,db.get_by_id(1))
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(db.get_payment_notices(1),[])

    async def test_upload_refreshes_cards_after_saving_receipt(self):
        self.context.user_data = {"attach_req_id": 1, "payment_prompt_msg_id": 400}
        self.req["payment_file_id"] = "receipt"
        update = SimpleNamespace(
            message=SimpleNamespace(photo=[], document=SimpleNamespace(
                mime_type="application/pdf", file_id="receipt"), reply_text=AsyncMock()),
            effective_user=SimpleNamespace(id=20, full_name="Бухгалтер"))
        with patch.object(db, "get_by_id", return_value=self.req), \
                patch.object(db, "set_payment_file") as save, \
                patch.object(db, "log_action"), \
                patch.object(core, "deliver_payment_to_pending", new=AsyncMock(return_value={10})), \
                patch.object(core, "refresh_all_cards", new=AsyncMock()) as refresh:
            await main.attach_file_received(update, self.context)
            save.assert_called_once_with(1, "receipt", 1)
            refresh.assert_awaited_once_with(self.bot, self.req)
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs["message_id"], 400)
        self.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
