import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web, ClientSession, ClientTimeout

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LOG_FILE = "bot_activity.log"

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if ADMIN_ID == 0:
    logger.warning("⚠️ ADMIN_ID не установлен! Модерация не будет работать")

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
            logger.error(f"Ошибка пересылки: {e}")

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
                await album[0].answer_media_group(media=media_group.build())
                photo_count = sum(1 for m in album if m.photo)
                video_count = sum(1 for m in album if m.video)
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"User {user_id} (@{album[0].from_user.username or 'unknown'}): "
                            f"processed {len(album)} files ({photo_count} photos, {video_count} videos)")
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")

    async def __call__(self, handler, message: Message, data):
        # Обрабатываем админ-команды
        if message.from_user.id == ADMIN_ID:
            if message.text == "/admin":
                await self._show_admin_panel(message)
                return
            elif message.text == "/logs":
                await self._show_logs(message)
                return

        # Обрабатываем медиа
        has_media = message.photo or message.video
        if has_media:
            user_id = message.from_user.id
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                asyncio.create_task(self._delayed_processing(user_id))
            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
                return  # не передаем дальше по цепочке

        # передаем управление дальше только для не-медиа сообщений
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
        while True:
            await asyncio.sleep(self.CLEANUP_TIMEOUT)
            current_time = datetime.now().timestamp()
            inactive_users = []
            for user_id, buffer in self.buffers.items():
                if buffer and (current_time - buffer[-1].date.timestamp()) > self.CLEANUP_TIMEOUT:
                    inactive_users.append(user_id)
            for user_id in inactive_users:
                del self.buffers[user_id]
                logger.info(f"Очищен неактивный буфер пользователя {user_id}")

# === ЗАПУСК БОТА ===
async def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        return

    timeout = ClientTimeout(total=60.0, connect=15.0, sock_read=30.0, sock_connect=15.0)
    session = ClientSession(timeout=timeout)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"), session=session)
    dp = Dispatcher()

    # Сброс зависших апдейтов
    await bot.delete_webhook(drop_pending_updates=True)

    middleware = MediaModerationMiddleware(bot=bot)
    dp.message.middleware(middleware)

    @dp.message(F.text == "/start")
    async def cmd_start(message: Message):
        welcome_text = (
            "Привет! Я собираю фото и видео в альбомы 📂\n\n"
            "Просто отправь медиа — и я соберу их в альбом."
        )
        await message.answer(welcome_text)

    @dp.message(F.text == "/help")
    async def cmd_help(message: Message):
        help_text = (
            "Как использовать бота:\n"
            "1. Отправляй фото и видео по одному или группой.\n"
            "2. Бот автоматически соберет их в альбомы по 10 файлов.\n"
            "3. Результат придет через ~1.5 секунды после последнего файла."
        )
        await message.answer(help_text)

    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/")
    setup_application(app, dp, bot=bot)

    async def on_shutdown(app):
        await bot.session.close()
        await session.close()
        logger.info("Бот остановлен, сессии закрыты")

    app.on_cleanup.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    # Render бесплатный тариф — используем localhost
    webhook_url = f"https://localhost:{port}/"
    try:
        await bot.set_webhook(webhook_url)
        logger.info(f"🌍 Webhook установлен: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки вебхука: {e}")

    logger.info(f"✅ Бот запущен на порту {port}")
    asyncio.create_task(middleware._cleanup_inactive_buffers())
    await asyncio.Event().wait()


if __name__ == "__main__":
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w", encoding="utf-8").close()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
