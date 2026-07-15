import os
import logging
import logging.handlers
import asyncio
import threading
import concurrent.futures
import tempfile
import json
from pyrogram import Client, filters
import random
import string
import datetime
import subprocess
from pyrogram.types import (Message, InlineKeyboardButton,
                           InlineKeyboardMarkup, ReplyKeyboardMarkup,
                           KeyboardButton, CallbackQuery)
from pyrogram.errors import MessageNotModified
import ffmpeg
import re
import time
import unicodedata  
from pymongo import MongoClient
from config import *
from bson.objectid import ObjectId
import uuid
import zipfile
import io
from bson.json_util import dumps
import psutil
import shutil
import zoneinfo

# ======================== WATCHDOG CONFIG ======================== #
WATCHDOG_INTERVAL = 60

last_watchdog_run = None
last_auto_expiry_check = None   

# ======================== CONSTANTE PARA ESTIMACIÓN DE TIEMPO ======================== #
# (Ya no se usa un tiempo fijo, solo se estima para el primer video en cola)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            "bot.log",
            when='H',
            interval=3,
            backupCount=10
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

PREMIUM_QUEUE_LIMIT = 4
ULTRA_QUEUE_LIMIT = 10
PRO_QUEUE_LIMIT = 2      

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DATABASE_NAME]
pending_col = db["pending"]
users_col = db["users"]
temp_keys_col = db["temp_keys"]
banned_col = db["banned_users"]
pending_confirmations_col = db["pending_confirmations"]
active_compressions_col = db["active_compressions"]
user_settings_col = db["user_settings"]
downloaded_videos_col = db["downloaded_videos"]
daily_stats_col = db["daily_stats"]
pending_payments_col = db["pending_payments"]

# ======================== NUEVA COLECCIÓN PARA CONTROL DE NOTIFICACIONES DE ACCESO DENEGADO ======================== #
access_denied_log_col = db["access_denied_log"]
access_denied_log_col.create_index("user_id", unique=True)

# ======================== NUEVA COLECCIÓN PARA CONTADOR SECUENCIAL ======================== #
counters_col = db["counters"]
if counters_col.count_documents({"_id": "pending_seq"}) == 0:
    counters_col.insert_one({"_id": "pending_seq", "seq": 0})
if counters_col.count_documents({"_id": "user_seq"}) == 0:
    counters_col.insert_one({"_id": "user_seq", "seq": 0})

api_id = API_ID
api_hash = API_HASH
bot_token = BOT_TOKEN

BOT_TEMP_DIR = os.path.join(os.getcwd(), "bot_temp")
os.makedirs(BOT_TEMP_DIR, exist_ok=True)
tempfile.tempdir = BOT_TEMP_DIR
logger.info(f"Directorio temporal del bot configurado: {BOT_TEMP_DIR}")

app = Client(
    "compress_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
)

admin_users = ADMINS_IDS
ban_users = []

banned_users_in_db = banned_col.find({}, {"user_id": 1})
for banned_user in banned_users_in_db:
    if banned_user["user_id"] not in ban_users:
        ban_users.append(banned_user["user_id"])

active_compressions_col.delete_many({})
logger.info("✅Compresiones activas previas eliminadas")
downloaded_videos_col.delete_many({})
logger.info("✅Videos descargados previos eliminados")

DEFAULT_VIDEO_SETTINGS = {
    'resolution': '-2:480',
    'crf': '28',
    'audio_bitrate': '64k',
    'fps': '23',
    'preset': 'veryfast',
    'codec': 'libx264'
}

compression_queue = asyncio.Queue()
processing_tasks = []
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

MAINTENANCE_MODE = False

cancel_tasks = {}
ffmpeg_processes = {}
active_messages = {}
compression_progress = {}
temp_custom_settings = {}

CUSTOM_RESOLUTION_OPTIONS = ['640x360', '-2:480', '-2:720']
CUSTOM_CRF_OPTIONS = ['25', '28', '30', '32', '35', '38', '40']
CUSTOM_FPS_OPTIONS = ['20', '22', '25', '28', '30', '35']
CUSTOM_AUDIO_OPTIONS = ['64k', '90k', '128k']

compression_processing_queue = asyncio.Queue()
download_queue = asyncio.PriorityQueue()

broadcast_sessions = {}

SUPPORTED_VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'ts', 'mov', 'flv', 'wmv', 'webm', 'm4v', '3gp',
    'mpeg', 'mpg', '3g2', 'rm', 'rmvb', 'vob', 'f4v', 'ogv', 'drc', 'nsv', 'mpe', 'm2v'
]

# ======================== VARIABLE GLOBAL PARA EL WORKER DE DESCARGA ======================== #
download_worker_task = None

# ======================== ALMACENAMIENTO TEMPORAL PARA CONFIGURACIÓN DE VIDEOS ======================== #
temp_video_configs = {}

# ======================== NUEVA VARIABLE PARA CONTROL DE TAREAS DE ACTUALIZACIÓN DE COLA ======================== #
user_queue_tasks = {}  # user_id -> {"chat_id": int, "message_id": int, "task": asyncio.Task}

# ======================== NUEVAS FUNCIONES PARA EL MODO DE COMPRESIÓN ======================== #
async def get_user_compression_mode(user_id: int) -> str:
    """Retorna 'after' (configurar al enviar) o 'before' (configurar antes de enviar)."""
    user_settings = user_settings_col.find_one({"user_id": user_id})
    if user_settings and "compression_mode" in user_settings:
        return user_settings["compression_mode"]
    return "before"

async def set_user_compression_mode(user_id: int, mode: str):
    """Guarda la preferencia del modo de compresión."""
    user_settings_col.update_one({"user_id": user_id}, {"$set": {"compression_mode": mode}}, upsert=True)
    logger.info(f"Modo de compresión actualizado para {user_id}: {mode}")

# ======================== FUNCIÓN PARA CALCULAR HORAS ESTIMADAS DE INICIO (SOLO PARA EL PRIMERO) ======================== #
async def calculate_estimated_start_times():
    """
    Calcula la hora estimada de inicio para cada video en la cola de compresión.
    - El primer video (posición 1) obtiene la hora de finalización de la compresión activa (si existe),
      o 'Inmediato' si no hay compresión activa.
    - Los demás videos obtienen None (Sin calcular...).
    Retorna una lista de diccionarios con compression_id, estimated_start (datetime o None) y position.
    """
    downloaded_videos = list(downloaded_videos_col.find().sort("timestamp", 1))
    if not downloaded_videos:
        return []

    # Verificar si hay compresión activa
    active_comp = active_compressions_col.find_one()
    now = datetime.datetime.now()
    estimated_start_first = None

    if active_comp:
        comp_id = active_comp.get("compression_id")
        if comp_id in compression_progress:
            progress = compression_progress[comp_id]
            percent = progress.get("percent", 0)
            if percent > 0:
                start_time_db = active_comp.get("start_time")
                if start_time_db:
                    elapsed = (now - start_time_db).total_seconds()
                    total_estimated = elapsed / (percent / 100) if percent > 0 else 0
                    remaining = total_estimated - elapsed
                    if remaining > 0:
                        estimated_start_first = now + datetime.timedelta(seconds=remaining)
                    else:
                        estimated_start_first = now
                else:
                    estimated_start_first = now
            else:
                estimated_start_first = now
        else:
            estimated_start_first = now
    else:
        # No hay compresión activa, el primero empieza ahora (Inmediato)
        estimated_start_first = now  # Se mostrará como "Inmediato"

    estimated_times = []
    for idx, video in enumerate(downloaded_videos):
        comp_id = video["compression_id"]
        if idx == 0:
            # Primer video en cola
            estimated_start = estimated_start_first
        else:
            # Resto de videos: sin calcular
            estimated_start = None
        estimated_times.append({
            "compression_id": comp_id,
            "estimated_start": estimated_start,
            "position": idx + 1
        })
    return estimated_times

def format_start_time(estimated_start):
    """Formatea la hora estimada de inicio para mostrarla en el mensaje."""
    if estimated_start is None:
        return "Sin calcular..."
    else:
        # Si es ahora o en menos de 1 minuto, mostramos "Inmediato"
        now = datetime.datetime.now()
        if (estimated_start - now).total_seconds() < 60:
            return "Calculando..."
        else:
            # Formato 12h con AM/PM en mayúsculas (zona Cuba)
            cuba_tz = zoneinfo.ZoneInfo("America/Havana")
            local_time = estimated_start.astimezone(cuba_tz)
            return local_time.strftime("%I:%M %p")

# ======================== FUNCIONES PARA OBTENER NÚMERO DE USUARIO ======================== #
def get_next_user_seq():
    """Obtiene el siguiente número secuencial para un nuevo usuario."""
    result = counters_col.find_one_and_update(
        {"_id": "user_seq"},
        {"$inc": {"seq": 1}},
        return_document=True
    )
    return result["seq"]

def get_user_number(user_id: int):
    """
    Retorna el número de usuario almacenado en la base de datos.
    Si el usuario existe pero no tiene número, se le asigna uno automáticamente.
    Si no existe, retorna None.
    """
    user = users_col.find_one({"user_id": user_id}, {"user_number": 1})
    if user:
        if "user_number" in user:
            return user["user_number"]
        else:
            # Asignar número secuencial
            new_number = get_next_user_seq()
            users_col.update_one({"user_id": user_id}, {"$set": {"user_number": new_number}})
            return new_number
    return None

# ======================== FIN DE NUEVAS FUNCIONES ======================== #

def is_supported_video_file(filename: str) -> bool:
    if not filename:
        return False
    ext = filename.split('.')[-1].lower()
    return ext in SUPPORTED_VIDEO_EXTENSIONS

# ======================== FUNCIONES PARA PERSONALIZACIÓN ======================== #

