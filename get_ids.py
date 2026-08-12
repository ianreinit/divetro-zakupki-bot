"""
Разовый вспомогательный скрипт.

Помогает узнать GROUP_CHAT_ID, а также user_id директора и бухгалтера,
которые нужны в .env.

Как использовать:
1. Добавьте бота в группу закупок.
2. Попросите директора и бухгалтера один раз написать боту /start в личке
   (это также нужно, чтобы бот вообще мог им что-то писать).
3. Кто-нибудь должен написать любое сообщение в группу.
4. Запустите: python get_ids.py
"""
import asyncio

from telegram import Bot

from config import BOT_TOKEN


async def main():
    bot = Bot(BOT_TOKEN)
    updates = await bot.get_updates()
    if not updates:
        print("Обновлений нет. Напишите что-нибудь в группу и боту в личку, затем запустите снова.")
        return
    for u in updates:
        if u.message:
            chat = u.message.chat
            user = u.message.from_user
            print(
                f"chat.id={chat.id}  chat.type={chat.type}  chat.title={chat.title!r}  "
                f"|  from user.id={user.id}  name={user.full_name!r}"
            )


if __name__ == "__main__":
    asyncio.run(main())
