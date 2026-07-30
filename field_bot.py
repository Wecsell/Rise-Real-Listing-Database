import os
import time
import logging
from dotenv import load_dotenv
from pyairtable import Api
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import glob

try:
    import gpxpy
    import gpxpy.gpx
except ImportError:
    gpxpy = None

try:
    import folium
except ImportError:
    folium = None

from app.access import describe_user, is_allowed
from app.airtable_client import field_exists
from app.single_instance import acquire

load_dotenv(override=True)

# Настройки логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Airtable
airtable_api = Api(os.environ.get('AIRTABLE_TOKEN'))
base = airtable_api.base(os.environ.get('AIRTABLE_BASE_ID'))
staging_table = base.table('Field Staging')

# In-memory хранилище для сбора данных "в поле"
# Структура: {chat_id: {'photos': [], 'audios': [], 'location': 'lat, lng'}}
sessions = {}

def get_session(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {'photos': [], 'audios': [], 'location': None}
    else:
        if 'photos' not in sessions[chat_id]: sessions[chat_id]['photos'] = []
        if 'audios' not in sessions[chat_id]: sessions[chat_id]['audios'] = []
    return sessions[chat_id]

async def check_access(update: Update) -> bool:
    user_id = update.effective_user.id
    allowed_str = os.environ.get('ALLOWED_TELEGRAM_USER_ID', '')

    if not allowed_str:
        await update.message.reply_text(
            f"🔒 Бот работает в приватном режиме, но ваш ID еще не добавлен в белый список!\n"
            f"Ваш Telegram ID: {user_id}\n\n"
            f"Пожалуйста, добавьте в файл .env строку:\n"
            f"ALLOWED_TELEGRAM_USER_ID={user_id}\n\n"
            f"Если листеров несколько, перечислите ID через запятую.\n"
            f"Затем перезапустите бота."
        )
        return False

    if not is_allowed(user_id, allowed_str):
        await update.message.reply_text(
            "⛔ Извините, этот бот приватный. Попросите добавить ваш Telegram ID в белый список."
        )
        return False

    return True

KEYBOARD = ReplyKeyboardMarkup([['💾 Сохранить объект']], resize_keyboard=True, is_persistent=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Привет! Я полевой бот-сборщик.\nПришли мне Фото (можно много), Голосовые сообщения (можно много) и Геопозицию. Когда все готово, нажми '💾 Сохранить объект'.",
        reply_markup=KEYBOARD
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    # Берем самое большое фото
    photo_file = await update.message.photo[-1].get_file()
    session = get_session(chat_id)
    session['photos'].append(photo_file.file_path)
    count = len(session['photos'])
    await update.message.reply_text(f"✅ Фото #{count} получено! (Можно отправить еще)", reply_markup=KEYBOARD)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    voice_file = await update.message.voice.get_file()
    session = get_session(chat_id)
    session['audios'].append(voice_file.file_path)
    count = len(session['audios'])
    await update.message.reply_text(f"✅ Голосовое #{count} получено! (Можно отправить еще)", reply_markup=KEYBOARD)

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    lat = update.message.location.latitude
    lng = update.message.location.longitude
    get_session(chat_id)['location'] = f"{lat}, {lng}"
    await update.message.reply_text("✅ Координаты получены!", reply_markup=KEYBOARD)

async def save_finding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    
    if not session['photos'] and not session['audios'] and not session['location']:
        await update.message.reply_text("❌ Пусто! Сначала пришли фото, аудио или геопозицию.", reply_markup=KEYBOARD)
        return
        
    await update.message.reply_text("⏳ Отправляю данные в Airtable...", reply_markup=KEYBOARD)
    
    fields = {
        'Status': 'New'
    }

    # Airtable принимает attachments как список словарей с ключом url
    if session['photos']:
        fields['Photo'] = [{'url': p} for p in session['photos']]
    if session['audios']:
        fields['Audio'] = [{'url': a} for a in session['audios']]
    if session['location']:
        fields['Coordinates'] = session['location']

    # Авторство находки: нужно для контроля покрытия при нескольких листерах
    # и чтобы уведомление о дубле ушло тому, кто эту находку прислал.
    # Поля могут отсутствовать в другой базе, поэтому пишем их с проверкой:
    # запись в несуществующее поле роняет весь запрос с 422.
    if field_exists('Field Staging', 'Submitted By'):
        fields['Submitted By'] = describe_user(update.effective_user)
    if field_exists('Field Staging', 'Telegram Chat ID'):
        fields['Telegram Chat ID'] = str(chat_id)
        
    try:
        staging_table.create(fields)
        # Очищаем сессию
        sessions[chat_id] = {'photo': None, 'audio': None, 'location': None}
        await update.message.reply_text("🎉 Успешно сохранено в Field Staging! Можно ехать к следующему билборду.", reply_markup=KEYBOARD)
    except Exception as e:
        logger.error(f"Ошибка Airtable: {e}")
        await update.message.reply_text(f"❌ Ошибка при сохранении в Airtable: {e}", reply_markup=KEYBOARD)

async def handle_gpx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    
    document = update.message.document
    if not document.file_name.lower().endswith(('.gpx', '.kml')):
        # Ignore non-gpx documents or tell user it's wrong format
        await update.message.reply_text("❌ Пожалуйста, отправьте файл с расширением .gpx", reply_markup=KEYBOARD)
        return
        
    await update.message.reply_text("⏳ Загружаю и обрабатываю трек...", reply_markup=KEYBOARD)
    
    file = await document.get_file()
    os.makedirs('data/gpx', exist_ok=True)
    
    timestamp = int(time.time())
    filepath = f"data/gpx/track_{timestamp}_{document.file_name}"
    await file.download_to_drive(filepath)
    
    points_count = 0
    if gpxpy:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                gpx = gpxpy.parse(f)
                for track in gpx.tracks:
                    for segment in track.segments:
                        points_count += len(segment.points)
        except Exception as e:
            logger.error(f"Ошибка парсинга GPX: {e}")
            
    await update.message.reply_text(f"✅ Трек успешно загружен! Сохранено точек: {points_count}. Используйте /map для просмотра карты.", reply_markup=KEYBOARD)

async def handle_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    
    if not folium or not gpxpy:
        await update.message.reply_text("❌ Библиотеки folium и gpxpy не установлены. Выполните pip install gpxpy folium", reply_markup=KEYBOARD)
        return
        
    await update.message.reply_text("⏳ Генерирую карту с маршрутами...", reply_markup=KEYBOARD)
    
    gpx_files = glob.glob("data/gpx/*.gpx")
    if not gpx_files:
        await update.message.reply_text("❌ Нет загруженных GPX треков.", reply_markup=KEYBOARD)
        return
        
    all_points = []
    lines = []
    for filepath in gpx_files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                gpx = gpxpy.parse(f)
                for track in gpx.tracks:
                    for segment in track.segments:
                        segment_points = [[p.latitude, p.longitude] for p in segment.points]
                        if segment_points:
                            lines.append(segment_points)
                            all_points.extend(segment_points)
        except Exception as e:
            logger.error(f"Ошибка чтения {filepath}: {e}")
            
    if not all_points:
        await update.message.reply_text("❌ В треках не найдено точек координат.", reply_markup=KEYBOARD)
        return
        
    # Создаем карту
    avg_lat = sum(p[0] for p in all_points) / len(all_points)
    avg_lng = sum(p[1] for p in all_points) / len(all_points)
    
    m = folium.Map(location=[avg_lat, avg_lng], zoom_start=14, tiles='OpenStreetMap')
    for line in lines:
        folium.PolyLine(line, color='blue', weight=4, opacity=0.8).add_to(m)
    
    map_file = "data/heatmap.html"
    m.save(map_file)
    
    with open(map_file, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename="Coverage_Map.html",
            caption=f"🗺️ Карта маршрутов на основе {len(gpx_files)} треков. Скачайте и откройте в браузере."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(
        "Я готов принимать объекты! Пришли мне Фото, Голосовое сообщение или Координаты.\n"
        "Как только всё загрузишь — нажми кнопку ниже 👇",
        reply_markup=KEYBOARD
    )

if __name__ == '__main__':
    # Без замка одновременно работали четыре копии бота: экземпляры отбирают
    # друг у друга getUpdates, и часть находок листеров теряется.
    acquire('field_bot')

    token = os.environ.get('TELEGRAM_FIELD_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_FIELD_BOT_TOKEN не задан в .env!")
        exit(1)
        
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('map', handle_map))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_gpx))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.Regex('^💾 Сохранить объект$'), save_finding))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex('^💾 Сохранить объект$'), handle_text))

    print("Field bot is running. Waiting for messages...")
    application.run_polling()