def get_resolution_keyboard(selected_resolution=None):
    buttons = []
    resolutions = [('640x360', '360'), ('-2:480', '480'), ('-2:720', '720')]
    row = []
    for resolution, label in resolutions:
        text = f"✔️ {label}" if selected_resolution == resolution else label
        row.append(InlineKeyboardButton(text, callback_data=f"custom_resolution_{resolution}"))
    if row:
        buttons.append(row)
    nav_buttons = []
    nav_buttons.append(InlineKeyboardButton("🔙 Regresar", callback_data="back_to_settings"))
    if selected_resolution:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="custom_next_crf"))
    if nav_buttons:
        buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_crf_keyboard(selected_crf=None):
    buttons = []
    row = []
    for crf in CUSTOM_CRF_OPTIONS:
        text = f"✔️ {crf}" if selected_crf == crf else crf
        row.append(InlineKeyboardButton(text, callback_data=f"custom_crf_{crf}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data="custom_back_resolution")]
    if selected_crf:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="custom_next_fps"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_fps_keyboard(selected_fps=None):
    buttons = []
    row = []
    for fps in CUSTOM_FPS_OPTIONS:
        text = f"✔️ {fps}" if selected_fps == fps else fps
        row.append(InlineKeyboardButton(text, callback_data=f"custom_fps_{fps}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data="custom_back_crf")]
    if selected_fps:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="custom_next_audio"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_audio_keyboard(selected_audio=None):
    buttons = []
    row = []
    for audio in CUSTOM_AUDIO_OPTIONS:
        text = f"✔️ {audio}" if selected_audio == audio else audio
        row.append(InlineKeyboardButton(text, callback_data=f"custom_audio_{audio}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data="custom_back_fps")]
    if selected_audio:
        nav_buttons.append(InlineKeyboardButton("Finalizar ✅", callback_data="custom_finish"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

# ======================== FUNCIONES PARA CONFIGURACIÓN DE VIDEOS (con compression_id) ======================== #

def get_resolution_keyboard_video(compression_id, selected_resolution=None):
    buttons = []
    resolutions = [('360', '360'), ('480', '480'), ('720', '720')]
    row = []
    for res, label in resolutions:
        text = f"✔️ {label}" if selected_resolution == res else label
        row.append(InlineKeyboardButton(text, callback_data=f"vid_res_{compression_id}_{res}"))
    if row:
        buttons.append(row)
    nav_buttons = []
    nav_buttons.append(InlineKeyboardButton("🔙 Regresar", callback_data=f"vid_back_res_{compression_id}"))
    if selected_resolution:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"vid_next_crf_{compression_id}"))
    nav_buttons.append(InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_crf_keyboard_video(compression_id, selected_crf=None):
    buttons = []
    row = []
    for crf in CUSTOM_CRF_OPTIONS:
        text = f"✔️ {crf}" if selected_crf == crf else crf
        row.append(InlineKeyboardButton(text, callback_data=f"vid_crf_{compression_id}_{crf}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data=f"vid_back_crf_{compression_id}")]
    if selected_crf:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"vid_next_fps_{compression_id}"))
    nav_buttons.append(InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_fps_keyboard_video(compression_id, selected_fps=None):
    buttons = []
    row = []
    for fps in CUSTOM_FPS_OPTIONS:
        text = f"✔️ {fps}" if selected_fps == fps else fps
        row.append(InlineKeyboardButton(text, callback_data=f"vid_fps_{compression_id}_{fps}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data=f"vid_back_fps_{compression_id}")]
    if selected_fps:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"vid_next_audio_{compression_id}"))
    nav_buttons.append(InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_audio_keyboard_video(compression_id, selected_audio=None):
    buttons = []
    row = []
    for audio in CUSTOM_AUDIO_OPTIONS:
        text = f"✔️ {audio}" if selected_audio == audio else audio
        row.append(InlineKeyboardButton(text, callback_data=f"vid_audio_{compression_id}_{audio}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav_buttons = [InlineKeyboardButton("🔙 Atrás", callback_data=f"vid_back_audio_{compression_id}")]
    if selected_audio:
        nav_buttons.append(InlineKeyboardButton("Finalizar ✅", callback_data=f"vid_finish_{compression_id}"))
    nav_buttons.append(InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}"))
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

async def apply_custom_settings(user_id, settings):
    try:
        current_settings = await get_user_video_settings(user_id)
        if 'resolution' in settings:
            current_settings['resolution'] = settings['resolution']
        if 'crf' in settings:
            current_settings['crf'] = settings['crf']
        if 'fps' in settings:
            current_settings['fps'] = settings['fps']
        if 'audio_bitrate' in settings:
            current_settings['audio_bitrate'] = settings['audio_bitrate']
        user_settings_col.update_one(
            {"user_id": user_id},
            {"$set": {"video_settings": current_settings}},
            upsert=True
        )
        logger.info(f"✅Configuración personalizada aplicada para usuario {user_id}: {settings}")
        return True
    except Exception as e:
        logger.error(f"❌Error aplicando configuración personalizada: {e}")
        return False

# ======================== FUNCIONES PARA ESTADÍSTICAS DIARIAS ======================== #

def get_today_date_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")

async def update_daily_stats_download(file_size_bytes: int):
    date_str = get_today_date_str()
    daily_stats_col.update_one(
        {"date": date_str},
        {"$inc": {"videos_downloaded": 1, "bytes_downloaded": file_size_bytes}},
        upsert=True
    )

async def update_daily_stats_compressed():
    date_str = get_today_date_str()
    daily_stats_col.update_one(
        {"date": date_str},
        {"$inc": {"videos_compressed": 1}},
        upsert=True
    )

async def update_daily_stats_recovery(increment: int = 1):
    date_str = get_today_date_str()
    daily_stats_col.update_one(
        {"date": date_str},
        {"$inc": {"auto_recoveries": increment}},
        upsert=True
    )

async def get_daily_stats() -> dict:
    date_str = get_today_date_str()
    doc = daily_stats_col.find_one({"date": date_str})
    if not doc:
        return {"videos_downloaded": 0, "bytes_downloaded": 0, "videos_compressed": 0, "auto_recoveries": 0}
    return doc

# ======================== EXPORTACIÓN/IMPORTACIÓN DB ======================== #

@app.on_message(filters.command("getdb") & filters.user(admin_users))
async def get_db_command(client, message):
    try:
        users = list(users_col.find({}))
        user_count = len(users)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
            json.dump(users, tmp_file, default=str, indent=4)
            tmp_file.flush()
            await message.reply_document(
                document=tmp_file.name,
                caption=f"📊 Copia de la base de datos de usuarios\n👤**Usuarios:** {user_count}"
            )
            os.unlink(tmp_file.name)
    except Exception as e:
        logger.error(f"❌Error en get_db_command: {e}", exc_info=True)
        await message.reply("❌ Error al exportar la base de datos")

@app.on_message(filters.command("restdb") & filters.user(admin_users))
async def rest_db_command(client, message):
    await message.reply(
        "🔄 **Modo restauración activado**\n\n"
        "Envía el archivo JSON de la base de datos que deseas restaurar."
    )

@app.on_message(filters.document & filters.user(admin_users))
async def handle_db_restore(client, message):
    try:
        if not message.document.file_name.endswith('.json'):
            return
        file_path = await message.download()
        with open(file_path, 'r', encoding='utf-8') as f:
            users_data = json.load(f)
        if not isinstance(users_data, list):
            await message.reply("❌ El archivo JSON no tiene la estructura correcta.")
            os.remove(file_path)
            return
        users_col.delete_many({})
        if users_data:
            for user in users_data:
                if 'join_date' in user and isinstance(user['join_date'], str):
                    user['join_date'] = datetime.datetime.fromisoformat(user['join_date'])
                if 'expires_at' in user and user['expires_at'] and isinstance(user['expires_at'], str):
                    user['expires_at'] = datetime.datetime.fromisoformat(user['expires_at'])
            users_col.insert_many(users_data)
        os.remove(file_path)
        await message.reply(
            f"✅ **Base de datos restaurada exitosamente**\n\nSe restauraron {len(users_data)} usuarios."
        )
        logger.info(f"✅Base de datos restaurada por {message.from_user.id} con {len(users_data)} usuarios")
    except json.JSONDecodeError:
        await message.reply("❌ El archivo no es un JSON válido.")
    except Exception as e:
        logger.error(f"❌Error restaurando base de datos: {e}", exc_info=True)
        await message.reply("❌ Error al restaurar la base de datos.")

# ======================== COMANDO BACKUP ======================== #

@app.on_message(filters.command("backup") & filters.user(admin_users))
async def backup_command(client, message):
    try:
        msg = await message.reply("🔄 **Creando backup de la base de datos...**")
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            collections = [
                "active_compressions", "banned_users", "pending_confirmations",
                "pending", "temp_keys", "user_settings", "users",
                "downloaded_videos", "daily_stats", "pending_payments"
            ]
            total_documents = 0
            for collection_name in collections:
                try:
                    collection = db[collection_name]
                    documents = list(collection.find({}))
                    json_data = dumps(documents, indent=2, default=str)
                    zip_file.writestr(f"{collection_name}.json", json_data)
                    total_documents += len(documents)
                    logger.info(f"✅Backup: {collection_name} - {len(documents)} documentos")
                except Exception as e:
                    logger.error(f"❌Error respaldando {collection_name}: {e}")
        zip_buffer.seek(0)
        current_date = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"backup_{current_date}.zip"
        await message.reply_document(
            document=zip_buffer,
            file_name=filename,
            caption=f"✅ **Backup completado**\n\n📊 **Colecciones respaldadas:** {len(collections)}\n📄 **Documentos totales:** {total_documents}\n⏰ **Fecha:** {current_date.replace('_', ' ')}"
        )
        try:
            await msg.delete()
        except:
            pass
        logger.info(f"✅Backup creado por {message.from_user.id} con {total_documents} documentos")
    except Exception as e:
        logger.error(f"❌Error en backup_command: {e}", exc_info=True)
        try:
            await msg.edit("❌ **Error al crear el backup**")
        except:
            await message.reply("❌ **Error al crear el backup**")

# ======================== COMANDO SETDAYS ======================== #

async def add_days_to_all_users(days: int, admin_id: int):
    try:
        users = list(users_col.find({"plan": {"$in": ["standard", "pro", "premium"]}, "expires_at": {"$exists": True}}))
        total_users = len(users)
        if total_users == 0:
            return 0, 0, "No hay usuarios con planes que expiran para actualizar."
        updated_count = 0
        failed_count = 0
        for user in users:
            try:
                user_id = user["user_id"]
                current_expires = user["expires_at"]
                if isinstance(current_expires, datetime.datetime):
                    new_expires = current_expires + datetime.timedelta(days=days)
                    users_col.update_one({"user_id": user_id}, {"$set": {"expires_at": new_expires}})
                    await reset_expiry_notification_flags(user_id)
                    updated_count += 1
                    try:
                        await send_protected_message(
                            user_id,
                            f"🎉 **¡Se han agregado {days} día(s) a tu plan!**\n\n¡Disfruta del tiempo adicional! 🎬"
                        )
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"❌Error notificando usuario {user_id}: {e}")
                        failed_count += 1
                else:
                    logger.error(f"❌Fecha de expiración inválida para usuario {user_id}: {current_expires}")
                    failed_count += 1
            except Exception as e:
                logger.error(f"❌Error actualizando usuario {user_id}: {e}")
                failed_count += 1
        return updated_count, failed_count, f"Proceso completado: {updated_count} actualizados, {failed_count} fallos."
    except Exception as e:
        logger.error(f"❌Error en add_days_to_all_users: {e}", exc_info=True)
        return 0, 0, f"Error general: {str(e)}"

@app.on_message(filters.command("setdays") & filters.user(admin_users))
async def setdays_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("⚠️ **Formato:** `/setdays <número_de_días>`\nEjemplo: `/setdays 2`")
            return
        days = int(parts[1])
        if days <= 0:
            await message.reply("❌ **El número de días debe ser mayor a 0**")
            return
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_setdays_{days}"),
             InlineKeyboardButton("⛔ Cancelar ⛔", callback_data="cancel_setdays")]
        ])
        await message.reply(
            f"⚠️ **¿Estás seguro de que quieres agregar {days} día(s) a TODOS los usuarios?**\n\n"
            f"• **Días a agregar**: {days}\n"
            f"• **Se notificará** a todos los usuarios afectados\n"
            f"• **Esta acción no se puede deshacer**",
            reply_markup=confirm_keyboard
        )
    except Exception as e:
        logger.error(f"❌Error en setdays_command: {e}", exc_info=True)
        await message.reply("❌ **Error al procesar el comando**")

# ======================== COMANDO STATUS ======================== #

def get_status_stats():
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage('/')
        def create_bar(percent, length=10):
            percent = max(0, min(100, percent))
            filled = int(length * percent / 100)
            bar = '█' * filled + '▒' * (length - filled)
            return f"{bar} {percent:.1f}%"
        cpu_bar = create_bar(cpu_percent)
        ram_bar = create_bar(ram.percent)
        swap_bar = create_bar(swap.percent)
        disk_bar = create_bar(disk.percent)
        ram_used = sizeof_fmt(ram.used)
        ram_total = sizeof_fmt(ram.total)
        disk_used = sizeof_fmt(disk.used)
        disk_total = sizeof_fmt(disk.total)
        stats_text = (
            "🖥️ **Estadísticas del Sistema en Tiempo Real**\n\n"
            f"**CPU**  : {cpu_bar}\n"
            f"**RAM**  : {ram_bar}\n          {ram_used}/{ram_total}\n"
            f"**SWAP** : {swap_bar}\n"
            f"**DISK** : {disk_bar}\n          {disk_used}/{disk_total}\n\n"
        )
        try:
            if hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures()
                if temps and 'coretemp' in temps:
                    cpu_temp = temps['coretemp'][0].current
                    stats_text += f"🌡️ **Temperatura CPU**: {cpu_temp}°C\n"
        except:
            pass
        return stats_text
    except Exception as e:
        logger.error(f"❌Error obteniendo estadísticas del sistema: {e}")
        return "❌ **Error al obtener estadísticas del sistema**"

@app.on_message(filters.command("status") & filters.user(admin_users))
async def status_command(client, message):
    try:
        stats = get_status_stats()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_status_stats")]])
        await message.reply(stats, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"❌Error en status_command: {e}", exc_info=True)
        await message.reply("❌ **Error al obtener estadísticas del sistema**")

@app.on_message(filters.command(["watchdog", "whactdog"]) & filters.user(admin_users))
async def watchdog_status_command(client, message):
    global last_watchdog_run
    if last_watchdog_run is None:
        await message.reply("🕒 El watchdog aún no se ha ejecutado. Espera unos segundos y vuelve a intentarlo.")
        return
    now = datetime.datetime.now()
    delta = now - last_watchdog_run
    minutes = int(delta.total_seconds() // 60)
    seconds = int(delta.total_seconds() % 60)
    time_str = f"{minutes} min {seconds} seg" if minutes > 0 else f"{seconds} seg"
    stats = await get_daily_stats()
    auto_recoveries = stats.get("auto_recoveries", 0)
    await message.reply(
        f"✅ **Watchdog funcionando correctamente**\n\n"
        f"📅 Última revisión: hace {time_str}\n"
        f"🔄 Reanudaciones automáticas: {auto_recoveries}"
    )

@app.on_message(filters.command("log") & filters.user(admin_users))
async def log_command(client, message):
    try:
        log_file = "bot.log"
        if not os.path.exists(log_file):
            await message.reply("❌ No se encontró el archivo de log.")
            return
        file_size = os.path.getsize(log_file)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bot_log_{timestamp}.json"
        await message.reply_document(
            document=log_file,
            file_name=filename,
            caption=f"📋 **Log del bot**\n📦 Tamaño: {sizeof_fmt(file_size)}"
        )
        logger.info(f"✅Log enviado por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en log_command: {e}", exc_info=True)
        await message.reply("❌ Error al enviar el log.")

# ======================== COMANDOS DE MANTENIMIENTO ======================== #

@app.on_message(filters.command("estado") & filters.private)
async def estado_command(client, message):
    try:
        user_id = message.from_user.id
        maintenance_status = get_maintenance_status()
        if maintenance_status:
            status_text = "⚙️ **BOT EN MANTENIMIENTO** ⚙️"
            status_desc = "➥El bot está actualmente en modo mantenimiento.\n\n**Vuelva a intentar más tarde.**\nUse /estado para ver el estado del bot"
        else:
            status_text = "✅ **BOT EN LÍNEA** ✅"
            status_desc = "➥El bot está funcionando normalmente.\n\n**Puede enviar videos para comprimir.**"
        response = f"{status_text}\n\n{status_desc}\n\n🕐 **Hora del servidor:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if user_id in admin_users:
            try:
                cpu_percent = psutil.cpu_percent(interval=0.5)
                ram = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                stats = (f"\n\n👑 **Vista de administrador:**\n"
                         f"• **CPU:** {cpu_percent:.1f}%\n"
                         f"• **RAM:** {ram.percent:.1f}%\n"
                         f"• **Disco:** {disk.percent:.1f}%\n"
                         f"• **Modo mantenimiento:** {'🟢 **ACTIVO**' if maintenance_status else '🔴 **DESACTIVO**'}")
                response += stats
            except Exception as e:
                logger.error(f"❌Error obteniendo estadísticas: {e}")
        await send_protected_message(message.chat.id, response)
    except Exception as e:
        logger.error(f"❌Error en estado_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "⚠️ **Error al verificar el estado del bot**")

@app.on_message(filters.command("man_on") & filters.user(admin_users))
async def maintenance_on_command(client, message):
    try:
        if MAINTENANCE_MODE:
            await message.reply("⚠️ **El modo mantenimiento ya está activado.**")
            return
        set_maintenance_mode(True)
        await message.reply("⚙️ **MANTENIMIENTO ACTIVADO** ⚙️\n\n➥El bot ahora está en modo mantenimiento:\n\n• Para desactivar, use el comando:\n/man_off")
        logger.info(f"✅Modo mantenimiento activado por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en maintenance_on_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al activar el modo mantenimiento**")

@app.on_message(filters.command("man_off") & filters.user(admin_users))
async def maintenance_off_command(client, message):
    try:
        if not MAINTENANCE_MODE:
            await message.reply("⚠️ **El modo mantenimiento ya está desactivado.**")
            return
        set_maintenance_mode(False)
        await message.reply("✅ **MANTENIMIENTO DESACTIVADO** ✅\n\n➥El bot vuelve a estar operativo para todos los usuarios.")
        logger.info(f"✅Modo mantenimiento desactivado por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en maintenance_off_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al desactivar el modo mantenimiento**")

# ======================== FUNCIONES AUXILIARES ======================== #

def format_time(seconds):
    if seconds < 0:
        return "00:00"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

def sizeof_fmt(num, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return "%3.2f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.2f%s%s" % (num, "Yi", suffix)

async def delete_message_after(message, seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"❌Error eliminando mensaje: {e}")

async def send_auto_delete_message(chat_id, text, delete_after=3, **kwargs):
    msg = await send_protected_message(chat_id, text, **kwargs)
    asyncio.create_task(delete_message_after(msg, delete_after))
    return msg

def get_maintenance_status():
    return MAINTENANCE_MODE

def set_maintenance_mode(status: bool):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = status
    logger.info(f"✅Modo mantenimiento {'activado' if status else 'desactivado'}")

async def check_maintenance_and_notify(user_id: int, chat_id: int, message_text: str = None):
    if MAINTENANCE_MODE and user_id not in admin_users:
        maintenance_message = ("⚙️**Bot en mantenimiento** ⚙️\n\n➥El bot está actualmente en modo mantenimiento.\n\nPor favor, espere a que termine el mantenimiento.\nUse /estado para ver el estado del bot")
        if message_text:
            await send_protected_message(chat_id, maintenance_message)
        else:
            msg = await send_protected_message(chat_id, maintenance_message)
            asyncio.create_task(delete_message_after(msg, 10))
        return True
    return False

# ======================== CONFIGURACIÓN POR USUARIO ======================== #

async def get_user_video_settings(user_id: int) -> dict:
    user_settings = user_settings_col.find_one({"user_id": user_id})
    if user_settings and "video_settings" in user_settings:
        return user_settings["video_settings"]
    return DEFAULT_VIDEO_SETTINGS.copy()

async def update_user_video_settings(user_id: int, command: str):
    try:
        settings = command.split()
        new_settings = {}
        for setting in settings:
            if '=' in setting:
                key, value = setting.split('=', 1)
                if key in DEFAULT_VIDEO_SETTINGS:
                    new_settings[key] = value
        if new_settings:
            user_settings_col.update_one({"user_id": user_id}, {"$set": {"video_settings": new_settings}}, upsert=True)
            logger.info(f"✅Configuración actualizada para usuario {user_id}: {new_settings}")
            return True
        return False
    except Exception as e:
        logger.error(f"❌Error actualizando configuración para usuario {user_id}: {e}", exc_info=True)
        return False

async def reset_user_video_settings(user_id: int):
    user_settings_col.delete_one({"user_id": user_id})
    logger.info(f"✅Configuración restablecida para usuario {user_id}")

async def cleanup_compression_data(compression_id: str):
    try:
        downloaded = downloaded_videos_col.find_one({"compression_id": compression_id})
        if downloaded:
            file_path = downloaded.get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"✅Archivo eliminado desde cleanup: {file_path}")
                except Exception as e:
                    logger.error(f"❌Error eliminando archivo {file_path}: {e}")
        pending_col.delete_one({"compression_id": compression_id})
        downloaded_videos_col.delete_one({"compression_id": compression_id})
        active_compressions_col.delete_one({"compression_id": compression_id})
        logger.info(f"✅Datos de compresión limpiados para {compression_id}")
        return True
    except Exception as e:
        logger.error(f"❌Error limpiando datos de compresión {compression_id}: {e}")
        return False

def generate_compression_id():
    return str(uuid.uuid4())

def get_next_pending_seq():
    result = counters_col.find_one_and_update(
        {"_id": "pending_seq"},
        {"$inc": {"seq": 1}},
        return_document=True
    )
    return result["seq"]

def register_cancelable_task(compression_id, task_type, task, original_message_id=None, progress_message_id=None):
    cancel_tasks[compression_id] = {"type": task_type, "task": task, "original_message_id": original_message_id, "progress_message_id": progress_message_id}

def unregister_cancelable_task(compression_id):
    if compression_id in cancel_tasks:
        del cancel_tasks[compression_id]

def register_ffmpeg_process(compression_id, process):
    ffmpeg_processes[compression_id] = process

def unregister_ffmpeg_process(compression_id):
    if compression_id in ffmpeg_processes:
        del ffmpeg_processes[compression_id]

def cancel_compression_task(compression_id):
    if compression_id in cancel_tasks:
        task_info = cancel_tasks[compression_id]
        try:
            if task_info["type"] == "download":
                task = task_info.get("task")
                if task and not task.done():
                    task.cancel()
                    logger.info(f"✅Tarea de descarga {compression_id} cancelada")
                return True
            elif task_info["type"] == "ffmpeg" and compression_id in ffmpeg_processes:
                process = ffmpeg_processes[compression_id]
                if process.poll() is None:
                    process.terminate()
                    time.sleep(0.5)
                    if process.poll() is None:
                        process.kill()
                    return True
            elif task_info["type"] == "upload":
                return True
        except Exception as e:
            logger.error(f"❌Error cancelando tarea {compression_id}: {e}")
    return False

def get_user_compression_ids(user_id):
    user_compressions = []
    for compression_id, task_info in cancel_tasks.items():
        compression_data = active_compressions_col.find_one({"compression_id": compression_id})
        if compression_data and compression_data.get("user_id") == user_id:
            user_compressions.append(compression_id)
    return user_compressions

def update_compression_progress(compression_id, stage, current=0, total=0, percent=0, file_name=""):
    compression_progress[compression_id] = {"stage": stage, "current": current, "total": total, "percent": percent, "file_name": file_name, "last_update": time.time()}

def remove_compression_progress(compression_id):
    if compression_id in compression_progress:
        del compression_progress[compression_id]

def create_mini_progress_bar(percent, bar_length=8):
    try:
        percent = max(0, min(100, percent))
        filled_length = int(bar_length * percent / 100)
        bar = '⬢' * filled_length + '⬡' * (bar_length - filled_length)
        return f"[{bar}] {int(percent)}%"
    except:
        return f"[⬡⬡⬡⬡⬡⬡⬡⬡] {int(percent)}%"

async def get_queue_status(user_id=None):
    try:
        active_compr = list(active_compressions_col.find({}))
        downloaded_videos = list(downloaded_videos_col.find().sort("timestamp", 1))
        pending_queue = list(pending_col.find().sort("seq", 1))
        active_count = len(active_compr)
        downloaded_count = len(downloaded_videos)
        pending_count = len(pending_queue)
        max_simultaneous = 1
        response = "📊 **Estado de la cola**\n\n"
        response += f"🔄 **Procesos activos:** {active_count}/{max_simultaneous}\n"
        response += f"✅ **Videos descargados:** {downloaded_count}\n"

        active_downloads = []
        for pending_item in pending_queue:
            comp_id = pending_item["compression_id"]
            if comp_id in compression_progress:
                stage = compression_progress[comp_id].get("stage", "")
                if stage in ("download", "download_starting"):
                    active_downloads.append(pending_item)

        max_downloads = 1
        active_downloads_count = len(active_downloads)
        response += f"\n⬇️**Descargas en curso:** {active_downloads_count}/{max_downloads}\n"

        if active_downloads:
            for item in active_downloads:
                comp_id = item["compression_id"]
                file_name = item.get("file_name", "Sin nombre")
                uid = item["user_id"]
                # Mostrar "Tu*" si el usuario coincide con el que consulta
                if user_id is not None and uid == user_id:
                    username_display = "(Tu)"
                else:
                    user_number = get_user_number(uid)
                    username_display = f"Usuario {user_number}" if user_number else f"Usuario {uid}"
                progress_data = compression_progress.get(comp_id, {})
                percent = progress_data.get("percent", 0)
                progress_bar = create_mini_progress_bar(percent)
                response += f"» {username_display} ➧ {progress_bar}\n"
        else:
            response += "**• Ninguna**\n"

        response += f"\n🗜️**Compresiones activas:** {len(active_compr)}/{max_simultaneous}\n"
        if active_compr:
            for comp in active_compr:
                compression_id = comp.get("compression_id")
                comp_user_id = comp.get("user_id")
                file_name = comp.get("file_name", "Sin nombre")
                # Mostrar "Tu*" si el usuario coincide con el que consulta
                if user_id is not None and comp_user_id == user_id:
                    username_display = "(Tu)"
                else:
                    user_number = get_user_number(comp_user_id)
                    username_display = f"Usuario {user_number}" if user_number else f"Usuario {comp_user_id}"
                percent = 0
                if compression_id in compression_progress:
                    progress_data = compression_progress[compression_id]
                    percent = progress_data.get("percent", 0)
                progress_bar = create_mini_progress_bar(percent)
                response += f"» {username_display} ➧ {progress_bar}\n"
        else:
            response += "**• Ninguno**\n"

        response += f"\n📥 **Videos descargados esperando compresión:**\n"
        if downloaded_videos:
            user_video_counts = {}
            for video in downloaded_videos:
                video_user_id = video.get("user_id")
                user_video_counts[video_user_id] = user_video_counts.get(video_user_id, 0) + 1
            for i, (video_user_id, count) in enumerate(user_video_counts.items(), 1):
                # Mostrar "Tu*" si el usuario coincide con el que consulta
                if user_id is not None and video_user_id == user_id:
                    username_display = "(Tu)"
                else:
                    user_number = get_user_number(video_user_id)
                    username_display = f"Usuario {user_number}" if user_number else f"Usuario {video_user_id}"
                response += f"» {i}. {username_display} ({count} videos)\n" if count > 1 else f"» {i}. {username_display}\n"
        else:
            response += "**• Ninguno**\n"

        unique_active_users = len(set(comp["user_id"] for comp in active_compr))
        unique_downloaded_users = len(set(video["user_id"] for video in downloaded_videos))
        response += f"\n📈 **Resumen total**:\n"
        response += f"   • Comprimiendo: {unique_active_users} usuario{'s' if unique_active_users != 1 else ''}\n"
        response += f"   • Descargados: {unique_downloaded_users} usuario{'s' if unique_downloaded_users != 1 else ''}\n"

        if user_id in admin_users:
            response += f"\n👑 **Vista de administrador:**\n"
            response += f"• Descargas activas: {active_downloads_count}/{max_downloads}\n"
            response += f"• Videos descargados en cola: {downloaded_count}\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_queue"),
             InlineKeyboardButton("❌ Cerrar", callback_data="close_queue")]
        ])
        return response, keyboard
    except Exception as e:
        logger.error(f"❌Error en get_queue_status: {e}")
        return "❌ **Error al obtener el estado de la cola**", None

# ======================== NUEVA FUNCIÓN PARA ACTUALIZACIÓN AUTOMÁTICA DE LA COLA ======================== #
async def update_queue_loop(chat_id: int, message_id: int, user_id: int):
    """Bucle que actualiza el mensaje de la cola cada 6 segundos."""
    try:
        while True:
            await asyncio.sleep(6)
            queue_status, keyboard = await get_queue_status(user_id)
            try:
                await app.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=queue_status,
                    reply_markup=keyboard
                )
            except MessageNotModified:
                pass  # No hubo cambios
            except Exception as e:
                logger.error(f"Error actualizando mensaje de cola {message_id}: {e}")
                break  # Si el mensaje ya no existe, salimos
    except asyncio.CancelledError:
        logger.info(f"Tarea de actualización de cola {message_id} cancelada")
    finally:
        # Limpiar la entrada del diccionario si aún existe
        if user_id in user_queue_tasks:
            data = user_queue_tasks.get(user_id)
            if data and data.get("message_id") == message_id and data.get("chat_id") == chat_id:
                del user_queue_tasks[user_id]

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client, message):
    user_id = message.from_user.id
    user_compression_ids = get_user_compression_ids(user_id)
    if user_compression_ids:
        canceled_count = 0
        for compression_id in user_compression_ids:
            if cancel_compression_task(compression_id):
                task_info = cancel_tasks.get(compression_id, {})
                progress_message_id = task_info.get("progress_message_id")
                if progress_message_id:
                    try:
                        await app.delete_messages(message.chat.id, progress_message_id)
                        if compression_id in active_messages:
                            del active_messages[compression_id]
                    except Exception as e:
                        logger.error(f"❌Error eliminando mensaje de progreso: {e}")
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                await cleanup_compression_data(compression_id)
                remove_compression_progress(compression_id)
                canceled_count += 1
        if canceled_count > 0:
            await send_protected_message(message.chat.id, f"⛔ **{canceled_count} compresión(es) cancelada(s)** ⛔")
        else:
            await send_protected_message(message.chat.id, "⚠️ **No se pudieron cancelar las operaciones activas**")
    else:
        result = pending_col.delete_many({"user_id": user_id})
        downloaded_result = downloaded_videos_col.delete_many({"user_id": user_id})
        total_canceled = result.deleted_count + downloaded_result.deleted_count
        if total_canceled > 0:
            await send_protected_message(message.chat.id, f"⛔ **Se cancelaron {total_canceled} tareas pendientes en la cola.** ⛔")
            await update_all_download_waiting_messages()
            await update_all_compression_waiting_messages()
        else:
            await send_protected_message(message.chat.id, "ℹ️ **No tienes operaciones activas ni en cola para cancelar.**")
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"❌Error borrando mensaje /cancel: {e}")

@app.on_message(filters.command("cancelqueue") & filters.private)
async def cancel_queue_command(client, message):
    try:
        user_id = message.from_user.id
        if user_id in ban_users:
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_denied_access_message(message.chat.id)
            return
        user_queue = list(pending_col.find({"user_id": user_id}).sort("seq", 1))
        if not user_queue:
            await send_protected_message(message.chat.id, "📋**No tienes videos en la cola de compresión.**")
            return
        parts = message.text.split()
        if len(parts) == 1:
            response = "**Tus videos en cola:**\n\n"
            for i, item in enumerate(user_queue, 1):
                file_name = item.get("file_name", "Sin nombre")
                timestamp = item.get("timestamp")
                time_str = timestamp.strftime("%H:%M:%S") if timestamp else "¿?"
                response += f"{i}. `{file_name}` (⏰ {time_str})\n"
            response += "\nPara cancelar un video, usa:\n/cancelqueue+num <num>\nPara cancelar todos, usa:\n/cancelqueue_all"
            await send_protected_message(message.chat.id, response)
            return
        if parts[1] == "_all":
            wait_message_ids = []
            for item in user_queue:
                wait_msg_id = item.get("wait_message_id")
                if wait_msg_id:
                    wait_message_ids.append(wait_msg_id)
            result = pending_col.delete_many({"user_id": user_id})
            downloaded_result = downloaded_videos_col.delete_many({"user_id": user_id})
            try:
                if wait_message_ids:
                    await app.delete_messages(chat_id=message.chat.id, message_ids=wait_message_ids)
            except Exception as e:
                logger.error(f"❌Error eliminando mensajes de espera: {e}")
            await send_protected_message(message.chat.id, f"✅ **Se cancelaron todos los videos de tu cola**\n• Videos eliminados de cola: {result.deleted_count}\n• Videos descargados eliminados: {downloaded_result.deleted_count}")
            await update_all_download_waiting_messages()
            await update_all_compression_waiting_messages()
            return
        try:
            index = int(parts[1]) - 1
            if index < 0 or index >= len(user_queue):
                await send_protected_message(message.chat.id, f"❌ **Número inválido.** Debe ser entre 1 y {len(user_queue)}")
                return
            video_to_cancel = user_queue[index]
            compression_id = video_to_cancel.get("compression_id")
            wait_message_id = video_to_cancel.get("wait_message_id")
            await cleanup_compression_data(compression_id)
            try:
                if wait_message_id:
                    await app.delete_messages(chat_id=message.chat.id, message_ids=[wait_message_id])
            except Exception as e:
                logger.error(f"❌Error eliminando mensaje de espera: {e}")
            await send_protected_message(message.chat.id, f"**Video cancelado:** `{video_to_cancel.get('file_name', 'Sin nombre')}`\n\n✅ Eliminado de la cola de compresión.")
            await update_all_download_waiting_messages()
        except ValueError:
            await send_protected_message(message.chat.id, "**Usa** /cancelqueue para ver la lista de la cola **o** /cancelqueue_all para eliminar todos los vídeos de la cola")
    except Exception as e:
        logger.error(f"❌Error en cancel_queue_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "**Error al procesar la solicitud.**")

async def has_active_compression(user_id: int) -> bool:
    return bool(active_compressions_col.find_one({"user_id": user_id}))

async def add_active_compression(compression_id: str, user_id: int, file_id: str, file_name: str):
    active_compressions_col.insert_one({"compression_id": compression_id, "user_id": user_id, "file_id": file_id, "file_name": file_name, "start_time": datetime.datetime.now()})

async def remove_active_compression(compression_id: str):
    active_compressions_col.delete_one({"compression_id": compression_id})

async def get_active_compressions_count(user_id: int) -> int:
    return active_compressions_col.count_documents({"user_id": user_id})

async def add_downloaded_video(user_id: int, file_path: str, file_name: str, compression_id: str, chat_id: int, wait_msg_id: int, caption: str = None, custom_settings: dict = None):
    doc = {
        "user_id": user_id,
        "file_path": file_path,
        "file_name": file_name,
        "compression_id": compression_id,
        "chat_id": chat_id,
        "wait_message_id": wait_msg_id,
        "timestamp": datetime.datetime.now(),
        "original_caption": caption,
        "custom_settings": custom_settings
    }
    downloaded_videos_col.insert_one(doc)

async def remove_downloaded_video(compression_id: str):
    downloaded_videos_col.delete_one({"compression_id": compression_id})

async def get_next_downloaded_video():
    return downloaded_videos_col.find_one().sort("timestamp", 1)

async def has_downloaded_videos(user_id: int) -> bool:
    return bool(downloaded_videos_col.find_one({"user_id": user_id}))

async def get_user_downloaded_count(user_id: int) -> int:
    return downloaded_videos_col.count_documents({"user_id": user_id})

async def has_pending_confirmation(user_id: int) -> bool:
    now = datetime.datetime.now()
    expiration_time = now - datetime.timedelta(minutes=10)
    pending_confirmations_col.delete_many({"user_id": user_id, "timestamp": {"$lt": expiration_time}})
    return bool(pending_confirmations_col.find_one({"user_id": user_id}))

async def create_confirmation(user_id: int, chat_id: int, message_id: int, file_id: str, file_name: str, caption: str = None):
    pending_confirmations_col.delete_many({"user_id": user_id})
    return pending_confirmations_col.insert_one({"user_id": user_id, "chat_id": chat_id, "message_id": message_id, "file_id": file_id, "file_name": file_name, "timestamp": datetime.datetime.now(), "caption": caption}).inserted_id

async def delete_confirmation(confirmation_id: ObjectId):
    pending_confirmations_col.delete_one({"_id": confirmation_id})

async def get_confirmation(confirmation_id: ObjectId):
    return pending_confirmations_col.find_one({"_id": confirmation_id})

async def register_new_user(user_id: int):
    if not users_col.find_one({"user_id": user_id}):
        logger.info(f"⛔Usuario no registrado: {user_id}")

async def should_protect_content(user_id: int) -> bool:
    if user_id in admin_users:
        return False
    user_plan = await get_user_plan(user_id)
    return user_plan is not None and user_plan.get("plan") == "standard"

async def send_protected_message(chat_id: int, text: str, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_message(chat_id, text, protect_content=protect, **kwargs)

async def send_protected_video(chat_id: int, video: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_video(chat_id, video, caption=caption, protect_content=protect, **kwargs)

async def send_protected_photo(chat_id: int, photo: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_photo(chat_id, photo, caption=caption, protect_content=protect, **kwargs)

async def send_protected_document(chat_id: int, document: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_document(chat_id, document, caption=caption, protect_content=protect, **kwargs)

async def send_protected_audio(chat_id: int, audio: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_audio(chat_id, audio, caption=caption, protect_content=protect, **kwargs)

async def send_protected_voice(chat_id: int, voice: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_voice(chat_id, voice, caption=caption, protect_content=protect, **kwargs)

async def get_user_queue_limit(user_id: int) -> int:
    user_plan = await get_user_plan(user_id)
    if user_plan is None:
        return 1
    plan = user_plan["plan"]
    if plan == "ultra":
        return ULTRA_QUEUE_LIMIT
    elif plan == "premium":
        return PREMIUM_QUEUE_LIMIT
    elif plan == "pro":
        return PRO_QUEUE_LIMIT
    else:
        return 1

def generate_temp_key(plan: str, duration_value: int, duration_unit: str):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    created_at = datetime.datetime.now()
    if duration_unit == 'minutes':
        expires_at = created_at + datetime.timedelta(minutes=duration_value)
    elif duration_unit == 'hours':
        expires_at = created_at + datetime.timedelta(hours=duration_value)
    else:
        expires_at = created_at + datetime.timedelta(days=duration_value)
    temp_keys_col.insert_one({"key": key, "plan": plan, "created_at": created_at, "expires_at": expires_at, "used": False, "duration_value": duration_value, "duration_unit": duration_unit})
    return key

def is_valid_temp_key(key):
    now = datetime.datetime.now()
    key_data = temp_keys_col.find_one({"key": key, "used": False, "expires_at": {"$gt": now}})
    return bool(key_data)

def mark_key_used(key):
    temp_keys_col.update_one({"key": key}, {"$set": {"used": True}})

@app.on_message(filters.command("generatekey") & filters.user(admin_users))
async def generate_key_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 4:
            await message.reply("⚠️ Formato: /generatekey <plan> <cantidad> <unidad>\nEjemplo: /generatekey standard 2 hours\nUnidades válidas: minutes, hours, days")
            return
        plan = parts[1].lower()
        valid_plans = ["standard", "pro", "premium"]
        if plan not in valid_plans:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(valid_plans)}")
            return
        duration_value = int(parts[2])
        if duration_value <= 0:
            await message.reply("⚠️ La cantidad debe ser un número positivo")
            return
        duration_unit = parts[3].lower()
        valid_units = ["minutes", "hours", "days"]
        if duration_unit not in valid_units:
            await message.reply(f"⚠️ Unidad inválida. Opciones válidas: {', '.join(valid_units)}")
            return
        key = generate_temp_key(plan, duration_value, duration_unit)
        duration_text = f"{duration_value} {duration_unit}"
        if duration_value == 1:
            duration_text = duration_text[:-1]
        await message.reply(f"**Clave {plan.capitalize()} generada**\n\nClave: `{key}`\nVálida por: {duration_text}\n\nComparte esta clave con el usuario usando:\n`/key {key}`")
    except Exception as e:
        logger.error(f"❌Error generando clave: {e}", exc_info=True)
        await message.reply("⚠️ Error al generar la clave")

@app.on_message(filters.command("listkeys") & filters.user(admin_users))
async def list_keys_command(client, message):
    try:
        now = datetime.datetime.now()
        keys = list(temp_keys_col.find({"used": False, "expires_at": {"$gt": now}}))
        if not keys:
            await message.reply("**No hay claves activas.**")
            return
        response = "**Claves temporales activas:**\n\n"
        for key in keys:
            expires_at = key["expires_at"]
            remaining = expires_at - now
            if remaining.days > 0:
                time_remaining = f"{remaining.days}d {remaining.seconds//3600}h"
            elif remaining.seconds >= 3600:
                time_remaining = f"{remaining.seconds//3600}h {(remaining.seconds%3600)//60}m"
            else:
                time_remaining = f"{remaining.seconds//60}m"
            duration_value = key.get("duration_value", 0)
            duration_unit = key.get("duration_unit", "days")
            duration_display = f"{duration_value} {duration_unit}"
            if duration_value == 1:
                duration_display = duration_display[:-1]
            response += f"• `{key['key']}`\n  ↳ Plan: {key['plan'].capitalize()}\n  ↳ Duración: {duration_display}\n  ⏱ Expira en: {time_remaining}\n\n"
        await message.reply(response)
    except Exception as e:
        logger.error(f"❌Error listando claves: {e}", exc_info=True)
        await message.reply("⚠️ Error al listar claves")

@app.on_message(filters.command("delkeys") & filters.user(admin_users))
async def del_keys_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("⚠️ Formato: /delkeys <key> o /delkeys --all")
            return
        option = parts[1]
        if option == "--all":
            result = temp_keys_col.delete_many({})
            await message.reply(f"**Se eliminaron {result.deleted_count} claves.**")
        else:
            key = option
            result = temp_keys_col.delete_one({"key": key})
            if result.deleted_count > 0:
                await message.reply(f"✅ **Clave {key} eliminada.**")
            else:
                await message.reply("⚠️ **Clave no encontrada.**")
    except Exception as e:
        logger.error(f"❌Error eliminando claves: {e}", exc_info=True)
        await message.reply("⚠️ **Error al eliminar claves**")

PLAN_DURATIONS = {"standard": "7 días", "pro": "15 días", "premium": "30 días", "ultra": "Ilimitado"}

async def reset_expiry_notification_flags(user_id: int):
    users_col.update_one(
        {"user_id": user_id},
        {"$set": {"expiry_reminder_sent": False, "expiry_notification_sent": False}}
    )
    logger.info(f"✅Bandera de notificación de expiración reiniciada para usuario {user_id}")

async def send_expiry_reminder(user_id: int, expires_at: datetime.datetime):
    text = (
        "🔔 **Notificación:**\n\n"
        "Te notificamos que tu acceso está a punto de terminar, te recordamos que puedes ampliar tu tiempo de uso adquiriendo un nuevo plan en el bot."
    )
    try:
        await send_protected_message(user_id, text)
        logger.info(f"✅Recordatorio de expiración enviado a usuario {user_id} (expira: {expires_at})")
    except Exception as e:
        logger.error(f"❌Error enviando recordatorio de expiración a {user_id}: {e}")

async def send_expiry_notification(user_id: int, expires_at: datetime.datetime):
    text = (
        "🔔 **Notificación:**\n\n"
        "Te notificamos que tu acceso ha expirado, puede volver a tener acceso adquiriendo un nuevo plan en el bot."
    )
    try:
        await send_protected_message(user_id, text)
        logger.info(f"✅Notificación de expiración enviada a usuario {user_id} (expiró: {expires_at})")
    except Exception as e:
        logger.error(f"❌Error enviando notificación de expiración a {user_id}: {e}")

async def check_expiring_plans():
    now = datetime.datetime.now()
    one_day_later = now + datetime.timedelta(days=1)
    query = {
        "plan": {"$in": ["standard", "pro", "premium"]},
        "expires_at": {"$gte": now, "$lte": one_day_later},
        "expiry_reminder_sent": {"$ne": True}
    }
    users_to_remind = users_col.find(query)
    count = 0
    for user in users_to_remind:
        user_id = user["user_id"]
        expires_at = user["expires_at"]
        if expires_at - now <= datetime.timedelta(days=1):
            await send_expiry_reminder(user_id, expires_at)
            users_col.update_one({"_id": user["_id"]}, {"$set": {"expiry_reminder_sent": True}})
            count += 1
    if count:
        logger.info(f"📢 Recordatorios de expiración enviados a {count} usuario(s)")

async def check_expired_plans():
    now = datetime.datetime.now()
    query = {
        "plan": {"$in": ["standard", "pro", "premium"]},
        "expires_at": {"$lt": now},
        "expiry_notification_sent": {"$ne": True}
    }
    users_expired = users_col.find(query)
    count = 0
    for user in users_expired:
        user_id = user["user_id"]
        expires_at = user["expires_at"]
        await send_expiry_notification(user_id, expires_at)
        users_col.update_one({"_id": user["_id"]}, {"$set": {"expiry_notification_sent": True}})
        count += 1
    if count:
        logger.info(f"📢 Notificaciones de expiración enviadas a {count} usuario(s)")

async def expiry_check_loop():
    global last_auto_expiry_check   
    while True:
        try:
            await asyncio.sleep(180)
            await check_expiring_plans()
            await check_expired_plans()
            last_auto_expiry_check = datetime.datetime.now()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌Error en expiry_check_loop: {e}", exc_info=True)
            await asyncio.sleep(3600)

@app.on_message(filters.command("checkexpiry") & filters.user(admin_users))
async def manual_check_expiry_command(client, message):
    await message.reply("🔄 Verificando planes próximos a expirar y expirados")
    await check_expiring_plans()
    await check_expired_plans()
    if last_auto_expiry_check is None:
        time_str = "Todavía"
    else:
        delta = datetime.datetime.now() - last_auto_expiry_check
        minutes = int(delta.total_seconds() // 60)
        seconds = int(delta.total_seconds() % 60)
        time_str = f"{minutes} min {seconds} seg" if minutes > 0 else f"{seconds} seg"
    await message.reply(f"✅ Verificación manual completada.\n\n📅 Última verificación automática: hace {time_str}")

async def get_user_plan(user_id: int) -> dict:
    user = users_col.find_one({"user_id": user_id})
    now = datetime.datetime.now()
    if user:
        plan = user.get("plan")
        if plan is None:
            users_col.delete_one({"user_id": user_id})
            return None
        if plan != "ultra":
            expires_at = user.get("expires_at")
            if expires_at and now > expires_at:
                users_col.delete_one({"user_id": user_id})
                return None
        update_data = {}
        if "last_used_date" not in user:
            update_data["last_used_date"] = None
        if update_data:
            users_col.update_one({"user_id": user_id}, {"$set": update_data})
            user.update(update_data)
        return user
    return None

async def set_user_plan(user_id: int, plan: str, notify: bool = True, expires_at: datetime = None):
    if plan not in PLAN_DURATIONS:
        return False
    if plan == "ultra":
        expires_at = None
    else:
        if expires_at is None:
            now = datetime.datetime.now()
            if plan == "standard":
                expires_at = now + datetime.timedelta(days=7)
            elif plan == "pro":
                expires_at = now + datetime.timedelta(days=15)
            elif plan == "premium":
                expires_at = now + datetime.timedelta(days=30)
    user_data = {"plan": plan}
    if expires_at is not None:
        user_data["expires_at"] = expires_at
    existing_user = users_col.find_one({"user_id": user_id})
    if not existing_user:
        user_data["join_date"] = datetime.datetime.now()
        # Asignar número de usuario secuencial
        user_data["user_number"] = get_next_user_seq()
    else:
        # Si el usuario ya existe pero no tiene user_number (por ejemplo, de versiones anteriores), se lo asignamos
        if "user_number" not in existing_user:
            user_data["user_number"] = get_next_user_seq()
    await reset_expiry_notification_flags(user_id)
    users_col.update_one({"user_id": user_id}, {"$set": user_data}, upsert=True)
    if notify:
        try:
            await send_protected_message(
                user_id,
                f"**➡️ Se te ha asignado un nuevo plan ✅**\n\nUse el comando /start para iniciar en el bot\n\n"
                f"• **Plan**: {plan.capitalize()}\n• **Duración**: {PLAN_DURATIONS[plan]}\n"
                f"• **Videos disponibles**: Ilimitados\n\n¡Disfruta de tus beneficios!"
            )
        except Exception as e:
            logger.error(f"❌Error notificando al usuario {user_id}: {e}")
    return True

async def check_user_limit(user_id: int) -> bool:
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return True
    return False

async def get_plan_info(user_id: int):
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return "🚫**No tienes un plan activo**🚫\n\n⬇️**Toque para ver nuestros planes**⬇️", None
    plan_name = user["plan"].capitalize()
    expires_at = user.get("expires_at")
    expires_text = "No expira"
    if isinstance(expires_at, datetime.datetime):
        now = datetime.datetime.now()
        time_remaining = expires_at - now
        if time_remaining.total_seconds() <= 0:
            expires_text = "Expirado"
        else:
            days = time_remaining.days
            hours = time_remaining.seconds // 3600
            minutes = (time_remaining.seconds % 3600) // 60
            seconds = time_remaining.seconds % 60
            if days > 0:
                expires_text = f"{days}d {hours}h {minutes}m {seconds}s"
            elif hours > 0:
                expires_text = f"{hours}h {minutes}m {seconds}s"
            else:
                expires_text = f"{minutes}m {seconds}s"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_plan"), InlineKeyboardButton("❌ Cerrar", callback_data="close_plan")]])
    return f"╭✠━━━━━━━━━━━━━━━━━━━━✠╮\n┠➣ **Plan actual**: {plan_name}\n┠➣ **Tiempo restante**:\n┠➣ {expires_text}\n╰✠━━━━━━━━━━━━━━━━━━━━✠╯", keyboard

async def has_pending_in_queue(user_id: int) -> bool:
    count = pending_col.count_documents({"user_id": user_id})
    return count > 0

def create_progress_bar(current, total, proceso, length=15):
    if total == 0:
        total = 1
    percent = (current / total) * 100
    filled = int(length * (current / total))
    bar = '⬢' * filled + '⬡' * (length - filled)
    return (f'    ╭━━━[🤖**Compress Fast**]━━━╮\n'
            f'┠ [{bar}] {percent:.1f}%\n'
            f'┠ **Procesado**: {sizeof_fmt(current)}/{sizeof_fmt(total)}\n'
            f'┠ **Estado**: __#{proceso}__')

last_progress_update = {}

async def progress_callback(current, total, msg, proceso, start_time):
    try:
        compression_key = None
        for comp_key, msg_id in active_messages.items():
            if msg_id == msg.id:
                compression_key = comp_key
                break
        if not compression_key:
            return
        compression_id = compression_key
        if isinstance(compression_key, str) and compression_key.endswith("_upload"):
            compression_id = compression_key.rsplit("_upload", 1)[0]
        if compression_id not in cancel_tasks:
            raise asyncio.CancelledError(f"Tarea {compression_id} cancelada durante {proceso}")
        progress_data = compression_progress.get(compression_id)
        if progress_data:
            stage = progress_data.get("stage")
            file_name = progress_data.get("file_name", "")
        else:
            stage = "unknown"
            file_name = ""
        if proceso == "DESCARGA" and stage == "download_starting" and current == 0:
            try:
                await msg.edit(
                    f"╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
                    f"┠⬇️ **Preparando descarga...** ⬇️\n"
                    f"┠⏱ **Por favor espere...**\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])
                )
            except MessageNotModified:
                pass
            return
        if proceso == "DESCARGA" and stage == "download_starting" and current > 0:
            update_compression_progress(compression_id, "download", current, total, (current/total)*100, file_name)
        now = datetime.datetime.now()
        key = (msg.chat.id, msg.id)
        last_time = last_progress_update.get(key)
        if last_time and (now - last_time).total_seconds() < 3:
            return
        last_progress_update[key] = now
        elapsed = time.time() - start_time
        percentage = (current / total) if total and total > 0 else 0
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        progress_bar = create_progress_bar(current, total, proceso)
        elapsed_str = format_time(elapsed)
        remaining_str = format_time(eta)
        stage_for_update = "download" if proceso == "DESCARGA" else "upload"
        update_compression_progress(compression_id, stage_for_update, current, total, percentage * 100, file_name)
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])

        # ========== CÁLCULO DE HORA DE FINALIZACIÓN (Cuba) usando zoneinfo ==========
        cuba_tz = zoneinfo.ZoneInfo("America/Havana")
        now_cuba = datetime.datetime.now(cuba_tz)
        eta_seconds = eta if eta > 0 else 0
        finish_time = now_cuba + datetime.timedelta(seconds=eta_seconds)
        finish_str = finish_time.strftime("%I:%M %p")  # AM/PM en mayúsculas
        # ========================================================================

        try:
            await msg.edit(
                f"   {progress_bar}\n"
                f"┠ **Velocidad** {sizeof_fmt(speed)}/s\n"
                f"┠ **Tiempo transcurrido:** {elapsed_str}\n"
                f"┠ **Tiempo restante:** {remaining_str}\n"
                f"┠ **Finaliza a las:** {finish_str}\n"
                f"╰━━━━━━━━━━━━━━━━━━╯\n",
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"❌Error editando mensaje de progreso: {e}")
            if compression_key in active_messages:
                del active_messages[compression_key]
    except asyncio.CancelledError as e:
        raise e
    except Exception as e:
        logger.error(f"❌Error en progress_callback: {e}", exc_info=True)

# ======================== NUEVAS FUNCIONES PARA ACTUALIZAR POSICIONES ======================== #

async def update_all_download_waiting_messages():
    pending = list(pending_col.find().sort("seq", 1))
    for idx, item in enumerate(pending, start=1):
        wait_msg_id = item.get("wait_message_id")
        if not wait_msg_id:
            continue
        compression_id = item["compression_id"]
        chat_id = item.get("chat_id")
        if not chat_id:
            continue
        text = (
            "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
            f"┠⏳ **Preparando descarga...** ⏳\n"
            f"┠📊 **Posición en cola:** #{idx}\n"
            f"┠⏱ **Esperando slot disponible...**\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯"
        )
        try:
            await app.edit_message_text(chat_id=chat_id, message_id=wait_msg_id, text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]]))
        except Exception as e:
            logger.error(f"❌Error actualizando mensaje de descarga {compression_id}: {e}")

# ======================== ACTUALIZACIÓN DE MENSAJES DE ESPERA DE COMPRESIÓN CON HORA ESTIMADA (SOLO PARA EL PRIMERO) ======================== #
async def update_all_compression_waiting_messages():
    # Obtener las horas estimadas para cada video en cola
    estimated_times = await calculate_estimated_start_times()
    times_dict = {item["compression_id"]: item for item in estimated_times}

    downloaded = list(downloaded_videos_col.find().sort("timestamp", 1))
    for idx, item in enumerate(downloaded, start=1):
        compression_id = item["compression_id"]
        wait_msg_id = item.get("wait_message_id")
        if not wait_msg_id:
            continue
        file_name = item.get("file_name", "Sin nombre")
        chat_id = item.get("chat_id")
        if not chat_id:
            continue

        # Obtener la hora estimada para este video
        time_info = times_dict.get(compression_id)
        if time_info:
            estimated_start = time_info["estimated_start"]
            position = time_info["position"]
            start_text = format_start_time(estimated_start)
        else:
            # Fallback: si no está en la lista, calcular posición manualmente
            start_text = "Sin calcular..."
            position = idx

        text = (
            "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
            "┠📥 **Video descargado**\n"
            f"┠📁 **Archivo:** `{file_name}`\n"
            f"┠📊 **Posición en cola:** {position}\n"
            "┠🔄 **Agregado a la cola de compresión**\n"
            f"┠ 🗜️**Comienza a las:** {start_text}\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯"
        )
        try:
            await app.edit_message_text(chat_id=chat_id, message_id=wait_msg_id, text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]]))
        except Exception as e:
            logger.error(f"❌Error actualizando mensaje de compresión {compression_id}: {e}")

# ======================== BUCLE DE ACTUALIZACIÓN PERIÓDICA (CADA 80 Segundos) ======================== #
async def update_compression_waiting_messages_loop():
    while True:
        await asyncio.sleep(80) 
        try:
            await update_all_compression_waiting_messages()
            logger.debug("Mensajes de cola de compresión actualizados (loop de 80 Seg)")
        except Exception as e:
            logger.error(f"Error en update_compression_waiting_messages_loop: {e}")

# ======================== FIN DE NUEVAS FUNCIONES ======================== #

async def get_download_queue_position(compression_id: str) -> int:
    pending_item = pending_col.find_one({"compression_id": compression_id})
    if not pending_item:
        return 0
    current_seq = pending_item["seq"]
    count = pending_col.count_documents({"seq": {"$lt": current_seq}})
    return count + 1

# ========= FUNCIÓN DE DESCARGA CON NOMBRE ÚNICO =========
async def download_file_immediately_worker(compression_id, user_id, chat_id, original_message_id, file_obj, file_name, wait_msg, caption=None, custom_settings=None):
    pending_check = pending_col.find_one({"compression_id": compression_id})
    if not pending_check:
        logger.info(f"✅Tarea {compression_id} ya no existe en pending_col, omitiendo descarga.")
        try:
            if wait_msg:
                await wait_msg.delete()
        except:
            pass
        return False

    base, ext = os.path.splitext(file_name)
    unique_filename = f"{compression_id}_{base}{ext}"
    temp_dir = tempfile.gettempdir()
    original_video_path = os.path.join(temp_dir, unique_filename)

    try:
        current_task = asyncio.current_task()
        register_cancelable_task(compression_id, "download", current_task, original_message_id=original_message_id, progress_message_id=wait_msg.id)
        update_compression_progress(compression_id, "download_starting", 0, 100, 0, file_name)

        async def update_message(new_text, reply_markup=None, force_new=False):
            nonlocal wait_msg
            try:
                if not force_new and wait_msg:
                    try:
                        await wait_msg.edit_text(new_text, reply_markup=reply_markup)
                        return wait_msg
                    except Exception as e:
                        logger.warning(f"⛔Error editando wait_msg: {e}, se creará uno nuevo")
                new_msg = await app.send_message(chat_id, new_text, reply_to_message_id=original_message_id, reply_markup=reply_markup)
                if wait_msg and wait_msg.id != new_msg.id:
                    try:
                        await wait_msg.delete()
                    except Exception as e:
                        logger.error(f"❌Error eliminando wait_msg anterior: {e}")
                wait_msg = new_msg
                pending_col.update_one({"compression_id": compression_id}, {"$set": {"wait_message_id": wait_msg.id}})
                active_messages[compression_id] = wait_msg.id
                return wait_msg
            except Exception as e:
                logger.error(f"❌Error en update_message: {e}")
                raise

        queue_position = await get_download_queue_position(compression_id)
        waiting_text = (
            "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
            "┠⏳ **Preparando descarga...** ⏳\n"
            f"┠📊 **Posición en cola:** #{queue_position}\n"
            f"┠⏱ **Esperando slot disponible...**\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯"
        )
        await update_message(waiting_text, InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]]))
        active_messages[compression_id] = wait_msg.id

        update_compression_progress(compression_id, "download_starting", 0, 100, 0, file_name)
        starting_text = (
            "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
            "┠⬇️ **Iniciando descarga...** ⬇️\n"
            "┠⏱ **Por favor espere...**\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯"
        )
        await update_message(starting_text, InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]]))
        await asyncio.sleep(1)

        try:
            start_download_time = time.time()
            original_video_path = await app.download_media(
                file_obj,
                file_name=unique_filename,
                progress=progress_callback,
                progress_args=(wait_msg, "DESCARGA", start_download_time)
            )
            if compression_id not in cancel_tasks:
                if original_video_path and os.path.exists(original_video_path):
                    os.remove(original_video_path)
                raise asyncio.CancelledError("Descarga cancelada")
            logger.info(f"📥Video descargado: {original_video_path}✅")
            file_size = os.path.getsize(original_video_path)
            await update_daily_stats_download(file_size)
            pending_col.delete_one({"compression_id": compression_id})
            await update_all_download_waiting_messages()
            await add_downloaded_video(user_id, original_video_path, file_name, compression_id, chat_id, wait_msg.id, caption, custom_settings)
            downloaded_count = downloaded_videos_col.count_documents({})

            # --- Calcular hora estimada de inicio para este video (solo si es el primero) ---
            estimated_times = await calculate_estimated_start_times()
            time_info = next((t for t in estimated_times if t["compression_id"] == compression_id), None)
            if time_info:
                position = time_info["position"]
                start_text = format_start_time(time_info["estimated_start"])
            else:
                # Fallback: si no está en la lista, usar posición actual
                position = downloaded_count
                start_text = "Sin calcular..."

            completion_text = (
                "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
                "┠📥 **Video descargado**\n"
                f"┠📁 **Archivo:** `{file_name}`\n"
                f"┠📊 **Posición en cola:** #{position}\n"
                "┠🔄 **Agregado a la cola de compresión**\n"
                f"┠ 🗜️**Comienza a las:** {start_text}\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯"
            )
            await update_message(completion_text, InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]]))
            await compression_processing_queue.put({"compression_id": compression_id, "user_id": user_id, "original_video_path": original_video_path, "file_name": file_name, "chat_id": chat_id, "original_message_id": original_message_id, "wait_msg_id": wait_msg.id, "wait_msg": wait_msg, "caption": caption, "custom_settings": custom_settings})
            return True
        except asyncio.CancelledError:
            logger.info(f"✅Descarga cancelada para compresión {compression_id}")
            if original_video_path and os.path.exists(original_video_path):
                try:
                    os.remove(original_video_path)
                    logger.info(f"✅Archivo temporal eliminado: {original_video_path}")
                except Exception as e:
                    logger.error(f"❌Error eliminando archivo {original_video_path}: {e}")
            cancel_text = (
                "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
                "┠⛔ **Descarga cancelada** ⛔\n"
                f"┠📁 **Archivo:** `{file_name}`\n"
                "┠❌ **Operación interrumpida**\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯"
            )
            await update_message(cancel_text)
            pending_col.delete_one({"compression_id": compression_id})
            await update_all_download_waiting_messages()
            return False
        except Exception as e:
            logger.error(f"❌Error en descarga: {e}", exc_info=True)
            if original_video_path and os.path.exists(original_video_path):
                try:
                    os.remove(original_video_path)
                except:
                    pass
            error_text = (
                "╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
                "┠❌ **Error en la descarga** ❌\n"
                f"┠📁 **Archivo:** `{file_name}`\n"
                f"┠⚠️ **Error:** {str(e)[:100]}\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯"
            )
            await update_message(error_text)
            pending_col.delete_one({"compression_id": compression_id})
            await update_all_download_waiting_messages()
            return False
        finally:
            if compression_id in active_messages:
                del active_messages[compression_id]
    except Exception as e:
        logger.error(f"❌Error en download_file_immediately_worker: {e}", exc_info=True)
        return False

