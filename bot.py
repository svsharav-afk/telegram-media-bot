import os
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web, ClientSession, ClientTimeout

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LOG_FILE = "bot_activity.log"

if ADMIN_ID == 0:
    print("⚠️ ADMIN_ID не установлен! Модерация не будет работать")

# === МИДЛВЕР ДЛЯ СБОРКИ И МОДЕРАЦИИ ===
class MediaModerationMiddleware:
    def __init__(self, bot=None):
        self.buffers = {}
        self.bot = bot
        self.CLEANUP_TIMEOUT = 300  # 5 минут

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
            album = all_media[i:i + 10]
            media_group = MediaGroupBuilder()
            for msg in album:
                if msg.photo:
                    media_group.add_photo(media=msg.photo[-1].file_id)
                elif msg.video:
                    media_group.add_video(media=msg.video.file_id)

            try:
                await album[0].answer_media_group(
                    media=media_group.build()
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
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] User {user_id} (@{username}): "
                        f"processed {total} files "
                        f"({photos} photos, {videos} videos)\n")
        except Exception as e:
            print(f"Ошибка записи в лог: {e}")

    async def __call__(self, handler, message: Message, data):
        if message.from_user.id == ADMIN_ID:
            if message.text == "/admin":
                await self._show_admin_panel(message)
                return
            elif message.text == "/logs":
                await self._show_logs(message)
                return

        has_media = message.photo or message.video
        if has_media:
            user_id = message.from_user.id
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                asyncio.create_task(self._delayed_processing(user_id))

            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
                return

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

    async def _cleanup_inactive_buffers(self):
        await asyncio.sleep(self.CLEANUP_TIMEOUT)
        current_time = datetime.now().timestamp()
        inactive_users = []
        for user_id, buffer in self.buffers.items():
            if buffer and (current_time - buffer[-1].date.timestamp()) > self.CLEANUP_TIMEOUT:
                inactive_users.append(user_id)
        for user_id in inactive_users:
            del self.buffers[user_id]
            print(f"Очищен неактивный буфер пользователя {user_id}")

# === ЗАПУСК БОТА ===
async def main():
    if not BOT_TOKEN:
        print("❌ ОШИБКА: BOT_TOKEN не установлен! Проверьте переменные окружения.")
        return

    # Настройка таймаута через aiohttp
    timeout = ClientTimeout(total=30)
    session = ClientSession(timeout=timeout)

    bot = Bot(
        token=BOT_TOKEN,
        parse_mode="HTML",
        session=session
    )
    dp = Dispatcher()

    # Сброс зависших апдейтов перед установкой вебхука
    await bot.delete_webhook(drop_pending_updates=True)

    middleware = MediaModerationMiddleware(bot=bot)
    dp.message.middleware(middleware)

    @dp.message(F.text == "/start")
    async def cmd_start(message: Message):
        await message.answer(
            "Create albums from forwarded media!\n\n"
            "Features ✨\n"
            "• Auto creation, just forward all the items at once and the bot will reply with a nice media album.\n"
            "• Images and videos supported."
        )

    @dp.message(F.text == "/help")
    async def cmd_help(message: Message):
        await message.answer(
            "How to use 🛠\n\n"
            "1. Send photos and videos one by one or in groups.\n"
            "2. The bot will automatically collect them into albums of 10 items.\n"
            "3. You'll get the result 1.5 seconds after the last file.\n\n"
            "📌 Example:\n"
            "You sent 19 photos → bot sends 2 albums: (10 + 9)\n\n"
            "⚠️ Important: send as photo/video, not as file."
        )

    @dp.message()
    async def handle_all(message: Message):
        pass

    port = int(os.getenv("PORT", "10000"))
    webhook_path = "/"
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    async def on_shutdown(app):
        await bot.session.close()

    app.on_cleanup.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    # === КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ ===
    # RENDER_EXTERNAL_HOSTNAME не работает на бесплатном тарифе
    # Используйте URL сервиса из панели управления Render
    SERVICE_URL = "https://telegram-media-bot-1xox.onrender.com"  # ← ЗАМЕНИТЕ НА ВАШ URL
    await bot.set_webhook(f"{SERVICE_URL}/")
    print(f"🌍 Webhook установлен: {SERVICE_URL}")
    print(f"✅ Бот запущен на порту {port}")

    asyncio.create_task(middleware._cleanup_inactive_buffers())
    await asyncio.Event().wait()


if __name__ == "__main__":
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w", encoding="utf-8").close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен вручную")
    except Exception as e:
        print(f"Критическая ошибка: {e}")