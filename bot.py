import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, DefaultBotProperties
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web, ClientSession, ClientTimeout

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LOG_FILE = "bot_activity.log"

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
                await album[0].answer_media_group(
                    media=media_group.build(),
                    request_timeout=60  # Увеличенный таймаут для отправки
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
                logger.error(f"Ошибка отправки: {e}")

    def _log_activity(self, user_id, username, total, photos, videos):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] User {user_id} (@{username}): "
                        f"processed {total} files "
                        f"({photos} photos, {videos} videos)\n")
        except Exception as e:
            logger.error(f"Ошибка записи в лог: {e}")

    async def __call__(self, handler, message: Message, data):
        # 1. Проверяем админ-команды
        if message.from_user.id == ADMIN_ID:
            if message.text == "/admin":
                await self._show_admin_panel(message)
                return
            elif message.text == "/logs":
                await self._show_logs(message)
                return

        # 2. Проверяем, есть ли фото или видео
        has_media = message.photo or message.video
        if has_media:
            user_id = message.from_user.id
            # Инициализируем буфер для пользователя
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                # Запускаем обработку через 1.5 сек
                asyncio.create_task(self._delayed_processing(user_id))
            
            # Добавляем сообщение в буфер (без дублей)
            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
                # ВАЖНО: НЕ передаем сообщение дальше по цепочке
                return
        
        # Передаем управление дальше только для не-медиа сообщений
        return await handler(message, data)
    
    async def _delayed_processing(self, user_id: int):
        """Задержанная обработка буфера"""
        await asyncio.sleep(1.5)
        await self._process_buffer(user_id)
    
    async def _show_admin_panel(self, message: Message):
        """Показывает админ-панель"""
        active_users = len(self.buffers)
        total_files = sum(len(buf) for buf in self.buffers.values())
        status = f"📊 Статус бота:\n• Активных пользователей: {active_users}\n• Файлов в обработке: {total_files}"
        await message.answer(status)
    
    async def _show_logs(self, message: Message):
        """Показывает последние логи"""
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
        """Очищает старые буферы для предотвращения утечки памяти"""
        await asyncio.sleep(self.CLEANUP_TIMEOUT)
        
        current_time = datetime.now().timestamp()
        inactive_users = []
        
        for user_id, buffer in self.buffers.items():
            # Если буфер не обновлялся более CLEANUP_TIMEOUT секунд
            if buffer and (current_time - buffer[-1].date.timestamp()) > self.CLEANUP_TIMEOUT:
                inactive_users.append(user_id)
        
        # Удаляем неактивные буферы
        for user_id in inactive_users:
            del self.buffers[user_id]
            logger.info(f"Очищен неактивный буфер пользователя {user_id}")

# === ЗАПУСК БОТА ===
async def main():
    # Проверка обязательных переменных
    if not BOT_TOKEN:
        logger.error("❌ ОШИБКА: BOT_TOKEN не установлен! Проверьте переменные окружения.")
        return
    
    # Настройка увеличенных таймаутов
    timeout = ClientTimeout(
        total=60.0,  # Общее время ожидания
        connect=15.0,  # Время на подключение
        sock_read=30.0,  # Время на чтение
        sock_connect=15.0  # Время на установку соединения
    )
    
    session = ClientSession(timeout=timeout)
    
    # Создаем бота с правильной настройкой parse_mode
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
        session=session
    )
    
    dp = Dispatcher()
    
    # Сброс зависших апдейтов перед установкой вебхука
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Создаем и регистрируем middleware
    middleware = MediaModerationMiddleware(bot=bot)
    dp.message.middleware(middleware)
    
    # Регистрируем хендлеры
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
        """Обрабатываем все остальные сообщения"""
        # Команды уже обработаны выше
        if message.text and message.text.startswith("/"):
            return
        # Остальные сообщения обрабатываются middleware
        pass

    # Получаем порт от Render
    port = int(os.getenv("PORT", "10000"))
    
    # Настраиваем вебхук
    webhook_path = "/"
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    # Добавляем обработку завершения работы
    async def on_shutdown(app):
        await bot.session.close()
        await session.close()
        logger.info("Бот остановлен, сессии закрыты")

    app.on_cleanup.append(on_shutdown)

    # Запускаем веб-сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    # Устанавливаем вебхук (используем правильный URL)
    service_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}.onrender.com"
    webhook_url = f"{service_url}/"
    
    try:
        await bot.set_webhook(webhook_url)
        logger.info(f"🌍 Webhook установлен: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки вебхука: {e}")
        # Попробуем альтернативный URL
        alt_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}"
        try:
            await bot.set_webhook(f"{alt_url}/")
            logger.info(f"🌍 Webhook установлен через альтернативный URL: {alt_url}/")
        except Exception as alt_e:
            logger.error(f"❌ Не удалось установить вебхук через альтернативный URL: {alt_e}")

    logger.info(f"✅ Бот запущен на порту {port}")
    
    # Запускаем фоновую задачу для очистки буферов
    asyncio.create_task(middleware._cleanup_inactive_buffers())
    
    # Ожидаем завершения
    await asyncio.Event().wait()

if __name__ == "__main__":
    # Создаем файл логов, если его нет
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w", encoding="utf-8").close()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nБот остановлен вручную")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")