# ========= WORKER DE DESCARGA (cola priorizada) CON RESILIENCIA =========
async def download_worker():
    while True:
        try:
            (seq, compression_id, user_id, chat_id, original_message_id,
             file_obj, file_name, wait_msg, caption, custom_settings) = await download_queue.get()
            logger.info(f"⬇️ Worker de descarga iniciando seq={seq} - {file_name}")
            pending_check = pending_col.find_one({"compression_id": compression_id})
            if not pending_check:
                logger.info(f"⏭️ Tarea {compression_id} ya no está pendiente (fue cancelada), omitiendo.")
                try:
                    if wait_msg:
                        await wait_msg.delete()
                except:
                    pass
                download_queue.task_done()
                continue
            await download_file_immediately_worker(
                compression_id, user_id, chat_id, original_message_id,
                file_obj, file_name, wait_msg, caption, custom_settings
            )
        except asyncio.CancelledError:
            logger.warning("⚠️ Worker de descarga cancelado. Saliendo...")
            break
        except Exception as e:
            logger.error(f"❌Error crítico en download_worker: {e}", exc_info=True)
            try:
                download_queue.task_done()
            except:
                pass
            await asyncio.sleep(1)
        finally:
            try:
                download_queue.task_done()
            except:
                pass

async def start_download_worker():
    global download_worker_task
    if download_worker_task is None or download_worker_task.done():
        if download_worker_task is not None:
            try:
                if download_worker_task.exception():
                    logger.error(f"❌ Worker de descarga murió con excepción: {download_worker_task.exception()}")
            except:
                pass
        logger.info("🔄 Iniciando nuevo worker de descarga...")
        download_worker_task = asyncio.create_task(download_worker())
        logger.info("✅ Worker de descarga iniciado correctamente.")
    else:
        logger.debug("Worker de descarga ya está activo.")

