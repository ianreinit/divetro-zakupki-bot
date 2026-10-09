import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import config
import main
import core

class NeedWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_before_type_selection_returns_to_menu_without_saving(self):
        update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(),text='Заказ 123'))
        context=SimpleNamespace(user_data={})
        with patch.object(main,'new_request',new=AsyncMock(return_value=main.ORDER_NO)) as menu:
            self.assertEqual(await main.order_no_received(update,context),main.ORDER_NO)
            menu.assert_awaited_once()
        self.assertNotIn('order_no',context.user_data)

    async def test_missing_sector_is_handled_for_skip_and_file(self):
        context=SimpleNamespace(user_data={'description':'Материалы'},bot=SimpleNamespace())
        query=SimpleNamespace(answer=AsyncMock(),edit_message_text=AsyncMock())
        update=SimpleNamespace(callback_query=query,message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(core,'publish_need',new=AsyncMock()) as publish:
            self.assertEqual(await main.need_skip_photo(update,context),-1)
            self.assertEqual(await main.need_photo_received(update,context),-1)
            publish.assert_not_awaited()
        self.assertIn('/new',query.edit_message_text.call_args.args[0])

    async def test_complete_need_is_published(self):
        user=SimpleNamespace(id=30,full_name='Директор')
        context=SimpleNamespace(user_data={'sector':config.SECTORS[0],'order_no':'123',
            'description':'Материалы','needed_by':'2026-10-15','urgency':'обычная'},bot=SimpleNamespace())
        query=SimpleNamespace(answer=AsyncMock(),edit_message_text=AsyncMock(),from_user=user)
        with patch.object(core,'publish_need',new=AsyncMock()) as publish:
            self.assertEqual(await main.need_skip_photo(SimpleNamespace(callback_query=query),context),-1)
            self.assertEqual(publish.call_args.kwargs['order_no'],'123')
        self.assertEqual(context.user_data,{})

    def test_restart_clears_stale_submission_not_other_work(self):
        context=SimpleNamespace(user_data={'sector':config.ADMIN_SECTOR,'amount':100,'order_no':'old','attach_req_id':2})
        main.reset_submission(context)
        self.assertEqual(context.user_data,{'attach_req_id':2})
