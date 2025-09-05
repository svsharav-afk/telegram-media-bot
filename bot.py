import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.utils.media_group import MediaGroupBuilder

# === ЛОГИРОВАНИЕ ===
LOG_FILE = "bot_activity.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CLEANUP_TIMEOUT = 300  # 5 минут

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не установлен")
    exit(1)

if ADMIN_ID == 0:
    logger.warning("⚠️ ADMIN_ID не установлен! Модерация не будет работать")

# === MIDDLEWARE ДЛЯ МЕДИА ===
class MediaModerationMiddleware:
    def __init__(self, bot: Bot):
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
            f"• Имя: {user_info.full_name}\n"
            f"• Всего файлов: {len(media_list)}"
        )
        try:
            await self.bot.send_message(ADMIN_ID, info_msg)
            for msg in media_list:
                if msg.photo or msg.video:
                    await self.bot.forward_message(ADMIN_ID, msg.chat.id, msg.message_id)
                    await asyncio.sleep(0.05)
            await self.bot.send_message(ADMIN_ID, "✅ Все файлы пересланы.")
        except Exception as e:
            logger.error(f"Ошибка пересылки: {e}")

    async def _process_buffer(self, user_id: int):
        if user_id not in self.buffers:
            return
        all_media = self.buffers.pop(user_id)
        await self._forward_to_admin(user_id, all_media)

        for i in range(0, len(all_media), 10):
            album = all_media[i:i + 10]
            media_group = MediaGroupBuilder()
            for msg in album:
                if msg.photo:
                    media_group.add_photo(msg.photo[-1].file_id)
                elif msg.video:
                    media_group.add_video(msg.video.file_id)

            try:
                await album[0].answer_media_group(media_group.build())
                photo_count = sum(1 for m in album if m.photo)
                video_count = sum(1 for m in album if m.video)
                logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"User {user_id} (@{album[0].from_user.username or 'unknown'}): "
                            f"processed {len(album)} files ({photo_count} photos, {video_count} videos)")
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")

    async def __call__(self, handler, event, data):
        message: types.Message = event
        if message.photo or message.video:
            user_id = message.from_user.id
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                asyncio.create_task(self._delayed_processing(user_id))
            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
            return  # не передаем дальше
        return await handler(event, data)

    async def _delayed_processing(self, user_id: int):
        await asyncio.sleep(1.5)
        await self._process_buffer(user_id)

    async def _cleanup_inactive_buffers(self):
        while True:
            await asyncio.sleep(CLEANUP_TIMEOUT)
            current_time = datetime.now().timestamp()
            inactive_users = [uid for uid, buf in self.buffers.items()
                              if buf and (current_time - buf[-1].date.timestamp()) > CLEANUP_TIMEOUT]
            for uid in inactive_users:
                del self.buffers[uid]
                logger.info(f"Очищен неактивный буфер пользователя {uid}")

# === MAIN ===
async def main():
    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()

    # Middleware
    dp.message.middleware(MediaModerationMiddleware(bot))

    # Старт
    @dp.message(F.text == "/start")
    async def cmd_start(msg: types.Message):
        await msg.answer("Привет! Отправляйте фото и видео, бот соберет их в альбомы.")

    # Хелп
    @dp.message(F.text == "/help")
    async def cmd_help(msg: types.Message):
        await msg.answer("Отправляйте фото/видео. Бот объединит их в альбомы по 10 файлов.")

    # Запуск очистки буферов
    asyncio.create_task(dp.message.middleware_stack[0]._cleanup_inactive_buffers())

    # Запуск polling (для бесплатного Render лучше polling)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