async def watchdog_download_worker():
    while True:
        await asyncio.sleep(30)
        try:
            global download_worker_task
            if download_worker_task is None or download_worker_task.done():
                logger.warning("⚠️ Watchdog: Worker de descarga detectado como muerto o no iniciado. Reiniciando...")
                await start_download_worker()
        except Exception as e:
            logger.error(f"❌Error en watchdog_download_worker: {e}", exc_info=True)

# ========= WORKER DE COMPRESIÓN (original) =========
async def process_compression_queue():
    while True:
        task = None
        try:
            task = await compression_processing_queue.get()
            video_data = downloaded_videos_col.find_one({"compression_id": task["compression_id"]})
            if not video_data:
                logger.info(f"⛔Video cancelado, saltando: {task['file_name']}")
                continue
            start_msg = None
            if task.get("wait_msg"):
                try:
                    start_msg = await task["wait_msg"].edit("🗜️ **Iniciando compresión** 🎬")
                except Exception as e:
                    logger.warning(f"⛔No se pudo editar wait_msg, se creará uno nuevo: {e}")
                    task["wait_msg"] = None
            if start_msg is None:
                start_msg = await app.send_message(task["chat_id"], "🗜️ **Iniciando compresión** 🎬", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{task['compression_id']}")]]))
                downloaded_videos_col.update_one({"compression_id": task["compression_id"]}, {"$set": {"wait_msg_id": start_msg.id}})
            await remove_downloaded_video(task["compression_id"])
            await update_all_compression_waiting_messages()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, threading_compress_video_from_path, task, start_msg)
        except Exception as e:
            logger.error(f"❌Error procesando video de la cola: {e}", exc_info=True)
            if task:
                try:
                    await app.send_message(task["chat_id"], f"⚠️ Error al procesar el video: {str(e)}")
                except:
                    pass
        finally:
            if task is not None:
                compression_processing_queue.task_done()

def threading_compress_video_from_path(task, start_msg):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(compress_video_from_path(task, start_msg))
    loop.close()

async def compress_video_from_path(task, start_msg):
    cleaned_up = False
    try:
        compression_id = task["compression_id"]
        user_id = task["user_id"]
        original_video_path = task["original_video_path"]
        file_name = task["file_name"]
        chat_id = task["chat_id"]
        original_message_id = task["original_message_id"]
        wait_msg = task["wait_msg"]
        original_caption = task.get("caption")
        custom_settings = task.get("custom_settings")

        if not os.path.exists(original_video_path):
            if wait_msg:
                await wait_msg.edit_text("❌ **Error: Archivo no encontrado**")
            await remove_downloaded_video(compression_id)
            return

        if custom_settings:
            user_video_settings = {
                'resolution': custom_settings.get('resolution', DEFAULT_VIDEO_SETTINGS['resolution']),
                'crf': custom_settings.get('crf', DEFAULT_VIDEO_SETTINGS['crf']),
                'audio_bitrate': custom_settings.get('audio_bitrate', DEFAULT_VIDEO_SETTINGS['audio_bitrate']),
                'fps': custom_settings.get('fps', DEFAULT_VIDEO_SETTINGS['fps']),
                'preset': DEFAULT_VIDEO_SETTINGS['preset'],
                'codec': DEFAULT_VIDEO_SETTINGS['codec']
            }
            res = custom_settings.get('resolution')
            if res in ['360', '480', '720']:
                if res == '360':
                    user_video_settings['resolution'] = '640x360'
                elif res == '480':
                    user_video_settings['resolution'] = '-2:480'
                elif res == '720':
                    user_video_settings['resolution'] = '-2:720'
        else:
            user_video_settings = await get_user_video_settings(user_id)

        await add_active_compression(compression_id, user_id, None, file_name)

        progress_bar = create_progress_bar(0, 100, "COMPRESIÓN")
        # ================== MENSAJE INICIAL CON "Calculando..." ==================
        msg = await app.send_message(
            chat_id=chat_id,
            text=f"   {progress_bar}\n"
                 f"┠ **Velocidad** 0.00B/s\n"
                 f"┠ **Tiempo transcurrido:** 00:00\n"
                 f"┠ **Tiempo restante:** 00:00\n"
                 f"┠ **Finaliza a las:** Calculando...\n"
                 f"╰━━━━━━━━━━━━━━━━━━╯\n",
            reply_to_message_id=original_message_id
        )
        active_messages[compression_id] = msg.id
        try:
            if start_msg:
                await start_msg.delete()
        except Exception:
            pass

        original_size = os.path.getsize(original_video_path)
        logger.info(f"✅Tamaño original: {original_size} bytes")
        await notify_group(app, await app.get_messages(chat_id, original_message_id), original_size, status="start")

        try:
            probe = ffmpeg.probe(original_video_path)
            dur_total = float(probe['format']['duration'])
            logger.info(f"✅Duración del video: {dur_total} segundos")
        except Exception as e:
            logger.error(f"❌Error obteniendo duración: {e}", exc_info=True)
            dur_total = 0

        if original_caption and original_caption.strip():
            sanitized = unicodedata.normalize('NFKD', original_caption).encode('ascii', 'ignore').decode('ascii')
            sanitized = re.sub(r'[^a-zA-Z0-9 _-]', '_', sanitized)
            sanitized = sanitized.strip().replace(' ', '_')
            sanitized = re.sub(r'_+', '_', sanitized)
            if not sanitized:
                sanitized = "video_comprimido"
            if len(sanitized) > 100:
                sanitized = sanitized[:100]
            base_name = sanitized
        else:
            base, ext = os.path.splitext(file_name)
            sanitized = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode('ascii')
            sanitized = re.sub(r'[^a-zA-Z0-9 _-]', '_', sanitized)
            sanitized = sanitized.strip().replace(' ', '_')
            sanitized = re.sub(r'_+', '_', sanitized)
            if not sanitized:
                sanitized = "video_comprimido"
            if len(sanitized) > 100:
                sanitized = sanitized[:100]
            base_name = sanitized + "_compressed"

        logger.info(f"✅Nombre base para el caption: {base_name}")

        temp_dir = os.path.dirname(original_video_path)
        compressed_video_path = os.path.join(temp_dir, f"{base_name}.mp4")
        counter = 1
        while os.path.exists(compressed_video_path):
            compressed_video_path = os.path.join(temp_dir, f"{base_name}_{counter}.mp4")
            counter += 1
        logger.info(f"✅Ruta de compresión: {compressed_video_path}")

        drawtext_filter = f"drawtext=text='@CompressFastBot':x=w-tw-10:y=10:fontsize=20:fontcolor=white"
        ffmpeg_command = [
            'ffmpeg', '-y', '-i', original_video_path,
            '-vf', f"scale={user_video_settings['resolution']},{drawtext_filter}",
            '-crf', user_video_settings['crf'],
            '-b:a', user_video_settings['audio_bitrate'],
            '-r', user_video_settings['fps'],
            '-preset', user_video_settings['preset'],
            '-c:v', user_video_settings['codec'],
            compressed_video_path
        ]
        logger.info(f"✅Comando FFmpeg: {' '.join(ffmpeg_command)}")

        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True, bufsize=1)
            register_cancelable_task(compression_id, "ffmpeg", process, original_message_id=original_message_id, progress_message_id=msg.id)
            register_ffmpeg_process(compression_id, process)
            update_compression_progress(compression_id, "compression", 0, 100, 0, file_name)

            last_percent = 0
            last_update_time = 0
            time_pattern = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
            log_interval = 120
            last_log_time = time.time()
            cancelled = False

            while True:
                if compression_id not in cancel_tasks:
                    logger.info(f"Compresión {compression_id} cancelada detectada en el bucle")
                    cancelled = True
                    process.kill()
                    break
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    if "error" in line.lower() or "warning" in line.lower():
                        logger.warning(f"⛔FFmpeg [{compression_id}]: {line.strip()}")
                    match = time_pattern.search(line)
                    if match and dur_total > 0:
                        time_str = match.group(1)
                        h, m, s = time_str.split(':')
                        current_time = int(h)*3600 + int(m)*60 + float(s)
                        percent = min(100, (current_time / dur_total) * 100)
                        compressed_size = 0
                        if os.path.exists(compressed_video_path):
                            compressed_size = os.path.getsize(compressed_video_path)
                        elapsed_time = datetime.datetime.now() - start_time
                        elapsed_seconds = elapsed_time.total_seconds()
                        remaining_seconds = (elapsed_seconds / percent) * (100 - percent) if percent > 0 else 0
                        elapsed_str = format_time(elapsed_seconds)
                        remaining_str = format_time(remaining_seconds)
                        update_compression_progress(compression_id, "compression", current_time, dur_total, percent, file_name)
                        now_time = time.time()
                        if now_time - last_log_time > log_interval:
                            logger.info(f"✅Compresión {compression_id}: {percent:.1f}% completado, tiempo transcurrido: {elapsed_str}, restante: {remaining_str}")
                            last_log_time = now_time
                        if percent - last_percent >= 5 or time.time() - last_update_time >= 5:
                            bar = create_compression_bar(percent)
                            cancel_button = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])

                            # ========== CÁLCULO DE HORA DE FINALIZACIÓN ==========
                            if percent == 0 or remaining_seconds == 0:
                                finish_str = "Calculando..."
                            else:
                                cuba_tz = zoneinfo.ZoneInfo("America/Havana")
                                now_cuba = datetime.datetime.now(cuba_tz)
                                remaining_seconds_safe = max(0, remaining_seconds)
                                finish_time = now_cuba + datetime.timedelta(seconds=remaining_seconds_safe)
                                finish_str = finish_time.strftime("%I:%M %p")  # AM/PM mayúsculas
                            # =====================================================

                            try:
                                await msg.edit(
                                    f"╭━━━━[**🤖Compress Fast**]━━━━━╮\n"
                                    f"┠🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
                                    f"┠**Progreso**: {bar}\n"
                                    f"┠**Tamaño**: {sizeof_fmt(compressed_size)}\n"
                                    f"┠**Tiempo transcurrido**: {elapsed_str}\n"
                                    f"┠**Tiempo restante**: {remaining_str}\n"
                                    f"┠**Finaliza a las**: {finish_str}\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                                    reply_markup=cancel_button
                                )
                            except MessageNotModified:
                                pass
                            except Exception as e:
                                logger.error(f"❌Error editando mensaje de progreso: {e}")
                                if compression_id in active_messages:
                                    del active_messages[compression_id]
                            last_percent = percent
                            last_update_time = time.time()

            if cancelled:
                logger.info(f"Compresión {compression_id} cancelada, limpiando...")
                if compressed_video_path and os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                try:
                    if wait_msg:
                        await wait_msg.delete()
                    await msg.delete()
                except:
                    pass
                await send_auto_delete_message(chat_id, "⛔ **Compresión cancelada** ⛔", reply_to_message_id=original_message_id)
                await cleanup_compression_data(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)
                cleaned_up = True
                return

            if process.returncode != 0:
                logger.error(f"❌FFmpeg terminó con código {process.returncode} para {compression_id}")
                await send_auto_delete_message(chat_id, "⛔ **Compresion Cancelada** ⛔\n\n📝 **El video no pudo ser comprimido correctamente o fue cancelado por el usuario**\n\n🔄 **Intente de nuevo o con otro vídeo**", reply_to_message_id=original_message_id)
                if compressed_video_path and os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                try:
                    if wait_msg:
                        await wait_msg.delete()
                    await msg.delete()
                except:
                    pass
                await cleanup_compression_data(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)
                cleaned_up = True
                return

            compressed_size = os.path.getsize(compressed_video_path)
            logger.info(f"✅Compresión completada. Tamaño comprimido: {compressed_size} bytes")
            try:
                probe = ffmpeg.probe(compressed_video_path)
                duration = int(float(probe.get('format', {}).get('duration', 0)))
                if duration == 0:
                    for stream in probe.get('streams', []):
                        if 'duration' in stream:
                            duration = int(float(stream['duration']))
                            break
                if duration == 0:
                    duration = 0
                logger.info(f"✅Duración del video comprimido: {duration} segundos")
                if dur_total > 0:
                    diff_percent = abs(duration - dur_total) / dur_total * 100
                    if diff_percent > 10:
                        logger.warning(f"⛔¡Posible compresión incompleta! Duración original: {dur_total:.2f}s, comprimida: {duration:.2f}s (diferencia del {diff_percent:.1f}%)")
                    else:
                        logger.info(f"✅Duración correcta: original {dur_total:.2f}s, comprimida {duration:.2f}s")
            except Exception as e:
                logger.error(f"❌Error obteniendo duración comprimido: {e}", exc_info=True)
                duration = 0

            thumbnail_path = f"{compressed_video_path}_thumb.jpg"
            try:
                (ffmpeg.input(compressed_video_path, ss=duration//2 if duration > 0 else 0).filter('scale', 320, -1).output(thumbnail_path, vframes=1).overwrite_output().run(capture_stdout=True, capture_stderr=True))
                logger.info(f"✅Miniatura generada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"❌Error generando miniatura: {e}", exc_info=True)
                thumbnail_path = None

            processing_time = datetime.datetime.now() - start_time
            processing_time_str = str(processing_time).split('.')[0]
            description = (
                f"➲ **{base_name}**\n"
                f"┖ ⏰ {processing_time_str}\n"
            )
            try:
                start_upload_time = time.time()
                register_cancelable_task(compression_id, "upload", None, original_message_id=original_message_id, progress_message_id=msg.id)
                update_compression_progress(compression_id, "upload", 0, 100, 0, file_name)
                if thumbnail_path and os.path.exists(thumbnail_path):
                    await send_protected_video(chat_id=chat_id, video=compressed_video_path, caption=description, thumb=thumbnail_path, duration=duration, reply_to_message_id=original_message_id, progress=progress_callback, progress_args=(msg, "SUBIDA", start_upload_time))
                else:
                    await send_protected_video(chat_id=chat_id, video=compressed_video_path, caption=description, duration=duration, reply_to_message_id=original_message_id, progress=progress_callback, progress_args=(msg, "SUBIDA", start_upload_time))
                logger.info("🗜️Video comprimido enviado✅")
                await update_daily_stats_compressed()
                await notify_group(app, await app.get_messages(chat_id, original_message_id), original_size, compressed_size=compressed_size, status="done", processing_time_str=processing_time_str, compressed_name=base_name)
                users_col.update_one({"user_id": user_id}, {"$inc": {"compressed_videos": 1}}, upsert=True)
                try:
                    if wait_msg:
                        await wait_msg.delete()
                        logger.info("✅Mensaje de espera eliminado")
                except Exception as e:
                    logger.error(f"❌Error eliminando mensaje de espera: {e}")
                try:
                    await msg.delete()
                    logger.info("✅Mensaje de progreso eliminado")
                except Exception as e:
                    logger.error(f"❌Error eliminando mensaje de progreso: {e}")
            except Exception as e:
                logger.error(f"❌Error enviando video: {e}", exc_info=True)
                await app.send_message(chat_id=chat_id, text="⚠️ **Error al enviar el video comprimido**")
        except Exception as e:
            logger.error(f"❌Error en compresión: {e}", exc_info=True)
            await msg.delete()
            await app.send_message(chat_id=chat_id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            if not cleaned_up:
                try:
                    await cleanup_compression_data(compression_id)
                    if compression_id in active_messages:
                        del active_messages[compression_id]
                    for file_path in [original_video_path, compressed_video_path]:
                        if file_path and os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info(f"✅Archivo temporal eliminado: {file_path}")
                    if 'thumbnail_path' in locals() and thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                        logger.info(f"✅Miniatura eliminada: {thumbnail_path}")
                    remove_compression_progress(compression_id)
                except Exception as e:
                    logger.error(f"❌Error eliminando archivos temporales: {e}", exc_info=True)
                cleaned_up = True
    except Exception as e:
        logger.critical(f"Error crítico en compress_video_from_path: {e}", exc_info=True)
        await app.send_message(chat_id=chat_id, text="⚠️ Ocurrió un error crítico al procesar el video")
    finally:
        unregister_cancelable_task(compression_id)
        unregister_ffmpeg_process(compression_id)
        remove_compression_progress(compression_id)

def create_compression_bar(percent, bar_length=10):
    try:
        percent = max(0, min(100, percent))
        filled_length = int(bar_length * percent / 100)
        bar = '⬢' * filled_length + '⬡' * (bar_length - filled_length)
        return f"[{bar}] {int(percent)}%"
    except Exception as e:
        logger.error(f"❌Error creando barra de progreso: {e}", exc_info=True)
        return f"**Progreso**: {int(percent)}%"

@app.on_message(filters.command(["deleteall"]) & filters.user(admin_users))
async def delete_all_pending(client, message):
    result = pending_col.delete_many({})
    downloaded_result = downloaded_videos_col.delete_many({})
    await message.reply(f"**🗑️Cola eliminada.**\n**➥Se eliminaron {result.deleted_count} elementos de la cola.**\n**➥Se eliminaron {downloaded_result.deleted_count} videos descargados.**")

@app.on_message(filters.regex(r"^/del_(\d+)$") & filters.user(admin_users))
async def delete_one_from_pending(client, message):
    match = message.text.strip().split("_")
    if len(match) != 2 or not match[1].isdigit():
        await message.reply("⚠️ Formato inválido. Usa `/del_1`, `/del_2`, etc.")
        return
    index = int(match[1]) - 1
    cola = list(pending_col.find().sort([("seq", 1)]))
    if index < 0 or index >= len(cola):
        await message.reply("⚠️ Número fuera de rango.")
        return
    eliminado = cola[index]
    pending_col.delete_one({"_id": eliminado["_id"]})
    file_name = eliminado.get("file_name", "¿?")
    user_id = eliminado["user_id"]
    tiempo = eliminado.get("timestamp")
    tiempo_str = tiempo.strftime("%Y-%m-d %H:%M:%S") if tiempo else "¿?"
    await message.reply(f"✅ Eliminado de la cola:\n📁 {file_name}\n👤 ID: `{user_id}`\n⏰ {tiempo_str}")

async def show_queue(client, message):
    queue_status = await get_queue_status(message.from_user.id if message.from_user.id not in admin_users else None)
    await message.reply(queue_status)

@app.on_message(filters.command("auto") & filters.user(admin_users))
async def startup_command(_, message):
    global processing_tasks
    msg = await message.reply("🔄 Iniciando procesamiento de la cola...")
    downloaded_videos = list(downloaded_videos_col.find().sort("timestamp", 1))
    for video in downloaded_videos:
        try:
            compression_id = video["compression_id"]
            user_id = video["user_id"]
            file_path = video["file_path"]
            file_name = video["file_name"]
            original_message = None
            try:
                pending_info = pending_col.find_one({"compression_id": compression_id})
                if pending_info:
                    chat_id = pending_info.get("chat_id")
                    message_id = pending_info.get("message_id")
                    if chat_id and message_id:
                        original_message = await app.get_messages(chat_id, message_id)
            except:
                pass
            task = {"compression_id": compression_id, "user_id": user_id, "original_video_path": file_path, "file_name": file_name, "chat_id": video.get("chat_id", user_id), "original_message_id": video.get("original_message_id", 0), "wait_msg_id": video.get("wait_msg_id", 0), "wait_msg": await app.send_message(user_id, f"🔄 Recuperando video descargado: {file_name}"), "caption": video.get("original_caption"), "custom_settings": video.get("custom_settings")}
            await compression_processing_queue.put(task)
        except Exception as e:
            logger.error(f"❌Error cargando video descargado: {e}")
    if not processing_tasks or all(task.done() for task in processing_tasks):
        processing_tasks = []
        for i in range(1):
            task = asyncio.create_task(process_compression_queue())
            processing_tasks.append(task)
        await msg.edit("✅ Procesamiento de cola iniciado con 1 worker")
    else:
        await msg.edit("✅ Los workers de procesamiento ya están activos.")

@app.on_message(filters.command("ls") & filters.user(admin_users))
async def daily_stats_command(client, message):
    try:
        stats = await get_daily_stats()
        videos_downloaded = stats.get("videos_downloaded", 0)
        bytes_downloaded = stats.get("bytes_downloaded", 0)
        videos_compressed = stats.get("videos_compressed", 0)
        auto_recoveries = stats.get("auto_recoveries", 0)
        gb_downloaded = bytes_downloaded / (1024 ** 3)
        now = datetime.datetime.now()
        date_str = now.strftime("%d/%m/%Y | %I:%M%p").lower()
        response = (
            f"📊 **Actividad del Bot (HOY):**\n"
            f"📅 **Fecha:** {date_str}\n\n"
            f"📥 **Videos descargados:** {videos_downloaded} videos\n"
            f"⬇️ **GB descargados:** {gb_downloaded:.2f} GB\n"
            f"🗜️ **Videos comprimidos:** {videos_compressed} videos\n"
            f"🔄 **Reanudaciones automáticas:** {auto_recoveries}"
        )
        await message.reply(response)
    except Exception as e:
        logger.error(f"❌Error en daily_stats_command: {e}", exc_info=True)
        await message.reply("❌ Error al obtener estadísticas.")

# ======================== INTERFAZ DE USUARIO ======================== #

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("⚙️ Settings"), KeyboardButton("📋 Planes")],
         [KeyboardButton("📊 Mi Plan"), KeyboardButton("ℹ️ Ayuda")],
         [KeyboardButton("👀 Ver Cola"), KeyboardButton("🗑️ Cancelar Cola")]],
        resize_keyboard=True, one_time_keyboard=False
    )

@app.on_message(filters.command("settings") & filters.private)
async def settings_menu(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗜️ Compresión General", callback_data="general_menu")],
        [InlineKeyboardButton("📱 Videos en Vertical", callback_data="reels_menu")],
        [InlineKeyboardButton("📺 Shows|Calidad media", callback_data="show_menu")],
        [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime_menu")],
        [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data="custom_quality_start")]
    ])
    await send_protected_message(message.chat.id, "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️", reply_markup=keyboard)

