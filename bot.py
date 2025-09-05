import os
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Должен быть числом
LOG_FILE = "bot_activity.log"

# === МИДЛВЕР ДЛЯ СБОРКИ И МОДЕРАЦИИ ===
class MediaModerationMiddleware:
    def __init__(self, bot=None):
        self.buffers = {}
        self.bot = bot

    async def _forward_to_admin(self, user_id: int, media_list: list):
        if not media_list or not ADMIN_ID:
            return
            
        user_info = media_list[0].from_user
        info_msg = (
            f"📩 Новые файлы от пользователя:\n"
            f"• ID: {user_info.id}\n"
            f"• Username: @{user_info.username or 'не указан'}\n"
            f"• Имя: {user_info.first_name} {user_info.last_name or ''}\n"
            f"• Всего файлов: {len(media_list)}"
        )
        
        try:
            await self.bot.send_message(ADMIN_ID, info_msg)
            for msg in media_list:
                if msg.photo or msg.video:
                    await self.bot.forward_message(
                        chat_id=ADMIN_ID,
                        from_chat_id=msg.chat.id,
                        message_id=msg.message_id
                    )
                    await asyncio.sleep(0.1)
            await self.bot.send_message(ADMIN_ID, "✅ Все файлы пересланы.")
        except Exception as e:
            print(f"Ошибка пересылки: {e}")

    async def _process_buffer(self, user_id: int):
        if user_id not in self.buffers:
            return
            
        all_media = self.buffers[user_id]
        del self.buffers[user_id]
        
        await self._forward_to_admin(user_id, all_media)
        
        for i in range(0, len(all_media), 10):
            album = all_media[i:i+10]
            media_group = MediaGroupBuilder()
            for msg in album:
                if msg.photo:
                    media_group.add_photo(media=msg.photo[-1].file_id)
                elif msg.video:
                    media_group.add_video(media=msg.video.file_id)
            
            try:
                await album[0].answer_media_group(
                    media=media_group.build(),
                    request_timeout=120
                )
                photo_count = sum(1 for m in album if m.photo)
                video_count = sum(1 for m in album if m.video)
                self._log_activity(
                    album[0].from_user.id,
                    album[0].from_user.username or "unknown",
                    len(album),
                    photo_count,
                    video_count
                )
            except Exception as e:
                print(f"Ошибка отправки: {e}")

    def _log_activity(self, user_id, username, total, photos, videos):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] User {user_id} (@{username}): "
                    f"processed {total} files "
                    f"({photos} photos, {videos} videos)\n")

    async def __call__(self, handler, message: Message, data):
        if message.from_user.id == ADMIN_ID:
            if message.text == "/admin":
                await self._show_admin_panel(message)
                return await handler(message, data)
            elif message.text == "/logs":
                await self._show_logs(message)
                return await handler(message, data)
        
        has_media = message.photo or message.video
        if has_media:
            user_id = message.from_user.id
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                asyncio.create_task(self._delayed_processing(user_id))
            
            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
        
        return await handler(message, data)
    
    async def _delayed_processing(self, user_id: int):
        await asyncio.sleep(1.5)
        await self._process_buffer(user_id)
    
    async def _show_admin_panel(self, message: Message):
        active_users = len(self.buffers)
        total_files = sum(len(buf) for buf in self.buffers.values())
        status = f"📊 Статус бота:\n• Активных пользователей: {active_users}\n• Файлов в обработке: {total_files}"
        await message.answer(status)
    
    async def _show_logs(self, message: Message):
        if not os.path.exists(LOG_FILE):
            await message.answer("Логи пусты")
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = f.readlines()[-20:]
            log_text = "📋 Последние операции:\n\n" + "".join(logs)
            await message.answer(log_text[:4000])
        except Exception as e:
            await message.answer(f"Ошибка: {str(e)}")

# === КОМАНДЫ БОТА ===
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    """Приветственное сообщение"""
    welcome_text = (
        "Create albums from forwarded media!\n\n"
        "Features ✨\n"
        "• Auto creation, just forward all the items at once and the bot will reply with a nice media album.\n"
        "• Images and videos supported."
    )
    await message.answer(welcome_text)

@dp.message(F.text == "/help")
async def cmd_help(message: Message):
    """Краткая помощь"""
    help_text = (
        "How to use 🛠\n\n"
        "1. Send photos and videos one by one or in groups.\n"
        "2. The bot will automatically collect them into albums of 10 items.\n"
        "3. You'll get the result 1.5 seconds after the last file.\n\n"
        "📌 Example:\n"
        "You sent 19 photos → bot sends 2 albums: (10 + 9)\n\n"
        "⚠️ Important: send as photo/video, not as file."
    )
    await message.answer(help_text)

@dp.message()
async def handle_all(message: Message):
    """Пропускаем команды /start и /help, остальное обрабатывает middleware"""
    if message.text and message.text.startswith("/"):
        return
    pass

# === ЗАПУСК БОТА ===
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.message.middleware(MediaModerationMiddleware(bot=bot))

    # Получаем порт от Render
    port = int(os.getenv("PORT", "10000"))
    
    # Настраиваем вебхук
    webhook_path = "/"
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    print(f"Бот запущен на порту {port}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    # Создаем файл логов, если его нет
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w", encoding="utf-8").close()
    
    asyncio.run(main())