# ======================== MENÚ DE PLANES MODIFICADO ======================== #
async def get_plan_menu(user_id: int):
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        message_text = (
            "**No tienes un plan activo.**\n"
            "Adquiere un plan para usar el bot.\n\n"
            "📋 Selecciona un plan para más información:"
        )
    else:
        plan_name = user["plan"].capitalize()
        expires_at = user.get("expires_at")
        if isinstance(expires_at, datetime.datetime):
            now = datetime.datetime.now()
            if expires_at > now:
                time_remaining = expires_at - now
                days = time_remaining.days
                hours = time_remaining.seconds // 3600
                minutes = (time_remaining.seconds % 3600) // 60
                if days > 0:
                    expires_text = f"{days}d {hours}h {minutes}m"
                elif hours > 0:
                    expires_text = f"{hours}h {minutes}m"
                else:
                    expires_text = f"{minutes}m"
            else:
                expires_text = "Expirado"
        else:
            expires_text = "No expira"
        message_text = (
            f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
            f"┠➣ **Tu plan actual:** {plan_name}\n"
            f"┠➣ **Tiempo restante:** {expires_text}\n"
            f"╰✠━━━━━━━━━━━━━━━━━━━━━━✠╯\n\n"
            "📋 Selecciona un plan para más información:"
        )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 Estándar", callback_data="plan_standard")],
        [InlineKeyboardButton("💎 Pro", callback_data="plan_pro")],
        [InlineKeyboardButton("👑 Premium", callback_data="plan_premium")]
    ])
    return message_text, keyboard

async def send_denied_access_message(chat_id: int):
    await send_protected_message(
        chat_id,
        "**🤖 Bot para comprimir videos**\nPuedo reducir el tamaño de los vídeos hasta un 80% o más y se verán bien sin perder tanta calidad.\n\n⬇️**Toque para ver nuestros planes**⬇️",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_start")]])
    )

async def can_notify_access_denied(user_id: int) -> bool:
    now = datetime.datetime.now()
    doc = access_denied_log_col.find_one({"user_id": user_id})
    if doc:
        last = doc.get("last_notification")
        if last and (now - last).total_seconds() < 300:
            return False
    access_denied_log_col.update_one({"user_id": user_id}, {"$set": {"last_notification": now}}, upsert=True)
    return True

async def send_no_plan_response(message: Message):
    user_id = message.from_user.id
    if not await can_notify_access_denied(user_id):
        return
    text = "🚫**Usted no tiene acceso para usar el bot**🚫\n⬇️**Toque para ver nuestros planes**⬇️"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_start")]])
    await app.send_message(message.chat.id, text, reply_to_message_id=message.id, reply_markup=keyboard)

async def show_plan_selection(chat_id, user_id, current_plan=None):
    text, keyboard = await get_plan_menu(user_id)
    await send_protected_message(chat_id, text, reply_markup=keyboard)

@app.on_message(filters.command("planes") & filters.private)
async def planes_command(client, message):
    try:
        texto, keyboard = await get_plan_menu(message.from_user.id)
        await send_protected_message(message.chat.id, texto, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"❌Error en planes_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "⚠️ Error al mostrar los planes")

@app.on_message(filters.command("convert") & filters.private & filters.reply)
async def convert_command(client, message: Message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id):
            return
        if user_id in ban_users:
            logger.warning(f"⛔Intento de uso por usuario baneado: {user_id}")
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_no_plan_response(message)
            return
        replied = message.reply_to_message
        if not replied or not replied.document:
            await send_protected_message(message.chat.id, "❌ **Debes responder a un documento que sea un vídeo.**")
            return
        doc = replied.document
        file_name = doc.file_name or "video_sin_nombre"
        if not is_supported_video_file(file_name):
            await send_protected_message(message.chat.id, f"❌ **Formato no soportado.**\nExtensiones válidas: {', '.join(SUPPORTED_VIDEO_EXTENSIONS)}")
            return
        await process_media_file(client, message, doc, file_name)
    except Exception as e:
        logger.error(f"❌Error en convert_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "⚠️ **Error al procesar el comando /convert**")

async def process_media_file(client, message: Message, file_obj, file_name: str):
    user_id = message.from_user.id
    if user_id in ban_users:
        logger.warning(f"⛔Intento de uso por usuario baneado: {user_id}")
        return
    user_plan = await get_user_plan(user_id)
    if user_plan is None or user_plan.get("plan") is None:
        await send_no_plan_response(message)
        return
    queue_limit = await get_user_queue_limit(user_id)
    pending_count = pending_col.count_documents({"user_id": user_id})
    downloaded_count = await get_user_downloaded_count(user_id)
    total_pending = pending_count + downloaded_count
    if total_pending >= queue_limit:
        await send_protected_message(
            message.chat.id,
            f"Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\nPor favor espera a que se procesen antes de enviar más.",
            reply_to_message_id=message.id
        )
        return

    compression_mode = await get_user_compression_mode(user_id)
    if compression_mode == "before":
        settings = await get_user_video_settings(user_id)
        await process_video_directly(user_id, message.chat.id, message.id, file_obj, file_name, message.caption, custom_settings=settings)
        logger.info(f"✅Video procesado directamente (modo before) para {user_id}: {file_name}")
    else:
        compression_id = generate_compression_id()
        seq = get_next_pending_seq()
        pending_col.insert_one({
            "user_id": user_id,
            "video_id": getattr(file_obj, 'file_id', None),
            "file_name": file_name,
            "chat_id": message.chat.id,
            "message_id": message.id,
            "compression_id": compression_id,
            "timestamp": datetime.datetime.now(),
            "caption": message.caption,
            "seq": seq,
            "status": "awaiting_config",
            "custom_settings": None,
            "wait_message_id": None
        })
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data=f"config_video_{compression_id}")],
            [InlineKeyboardButton("⚙️ Resolucion ⚙️", callback_data=f"select_resolution_{compression_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}")]
        ])
        await send_protected_message(
            message.chat.id,
            "🛠️ **Personaliza la calidad de este video**\n\nConfigura la calidad de compresión para este video.\n**Para cambiar de configuración de compresión use /mode**",
            reply_to_message_id=message.id,
            reply_markup=keyboard
        )
        logger.info(f"✅Video en espera de configuración (modo after) para {user_id}: {compression_id}")

async def process_video_directly(user_id: int, chat_id: int, original_message_id: int, file_obj, file_name: str, caption: str = None, custom_settings: dict = None):
    if not file_name:
        file_name = "video_sin_nombre"
    queue_limit = await get_user_queue_limit(user_id)
    pending_count = pending_col.count_documents({"user_id": user_id})
    downloaded_count = await get_user_downloaded_count(user_id)
    total_pending = pending_count + downloaded_count
    if total_pending >= queue_limit:
        await send_protected_message(chat_id, f"Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\nPor favor espera a que se procesen antes de enviar más.", reply_to_message_id=original_message_id)
        return False

    compression_id = generate_compression_id()
    seq = get_next_pending_seq()
    pending_col.insert_one({
        "user_id": user_id,
        "video_id": getattr(file_obj, 'file_id', None),
        "file_name": file_name,
        "chat_id": chat_id,
        "message_id": original_message_id,
        "wait_message_id": None,
        "compression_id": compression_id,
        "timestamp": datetime.datetime.now(),
        "caption": caption,
        "seq": seq,
        "status": "pending",
        "custom_settings": custom_settings
    })
    cancel_button = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])
    initial_msg = await send_protected_message(
        chat_id,
        f"**Procesando vídeo⚙️**\n`{file_name}`\n\n**Esto puede tardar unos minutos** ⏰",
        reply_to_message_id=original_message_id,
        reply_markup=cancel_button
    )
    pending_col.update_one({"compression_id": compression_id}, {"$set": {"wait_message_id": initial_msg.id}})
    await download_queue.put((
        seq, compression_id, user_id, chat_id, original_message_id,
        file_obj, file_name, initial_msg, caption, custom_settings
    ))
    logger.info(f"✅Tarea de descarga encolada con seq={seq} para {file_name}")
    return True

async def enqueue_video_for_compression(compression_id: str, custom_settings: dict):
    pending_item = pending_col.find_one({"compression_id": compression_id})
    if not pending_item:
        logger.error(f"❌No se encontró pending para compression_id {compression_id}")
        return False
    pending_col.update_one(
        {"compression_id": compression_id},
        {"$set": {"custom_settings": custom_settings, "status": "pending"}}
    )
    chat_id = pending_item["chat_id"]
    message_id = pending_item["message_id"]
    try:
        original_message = await app.get_messages(chat_id, message_id)
        if original_message.video:
            file_obj = original_message.video
        elif original_message.document:
            file_obj = original_message.document
        else:
            logger.error(f"❌El mensaje original no contiene video ni documento para {compression_id}")
            return False
    except Exception as e:
        logger.error(f"❌Error recuperando mensaje original {message_id}: {e}")
        return False
    file_name = pending_item["file_name"]
    caption = pending_item.get("caption")
    user_id = pending_item["user_id"]
    queue_position = await get_download_queue_position(compression_id)
    wait_msg = await send_protected_message(
        chat_id,
        f"⏳ **Video agregado a la cola**\n\n`{file_name}`\n\n📊 **Posición en cola:** #{queue_position}\n⏱ **Esperando slot disponible...**",
        reply_to_message_id=message_id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])
    )
    pending_col.update_one({"compression_id": compression_id}, {"$set": {"wait_message_id": wait_msg.id}})
    await update_all_download_waiting_messages()
    seq = pending_item["seq"]
    await download_queue.put((
        seq, compression_id, user_id, chat_id, message_id,
        file_obj, file_name, wait_msg, caption, custom_settings
    ))
    logger.info(f"✅Video {file_name} encolado para descarga con config personalizada")
    return True

async def create_payment_request(user_id: int, plan: str, payment_method: str = None):
    payment_id = ObjectId()
    pending_payments_col.insert_one({"_id": payment_id, "user_id": user_id, "plan": plan, "payment_method": payment_method, "status": "awaiting_capture", "timestamp": datetime.datetime.now()})
    return payment_id

async def get_payment_request(payment_id: ObjectId):
    return pending_payments_col.find_one({"_id": payment_id})

async def update_payment_status(payment_id: ObjectId, status: str, receipt_photo_id: str = None, instruction_msg_id: int = None, confirmation_msg_id: int = None):
    update_data = {"status": status}
    if receipt_photo_id:
        update_data["receipt_photo_id"] = receipt_photo_id
    if instruction_msg_id is not None:
        update_data["instruction_msg_id"] = instruction_msg_id
    if confirmation_msg_id is not None:
        update_data["confirmation_msg_id"] = confirmation_msg_id
    pending_payments_col.update_one({"_id": payment_id}, {"$set": update_data})

async def delete_payment_request(payment_id: ObjectId):
    pending_payments_col.delete_one({"_id": payment_id})

# ======================== CALLBACK HANDLER MODIFICADO ======================== #
@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in admin_users and MAINTENANCE_MODE:
        await callback_query.answer("🔧 Bot en mantenimiento\n\nPor favor, espere a que termine.", show_alert=True)
        return

    data = callback_query.data
    if data == "mode_after":
        await set_user_compression_mode(user_id, "after")
        await callback_query.message.edit_text("**Configuración aplicada** ✅⚙️\n\nModo: **Configurar calidad al enviar video**")
        await callback_query.answer("✅ Modo actualizado")
        return
    elif data == "mode_before":
        await set_user_compression_mode(user_id, "before")
        await callback_query.message.edit_text("**Configuración aplicada** ✅⚙️\n\nModo: **Configurar calidad antes de enviar video**")
        await callback_query.answer("✅ Modo actualizado")
        return

    if data.startswith("config_video_"):
        compression_id = data.split("_")[2]
        pending_item = pending_col.find_one({"compression_id": compression_id})
        if not pending_item or pending_item.get("status") != "awaiting_config":
            await callback_query.answer("⚠️ Este video ya no está disponible para configurar.", show_alert=True)
            return
        temp_video_configs[compression_id] = {}
        keyboard = get_resolution_keyboard_video(compression_id)
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 1/4\n\nSelecciona la resolución:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    # ================== NUEVOS BLOQUES PARA SELECCIÓN RÁPIDA DE RESOLUCIÓN ================== #
    if data.startswith("select_resolution_"):
        compression_id = data.split("_")[2]
        pending_item = pending_col.find_one({"compression_id": compression_id})
        if not pending_item or pending_item.get("status") != "awaiting_config":
            await callback_query.answer("⚠️ Este video ya no está disponible.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("360p", callback_data=f"quick_res_{compression_id}_360")],
            [InlineKeyboardButton("480p", callback_data=f"quick_res_{compression_id}_480")],
            [InlineKeyboardButton("720p", callback_data=f"quick_res_{compression_id}_720")],
            [InlineKeyboardButton("🔙 Volver", callback_data=f"back_to_config_{compression_id}")]
        ])
        await callback_query.message.edit_text("🗜️ **Selecciona la calidad para el video**", reply_markup=keyboard)
        await callback_query.answer()
        return

    if data.startswith("quick_res_"):
        parts = data.split("_")
        compression_id = parts[2]
        resolution = parts[3]  # '360', '480', '720'
        # Configuraciones predefinidas
        configs = {
            "360": {"resolution": "640x360", "crf": "28", "fps": "20", "audio_bitrate": "64k"},
            "480": {"resolution": "-2:480", "crf": "30", "fps": "20", "audio_bitrate": "64k"},
            "720": {"resolution": "-2:720", "crf": "30", "fps": "20", "audio_bitrate": "64k"}
        }
        custom_settings = configs.get(resolution)
        if not custom_settings:
            await callback_query.answer("❌ Resolución no válida", show_alert=True)
            return
        pending_item = pending_col.find_one({"compression_id": compression_id})
        if not pending_item or pending_item.get("status") != "awaiting_config":
            await callback_query.answer("⚠️ Este video ya no está disponible.", show_alert=True)
            return
        # Encolar con la configuración rápida
        success = await enqueue_video_for_compression(compression_id, custom_settings)
        if success:
            await callback_query.message.edit_text("✅ **Configuración aplicada.**\nEl video se está procesando.")
            asyncio.create_task(delete_message_after(callback_query.message, 2))
            await callback_query.answer("✅ Video encolado con calidad rápida.")
        else:
            await callback_query.message.edit_text("❌ **Error al procesar el video. Intenta de nuevo.**")
            await callback_query.answer("Error al encolar.", show_alert=True)
        return

    if data.startswith("back_to_config_"):
        compression_id = data.split("_")[3]  # back_to_config_{compression_id}
        pending_item = pending_col.find_one({"compression_id": compression_id})
        if not pending_item:
            await callback_query.answer("⚠️ Este video ya no está disponible.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data=f"config_video_{compression_id}")],
            [InlineKeyboardButton("⚙️ Resolucion ⚙️", callback_data=f"select_resolution_{compression_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}")]
        ])
        await callback_query.message.edit_text(
            "🛠️ **Personaliza la calidad de este video**\n\nConfigura la calidad de compresión para este video.\n**Para cambiar de configuración de compresión use /mode**",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return
    # ================== FIN DE LOS NUEVOS BLOQUES ================== #

    if data.startswith("vid_res_"):
        parts = data.split("_")
        compression_id = parts[2]
        resolution = parts[3]
        if compression_id not in temp_video_configs:
            temp_video_configs[compression_id] = {}
        temp_video_configs[compression_id]['resolution'] = resolution
        keyboard = get_resolution_keyboard_video(compression_id, resolution)
        await callback_query.message.edit_text(
            f"⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 1/4\n\nResolución seleccionada: {resolution}p",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    # ----- NUEVO MANEJADOR PARA "🔙 Regresar" en el paso de resolución -----
    if data.startswith("vid_back_res_"):
        compression_id = data.split("_")[3]  # vid_back_res_{compression_id}
        pending_item = pending_col.find_one({"compression_id": compression_id})
        if not pending_item:
            await callback_query.answer("⚠️ Este video ya no está disponible.", show_alert=True)
            return
        # Volver al menú principal de configuración (igual que en "back_to_config_")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data=f"config_video_{compression_id}")],
            [InlineKeyboardButton("⚙️ Resolucion ⚙️", callback_data=f"select_resolution_{compression_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"vid_cancel_{compression_id}")]
        ])
        await callback_query.message.edit_text(
            "🛠️ **Personaliza la calidad de este video**\n\nConfigura la calidad de compresión para este video.\n**Para cambiar de configuración de compresión use /mode**",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return
    # ----------------------------------------------------------

    if data.startswith("vid_next_crf_"):
        compression_id = data.split("_")[3]
        if compression_id not in temp_video_configs or 'resolution' not in temp_video_configs[compression_id]:
            await callback_query.answer("Debes seleccionar una resolución primero.", show_alert=True)
            return
        keyboard = get_crf_keyboard_video(compression_id)
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 2/4\n\nSelecciona el nivel CRF:\n➥ Menor valor = mejor calidad (archivo más grande)\n➥ Mayor valor = menor calidad (archivo más pequeño)",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_back_crf_"):
        compression_id = data.split("_")[3]
        keyboard = get_resolution_keyboard_video(compression_id, temp_video_configs.get(compression_id, {}).get('resolution'))
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 1/4\n\nSelecciona la resolución:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_crf_"):
        parts = data.split("_")
        compression_id = parts[2]
        crf = parts[3]
        if compression_id not in temp_video_configs:
            temp_video_configs[compression_id] = {}
        temp_video_configs[compression_id]['crf'] = crf
        keyboard = get_crf_keyboard_video(compression_id, crf)
        await callback_query.message.edit_text(
            f"⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 2/4\n\nCRF seleccionado: {crf}",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_next_fps_"):
        compression_id = data.split("_")[3]
        if compression_id not in temp_video_configs or 'crf' not in temp_video_configs[compression_id]:
            await callback_query.answer("Debes seleccionar un CRF primero.", show_alert=True)
            return
        keyboard = get_fps_keyboard_video(compression_id)
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 3/4\n\nSelecciona los FPS:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_back_fps_"):
        compression_id = data.split("_")[3]
        keyboard = get_crf_keyboard_video(compression_id, temp_video_configs.get(compression_id, {}).get('crf'))
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 2/4\n\nSelecciona el nivel CRF:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_fps_"):
        parts = data.split("_")
        compression_id = parts[2]
        fps = parts[3]
        if compression_id not in temp_video_configs:
            temp_video_configs[compression_id] = {}
        temp_video_configs[compression_id]['fps'] = fps
        keyboard = get_fps_keyboard_video(compression_id, fps)
        await callback_query.message.edit_text(
            f"⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 3/4\n\nFPS seleccionado: {fps}",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_next_audio_"):
        compression_id = data.split("_")[3]
        if compression_id not in temp_video_configs or 'fps' not in temp_video_configs[compression_id]:
            await callback_query.answer("Debes seleccionar un FPS primero.", show_alert=True)
            return
        keyboard = get_audio_keyboard_video(compression_id)
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 4/4\n\nSelecciona la calidad de audio:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_back_audio_"):
        compression_id = data.split("_")[3]
        keyboard = get_fps_keyboard_video(compression_id, temp_video_configs.get(compression_id, {}).get('fps'))
        await callback_query.message.edit_text(
            "⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 3/4\n\nSelecciona los FPS:",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_audio_"):
        parts = data.split("_")
        compression_id = parts[2]
        audio = parts[3]
        if compression_id not in temp_video_configs:
            temp_video_configs[compression_id] = {}
        temp_video_configs[compression_id]['audio_bitrate'] = audio
        keyboard = get_audio_keyboard_video(compression_id, audio)
        await callback_query.message.edit_text(
            f"⚙️ **CONFIGURAR CALIDAD PARA ESTE VIDEO** - PASO 4/4\n\nAudio seleccionado: {audio}",
            reply_markup=keyboard
        )
        await callback_query.answer()
        return

    if data.startswith("vid_finish_"):
        compression_id = data.split("_")[2]
        if compression_id not in temp_video_configs:
            await callback_query.answer("Error: no hay configuración para este video.", show_alert=True)
            return
        config = temp_video_configs[compression_id]
        required = ['resolution', 'crf', 'fps', 'audio_bitrate']
        if not all(k in config for k in required):
            await callback_query.answer("Debes completar todos los pasos.", show_alert=True)
            return
        success = await enqueue_video_for_compression(compression_id, config)
        if success:
            await callback_query.message.edit_text("✅ **Configuración aplicada.**\nEl video se está procesando.")
            asyncio.create_task(delete_message_after(callback_query.message, 2))
            if compression_id in temp_video_configs:
                del temp_video_configs[compression_id]
            await callback_query.answer("✅ Video encolado con tu configuración.")
        else:
            await callback_query.message.edit_text("❌ **Error al procesar el video. Intenta de nuevo.**")
            await callback_query.answer("Error al encolar.", show_alert=True)
        return

    if data.startswith("vid_cancel_"):
        compression_id = data.split("_")[2]
        await cleanup_compression_data(compression_id)
        if compression_id in temp_video_configs:
            del temp_video_configs[compression_id]
        await callback_query.message.edit_text("❌ **Configuración cancelada.**\n**El video no se procesará.**")
        await callback_query.answer("Cancelado.")
        return

    config_map = {
        "general_v1": "resolution=-2:480 crf=28 audio_bitrate=64k fps=22 preset=veryfast codec=libx264",
        "general_v2": "resolution=-2:720 crf=30 audio_bitrate=128k fps=22 preset=veryfast codec=libx264",
        "reels_v1": "resolution=-2:480 crf=25 audio_bitrate=64k fps=30 preset=veryfast codec=libx264",
        "reels_v2": "resolution=-2:720 crf=25 audio_bitrate=128k fps=30 preset=veryfast codec=libx264",
        "show_v1": "resolution=-2:480 crf=32 audio_bitrate=64k fps=20 preset=veryfast codec=libx264",
        "show_v2": "resolution=-2:720 crf=34 audio_bitrate=128k fps=20 preset=veryfast codec=libx264",
        "anime_v1": "resolution=-2:480 crf=32 audio_bitrate=64k fps=18 preset=veryfast codec=libx264",
        "anime_v2": "resolution=-2:480 crf=25 audio_bitrate=128k fps=18 preset=veryfast codec=libx264"
    }
    quality_names = {
        "general_v1": "🗜️ Compresión General - V1\n(audio normal y calidad media)",
        "general_v2": "🗜️ Compresión General - V2\n(mejor audio y calidad alta)",
        "reels_v1": "📱 Videos en Vertical - V1\n(audio normal)",
        "reels_v2": "📱 Videos en Vertical - V2\n(mejor audio)",
        "show_v1": "📺 Shows|Calidad media - V1\n(audio normal y calidad media)",
        "show_v2": "📺 Shows|Calidad media - V2\n(mejor audio y calidad alta)",
        "anime_v1": "🎬 Anime y series animadas - V1\n(audio normal y calidad media)",
        "anime_v2": "🎬 Anime y series animadas - V2\n(mejor audio y calidad alta)"
    }
    if data == "refresh_status_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ver estas estadísticas", show_alert=True)
            return
        try:
            stats = get_status_stats()
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_status_stats")]])
            await callback_query.message.edit_text(stats, reply_markup=keyboard)
            await callback_query.answer("✅ Estadísticas actualizadas")
        except Exception as e:
            logger.error(f"❌Error actualizando estadísticas del sistema: {e}")
            await callback_query.answer("❌ Error al actualizar estadísticas", show_alert=True)
        return
    elif data == "refresh_admin_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ver estas estadísticas", show_alert=True)
            return
        try:
            pipeline = [{"$match": {"plan": {"$exists": True, "$ne": None}}}, {"$group": {"_id": "$plan", "count": {"$sum": 1}}}]
            stats = list(users_col.aggregate(pipeline))
            total_users = users_col.count_documents({})
            total_downloaded = downloaded_videos_col.count_documents({})
            total_pending = pending_col.count_documents({})
            active_compr = list(active_compressions_col.find({}))
            total_active = len(active_compr)
            response = "📊 **Estadísticas de Administrador**\n\n"
            response += f"👥 **Total de usuarios:** {total_users}\n"
            response += f"📥 **Videos descargados en cola:** {total_downloaded}\n"
            response += f"⏳ **Videos pendientes de descargar:** {total_pending}\n"
            response += f"⬇️ **Descargas activas:** 1/1\n"
            response += f"🔄 **Compresiones activas:** {total_active}\n\n"
            if total_active > 0:
                response += "📋 **Compresiones activas:**\n"
                for i, comp in enumerate(active_compr, 1):
                    comp_user_id = comp.get("user_id")
                    file_name = comp.get("file_name", "Sin nombre")
                    start_time = comp.get("start_time")
                    user_number = get_user_number(comp_user_id)
                    if user_number:
                        username = f"Usuario {user_number}"
                    else:
                        username = f"Usuario {comp_user_id}"
                    start_str = start_time.strftime("%H:%M:%S") if isinstance(start_time, datetime.datetime) else "¿?"
                    response += f"{i}. {username} - `{file_name}` (⏰ {start_str})\n"
                response += "\n"
            response += "📝 **Distribución por Planes:**\n"
            plan_names = {"standard": "🧩 Estándar", "pro": "💎 Pro", "premium": "👑 Premium", "ultra": "🚀 Ultra"}
            for stat in stats:
                plan_type = stat["_id"]
                count = stat["count"]
                plan_name = plan_names.get(plan_type, plan_type.capitalize() if plan_type else "❓ Desconocido")
                response += f"\n{plan_name}:\n  👥 Usuarios: {count}\n"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_admin_stats"), InlineKeyboardButton("❌ Cerrar", callback_data="close_admin_stats")]])
            await callback_query.message.edit_text(response, reply_markup=keyboard)
            await callback_query.answer("✅ Estadísticas actualizadas")
        except Exception as e:
            logger.error(f"❌Error actualizando estadísticas de administrador: {e}")
            await callback_query.answer("❌ Error al actualizar estadísticas", show_alert=True)
        return
    elif data == "close_admin_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden cerrar este mensaje", show_alert=True)
            return
        try:
            await callback_query.message.delete()
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"❌Error cerrando mensaje de estadísticas de administrador: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje")
        return
    elif data == "custom_quality_start":
        temp_custom_settings[user_id] = {}
        keyboard = get_resolution_keyboard()
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\nSelecciona la resolución del video:", reply_markup=keyboard)
        return
    elif data.startswith("custom_resolution_"):
        resolution_value = data.replace("custom_resolution_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['resolution'] = resolution_value
        keyboard = get_resolution_keyboard(resolution_value)
        await callback_query.message.edit_text(f"⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\nSelecciona la resolucion:", reply_markup=keyboard)
        return
    elif data == "custom_next_crf":
        if user_id not in temp_custom_settings or 'resolution' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar una resolución primero.", show_alert=True)
            return
        keyboard = get_crf_keyboard()
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\nSelecciona el nivel de compresión CRF:\n➥Menor valor = mejor calidad (archivo más grande)\n➥Mayor valor = menor calidad (archivo más pequeño)", reply_markup=keyboard)
        return
    elif data == "custom_back_resolution":
        keyboard = get_resolution_keyboard(temp_custom_settings.get(user_id, {}).get('resolution'))
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\nSelecciona la resolución del video:", reply_markup=keyboard)
        return
    elif data.startswith("custom_crf_"):
        crf_value = data.replace("custom_crf_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['crf'] = crf_value
        keyboard = get_crf_keyboard(crf_value)
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\nSelecciona el nivel de compresión CRF:\n➥Menor valor = mejor calidad (archivo más grande)\n➥Mayor valor = menor calidad (archivo más pequeño)", reply_markup=keyboard)
        return
    elif data == "custom_next_fps":
        if user_id not in temp_custom_settings or 'crf' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un CRF primero.", show_alert=True)
            return
        keyboard = get_fps_keyboard()
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\nSelecciona el FPS:", reply_markup=keyboard)
        return
    elif data == "custom_back_crf":
        keyboard = get_crf_keyboard(temp_custom_settings.get(user_id, {}).get('crf'))
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\nSelecciona el nivel de compresión CRF:\n➥Menor valor = mejor calidad (archivo más grande)\n➥Mayor valor = menor calidad (archivo más pequeño)", reply_markup=keyboard)
        return
    elif data.startswith("custom_fps_"):
        fps_value = data.replace("custom_fps_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['fps'] = fps_value
        keyboard = get_fps_keyboard(fps_value)
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\nSelecciona el FPS:", reply_markup=keyboard)
        return
    elif data == "custom_next_audio":
        if user_id not in temp_custom_settings or 'fps' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un FPS primero.", show_alert=True)
            return
        keyboard = get_audio_keyboard()
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 4/4**⚙️\n\nSelecciona la calidad de audio:", reply_markup=keyboard)
        return
    elif data == "custom_back_fps":
        keyboard = get_fps_keyboard(temp_custom_settings.get(user_id, {}).get('fps'))
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\nSelecciona el FPS:", reply_markup=keyboard)
        return
    elif data.startswith("custom_audio_"):
        audio_value = data.replace("custom_audio_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['audio_bitrate'] = audio_value
        keyboard = get_audio_keyboard(audio_value)
        await callback_query.message.edit_text("⚙️**CONFIGURAR CALIDAD - PASO 4/4**⚙️\n\nSelecciona la calidad de audio:\n\n➥Menor valor = mejor calidad (archivo más grande)\n➥Mayor valor = menor calidad (archivo más pequeño)", reply_markup=keyboard)
        return
    elif data == "custom_finish":
        if user_id not in temp_custom_settings:
            await callback_query.answer("Error en la configuración. Intenta nuevamente.", show_alert=True)
            return
        user_settings = temp_custom_settings[user_id]
        required_keys = ['resolution', 'crf', 'fps', 'audio_bitrate']
        if not all(key in user_settings for key in required_keys):
            await callback_query.answer("Debes completar todos los pasos de configuración.", show_alert=True)
            return
        success = await apply_custom_settings(user_id, user_settings)
        if success:
            if user_id in temp_custom_settings:
                del temp_custom_settings[user_id]
            resolution_name = ""
            if user_settings['resolution'] == '640x360':
                resolution_name = "360p"
            elif user_settings['resolution'] == '-2:480':
                resolution_name = "480p"
            elif user_settings['resolution'] == '-2:720':
                resolution_name = "720p"
            confirmation_text = (f"✅ **CALIDAD PERSONALIZADA CONFIGURADA**\n\n**Configuración aplicada:**\n"
                                 f"• **Resolución:** {resolution_name}\n• **Compresión CRF:** {user_settings['crf']}\n"
                                 f"• **FPS:** {user_settings['fps']}\n• **Audio:** {user_settings['audio_bitrate']}")
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a Settings", callback_data="back_to_settings")]])
            await callback_query.message.edit_text(confirmation_text, reply_markup=back_keyboard)
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
        return
    elif data.startswith("cancel_task_"):
        compression_id = data.split("_")[2]
        if compression_id in cancel_tasks:
            task_info = cancel_tasks[compression_id]
            if task_info.get("type") == "download":
                pending_data = pending_col.find_one({"compression_id": compression_id})
                if pending_data and callback_query.from_user.id != pending_data["user_id"]:
                    await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
                    return
            else:
                compression_data = active_compressions_col.find_one({"compression_id": compression_id})
                if compression_data and callback_query.from_user.id != compression_data["user_id"]:
                    await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
                    return
            if cancel_compression_task(compression_id):
                task_info = cancel_tasks.get(compression_id, {})
                original_message_id = task_info.get("original_message_id")
                progress_message_id = task_info.get("progress_message_id")
                await cleanup_compression_data(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)
                try:
                    await callback_query.message.delete()
                except Exception as e:
                    logger.error(f"❌Error eliminando mensaje de cancelación: {e}")
                if compression_id in active_messages:
                    del active_messages[compression_id]
                if f"{compression_id}_upload" in active_messages:
                    del active_messages[f"{compression_id}_upload"]
                await callback_query.answer("⛔ Compresión cancelada ⛔", show_alert=False)
                try:
                    msg = await send_protected_message(callback_query.message.chat.id, "⛔ **Descarga/Compresión cancelada** ⛔", reply_to_message_id=original_message_id)
                except:
                    msg = await send_protected_message(callback_query.message.chat.id, "⛔ **Descarga/Compresión cancelada** ⛔")
                asyncio.create_task(delete_message_after(msg, 5))
                await update_all_download_waiting_messages()
                await update_all_compression_waiting_messages()
            else:
                await callback_query.answer("⚠️ No se pudo cancelar la tarea", show_alert=True)
            return
        compression_data = active_compressions_col.find_one({"compression_id": compression_id})
        if not compression_data:
            downloaded_data = downloaded_videos_col.find_one({"compression_id": compression_id})
            if downloaded_data:
                if callback_query.from_user.id != downloaded_data["user_id"]:
                    await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
                    return
                await cleanup_compression_data(compression_id)
                if os.path.exists(downloaded_data.get("file_path", "")):
                    os.remove(downloaded_data["file_path"])
                await callback_query.answer("✅ Video descargado eliminado de la cola de compresión", show_alert=True)
                try:
                    await callback_query.message.delete()
                except:
                    pass
                await update_all_compression_waiting_messages()
                return
            pending_data = pending_col.find_one({"compression_id": compression_id})
            if pending_data:
                if callback_query.from_user.id != pending_data["user_id"]:
                    await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
                    return
                await cleanup_compression_data(compression_id)
                wait_message_id = pending_data.get("wait_message_id")
                if wait_message_id:
                    try:
                        await app.delete_messages(callback_query.message.chat.id, wait_message_id)
                    except:
                        pass
                await callback_query.answer("✅ Video eliminado de la cola de descarga", show_alert=True)
                try:
                    await callback_query.message.delete()
                except:
                    pass
                await update_all_download_waiting_messages()
                return
            await callback_query.answer("⚠️ Esta tarea ya ha finalizado o no existe", show_alert=True)
            return
        else:
            if callback_query.from_user.id != compression_data["user_id"]:
                await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
                return
            if cancel_compression_task(compression_id):
                task_info = cancel_tasks.get(compression_id, {})
                original_message_id = task_info.get("original_message_id")
                progress_message_id = task_info.get("progress_message_id")
                await cleanup_compression_data(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)
                if progress_message_id:
                    try:
                        await app.delete_messages(callback_query.message.chat.id, progress_message_id)
                        if compression_id in active_messages:
                            del active_messages[compression_id]
                    except Exception as e:
                        logger.error(f"❌Error eliminando mensaje de progreso: {e}")
                await callback_query.answer("⛔ Compresión cancelada ⛔", show_alert=False)
                try:
                    msg = await send_protected_message(callback_query.message.chat.id, "⛔ **Compresión cancelada** ⛔", reply_to_message_id=original_message_id)
                except:
                    msg = await send_protected_message(callback_query.message.chat.id, "⛔ **Compresión cancelada** ⛔")
                asyncio.create_task(delete_message_after(msg, 3))
                await update_all_compression_waiting_messages()
            else:
                await callback_query.answer("⚠️ No se pudo cancelar la tarea", show_alert=True)
        return

    # ========== NUEVO MANEJADOR PARA REFRESH_QUEUE (opcional, ya no necesario pero se conserva) ==========
    if data == "refresh_queue":
        try:
            queue_text, queue_keyboard = await get_queue_status(user_id)
            await callback_query.message.edit_text(queue_text, reply_markup=queue_keyboard)
            await callback_query.answer("✅ Estado de la cola actualizado")
        except Exception as e:
            logger.error(f"❌Error actualizando cola: {e}")
            await callback_query.answer("⏳Procesando información⏳...")
        return

    # ========== NUEVO MANEJADOR PARA CLOSE_QUEUE ==========
    elif data == "close_queue":
        try:
            # Cancelar la tarea de actualización si existe
            if user_id in user_queue_tasks:
                data_task = user_queue_tasks[user_id]
                data_task["task"].cancel()
                del user_queue_tasks[user_id]
            await callback_query.message.delete()
            # Opcional: eliminar también el mensaje anterior (si lo hubiera)
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"Error cerrando mensaje de cola: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje")
        return

    elif data == "refresh_plan":
        try:
            plan_info, keyboard = await get_plan_info(user_id)
            await callback_query.message.edit_text(plan_info, reply_markup=keyboard)
            await callback_query.answer("✅ Información del plan actualizada")
        except Exception as e:
            logger.error(f"❌Error actualizando plan: {e}")
            await callback_query.answer("⏳Procesando información⏳...")
        return
    elif data == "close_plan":
        try:
            await callback_query.message.delete()
            try:
                message_id = callback_query.message.id
                await app.delete_messages(callback_query.message.chat.id, [message_id - 1])
            except Exception as e:
                logger.error(f"❌Error eliminando mensaje original de mi plan: {e}")
                try:
                    async for message in app.get_chat_history(callback_query.message.chat.id, limit=5):
                        if message.text and "📊 Mi Plan" in message.text:
                            await message.delete()
                            break
                except Exception as e2:
                    logger.error(f"❌Error alternativo eliminando mensaje mi plan: {e2}")
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"❌Error cerrando mensaje de plan: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje")
        return
    if data.startswith("confirm_setdays_"):
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ejecutar esta acción", show_alert=True)
            return
        try:
            days = int(data.split("_")[2])
            await callback_query.message.edit_text(f"🔄 **Agregando {days} día(s) a todos los usuarios...**\n\n⏳ Esto puede tomar varios minutos...")
            updated_count, failed_count, result_message = await add_days_to_all_users(days, callback_query.from_user.id)
            result_text = (f"✅ **Proceso de agregar días completado**\n\n• **Días agregados**: {days}\n• **Usuarios actualizados**: {updated_count}\n• **Errores**: {failed_count}\n\n{result_message}")
            await callback_query.message.edit_text(result_text)
            await callback_query.answer("✅ Proceso completado")
        except Exception as e:
            logger.error(f"❌Error en confirm_setdays: {e}", exc_info=True)
            await callback_query.message.edit_text("❌ **Error al ejecutar el comando**")
            await callback_query.answer("❌ Error en el proceso")
        return
    elif data == "cancel_setdays":
        await callback_query.message.edit_text("❌ **Operación cancelada**")
        await callback_query.answer("Operación cancelada")
        return
    elif data.startswith("cancel_payment_"):
        payment_id_str = data.split("_")[2]
        payment_id = ObjectId(payment_id_str)
        payment = await get_payment_request(payment_id)
        if not payment:
            await callback_query.answer("⚠️ Esta solicitud ya no existe.", show_alert=True)
            return
        if payment["user_id"] != callback_query.from_user.id:
            await callback_query.answer("⚠️ No tienes permiso para cancelar este pago.", show_alert=True)
            return
        await delete_payment_request(payment_id)
        cancel_message = "❌ **Pago cancelado por el usuario**"
        back_button = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="plan_back")]])
        await callback_query.message.edit_text(cancel_message, reply_markup=back_button)
        await callback_query.answer("Pago cancelado.")
        return
    if data.startswith(("confirm_", "cancel_")):
        action, confirmation_id_str = data.split('_', 1)
        confirmation_id = ObjectId(confirmation_id_str)
        confirmation = await get_confirmation(confirmation_id)
        if not confirmation:
            await callback_query.answer("⚠️ Esta solicitud ha expirado o ya fue procesada.", show_alert=True)
            return
        user_id = callback_query.from_user.id
        if user_id != confirmation["user_id"]:
            await callback_query.answer("⚠️ No tienes permiso para esta acción.", show_alert=True)
            return
        if action == "confirm":
            if await check_user_limit(user_id):
                await callback_query.answer("⚠️ Has alcanzado tu límite mensual de compresiones.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return
            user_plan = await get_user_plan(user_id)
            queue_limit = await get_user_queue_limit(user_id)
            pending_count = pending_col.count_documents({"user_id": user_id})
            downloaded_count = downloaded_videos_col.count_documents({"user_id": user_id})
            total_pending = pending_count + downloaded_count
            if total_pending >= queue_limit:
                await callback_query.answer(f"⚠️ Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\nEspera a que se procesen antes de enviar más.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return
            try:
                message = await app.get_messages(confirmation["chat_id"], confirmation["message_id"])
            except Exception as e:
                logger.error(f"❌Error obteniendo mensaje: {e}")
                await callback_query.answer("⚠️ Error al obtener el video. Intenta enviarlo de nuevo.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return
            if message.video:
                file_obj = message.video
            elif message.document:
                file_obj = message.document
            else:
                await callback_query.answer("⚠️ El mensaje ya no contiene un archivo válido.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return
            file_name = confirmation["file_name"]
            file_id = confirmation["file_id"]
            compression_id = generate_compression_id()
            caption = confirmation.get("caption")
            pending_col.insert_one({"user_id": user_id, "video_id": file_id, "file_name": file_name, "chat_id": message.chat.id, "message_id": message.id, "wait_message_id": None, "compression_id": compression_id, "timestamp": datetime.datetime.now(), "caption": caption})
            queue_position = await get_download_queue_position(compression_id)
            wait_msg = await callback_query.message.edit_text(
                f"⏳ **Video agregado a la cola**\n\n`{file_name}`\n\n📊 **Posición en cola:** #{queue_position}\n⏱ **Esperando slot disponible...**",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]])
            )
            pending_col.update_one({"compression_id": compression_id}, {"$set": {"wait_message_id": wait_msg.id}})
            await update_all_download_waiting_messages()
            asyncio.create_task(download_file_immediately_worker(compression_id, user_id, message.chat.id, message.id, file_obj, file_name, wait_msg, caption))
            await delete_confirmation(confirmation_id)
            logger.info(f"✅Confirmación procesada para {user_id}: {file_name}")
        elif action == "cancel":
            await delete_confirmation(confirmation_id)
            await callback_query.answer("⛔ Compresión cancelada ⛔", show_alert=False)
            try:
                await callback_query.message.edit_text("⛔ **Compresión cancelada** ⛔")
                await asyncio.sleep(5)
                await callback_query.message.delete()
            except:
                pass
        return
    if data.endswith("_menu"):
        quality_type = data.replace("_menu", "")
        if quality_type == "general":
            title = "🗜️ **Compresión General**"
        elif quality_type == "reels":
            title = "📱 **Videos en Vertical**"
        elif quality_type == "show":
            title = "📺 **Shows|Calidad media**"
        elif quality_type == "anime":
            title = "🎬 **Anime y series animadas**"
        else:
            title = "Seleccionar Calidad"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("V1 (audio normal y calidad media)", callback_data=f"{quality_type}_v1")], [InlineKeyboardButton("V2 (mejor audio y calidad alta)", callback_data=f"{quality_type}_v2")], [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]])
        await callback_query.message.edit_text(f"{title}\n\nSelecciona la calidad a usar:", reply_markup=keyboard)
        return
    if data == "plan_back":
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"❌Error en plan_back: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al volver al menú de planes", show_alert=True)
        return
    if data in ["show_plans_from_start", "show_plans_from_video"]:
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"❌Error mostrando planes desde callback: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al mostrar los planes", show_alert=True)
        return
    elif data.startswith("plan_"):
        plan_type = data.split("_")[1]
        user_id = callback_query.from_user.id
        if plan_type == "standard":
            description = (
                "🧩**Plan Estándar**🧩\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n\n"
                "❌ **Desventajas:**\n"
                "• **No podrá reenviar del bot**\n"
                "• **Solo podrá comprimir 1 video a la vez**\n\n"
                "• **Precio:** 0.35 USDT💰| 250 Cup💳 | 150 Cup SM📱\n"
                "• **Duración 7 días**\n"
            )
        elif plan_type == "pro":
            description = (
                "💎**Plan Pro**💎\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Podrá reenviar del bot**\n• **Podrá comprimir 2 videos a la vez**\n\n"
                "• **Precio:** 0.61 USDT💰| 600 Cup💳 | 300 Cup SM📱\n"
                "• **Duración 15 días**\n"
            )
        elif plan_type == "premium":
            description = (
                "👑**Plan Premium**👑\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Soporte prioritario 24/7**\n"
                "• **Podrá reenviar del bot**\n"
                f"• **Múltiples videos en cola (hasta {PREMIUM_QUEUE_LIMIT})**\n\n"
                "• **Precio:** 0.86 USDT💰| 850 Cup💳 | 425 Cup SM📱\n"
                "• **Duración 30 días**\n"
            )
        else:
            await callback_query.answer("Plan no válido", show_alert=True)
            return
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back"),
             InlineKeyboardButton("💳 PAGAR AHORA", callback_data=f"pay_plan_{plan_type}")]
        ])
        await callback_query.message.edit_text(description, reply_markup=back_keyboard)
        return
    elif data.startswith("pay_plan_"):
        plan = data.split("_")[2]
        user_id = callback_query.from_user.id
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("USDT (BEP20)💰", callback_data=f"payment_method_usdt_{plan}")],
            [InlineKeyboardButton("MiTransfer💳", callback_data=f"payment_method_mtransfer_{plan}")],
            [InlineKeyboardButton("Saldo Móvil📱", callback_data=f"payment_method_saldo_{plan}")],
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back")]
        ])
        await callback_query.message.edit_text("**Seleccione método de Pago:**", reply_markup=keyboard)
        return
    elif data.startswith("payment_method_usdt_"):
        plan = data.split("_")[3]
        user_id = callback_query.from_user.id
        payment_id = await create_payment_request(user_id, plan, "usdt")
        payment_msg = (
            "➥ **Pago vía USDT (BEP20)**\n\n"
            "`0xa2d5ED8f66291bA5a0eADaB135C47900b81001A4`\n\n"
            "**Cuando realice la transferencia toque en\n✅ Verificar.**"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verificar", callback_data=f"verify_payment_{payment_id}")],
            [InlineKeyboardButton("🔙 Volver", callback_data=f"back_to_methods_{plan}_{payment_id}")]
        ])
        await callback_query.message.edit_text(payment_msg, reply_markup=keyboard)
        return
    elif data.startswith("payment_method_mtransfer_"):
        plan = data.split("_")[3]
        user_id = callback_query.from_user.id
        payment_id = await create_payment_request(user_id, plan, "mtransfer")
        payment_msg = (
            "➥ **Pago vía MiTransfer:**\n"
            "📱   `51719347`\n\n"
            "**Tutorial:**\n@Recargas_Via_MiTransfer\n\nSi usa iPhone contáctenme directamente @VirtualMix_Shop para enviarle una Tarjeta\n\n"
            "**Cuando realice la transferencia toque en\n✅ Verificar.**"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verificar", callback_data=f"verify_payment_{payment_id}")],
            [InlineKeyboardButton("🔙 Volver", callback_data=f"back_to_methods_{plan}_{payment_id}")]
        ])
        await callback_query.message.edit_text(payment_msg, reply_markup=keyboard)
        return
    elif data.startswith("payment_method_saldo_"):
        plan = data.split("_")[3]
        user_id = callback_query.from_user.id
        payment_id = await create_payment_request(user_id, plan, "saldo")
        payment_msg = (
            "➥ **Pago vía Saldo Móvil:**\n"
            "📱   `51719347`\n\n"
            "**Cuando realice la transferencia toque en\n✅ Verificar**."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verificar", callback_data=f"verify_payment_{payment_id}")],
            [InlineKeyboardButton("🔙 Volver", callback_data=f"back_to_methods_{plan}_{payment_id}")]
        ])
        await callback_query.message.edit_text(payment_msg, reply_markup=keyboard)
        return
    elif data.startswith("back_to_methods_"):
        parts = data.split("_")
        plan = parts[3]
        payment_id_str = parts[4]
        payment_id = ObjectId(payment_id_str)
        await delete_payment_request(payment_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("USDT (BEP20)💰", callback_data=f"payment_method_usdt_{plan}")],
            [InlineKeyboardButton("Via MiTransfer💳", callback_data=f"payment_method_mtransfer_{plan}")],
            [InlineKeyboardButton("Saldo Móvil📱", callback_data=f"payment_method_saldo_{plan}")],
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back")]
        ])
        await callback_query.message.edit_text("**➥Seleccione método de Pago:**", reply_markup=keyboard)
        return
    elif data.startswith("verify_payment_"):
        payment_id_str = data.split("_")[2]
        payment_id = ObjectId(payment_id_str)
        payment = await get_payment_request(payment_id)
        if not payment:
            await callback_query.answer("⚠️ Esta solicitud de pago ya fue procesada o no existe.", show_alert=True)
            return
        if payment["user_id"] != callback_query.from_user.id:
            await callback_query.answer("⚠️ No tienes permiso para verificar este pago.", show_alert=True)
            return
        if payment["status"] != "awaiting_capture":
            await callback_query.answer("⚠️ Este pago ya fue verificado o cancelado.", show_alert=True)
            return
        await update_payment_status(payment_id, "waiting_receipt", instruction_msg_id=callback_query.message.id)
        await callback_query.message.edit_text("📸 **Mande captura de pantalla de la transferencia**\n\nLa captura debe ser completa, no recortada.\n\n", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancelar Pago ⛔", callback_data=f"cancel_payment_{payment_id}")]]))
        await callback_query.answer("✅ Envía la captura de tu transferencia.")
        return
    elif data.startswith("admin_accept_payment_"):
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo administradores pueden realizar esta acción.", show_alert=True)
            return
        payment_id_str = data.split("_")[3]
        payment_id = ObjectId(payment_id_str)
        payment = await get_payment_request(payment_id)
        if not payment or payment["status"] != "pending_admin":
            await callback_query.answer("⚠️ Esta solicitud ya fue procesada o no existe.", show_alert=True)
            return
        user_id = payment["user_id"]
        plan = payment["plan"]
        try:
            user_info = await app.get_users(user_id)
            username = f"@{user_info.username}" if user_info.username else "Sin username"
        except:
            username = "Sin username"
        success = await set_user_plan(user_id, plan, notify=True)
        if not success:
            await send_protected_message(user_id, f"⚠️ Hubo un problema al activar tu plan **{plan.capitalize()}**. Contacta al soporte.\n@VirtualMix_Shop")
        confirmation_msg_id = payment.get("confirmation_msg_id")
        if confirmation_msg_id:
            try:
                await app.delete_messages(chat_id=user_id, message_ids=[confirmation_msg_id])
            except Exception as e:
                logger.error(f"❌Error eliminando mensaje de confirmación {confirmation_msg_id}: {e}")
        await delete_payment_request(payment_id)
        await callback_query.message.edit_text(f"✅ **Plan {plan.capitalize()} activado para el usuario:**\n👤 {username}\n🆔 ID: `{user_id}`")
        await callback_query.answer("Plan activado.")
        return
    elif data.startswith("admin_reject_payment_"):
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo administradores pueden realizar esta acción.", show_alert=True)
            return
        payment_id_str = data.split("_")[3]
        payment_id = ObjectId(payment_id_str)
        payment = await get_payment_request(payment_id)
        if not payment or payment["status"] != "pending_admin":
            await callback_query.answer("⚠️ Esta solicitud ya fue procesada o no existe.", show_alert=True)
            return
        user_id = payment["user_id"]
        plan = payment["plan"]
        try:
            user_info = await app.get_users(user_id)
            username = f"@{user_info.username}" if user_info.username else "Sin username"
        except:
            username = "Sin username"
        await send_protected_message(user_id, f"❌ **Pago rechazado**\n\nTu solicitud de pago para el plan **{plan.capitalize()}** ha sido rechazada.\n\nPor favor, verifica los datos de transferencia y vuelve a intentarlo.\nSi crees que es un error, contacta al soporte: @VirtualMix_Shop")
        confirmation_msg_id = payment.get("confirmation_msg_id")
        if confirmation_msg_id:
            try:
                await app.delete_messages(chat_id=user_id, message_ids=[confirmation_msg_id])
            except Exception as e:
                logger.error(f"❌Error eliminando mensaje de confirmación {confirmation_msg_id}: {e}")
        await delete_payment_request(payment_id)
        await callback_query.message.edit_text(f"❌ **Pago rechazado para el usuario:**\n👤 {username}\n🆔 ID: `{user_id}`")
        await callback_query.answer("Pago rechazado.")
        return
    config = config_map.get(data)
    if config:
        user_id = callback_query.from_user.id
        if await update_user_video_settings(user_id, config):
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]])
            quality_name = quality_names.get(data, "Calidad Desconocida")
            message_text = f"**{quality_name}\naplicada correctamente**✅"
            await callback_query.message.edit_text(message_text, reply_markup=back_keyboard)
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
    elif data == "back_to_settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗜️ Compresión General", callback_data="general_menu")],
            [InlineKeyboardButton("📱 Videos en Vertical", callback_data="reels_menu")],
            [InlineKeyboardButton("📺 Shows|Calidad media", callback_data="show_menu")],
            [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime_menu")],
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data="custom_quality_start")]
        ])
        await callback_query.message.edit_text("⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️", reply_markup=keyboard)
    else:
        await callback_query.answer("Opción inválida.", show_alert=True)

# ======================== HANDLER DE PAGOS (fotos) ======================== #
@app.on_message(filters.photo & filters.private)
async def handle_payment_capture(client, message: Message):
    user_id = message.from_user.id
    payment = pending_payments_col.find_one({"user_id": user_id, "status": "waiting_receipt"})
    if not payment:
        return
    payment_id = payment["_id"]
    photo = message.photo
    file_id = photo.file_id
    await update_payment_status(payment_id, "pending_admin", file_id)
    instruction_msg_id = payment.get("instruction_msg_id")
    if instruction_msg_id:
        try:
            await app.delete_messages(chat_id=user_id, message_ids=[instruction_msg_id])
            logger.info(f"✅ Mensaje de instrucciones {instruction_msg_id} eliminado para usuario {user_id}")
        except Exception as e:
            logger.error(f"❌Error eliminando mensaje de instrucciones {instruction_msg_id}: {e}")
            try:
                await app.edit_message_text(chat_id=user_id, message_id=instruction_msg_id, text=".")
                logger.info(f"✅ Mensaje de instrucciones editado a punto para usuario {user_id}")
            except Exception as e2:
                logger.error(f"❌No se pudo editar el mensaje de instrucciones: {e2}")
    else:
        logger.warning(f"⚠️ No se encontró instruction_msg_id para la solicitud {payment_id}")
    confirmation_msg = await send_protected_message(user_id, "✅ **Solicitud de compra enviada**\n\nEspera la confirmación de tu pago por el administrador.\nRecibirás una notificación cuando tu plan sea activado.")
    await update_payment_status(payment_id, "pending_admin", file_id, instruction_msg_id=None, confirmation_msg_id=confirmation_msg.id)
    plan = payment["plan"]
    try:
        user_info = await app.get_users(user_id)
        username = f"@{user_info.username}" if user_info.username else "Sin username"
    except:
        username = "Sin username"
    caption = f"**Nueva solicitud de Plan📝**\n\n👤Usuario: {username}\n🆔ID: `{user_id}`\n💠Plan: {plan.capitalize()}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aceptar", callback_data=f"admin_accept_payment_{payment_id}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"admin_reject_payment_{payment_id}")]])
    for admin_id in admin_users:
        try:
            await app.send_photo(chat_id=admin_id, photo=file_id, caption=caption, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"❌Error enviando notificación de pago a admin {admin_id}: {e}")
    logger.info(f"✅Solicitud de pago {payment_id} de {user_id} enviada a admins.")
    try:
        await message.delete()
    except:
        pass

@app.on_message(filters.command("start"))
async def start_command(client, message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id, "start"):
            return
        if user_id in ban_users:
            logger.warning(f"⛔Usuario baneado intentó usar /start: {user_id}")
            return
        pending_payments_col.delete_many({"user_id": user_id, "status": {"$in": ["awaiting_capture", "waiting_receipt"]}})
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_denied_access_message(message.chat.id)
            return
        image_path = "logo.jpg"
        caption = ("**🤖 Bot para comprimir videos**\n➣**Creado por** @boyPhonk\n\n"
                   "**¡Bienvenido!** Puedo reducir el tamaño de los vídeos hasta un 80% o más y se verán bien sin perder tanta calidad.\n"
                   "Usa los botones del menú para interactuar conmigo.\nSi tiene duda use el botón ℹ️ Ayuda\n\n**⚙️ Versión 31.8.0 ⚙️**")
        await send_protected_photo(chat_id=message.chat.id, photo=image_path, caption=caption, reply_markup=get_main_menu_keyboard())
        logger.info(f"🆗Comando /start ejecutado por {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en handle_start: {e}", exc_info=True)

@app.on_message(filters.text & filters.private)
async def main_menu_handler(client, message):
    try:
        user_id = message.from_user.id
        text = message.text.lower()
        if user_id not in admin_users:
            if await check_maintenance_and_notify(user_id, message.chat.id, text):
                return
        if user_id in ban_users:
            return
        if text == "⚙️ settings":
            await settings_menu(client, message)
        elif text == "📋 planes":
            await planes_command(client, message)
        elif text == "📊 mi plan":
            await my_plan_command(client, message)
        elif text == "ℹ️ ayuda":
            support_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("👨🏻‍💻 Soporte", url="https://t.me/boyPhonk")]])
            await send_protected_message(message.chat.id, "👨🏻‍💻 **Información**\n\n➣ **Configurar calidad**:\n• Usa el botón ⚙️ Settings\n➣ **Para comprimir un video**:\n• Envíalo directamente al bot (tanto como vídeo nativo o como documento de vídeo)\n• Extensiones válidas: mp4, mkv, avi, ts, mov, flv, wmv, webm, m4v, 3gp, mpeg, mpg, 3g2, rm, rmvb, vob, f4v, ogv, drc, nsv, mpe, m2v\n➣ **Ver planes**:\n• Usa el botón 📋 Planes\n➣ **Ver tu estado**:\n• Usa el botón 📊 Mi Plan\n➣ **Usa** /start **para iniciar en el bot nuevamente o para actualizar**\n➣ **Ver cola de compresión**:\n• Usa el botón 👀 Ver Cola\n➣ **Cancelar videos de la cola**:\n• Usa el botón 🗑️ Cancelar Cola\n➣ **Para ver su configuración de compresión actual use**: /calidad\n➣ **Para ver el estado del bot use**: /estado\n• Simplemente envía el documento y el bot lo procesará automáticamente.\n➣ **Comando** /mode **agregado.**\n• Ahora pueden elegir entre si comprimír directo sin configurar calidad o configurarle la calidad a cada video antes de comprimír\n\n**NUEVO SISTEMA:**\n• Los videos se descargan inmediatamente y se agregarán a la cola de compresión\n• Progreso en tiempo real", reply_markup=support_keyboard)
        elif text == "👀 ver cola":
            await queue_command(client, message)
        elif text == "🗑️ cancelar cola":
            await cancel_queue_command(client, message)
        elif text == "/cancel":
            await cancel_command(client, message)
        else:
            await handle_message(client, message)
    except Exception as e:
        logger.error(f"❌Error en main_menu_handler: {e}", exc_info=True)

@app.on_message(filters.command("mode") & filters.private)
async def mode_command(client, message):
    user_id = message.from_user.id
    user_plan = await get_user_plan(user_id)
    if user_plan is None or user_plan.get("plan") is None:
        await send_denied_access_message(message.chat.id)
        return
    current_mode = await get_user_compression_mode(user_id)
    mode_text = (
        "**Seleccione el modo de compresión:**\n\n"
        f"{'✅' if current_mode == 'after' else '❌'} • **Configurar calidad al enviar video**\n"
        f"{'✅' if current_mode == 'before' else '❌'} • **Configurar calidad antes de enviar video**\n\n"
        "Elige el modo que prefieras para tus videos."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ Configurar calidad al enviar video", callback_data="mode_after")],
        [InlineKeyboardButton("⚙️ Configurar calidad antes de enviar video", callback_data="mode_before")]
    ])
    await send_protected_message(message.chat.id, mode_text, reply_markup=keyboard)

@app.on_message(filters.command("desuser") & filters.user(admin_users))
async def unban_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /desuser <user_id>")
            return
        user_id = int(parts[1])
        if user_id in ban_users:
            ban_users.remove(user_id)
        result = banned_col.delete_one({"user_id": user_id})
        if result.deleted_count > 0:
            await message.reply(f"Usuario {user_id} desbaneado exitosamente.")
            try:
                await app.send_message(user_id, "✅ **Tu acceso al bot ha sido restaurado.**\n\nAhora puedes volver a usar el bot.")
            except Exception as e:
                logger.error(f"❌No se pudo notificar al usuario {user_id}: {e}")
        else:
            await message.reply(f"El usuario {user_id} no estaba baneado.")
        logger.info(f"✅Usuario desbaneado: {user_id} por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en unban_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al desbanear usuario. Formato: /desuser [user_id]")

@app.on_message(filters.command("deleteuser") & filters.user(admin_users))
async def delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /deleteuser <user_id>")
            return
        user_id = int(parts[1])
        result = users_col.delete_one({"user_id": user_id})
        if user_id not in ban_users:
            ban_users.append(user_id)
        banned_col.insert_one({"user_id": user_id, "banned_at": datetime.datetime.now()})
        pending_result = pending_col.delete_many({"user_id": user_id})
        downloaded_result = downloaded_videos_col.delete_many({"user_id": user_id})
        user_settings_col.delete_one({"user_id": user_id})
        await message.reply(f"Usuario {user_id} eliminado y baneado exitosamente.\n🗑️ Tareas pendientes eliminadas: {pending_result.deleted_count}\n🗑️ Videos descargados eliminados: {downloaded_result.deleted_count}")
        logger.info(f"✅Usuario eliminado y baneado: {user_id} por admin {message.from_user.id}")
        try:
            await app.send_message(user_id, "🔒 **Tu acceso al bot ha sido revocado.**\n\nNo podrás usar el bot hasta nuevo aviso.")
        except Exception as e:
            logger.error(f"❌No se pudo notificar al usuario {user_id}: {e}")
    except Exception as e:
        logger.error(f"❌Error en delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuario. Formato: /deleteuser [user_id]")

@app.on_message(filters.command("viewban") & filters.user(admin_users))
async def view_banned_users_command(client, message):
    try:
        banned_users = list(banned_col.find({}))
        if not banned_users:
            await message.reply("**No hay usuarios baneados.**")
            return
        response = "**Usuarios Baneados**\n\n"
        for i, banned_user in enumerate(banned_users, 1):
            user_id = banned_user["user_id"]
            banned_at = banned_user.get("banned_at", "Fecha desconocida")
            try:
                user = await app.get_users(user_id)
                username = f"@{user.username}" if user.username else "Sin username"
            except:
                username = "Sin username"
            banned_at_str = banned_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(banned_at, datetime.datetime) else str(banned_at)
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   ⏰ Fecha: {banned_at_str}\n\n"
        await message.reply(response)
    except Exception as e:
        logger.error(f"❌Error en view_banned_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al obtener la lista de usuarios baneados")

@app.on_message(filters.command(["banuser", "deluser"]) & filters.user(admin_users))
async def ban_or_delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /comando <user_id>")
            return
        ban_user_id = int(parts[1])
        if ban_user_id in admin_users:
            await message.reply("No puedes banear a un administrador.")
            return
        result = users_col.delete_one({"user_id": ban_user_id})
        if ban_user_id not in ban_users:
            ban_users.append(ban_user_id)
        banned_col.insert_one({"user_id": ban_user_id, "banned_at": datetime.datetime.now()})
        user_settings_col.delete_one({"user_id": ban_user_id})
        downloaded_videos_col.delete_many({"user_id": ban_user_id})
        await message.reply(f"Usuario {ban_user_id} baneado y eliminado de la base de datos." if result.deleted_count > 0 else f"Usuario {ban_user_id} baneado (no estaba en la base de datos).")
    except Exception as e:
        logger.error(f"❌Error en ban_or_delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("key") & filters.private)
async def key_command(client, message):
    try:
        user_id = message.from_user.id
        if user_id in ban_users:
            await send_protected_message(message.chat.id, "🚫 Tu acceso ha sido revocado.")
            return
        logger.info(f"🆗Comando key recibido de {user_id}")
        if not message.text or len(message.text.split()) < 2:
            await send_protected_message(message.chat.id, "❌ Formato: /key <clave>")
            return
        key = message.text.split()[1].strip()
        now = datetime.datetime.now()
        key_data = temp_keys_col.find_one({"key": key, "used": False})
        if not key_data:
            await send_protected_message(message.chat.id, "❌ **Clave inválida o ya ha sido utilizada.**")
            return
        if key_data["expires_at"] < now:
            await send_protected_message(message.chat.id, "❌ **La clave ha expirado.**")
            return
        temp_keys_col.update_one({"_id": key_data["_id"]}, {"$set": {"used": True}})
        new_plan = key_data["plan"]
        duration_value = key_data["duration_value"]
        duration_unit = key_data["duration_unit"]
        if duration_unit == "minutes":
            expires_at = datetime.datetime.now() + datetime.timedelta(minutes=duration_value)
        elif duration_unit == "hours":
            expires_at = datetime.datetime.now() + datetime.timedelta(hours=duration_value)
        else:
            expires_at = datetime.datetime.now() + datetime.timedelta(days=duration_value)
        success = await set_user_plan(user_id, new_plan, notify=False, expires_at=expires_at)
        if success:
            duration_text = f"{duration_value} {duration_unit}"
            if duration_value == 1:
                duration_text = duration_text[:-1]
            await send_protected_message(message.chat.id, f"✅ **Plan {new_plan.capitalize()} activado!**\n**Válido por {duration_text}**\n\nUse el comando /start para iniciar en el bot")
            logger.info(f"✅Plan actualizado a {new_plan} para {user_id} con clave {key}")
        else:
            await send_protected_message(message.chat.id, "❌ **Error al activar el plan. Contacta con el administrador.**")
    except Exception as e:
        logger.error(f"❌Error en key_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al procesar la solicitud de acceso**")

sent_messages = {}

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

@app.on_message(filters.command("myplan") & filters.private)
async def my_plan_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_denied_access_message(message.chat.id)
        else:
            plan_info, keyboard = await get_plan_info(user_id)
            await send_protected_message(message.chat.id, plan_info, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"❌Error en my_plan_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "⚠️ **Error al obtener información de tu plan**", reply_markup=get_main_menu_keyboard())

@app.on_message(filters.command("setplan") & filters.user(admin_users))
async def set_plan_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("Formato: /setplan <user_id> <plan>")
            return
        user_id = int(parts[1])
        plan = parts[2].lower()
        if plan not in PLAN_DURATIONS:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(PLAN_DURATIONS.keys())}")
            return
        if await set_user_plan(user_id, plan):
            await message.reply(f"**Plan del usuario {user_id} actualizado a {plan}.**")
        else:
            await message.reply("⚠️ **Error al actualizar el plan.**")
    except Exception as e:
        logger.error(f"❌Error en set_plan_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error en el comando**")

@app.on_message(filters.command("userinfo") & filters.user(admin_users))
async def user_info_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /userinfo <user_id>")
            return
        user_id = int(parts[1])
        user = await get_user_plan(user_id)
        try:
            user_info = await app.get_users(user_id)
            username = f"@{user_info.username}" if user_info.username else "Sin username"
        except:
            username = "Sin username"
        if user:
            plan_name = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            join_date = user.get("join_date", "Desconocido")
            expires_at = user.get("expires_at", "No expira")
            compressed_videos = user.get("compressed_videos", 0)
            user_number = user.get("user_number", "N/A")
            if isinstance(join_date, datetime.datetime):
                join_date = join_date.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(expires_at, datetime.datetime):
                expires_at = expires_at.strftime("%Y-%m-%d %H:%M:%S")
            await message.reply(f"👤**Usuario**: {username}\n🆔 **ID**: `{user_id}`\n🔢 **Número**: {user_number}\n📝 **Plan**: {plan_name}\n🎬 **Videos comprimidos**: {compressed_videos}\n📅 **Fecha de registro**: {join_date}\n⏰ **Expira**: {expires_at}")
        else:
            await message.reply("⚠️ Usuario no registrado o sin plan")
    except Exception as e:
        logger.error(f"❌Error en user_info_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("restuser") & filters.user(admin_users))
async def reset_all_users_command(client, message):
    try:
        result = users_col.delete_many({})
        user_settings_col.delete_many({})
        downloaded_videos_col.delete_many({})
        await message.reply(f"**Todos los usuarios han sido eliminados**\nUsuarios eliminados: {result.deleted_count}\nVideos descargados eliminados: {downloaded_videos_col.count_documents({})}")
        logger.info(f"✅Todos los usuarios eliminados por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌Error en reset_all_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuarios")

@app.on_message(filters.command("user") & filters.user(admin_users))
async def list_users_command(client, message):
    try:
        all_users = list(users_col.find({}))
        if not all_users:
            await message.reply("⛔**No hay usuarios registrados.**⛔")
            return
        response = "**Lista de Usuarios Registrados**\n\n"
        for user in all_users:
            user_id = user["user_id"]
            plan = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            user_number = user.get("user_number", "?")
            try:
                user_info = await app.get_users(user_id)
                username = f"@{user_info.username}" if user_info.username else "Sin username"
            except:
                username = "Sin username"
            response += f"{user_number}• 👤 {username}\n   🆔 ID: `{user_id}`\n   📝 Plan: {plan}\n\n"
        await message.reply(response)
    except Exception as e:
        logger.error(f"❌Error en list_users_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al listar usuarios**")

@app.on_message(filters.command("admin") & filters.user(admin_users))
async def admin_stats_command(client, message):
    try:
        pipeline = [{"$match": {"plan": {"$exists": True, "$ne": None}}}, {"$group": {"_id": "$plan", "count": {"$sum": 1}}}]
        stats = list(users_col.aggregate(pipeline))
        total_users = users_col.count_documents({})
        total_downloaded = downloaded_videos_col.count_documents({})
        total_pending = pending_col.count_documents({})
        active_compr = list(active_compressions_col.find({}))
        total_active = len(active_compr)
        response = "📊 **Estadísticas de Administrador**\n\n"
        response += f"👥 **Total de usuarios:** {total_users}\n"
        response += f"📥 **Videos descargados en cola:** {total_downloaded}\n"
        response += f"⏳ **Videos pendientes de descargar:** {total_pending}\n"
        response += f"⬇️ **Descargas activas:** 1/1\n"
        response += f"🔄 **Compresiones activas:** {total_active}\n\n"
        if total_active > 0:
            response += "📋 **Compresiones activas:**\n"
            for i, comp in enumerate(active_compr, 1):
                comp_user_id = comp.get("user_id")
                file_name = comp.get("file_name", "Sin nombre")
                start_time = comp.get("start_time")
                user_number = get_user_number(comp_user_id)
                if user_number:
                    username = f"Usuario {user_number}"
                else:
                    username = f"Usuario {comp_user_id}"
                start_str = start_time.strftime("%H:%M:%S") if isinstance(start_time, datetime.datetime) else "¿?"
                response += f"{i}. {username} - `{file_name}` (⏰ {start_str})\n"
            response += "\n"
        response += "📝 **Distribución por Planes:**\n"
        plan_names = {"standard": "🧩 Estándar", "pro": "💎 Pro", "premium": "👑 Premium", "ultra": "🚀 Ultra"}
        for stat in stats:
            plan_type = stat["_id"]
            count = stat["count"]
            plan_name = plan_names.get(plan_type, plan_type.capitalize() if plan_type else "❓ Desconocido")
            response += f"\n{plan_name}:\n  👥 Usuarios: {count}\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_admin_stats"), InlineKeyboardButton("❌ Cerrar", callback_data="close_admin_stats")]])
        await message.reply(response, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"❌Error en admin_stats_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al generar estadísticas**")

async def broadcast_message(admin_id: int, message_text: str):
    try:
        user_ids = set()
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        if total_users == 0:
            await app.send_message(admin_id, "📭 No hay usuarios para enviar el mensaje.")
            return
        await app.send_message(admin_id, f"📤 **Iniciando difusión a {total_users} usuarios...**\n⏱ Esto puede tomar varios minutos.")
        success = 0
        failed = 0
        count = 0
        for user_id in user_ids:
            count += 1
            try:
                await send_protected_message(user_id, f"**🔔Notificación:**\n\n{message_text}")
                success += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"❌Error enviando mensaje a {user_id}: {e}")
                failed += 1
        await app.send_message(admin_id, f"✅ **Difusión completada!**\n\n👥 Total de usuarios: {total_users}\n✅ Enviados correctamente: {success}\n❌ Fallidos: {failed}")
    except Exception as e:
        logger.error(f"❌Error en broadcast_message: {e}", exc_info=True)
        await app.send_message(admin_id, f"⚠️ Error en difusión: {str(e)}")

@app.on_message(filters.command("msg") & filters.user(admin_users))
async def broadcast_command(client, message):
    try:
        if not message.text or len(message.text.split()) < 2:
            await message.reply("⚠️ Formato: /msg <mensaje>")
            return
        parts = message.text.split(maxsplit=1)
        broadcast_text = parts[1] if len(parts) > 1 else ""
        if not broadcast_text.strip():
            await message.reply("⚠️ El mensaje no puede estar vacío")
            return
        admin_id = message.from_user.id
        asyncio.create_task(broadcast_message(admin_id, broadcast_text))
        await message.reply("📤 **Difusión iniciada!**\n⏱ Los mensajes se enviarán progresivamente a todos los usuarios.\nRecibirás un reporte final cuando se complete.")
    except Exception as e:
        logger.error(f"❌Error en broadcast_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al iniciar la difusión")

@app.on_message(filters.command("broadcast") & filters.user(admin_users))
async def broadcast_multimedia_command(client, message):
    user_id = message.from_user.id
    broadcast_sessions[user_id] = {"type": "awaiting_content"}
    await message.reply("📢 **Modo difusión activado**\n\nEnvía el mensaje que deseas difundir a todos los usuarios con plan activo.\nPuede ser texto, foto, video, audio, voz o documento (con o sin caption).\nPara cancelar, usa /cancelbroadcast")

@app.on_message(filters.command("cancelbroadcast") & filters.user(admin_users))
async def cancel_broadcast_command(client, message):
    user_id = message.from_user.id
    if user_id in broadcast_sessions:
        del broadcast_sessions[user_id]
        await message.reply("❌ **Difusión cancelada.**")
    else:
        await message.reply("ℹ️ No hay ninguna difusión activa.")

@app.on_message(filters.all & filters.user(admin_users), group=1)
async def broadcast_content_handler(client, message):
    user_id = message.from_user.id
    if user_id not in broadcast_sessions:
        return
    if message.text and message.text.startswith('/'):
        return
    await process_broadcast(client, message)
    del broadcast_sessions[user_id]

async def process_broadcast(client, message):
    admin_id = message.from_user.id
    users = list(users_col.find({"plan": {"$in": ["standard", "pro", "premium", "ultra"]}, "$or": [{"expires_at": {"$exists": False}}, {"expires_at": None}, {"expires_at": {"$gt": datetime.datetime.now()}}]}))
    user_ids = [u["user_id"] for u in users if u["user_id"] not in ban_users]
    if not user_ids:
        await message.reply("📭 No hay usuarios con plan activo para enviar el mensaje.")
        return
    status_msg = await message.reply(f"📤 **Iniciando difusión a {len(user_ids)} usuarios...**\n⏱ Esto puede tomar varios minutos.")
    success = 0
    failed = 0
    media_type = None
    media = None
    caption = message.caption if message.caption else None
    if message.text:
        media_type = "text"
        content = message.text
    elif message.photo:
        media_type = "photo"
        media = message.photo.file_id
    elif message.video:
        media_type = "video"
        media = message.video.file_id
    elif message.document:
        media_type = "document"
        media = message.document.file_id
    elif message.audio:
        media_type = "audio"
        media = message.audio.file_id
    elif message.voice:
        media_type = "voice"
        media = message.voice.file_id
    else:
        await status_msg.edit_text("❌ Tipo de mensaje no soportado para difusión.")
        return
    for user_id in user_ids:
        try:
            if media_type == "text":
                await send_protected_message(user_id, content)
            elif media_type == "photo":
                await send_protected_photo(user_id, media, caption=caption)
            elif media_type == "video":
                await send_protected_video(user_id, media, caption=caption)
            elif media_type == "document":
                await send_protected_document(user_id, media, caption=caption)
            elif media_type == "audio":
                await send_protected_audio(user_id, media, caption=caption)
            elif media_type == "voice":
                await send_protected_voice(user_id, media, caption=caption)
            success += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"❌Error enviando broadcast a {user_id}: {e}")
            failed += 1
    await status_msg.edit_text(f"✅ **Difusión completada!**\n\n👥 Total de usuarios: {len(user_ids)}\n✅ Enviados correctamente: {success}\n❌ Fallidos: {failed}")

async def queue_command(client, message):
    user_id = message.from_user.id
    user_plan = await get_user_plan(user_id)
    if user_plan is None or user_plan.get("plan") is None:
        await send_denied_access_message(message.chat.id)
        return

    queue_status, keyboard = await get_queue_status(user_id)

    # Si ya hay un mensaje de cola para este usuario, cancelar tarea y eliminar mensaje anterior
    if user_id in user_queue_tasks:
        data = user_queue_tasks[user_id]
        data["task"].cancel()
        try:
            await app.delete_messages(chat_id=data["chat_id"], message_ids=[data["message_id"]])
        except Exception as e:
            logger.error(f"Error eliminando mensaje de cola anterior: {e}")
        del user_queue_tasks[user_id]

    # Enviar mensaje nuevo
    msg = await send_protected_message(message.chat.id, queue_status, reply_markup=keyboard)
    chat_id = msg.chat.id
    message_id = msg.id

    # Crear la tarea de actualización
    task = asyncio.create_task(update_queue_loop(chat_id, message_id, user_id))
    user_queue_tasks[user_id] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "task": task
    }

async def notify_all_users(message_text: str):
    try:
        user_ids = set()
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        if total_users == 0:
            return 0, 0
        success = 0
        failed = 0
        for user_id in user_ids:
            try:
                await send_protected_message(user_id, message_text)
                success += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"❌Error enviando mensaje de notificación a {user_id}: {e}")
                failed += 1
        return success, failed
    except Exception as e:
        logger.error(f"❌Error en notify_all_users: {e}", exc_info=True)
        return 0, 0

async def clean_temp_directory():
    try:
        if not os.path.exists(BOT_TEMP_DIR):
            logger.warning(f"El directorio temporal {BOT_TEMP_DIR} no existe.")
            return
        for root, dirs, files in os.walk(BOT_TEMP_DIR, topdown=False):
            for name in files:
                file_path = os.path.join(root, name)
                try:
                    os.remove(file_path)
                    logger.debug(f"Archivo temporal eliminado: {file_path}")
                except Exception as e:
                    logger.error(f"Error eliminando archivo {file_path}: {e}")
            for name in dirs:
                dir_path = os.path.join(root, name)
                try:
                    shutil.rmtree(dir_path, ignore_errors=True)
                    logger.debug(f"Directorio temporal eliminado: {dir_path}")
                except Exception as e:
                    logger.error(f"Error eliminando directorio {dir_path}: {e}")
        logger.info(f"✅ Directorio temporal {BOT_TEMP_DIR} limpiado completamente.")
    except Exception as e:
        logger.error(f"❌Error limpiando directorio temporal: {e}")

async def restart_bot():
    try:
        await clean_temp_directory()
        for compression_id, process in list(ffmpeg_processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
            except Exception as e:
                logger.error(f"❌Error terminando proceso FFmpeg para {compression_id}: {e}")
        ffmpeg_processes.clear()
        cancel_tasks.clear()
        active_messages.clear()
        while not compression_processing_queue.empty():
            try:
                compression_processing_queue.get_nowait()
                compression_processing_queue.task_done()
            except asyncio.QueueEmpty:
                break
        result = pending_col.delete_many({})
        downloaded_result = downloaded_videos_col.delete_many({})
        logger.info(f"✅Eliminados {result.deleted_count} elementos de la cola")
        logger.info(f"✅Eliminados {downloaded_result.deleted_count} videos descargados")
        active_compressions_col.delete_many({})
        notification_text = "🔔**Notificación:**\n\nEl bot ha sido reiniciado\ntodos los procesos se han cancelado.\n\n✅ **Ahora puedes enviar nuevos videos para comprimir**."
        success, failed = await notify_all_users(notification_text)
        try:
            await app.send_message(-1003896005361, f"**Notificación de reinicio completada!**\n\n✅ Enviados correctamente: {success}\n❌ Fallidos: {failed}")
        except Exception as e:
            logger.error(f"❌Error enviando notificación de reinicio al grupo: {e}")
        return True, success, failed
    except Exception as e:
        logger.error(f"❌Error en restart_bot: {e}", exc_info=True)
        return False, 0, 0

@app.on_message(filters.command("restart") & filters.user(admin_users))
async def restart_command(client, message):
    try:
        msg = await message.reply("🔄 Reiniciando bot...")
        success, notifications_sent, notifications_failed = await restart_bot()
        if success:
            await msg.edit(f"**Bot reiniciado con éxito**\n\n✅ Todos los procesos activos cancelados\n✅ Cola de compresión vaciada\n✅ Videos descargados eliminados\n✅ Procesos FFmpeg terminados\n✅ Estado interno limpiado\n✅ Directorio temporal limpiado\n\n📤 Notificaciones enviadas: {notifications_sent}\n❌ Notificaciones fallidas: {notifications_failed}")
        else:
            await msg.edit("⚠️ **Error al reiniciar el bot.**")
    except Exception as e:
        logger.error(f"❌Error en restart_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al ejecutar el comando de reinicio")

@app.on_message(filters.command(["calidad", "quality"]) & filters.private)
async def calidad_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_denied_access_message(message.chat.id)
            return
        if len(message.text.split()) < 2:
            current_settings = await get_user_video_settings(user_id)
            resolution = current_settings['resolution']
            resolution_display = resolution.split('x')[1] if 'x' in resolution else resolution
            response = (f"**Tu configuración actual de compresión:**\n\n• **Resolución**: `{resolution_display}`\n• **CRF**: `{current_settings['crf']}`\n• **FPS**: `{current_settings['fps']}`\n• **Bitrate de audio**: `{current_settings['audio_bitrate']}`\n\nPara restablecer a la configuración por defecto, usa /resetcalidad")
            await send_protected_message(message.chat.id, response)
            return
        command_text = message.text.split(maxsplit=1)[1]
        success = await update_user_video_settings(user_id, command_text)
        if success:
            new_settings = await get_user_video_settings(user_id)
            response = "✅ **Configuración actualizada correctamente:**\n\n"
            for key, value in new_settings.items():
                response += f"• **{key}**: `{value}`\n"
            await send_protected_message(message.chat.id, response)
        else:
            await send_protected_message(message.chat.id, "❌ **Error al actualizar la configuración.**\nFormato correcto: /calidad resolution=-2:480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264")
    except Exception as e:
        logger.error(f"❌Error en calidad_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al procesar el comando.**\nFormato correcto: /calidad resolution=-2:480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264")

@app.on_message(filters.command("resetcalidad") & filters.private)
async def reset_calidad_command(client, message):
    try:
        user_id = message.from_user.id
        await reset_user_video_settings(user_id)
        default_settings = await get_user_video_settings(user_id)
        resolution = default_settings['resolution']
        resolution_display = resolution.split('x')[1] if 'x' in resolution else resolution
        response = (f"✅ **Configuración restablecida a los valores por defecto:**\n\n• **resolución**: {resolution_display}\n• **crf**: {default_settings['crf']}\n• **fps**: {default_settings['fps']}\n• **audio_bitrate**: {default_settings['audio_bitrate']}")
        await send_protected_message(message.chat.id, response)
    except Exception as e:
        logger.error(f"❌Error en reset_calidad_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al restablecer la configuración.**")

@app.on_message(filters.video & filters.private)
async def handle_video(client, message: Message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id):
            return
        file_name = getattr(message.video, 'file_name', None)
        if not file_name:
            file_name = "video_sin_nombre"
        await process_media_file(client, message, message.video, file_name)
    except Exception as e:
        logger.error(f"❌Error en handle_video: {e}", exc_info=True)

@app.on_message(filters.document & filters.private)
async def handle_document_video(client, message: Message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id):
            return
        doc = message.document
        if not doc:
            return
        file_name = doc.file_name or ""
        if not is_supported_video_file(file_name):
            mime = doc.mime_type or ""
            if not mime.startswith('video/'):
                await send_protected_message(
    message.chat.id,
    f"❌ **Formato no soportado.**\nExtensiones válidas: {', '.join(SUPPORTED_VIDEO_EXTENSIONS)}",
    reply_to_message_id=message.id
)
                return
        await process_media_file(client, message, doc, file_name)
        logger.info(f"✅Documento de vídeo procesado automáticamente para {user_id}: {file_name}")
    except Exception as e:
        logger.error(f"❌Error en handle_document_video: {e}", exc_info=True)

@app.on_message(filters.text)
async def handle_message(client, message):
    try:
        user_id = message.from_user.id
        if user_id not in admin_users:
            if await check_maintenance_and_notify(user_id, message.chat.id, message.text):
                return
        text = message.text
        username = message.from_user.username
        chat_id = message.chat.id
        if user_id in ban_users:
            return
        logger.info(f"💬Mensaje recibido de {user_id}: {text}")
        if text.startswith(('/calidad', '.calidad', '/quality', '.quality')):
            await calidad_command(client, message)
        elif text.startswith(('/resetcalidad', '.resetcalidad')):
            await reset_calidad_command(client, message)
        elif text.startswith(('/settings', '.settings')):
            await settings_menu(client, message)
        elif text.startswith(('/banuser', '.banuser', '/deluser', '.deluser')):
            if user_id in admin_users:
                await ban_or_delete_user_command(client, message)
            else:
                logger.warning(f"⛔Intento no autorizado de banuser/deluser por {user_id}")
        elif text.startswith(('/cola', '.cola')):
            if user_id in admin_users:
                await show_queue(client, message)
        elif text.startswith(('/auto', '.auto')):
            if user_id in admin_users:
                await startup_command(client, message)
        elif text.startswith(('/myplan', '.myplan')):
            await my_plan_command(client, message)
        elif text.startswith(('/setplan', '.setplan')):
            if user_id in admin_users:
                await set_plan_command(client, message)
        elif text.startswith(('/userinfo', '.userinfo')):
            if user_id in admin_users:
                await user_info_command(client, message)
        elif text.startswith(('/planes', '.planes')):
            await planes_command(client, message)
        elif text.startswith(('/generatekey', '.generatekey')):
            if user_id in admin_users:
                await generate_key_command(client, message)
        elif text.startswith(('/listkeys', '.listkeys')):
            if user_id in admin_users:
                await list_keys_command(client, message)
        elif text.startswith(('/delkeys', '.delkeys')):
            if user_id in admin_users:
                await del_keys_command(client, message)
        elif text.startswith(('/user', '.user')):
            if user_id in admin_users:
                await list_users_command(client, message)
        elif text.startswith(('/admin', '.admin')):
            if user_id in admin_users:
                await admin_stats_command(client, message)
        elif text.startswith(('/restuser', '.restuser')):
            if user_id in admin_users:
                await reset_all_users_command(client, message)
        elif text.startswith(('/desuser', '.desuser')):
            if user_id in admin_users:
                await unban_user_command(client, message)
        elif text.startswith(('/deleteuser', '.deleteuser')):
            if user_id in admin_users:
                await delete_user_command(client, message)
        elif text.startswith(('/viewban', '.viewban')):
            if user_id in admin_users:
                await view_banned_users_command(client, message)
        elif text.startswith(('/msg', '.msg')):
            if user_id in admin_users:
                await broadcast_command(client, message)
        elif text.startswith(('/broadcast', '.broadcast')):
            if user_id in admin_users:
                await broadcast_multimedia_command(client, message)
        elif text.startswith(('/cancelbroadcast', '.cancelbroadcast')):
            if user_id in admin_users:
                await cancel_broadcast_command(client, message)
        elif text.startswith(('/cancel', '.cancel')):
            await cancel_command(client, message)
        elif text.startswith(('/cancelqueue', '.cancelqueue')):
            await cancel_queue_command(client, message)
        elif text.startswith(('/key', '.key')):
            await key_command(client, message)
        elif text.startswith(('/restart', '.restart')):
            if user_id in admin_users:
                await restart_command(client, message)
        elif text.startswith(('/getdb', '.getdb')):
            if user_id in admin_users:
                await get_db_command(client, message)
        elif text.startswith(('/restdb', '.restdb')):
            if user_id in admin_users:
                await rest_db_command(client, message)
        elif text.startswith(('/backup', '.backup')):
            if user_id in admin_users:
                await backup_command(client, message)
        elif text.startswith(('/setdays', '.setdays')):
            if user_id in admin_users:
                await setdays_command(client, message)
        elif text.startswith(('/status', '.status')):
            if user_id in admin_users:
                await status_command(client, message)
        elif text.startswith(('/ls', '.ls')):
            if user_id in admin_users:
                await daily_stats_command(client, message)
        elif text.startswith(('/watchdog', '.watchdog', '/whactdog', '.whactdog')):
            if user_id in admin_users:
                await watchdog_status_command(client, message)
        elif text.startswith(('/log', '.log')):
            if user_id in admin_users:
                await log_command(client, message)
        elif text.startswith(('/checkexpiry', '.checkexpiry')):
            if user_id in admin_users:
                await manual_check_expiry_command(client, message)
        elif text.startswith(('/estado', '.estado')):
            await estado_command(client, message)
        elif text.startswith(('/man_on', '.man_on')):
            if user_id in admin_users:
                await maintenance_on_command(client, message)
        elif text.startswith(('/man_off', '.man_off')):
            if user_id in admin_users:
                await maintenance_off_command(client, message)
        elif text.startswith(('/mode', '.mode')):
            await mode_command(client, message)
        elif text.startswith(('/convert', '.convert')):
            await send_protected_message(message.chat.id, "✅ **Ya no es necesario usar /convert.** Simplemente envía el documento de vídeo y el bot lo procesará automáticamente.")
        if message.reply_to_message:
            original_message = sent_messages.get(message.reply_to_message.id)
            if original_message:
                user_id = original_message["user_id"]
                sender_info = f"Respuesta de @{message.from_user.username}" if message.from_user.username else f"Respuesta de user ID: {message.from_user.id}"
                await send_protected_message(user_id, f"{sender_info}: {message.text}")
                logger.info(f"💬Respuesta enviada a {user_id}")
    except Exception as e:
        logger.error(f"❌Error en handle_message: {e}", exc_info=True)

async def notify_group(client, message: Message, original_size: int, compressed_size: int = None, status: str = "start", processing_time_str: str = None, compressed_name: str = None):
    try:
        group_id = -1003896005361
        try:
            await client.get_chat(group_id)
        except Exception as e:
            logger.error(f"❌El bot no puede acceder al grupo {group_id}: {e}")
            return
        user = message.from_user
        username = f"@{user.username}" if user.username else "Sin username"
        if message.video:
            original_file_name = message.video.file_name or "Sin nombre"
        elif message.document:
            original_file_name = message.document.file_name or "Sin nombre"
        else:
            original_file_name = "Desconocido"
        size_mb = original_size // (1024 * 1024)
        if status == "start":
            text = f"🗜️ **Nuevo video recibido para comprimir**\n\n👤 **Usuario:** {username}\n🆔 **ID:** `{user.id}`\n📦 **Tamaño original:** {size_mb} MB\n📁 **Nombre:** `{original_file_name}`"
        elif status == "done" and compressed_size is not None:
            compressed_mb = compressed_size // (1024 * 1024)
            display_name = compressed_name if compressed_name else original_file_name
            text_lines = [
                "✅ **Video comprimido y enviado**\n",
                f"👤 **Usuario:** {username}",
                f"🆔 **ID:** `{user.id}`",
                f"📦 **Tamaño original:** {size_mb} MB",
                f"📉 **Tamaño comprimido:** {compressed_mb} MB"
            ]
            if processing_time_str:
                text_lines.append(f"⏰ **Tiempo transcurrido:** {processing_time_str}")
            text_lines.append(f"📁 **Nombre:** `{display_name}`")
            text = "\n".join(text_lines)
        else:
            return
        await client.send_message(chat_id=group_id, text=text)
        logger.info(f"💬Notificación enviada al grupo: {user.id} - {display_name if status=='done' else original_file_name} ({status})")
    except Exception as e:
        logger.error(f"❌Error enviando notificación al grupo: {e}", exc_info=True)

async def recover_pending_compressions():
    global processing_tasks
    try:
        downloaded_count = downloaded_videos_col.count_documents({})
        if downloaded_count == 0:
            return
        active_count = active_compressions_col.count_documents({})
        if active_count > 0:
            return
        logger.info("🔱Watchdog: No hay compresiones activas pero hay videos descargados. Recuperando🔄...")
        downloaded_videos = list(downloaded_videos_col.find().sort("timestamp", 1))
        for video in downloaded_videos:
            compression_id = video["compression_id"]
            user_id = video["user_id"]
            file_path = video["file_path"]
            file_name = video["file_name"]
            chat_id = video.get("chat_id", user_id)
            wait_msg_id = video.get("wait_msg_id")
            wait_msg = None
            if wait_msg_id:
                try:
                    wait_msg = await app.get_messages(chat_id, wait_msg_id)
                except Exception as e:
                    logger.warning(f"⛔No se pudo recuperar mensaje {wait_msg_id} para {compression_id}: {e}")
            if wait_msg is None:
                try:
                    wait_msg = await app.send_message(chat_id, f"🔄 **Reanudando compresión**\n\n📁 `{file_name}`\n⏳ Preparando para comprimir...\n\n❌**Este mensaje se autoeliminará en 5 segundos**❌")
                    asyncio.create_task(delete_message_after(wait_msg, 5))
                    downloaded_videos_col.update_one({"compression_id": compression_id}, {"$set": {"wait_msg_id": wait_msg.id}})
                    logger.info(f"✅Nuevo mensaje de espera creado para {compression_id}")
                except Exception as e:
                    logger.error(f"❌Error creando nuevo mensaje de espera para {compression_id}: {e}")
                    wait_msg = None
            task = {"compression_id": compression_id, "user_id": user_id, "original_video_path": file_path, "file_name": file_name, "chat_id": chat_id, "original_message_id": video.get("original_message_id", 0), "wait_msg_id": wait_msg.id if wait_msg else 0, "wait_msg": wait_msg, "caption": video.get("original_caption"), "custom_settings": video.get("custom_settings")}
            await compression_processing_queue.put(task)
            logger.info(f"✅Watchdog: Video {file_name} añadido a la cola de compresión.")
        await update_daily_stats_recovery(1)
        workers_revived = 0
        new_tasks = []
        for i, task in enumerate(processing_tasks):
            if task.done():
                try:
                    exc = task.exception()
                    if exc:
                        logger.error(f"❌Worker {i} terminó con excepción: {exc}")
                    else:
                        logger.warning(f"⛔Worker {i} terminó inesperadamente")
                except:
                    logger.warning(f"⛔Worker {i} no está activo")
                new_task = asyncio.create_task(process_compression_queue())
                new_tasks.append(new_task)
                workers_revived += 1
                logger.info(f"✅Worker {i} reemplazado por nuevo worker")
            else:
                new_tasks.append(task)
        processing_tasks = new_tasks
        if not processing_tasks:
            logger.warning("⛔No hay workers de procesamiento, creando uno nuevo.🔄")
            processing_tasks.append(asyncio.create_task(process_compression_queue()))
            workers_revived += 1
        if workers_revived > 0:
            logger.info(f"✅Watchdog: {workers_revived} worker(s) reiniciado(s)")
    except Exception as e:
        logger.error(f"❌Error en recover_pending_compressions: {e}", exc_info=True)

async def watchdog_loop():
    global last_watchdog_run
    while True:
        try:
            if last_watchdog_run is not None:
                await asyncio.sleep(WATCHDOG_INTERVAL)
            logger.debug("Ejecutando watchdog...")
            await recover_pending_compressions()
            last_watchdog_run = datetime.datetime.now()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌Error en watchdog_loop: {e}", exc_info=True)
            last_watchdog_run = datetime.datetime.now()
            await asyncio.sleep(60)

async def send_log_periodically():
    group_id = -1003896005361
    log_file = "bot.log"
    while True:
        await asyncio.sleep(3600)
        try:
            if not os.path.exists(log_file):
                logger.warning(f"⛔Archivo de log {log_file} no encontrado, omisión.")
                continue
            file_size = os.path.getsize(log_file)
            if file_size == 0:
                logger.info("⛔Archivo de log vacío, no se envía.")
                continue
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"bot_log_{timestamp}.json"
            await app.send_document(chat_id=group_id, document=log_file, file_name=filename, caption=f"📋 **Log del bot (envío periódico)**\n📦 Tamaño: {sizeof_fmt(file_size)}")
            logger.info(f"✅Log periódico enviado a {group_id}")
        except Exception as e:
            logger.error(f"❌Error en envío periódico de log: {e}", exc_info=True)

async def start_workers():
    global processing_tasks
    processing_tasks = []
    for i in range(1):
        task = asyncio.create_task(process_compression_queue())
        processing_tasks.append(task)
    await start_download_worker()
    asyncio.create_task(watchdog_download_worker())
    logger.info(f"✅Iniciados {len(processing_tasks)} workers de compresión y 1 worker de descarga")

async def main():
    await start_workers()
    asyncio.create_task(watchdog_loop())
    asyncio.create_task(send_log_periodically())
    asyncio.create_task(expiry_check_loop())
    asyncio.create_task(update_compression_waiting_messages_loop())  # Actualiza cada 5 min
    await app.start()
    bot_info = await app.get_me()
    logger.info(f"🌐Bot iniciado: @{bot_info.username}🌐")
    await asyncio.Event().wait()

try:
    logger.info("🌐Iniciando el bot🌐...")
    app.run(main())
except Exception as e:
    logger.critical(f"Error fatal al iniciar el bot: {e}", exc_info=True)