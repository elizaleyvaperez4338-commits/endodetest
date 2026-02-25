import os
import logging
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
from pymongo import MongoClient
from config import *
from bson.objectid import ObjectId
import uuid
import zipfile
import io
from bson.json_util import dumps
import psutil

# ======================== WATCHDOG CONFIG ======================== #
WATCHDOG_INTERVAL = 120  # 2 minutos (ajusta según necesidad)

# Variable para registrar la última ejecución del watchdog
last_watchdog_run = None

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Límite de cola para usuarios premium
PREMIUM_QUEUE_LIMIT = 3
ULTRA_QUEUE_LIMIT = 10

# Conexión a MongoDB
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

# Configuración del bot
api_id = API_ID
api_hash = API_HASH
bot_token = BOT_TOKEN

app = Client(
    "compress_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
)

# Administradores del bot
admin_users = ADMINS_IDS
ban_users = []

# Cargar usuarios baneados y limpiar compresiones activas al iniciar
banned_users_in_db = banned_col.find({}, {"user_id": 1})
for banned_user in banned_users_in_db:
    if banned_user["user_id"] not in ban_users:
        ban_users.append(banned_user["user_id"])

# Limpiar compresiones activas previas al iniciar
active_compressions_col.delete_many({})
logger.info("Compresiones activas previas eliminadas")

# Eliminar todos los registros de videos descargados al iniciar el bot
downloaded_videos_col.delete_many({})
logger.info("Videos descargados previos eliminados")

# Configuración de compresión de video (configuración global por defecto)
DEFAULT_VIDEO_SETTINGS = {
    'resolution': '1280x720',
    'crf': '28',
    'audio_bitrate': '64k',
    'fps': '23',
    'preset': 'veryfast',
    'codec': 'libx264'
}

# Variables globales para la cola
compression_queue = asyncio.Queue()
processing_tasks = []
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# Modo mantenimiento
MAINTENANCE_MODE = False

# Diccionarios para gestión de compresiones
cancel_tasks = {}
ffmpeg_processes = {}
active_messages = {}

# Progreso en tiempo real
compression_progress = {}

# Configuraciones temporales durante flujo personalizado
temp_custom_settings = {}

# Valores para personalización
CUSTOM_RESOLUTION_OPTIONS = ['640x360', '854x480', '1280x720']  
CUSTOM_CRF_OPTIONS = ['25', '28', '30', '32', '35', '38', '40']
CUSTOM_FPS_OPTIONS = ['20', '22', '25', '28', '30', '35']
CUSTOM_AUDIO_OPTIONS = ['64k', '70k', '80k', '90k', '128k']

# Sistema de descarga inmediata
MAX_CONCURRENT_DOWNLOADS = 1
current_downloads = 0
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
compression_processing_queue = asyncio.Queue()

# Extensiones de video soportadas
SUPPORTED_VIDEO_EXTENSIONS = ['mp4', 'mkv', 'avi', 'ts', 'mov', 'flv', 'wmv', 'webm', 'm4v', '3gp']

def is_supported_video_file(filename: str) -> bool:
    if not filename:
        return False
    ext = filename.split('.')[-1].lower()
    return ext in SUPPORTED_VIDEO_EXTENSIONS

# ======================== FUNCIONES PARA PERSONALIZACIÓN ======================== #

def get_resolution_keyboard(selected_resolution=None):
    buttons = []
    resolutions = [('640x360', '360'), ('854x480', '480'), ('1280x720', '720')]
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
        logger.info(f"Configuración personalizada aplicada para usuario {user_id}: {settings}")
        return True
    except Exception as e:
        logger.error(f"Error aplicando configuración personalizada: {e}")
        return False

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
        logger.error(f"Error en get_db_command: {e}", exc_info=True)
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
            f"✅ **Base de datos restaurada exitosamente**\n\n"
            f"Se restauraron {len(users_data)} usuarios."
        )
        logger.info(f"Base de datos restaurada por {message.from_user.id} con {len(users_data)} usuarios")
    except json.JSONDecodeError:
        await message.reply("❌ El archivo no es un JSON válido.")
    except Exception as e:
        logger.error(f"Error restaurando base de datos: {e}", exc_info=True)
        await message.reply("❌ Error al restaurar la base de datos.")

# ======================== COMANDO BACKUP ======================== #

@app.on_message(filters.command("backup") & filters.user(admin_users))
async def backup_command(client, message):
    try:
        msg = await message.reply("🔄 **Creando backup de la base de datos...**")
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            collections = [
                "active_compressions",
                "banned_users", 
                "pending_confirmations",
                "pending",
                "temp_keys",
                "user_settings",
                "users",
                "downloaded_videos"
            ]
            total_documents = 0
            for collection_name in collections:
                try:
                    collection = db[collection_name]
                    documents = list(collection.find({}))
                    json_data = dumps(documents, indent=2, default=str)
                    zip_file.writestr(f"{collection_name}.json", json_data)
                    total_documents += len(documents)
                    logger.info(f"Backup: {collection_name} - {len(documents)} documentos")
                except Exception as e:
                    logger.error(f"Error respaldando {collection_name}: {e}")
        zip_buffer.seek(0)
        current_date = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"backup_{current_date}.zip"
        await message.reply_document(
            document=zip_buffer,
            file_name=filename,
            caption=f"✅ **Backup completado**\n\n"
                   f"📊 **Colecciones respaldadas:** {len(collections)}\n"
                   f"📄 **Documentos totales:** {total_documents}\n"
                   f"⏰ **Fecha:** {current_date.replace('_', ' ')}"
        )
        try:
            await msg.delete()
        except:
            pass
        logger.info(f"Backup creado por {message.from_user.id} con {total_documents} documentos")
    except Exception as e:
        logger.error(f"Error en backup_command: {e}", exc_info=True)
        try:
            await msg.edit("❌ **Error al crear el backup**")
        except:
            await message.reply("❌ **Error al crear el backup**")

# ======================== COMANDO SETDAYS ======================== #

async def add_days_to_all_users(days: int, admin_id: int):
    try:
        users = list(users_col.find({
            "plan": {"$in": ["standard", "pro", "premium"]},
            "expires_at": {"$exists": True}
        }))
        total_users = len(users)
        updated_count = 0
        failed_count = 0
        if total_users == 0:
            return 0, 0, "No hay usuarios con planes que expiran para actualizar."
        for user in users:
            try:
                user_id = user["user_id"]
                current_expires = user["expires_at"]
                if isinstance(current_expires, datetime.datetime):
                    new_expires = current_expires + datetime.timedelta(days=days)
                    users_col.update_one(
                        {"user_id": user_id},
                        {"$set": {"expires_at": new_expires}}
                    )
                    updated_count += 1
                    try:
                        await send_protected_message(
                            user_id,
                            f"🎉 **¡Se han agregado {days} día(s) a tu plan!**\n\n"
                            f"¡Disfruta del tiempo adicional! 🎬"
                        )
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Error notificando usuario {user_id}: {e}")
                        failed_count += 1
                else:
                    logger.error(f"Fecha de expiración inválida para usuario {user_id}: {current_expires}")
                    failed_count += 1
            except Exception as e:
                logger.error(f"Error actualizando usuario {user_id}: {e}")
                failed_count += 1
        return updated_count, failed_count, f"Proceso completado: {updated_count} actualizados, {failed_count} fallos."
    except Exception as e:
        logger.error(f"Error en add_days_to_all_users: {e}", exc_info=True)
        return 0, 0, f"Error general: {str(e)}"

@app.on_message(filters.command("setdays") & filters.user(admin_users))
async def setdays_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("⚠️ **Formato:** `/setdays <número_de_días>`\nEjemplo: `/setdays 2`")
            return
        try:
            days = int(parts[1])
            if days <= 0:
                await message.reply("❌ **El número de días debe ser mayor a 0**")
                return
        except ValueError:
            await message.reply("❌ **El valor debe ser un número entero**")
            return
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_setdays_{days}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="cancel_setdays")
            ]
        ])
        await message.reply(
            f"⚠️ **¿Estás seguro de que quieres agregar {days} día(s) a TODOS los usuarios?**\n\n"
            f"• **Días a agregar**: {days}\n"
            f"• **Se notificará** a todos los usuarios afectados\n"
            f"• **Esta acción no se puede deshacer**",
            reply_markup=confirm_keyboard
        )
    except Exception as e:
        logger.error(f"Error en setdays_command: {e}", exc_info=True)
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
            f"**RAM**  : {ram_bar}\n"
            f"          {ram_used}/{ram_total}\n"
            f"**SWAP** : {swap_bar}\n"
            f"**DISK** : {disk_bar}\n"
            f"          {disk_used}/{disk_total}\n\n"
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
        logger.error(f"Error obteniendo estadísticas del sistema: {e}")
        return "❌ **Error al obtener estadísticas del sistema**"

@app.on_message(filters.command("status") & filters.user(admin_users))
async def status_command(client, message):
    try:
        stats = get_status_stats()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_status_stats")]
        ])
        await message.reply(stats, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error en status_command: {e}", exc_info=True)
        await message.reply("❌ **Error al obtener estadísticas del sistema**")
        
@app.on_message(filters.command(["watchdog", "whactdog"]) & filters.user(admin_users))
async def watchdog_status_command(client, message):
    global last_watchdog_run
    if last_watchdog_run is None:
        await message.reply("🕒 El watchdog aún no se ha ejecutado.")
        return
    now = datetime.datetime.now()
    delta = now - last_watchdog_run
    minutes = int(delta.total_seconds() // 60)
    seconds = int(delta.total_seconds() % 60)
    if minutes > 0:
        time_str = f"{minutes} min {seconds} seg"
    else:
        time_str = f"{seconds} seg"
    await message.reply(
        f"🛡️ **Watchdog funcionando correctamente**\n"
        f"📅 Última revisión: hace {time_str}"
    )        

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
        response = (
            f"{status_text}\n\n"
            f"{status_desc}\n\n"
            f"🕐 **Hora del servidor:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if user_id in admin_users:
            try:
                cpu_percent = psutil.cpu_percent(interval=0.5)
                ram = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                stats = (
                    f"\n\n👑 **Vista de administrador:**\n"
                    f"• **CPU:** {cpu_percent:.1f}%\n"
                    f"• **RAM:** {ram.percent:.1f}%\n"
                    f"• **Disco:** {disk.percent:.1f}%\n"
                    f"• **Modo mantenimiento:**\n{'🟢 **ACTIVO**' if maintenance_status else '🔴 **DESACTIVO**'}"
                )
                response += stats
            except Exception as e:
                logger.error(f"Error obteniendo estadísticas: {e}")
        await send_protected_message(message.chat.id, response)
    except Exception as e:
        logger.error(f"Error en estado_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "⚠️ **Error al verificar el estado del bot**"
        )

@app.on_message(filters.command("man_on") & filters.user(admin_users))
async def maintenance_on_command(client, message):
    try:
        if MAINTENANCE_MODE:
            await message.reply("⚠️ **El modo mantenimiento ya está activado.**")
            return
        set_maintenance_mode(True)
        await message.reply(
            "⚙️ **MANTENIMIENTO ACTIVADO** ⚙️\n\n"
            "➥El bot ahora está en modo mantenimiento:\n\n"
            "• Para desactivar, use el comando:\n/man_off"
        )
        logger.info(f"Modo mantenimiento activado por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en maintenance_on_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al activar el modo mantenimiento**")

@app.on_message(filters.command("man_off") & filters.user(admin_users))
async def maintenance_off_command(client, message):
    try:
        if not MAINTENANCE_MODE:
            await message.reply("⚠️ **El modo mantenimiento ya está desactivado.**")
            return
        set_maintenance_mode(False)
        await message.reply(
            "✅ **MANTENIMIENTO DESACTIVADO** ✅\n\n"
            "➥El bot vuelve a estar operativo para todos los usuarios."
        )
        logger.info(f"Modo mantenimiento desactivado por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en maintenance_off_command: {e}", exc_info=True)
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
        logger.error(f"Error eliminando mensaje: {e}")

async def send_auto_delete_message(chat_id, text, delete_after=3, **kwargs):
    msg = await send_protected_message(chat_id, text, **kwargs)
    asyncio.create_task(delete_message_after(msg, delete_after))
    return msg

def get_maintenance_status():
    return MAINTENANCE_MODE

def set_maintenance_mode(status: bool):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = status
    logger.info(f"Modo mantenimiento {'activado' if status else 'desactivado'}")

async def check_maintenance_and_notify(user_id: int, chat_id: int, message_text: str = None):
    if MAINTENANCE_MODE:
        if user_id not in admin_users:
            maintenance_message = (
                "⚙️**Bot en mantenimiento** ⚙️\n\n"
                "➥El bot está actualmente en modo mantenimiento.\n\n"
                "Por favor, espere a que termine el mantenimiento.\nUse /estado para ver el estado del bot"
            )
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
            user_settings_col.update_one(
                {"user_id": user_id},
                {"$set": {"video_settings": new_settings}},
                upsert=True
            )
            logger.info(f"Configuración actualizada para usuario {user_id}: {new_settings}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error actualizando configuración para usuario {user_id}: {e}", exc_info=True)
        return False

async def reset_user_video_settings(user_id: int):
    user_settings_col.delete_one({"user_id": user_id})
    logger.info(f"Configuración restablecida para usuario {user_id}")

async def cleanup_compression_data(compression_id: str):
    try:
        pending_col.delete_one({"compression_id": compression_id})
        downloaded_videos_col.delete_one({"compression_id": compression_id})
        active_compressions_col.delete_one({"compression_id": compression_id})
        logger.info(f"Datos de compresión limpiados para {compression_id}")
        return True
    except Exception as e:
        logger.error(f"Error limpiando datos de compresión {compression_id}: {e}")
        return False

def generate_compression_id():
    return str(uuid.uuid4())

def register_cancelable_task(compression_id, task_type, task, original_message_id=None, progress_message_id=None):
    cancel_tasks[compression_id] = {
        "type": task_type, 
        "task": task, 
        "original_message_id": original_message_id,
        "progress_message_id": progress_message_id
    }

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
                if compression_id in cancel_tasks:
                    unregister_cancelable_task(compression_id)
                    compression_data = active_compressions_col.find_one({"compression_id": compression_id})
                    if compression_data:
                        file_name = compression_data.get("file_name", "")
                        temp_dir = tempfile.gettempdir()
                        for filename in os.listdir(temp_dir):
                            if file_name in filename:
                                try:
                                    file_path = os.path.join(temp_dir, filename)
                                    os.remove(file_path)
                                    logger.info(f"Archivo temporal eliminado durante cancelación: {file_path}")
                                except:
                                    pass
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
            logger.error(f"Error cancelando tarea {compression_id}: {e}")
    return False

def get_user_compression_ids(user_id):
    user_compressions = []
    for compression_id, task_info in cancel_tasks.items():
        compression_data = active_compressions_col.find_one({"compression_id": compression_id})
        if compression_data and compression_data.get("user_id") == user_id:
            user_compressions.append(compression_id)
    return user_compressions

def update_compression_progress(compression_id, stage, current=0, total=0, percent=0, file_name=""):
    compression_progress[compression_id] = {
        "stage": stage,
        "current": current,
        "total": total,
        "percent": percent,
        "file_name": file_name,
        "last_update": time.time()
    }

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
        pending_queue = list(pending_col.find().sort("timestamp", 1))
        active_count = len(active_compr)
        downloaded_count = len(downloaded_videos)
        pending_count = len(pending_queue)
        max_simultaneous = 1
        response = "📊 **Estado de la cola**\n\n"
        response += f"🔄 **Procesos activos:** {active_count}/{max_simultaneous}\n"
        response += f"✅ **Videos descargados:** {downloaded_count}\n"
        downloads_in_progress = []
        if current_downloads > 0:
            for comp_id in list(cancel_tasks):
                task_info = cancel_tasks.get(comp_id)
                if task_info and task_info.get("type") == "download":
                    pending_data = pending_col.find_one({"compression_id": comp_id})
                    if pending_data:
                        if comp_id in compression_progress:
                            progress_data = compression_progress[comp_id]
                            if progress_data.get("percent", 0) > 0:
                                downloads_in_progress.append({
                                    "data": pending_data,
                                    "compression_id": comp_id,
                                    "progress_data": progress_data
                                })
        response += f"\n⬇️**Descargas en curso**: {len(downloads_in_progress)}/{MAX_CONCURRENT_DOWNLOADS}\n"
        if downloads_in_progress:
            for i, download_info in enumerate(downloads_in_progress, 1):
                download = download_info["data"]
                compression_id = download_info["compression_id"]
                user_id_download = download["user_id"]
                file_name = download.get("file_name", "Sin nombre")
                try:
                    user = await app.get_users(user_id_download)
                    username = f"@{user.username}" if user.username else f"Usuario {user_id_download}"
                except:
                    username = f"Usuario {user_id_download}"
                stage_display = "⬇️ **Descarga**"
                progress_bar = "[⬡⬡⬡⬡⬡⬡⬡⬡] 0%"
                if compression_id in compression_progress:
                    progress_data = compression_progress[compression_id]
                    stage = progress_data["stage"]
                    percent = progress_data["percent"]
                    if stage == "download" or stage == "download_starting":
                        stage_display = "⬇️ **Descarga**"
                    elif stage == "compression":
                        stage_display = "🗜️**Compresión**"
                    elif stage == "upload":
                        stage_display = "⬆️Subida"
                    progress_bar = create_mini_progress_bar(percent)
                response += f"{i}. {username} ➧ {progress_bar}\n[{stage_display}]\n"
        else:
            response += "**• Ninguna**\n"
        response += f"\n🗜️**Compresiones activas**: {len(active_compr)}/{max_simultaneous}\n"
        if active_compr:
            for i, comp in enumerate(active_compr, 1):
                compression_id = comp.get("compression_id")
                comp_user_id = comp.get("user_id")
                file_name = comp.get("file_name", "Sin nombre")
                try:
                    user = await app.get_users(comp_user_id)
                    username = f"@{user.username}" if user.username else f"Usuario {comp_user_id}"
                except:
                    username = f"Usuario {comp_user_id}"
                stage_display = "🗜️**Compresión**"
                progress_bar = "[⬡⬡⬡⬡⬡⬡⬡⬡] 0%"
                if compression_id in compression_progress:
                    progress_data = compression_progress[compression_id]
                    stage = progress_data["stage"]
                    percent = progress_data["percent"]
                    if stage == "download" or stage == "download_starting":
                        stage_display = "⬇️ Descarga"
                    elif stage == "compression":
                        stage_display = "🗜️**Compresión**"
                    elif stage == "upload":
                        stage_display = "⬆️Subida"
                    progress_bar = create_mini_progress_bar(percent)
                response += f"{i}. {username} ➧ {progress_bar}\n[{stage_display}]\n"
        else:
            response += "**• Ninguno**\n"
        response += f"\n📥 **Videos descargados esperando compresión:**\n"
        if downloaded_videos:
            user_video_counts = {}
            for video in downloaded_videos:
                video_user_id = video.get("user_id")
                if video_user_id in user_video_counts:
                    user_video_counts[video_user_id] += 1
                else:
                    user_video_counts[video_user_id] = 1
            for i, (video_user_id, count) in enumerate(user_video_counts.items(), 1):
                try:
                    user = await app.get_users(video_user_id)
                    username = f"@{user.username}" if user.username else f"Usuario {video_user_id}"
                except:
                    username = f"Usuario {video_user_id}"
                if count > 1:
                    response += f"{i}. {username} ({count} videos)\n"
                else:
                    response += f"{i}. {username}\n"
        else:
            response += "**• Ninguno**\n"
        unique_active_users = len(set(comp["user_id"] for comp in active_compr))
        unique_downloaded_users = len(set(video["user_id"] for video in downloaded_videos))
        response += f"\n📈 **Resumen total**:\n"
        response += f"   • Comprimiendo: {unique_active_users} usuario{'s' if unique_active_users != 1 else ''}\n"
        response += f"   • Descargados: {unique_downloaded_users} usuario{'s' if unique_downloaded_users != 1 else ''}\n"
        if user_id in admin_users:
            response += f"\n👑 **Vista de administrador:**\n"
            response += f"• Descargas simultáneas: {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
            response += f"• Videos descargados en cola: {downloaded_count}\n"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_queue"),
                InlineKeyboardButton("❌ Cerrar", callback_data="close_queue")
            ]
        ])
        return response, keyboard
    except Exception as e:
        logger.error(f"Error en get_queue_status: {e}")
        return "❌ **Error al obtener el estado de la cola**", None

def cancellation_checker():
    while True:
        try:
            for compression_id in list(cancel_tasks.keys()):
                task_info = cancel_tasks[compression_id]
                if task_info["type"] == "ffmpeg" and compression_id in ffmpeg_processes:
                    process = ffmpeg_processes[compression_id]
                    if process.poll() is not None:
                        unregister_cancelable_task(compression_id)
                        unregister_ffmpeg_process(compression_id)
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Error en cancellation_checker: {e}")
            time.sleep(1)

cancellation_thread = threading.Thread(target=cancellation_checker, daemon=True)
cancellation_thread.start()

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client, message):
    user_id = message.from_user.id
    user_compression_ids = get_user_compression_ids(user_id)
    if user_compression_ids:
        canceled_count = 0
        for compression_id in user_compression_ids:
            if cancel_compression_task(compression_id):
                task_info = cancel_tasks.get(compression_id, {})
                original_message_id = task_info.get("original_message_id")
                progress_message_id = task_info.get("progress_message_id")
                if progress_message_id:
                    try:
                        await app.delete_messages(message.chat.id, progress_message_id)
                        if compression_id in active_messages:
                            del active_messages[compression_id]
                    except Exception as e:
                        logger.error(f"Error eliminando mensaje de progreso: {e}")
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                await cleanup_compression_data(compression_id)
                remove_compression_progress(compression_id)
                canceled_count += 1
        if canceled_count > 0:
            await send_protected_message(
                message.chat.id,
                f"⛔ **{canceled_count} compresión(es) cancelada(s)** ⛔"
            )
        else:
            await send_protected_message(
                message.chat.id,
                "⚠️ **No se pudieron cancelar las operaciones activas**"
            )
    else:
        result = pending_col.delete_many({"user_id": user_id})
        downloaded_result = downloaded_videos_col.delete_many({"user_id": user_id})
        total_canceled = result.deleted_count + downloaded_result.deleted_count
        if total_canceled > 0:
            await send_protected_message(
                message.chat.id,
                f"⛔ **Se cancelaron {total_canceled} tareas pendientes en la cola.** ⛔"
            )
        else:
            await send_protected_message(
                message.chat.id,
                "ℹ️ **No tienes operaciones activas ni en cola para cancelar.**"
            )
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Error borrando mensaje /cancel: {e}")

@app.on_message(filters.command("cancelqueue") & filters.private)
async def cancel_queue_command(client, message):
    try:
        user_id = message.from_user.id
        if user_id in ban_users:
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso para usar este bot.**\n\n"
                "💲 Para ver los planes disponibles usa el comando /planes\n\n"
                "👨🏻‍💻 Para más información, contacte a @VirtualMix_Shop."
            )
            return
        user_queue = list(pending_col.find({"user_id": user_id}).sort("timestamp", 1))
        if not user_queue:
            await send_protected_message(
                message.chat.id,
                "📋**No tienes videos en la cola de compresión.**"
            )
            return
        parts = message.text.split()
        if len(parts) == 1:
            response = "**Tus videos en cola:**\n\n"
            for i, item in enumerate(user_queue, 1):
                file_name = item.get("file_name", "Sin nombre")
                timestamp = item.get("timestamp")
                time_str = timestamp.strftime("%H:%M:%S") if timestamp else "¿?"
                response += f"{i}. `{file_name}` (⏰ {time_str})\n"
            response += "\nPara cancelar un video, usa:\n/cancelqueue+num <num>\n"
            response += "Para cancelar todos, usa:\n/cancelqueue_all"
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
                logger.error(f"Error eliminando mensajes de espera: {e}")
            await send_protected_message(
                message.chat.id,
                f"✅ **Se cancelaron todos los videos de tu cola**\n"
                f"• Videos eliminados de cola: {result.deleted_count}\n"
                f"• Videos descargados eliminados: {downloaded_result.deleted_count}"
            )
            return
        try:
            index = int(parts[1]) - 1
            if index < 0 or index >= len(user_queue):
                await send_protected_message(
                    message.chat.id,
                    f"❌ **Número inválido.** Debe ser entre 1 y {len(user_queue)}"
                )
                return
            video_to_cancel = user_queue[index]
            compression_id = video_to_cancel.get("compression_id")
            wait_message_id = video_to_cancel.get("wait_message_id")
            await cleanup_compression_data(compression_id)
            try:
                if wait_message_id:
                    await app.delete_messages(chat_id=message.chat.id, message_ids=[wait_message_id])
            except Exception as e:
                logger.error(f"Error eliminando mensaje de espera: {e}")
            await send_protected_message(
                message.chat.id,
                f"**Video cancelado:** `{video_to_cancel.get('file_name', 'Sin nombre')}`\n\n"
                f"✅ Eliminado de la cola de compresión."
            )
        except ValueError:
            await send_protected_message(
                message.chat.id,
                "**Usa** /cancelqueue para ver la lista de la cola **o** /cancelqueue_all para eliminar todos los vídeos de la cola"
            )
    except Exception as e:
        logger.error(f"Error en cancel_queue_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "**Error al procesar la solicitud.**"
        )

async def has_active_compression(user_id: int) -> bool:
    return bool(active_compressions_col.find_one({"user_id": user_id}))

async def add_active_compression(compression_id: str, user_id: int, file_id: str, file_name: str):
    active_compressions_col.insert_one({
        "compression_id": compression_id,
        "user_id": user_id,
        "file_id": file_id,
        "file_name": file_name,
        "start_time": datetime.datetime.now()
    })

async def remove_active_compression(compression_id: str):
    active_compressions_col.delete_one({"compression_id": compression_id})

async def get_active_compressions_count(user_id: int) -> int:
    return active_compressions_col.count_documents({"user_id": user_id})

async def add_downloaded_video(user_id: int, file_path: str, file_name: str, compression_id: str):
    downloaded_videos_col.insert_one({
        "user_id": user_id,
        "file_path": file_path,
        "file_name": file_name,
        "compression_id": compression_id,
        "timestamp": datetime.datetime.now()
    })

async def remove_downloaded_video(compression_id: str):
    downloaded_videos_col.delete_one({"compression_id": compression_id})

async def get_next_downloaded_video():
    video = downloaded_videos_col.find_one().sort("timestamp", 1)
    return video

async def has_downloaded_videos(user_id: int) -> bool:
    return bool(downloaded_videos_col.find_one({"user_id": user_id}))

async def get_user_downloaded_count(user_id: int) -> int:
    return downloaded_videos_col.count_documents({"user_id": user_id})

async def has_pending_confirmation(user_id: int) -> bool:
    now = datetime.datetime.now()
    expiration_time = now - datetime.timedelta(minutes=10)
    pending_confirmations_col.delete_many({
        "user_id": user_id,
        "timestamp": {"$lt": expiration_time}
    })
    return bool(pending_confirmations_col.find_one({"user_id": user_id}))

async def create_confirmation(user_id: int, chat_id: int, message_id: int, file_id: str, file_name: str):
    pending_confirmations_col.delete_many({"user_id": user_id})
    return pending_confirmations_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_id": file_id,
        "file_name": file_name,
        "timestamp": datetime.datetime.now()
    }).inserted_id

async def delete_confirmation(confirmation_id: ObjectId):
    pending_confirmations_col.delete_one({"_id": confirmation_id})

async def get_confirmation(confirmation_id: ObjectId):
    return pending_confirmations_col.find_one({"_id": confirmation_id})

async def register_new_user(user_id: int):
    if not users_col.find_one({"user_id": user_id}):
        logger.info(f"Usuario no registrado: {user_id}")

async def should_protect_content(user_id: int) -> bool:
    if user_id in admin_users:
        return False
    user_plan = await get_user_plan(user_id)
    return user_plan is None or user_plan["plan"] == "standard"

async def send_protected_message(chat_id: int, text: str, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_message(chat_id, text, protect_content=protect, **kwargs)

async def send_protected_video(chat_id: int, video: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_video(chat_id, video, caption=caption, protect_content=protect, **kwargs)

async def send_protected_photo(chat_id: int, photo: str, caption: str = None, **kwargs):
    protect = await should_protect_content(chat_id)
    return await app.send_photo(chat_id, photo, caption=caption, protect_content=protect, **kwargs)

async def get_user_queue_limit(user_id: int) -> int:
    user_plan = await get_user_plan(user_id)
    if user_plan is None:
        return 1
    if user_plan["plan"] == "ultra":
        return ULTRA_QUEUE_LIMIT
    return PREMIUM_QUEUE_LIMIT if user_plan["plan"] == "premium" else 1

def generate_temp_key(plan: str, duration_value: int, duration_unit: str):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    created_at = datetime.datetime.now()
    if duration_unit == 'minutes':
        expires_at = created_at + datetime.timedelta(minutes=duration_value)
    elif duration_unit == 'hours':
        expires_at = created_at + datetime.timedelta(hours=duration_value)
    else:
        expires_at = created_at + datetime.timedelta(days=duration_value)
    temp_keys_col.insert_one({
        "key": key,
        "plan": plan,
        "created_at": created_at,
        "expires_at": expires_at,
        "used": False,
        "duration_value": duration_value,
        "duration_unit": duration_unit
    })
    return key

def is_valid_temp_key(key):
    now = datetime.datetime.now()
    key_data = temp_keys_col.find_one({
        "key": key,
        "used": False,
        "expires_at": {"$gt": now}
    })
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
        try:
            duration_value = int(parts[2])
            if duration_value <= 0:
                await message.reply("⚠️ La cantidad debe ser un número positivo")
                return
        except ValueError:
            await message.reply("⚠️ La cantidad debe ser un número entero")
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
        await message.reply(
            f"**Clave {plan.capitalize()} generada**\n\n"
            f"Clave: `{key}`\n"
            f"Válida por: {duration_text}\n\n"
            f"Comparte esta clave con el usuario usando:\n"
            f"`/key {key}`"
        )
    except Exception as e:
        logger.error(f"Error generando clave: {e}", exc_info=True)
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
            response += (
                f"• `{key['key']}`\n"
                f"  ↳ Plan: {key['plan'].capitalize()}\n"
                f"  ↳ Duración: {duration_display}\n"
                f"  ⏱ Expira en: {time_remaining}\n\n"
            )
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error listando claves: {e}", exc_info=True)
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
        logger.error(f"Error eliminando claves: {e}", exc_info=True)
        await message.reply("⚠️ **Error al eliminar claves**")

PLAN_DURATIONS = {
    "standard": "7 días",
    "pro": "15 días",
    "premium": "30 días",
    "ultra": "Ilimitado"  
}

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
    user_data = {
        "plan": plan
    }
    if expires_at is not None:
        user_data["expires_at"] = expires_at
    existing_user = users_col.find_one({"user_id": user_id})
    if not existing_user:
        user_data["join_date"] = datetime.datetime.now()
    users_col.update_one(
        {"user_id": user_id},
        {"$set": user_data},
        upsert=True
    )
    if notify:
        try:
            await send_protected_message(
                user_id,
                f"**¡Se te ha asignado un nuevo plan!**\n"
                f"Use el comando /start para iniciar en el bot\n\n"
                f"• **Plan**: {plan.capitalize()}\n"
                f"• **Duración**: {PLAN_DURATIONS[plan]}\n"
                f"• **Videos disponibles**: Ilimitados\n\n"
                f"¡Disfruta de tus beneficios! 🎬"
            )
        except Exception as e:
            logger.error(f"Error notificando al usuario {user_id}: {e}")
    return True

async def check_user_limit(user_id: int) -> bool:
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return True
    return False

async def get_plan_info(user_id: int):
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return (
            "**No tienes un plan activo.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
            None
        )
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
            elif minutes > 0:
                expires_text = f"{minutes}m {seconds}s"
            else:
                expires_text = f"{seconds}s"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_plan"),
            InlineKeyboardButton("❌ Cerrar", callback_data="close_plan")
        ]
    ])
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠➣ **Plan actual**: {plan_name}\n"
        f"┠➣ **Tiempo restante**:\n"
        f"┠➣ {expires_text}\n"
        f"╰✠━━━━━━━━━━━━━━━━━━✠╯",
        keyboard
    )

async def has_pending_in_queue(user_id: int) -> bool:
    count = pending_col.count_documents({"user_id": user_id})
    return count > 0

def create_progress_bar(current, total, proceso, length=15):
    if total == 0:
        total = 1
    percent = (current / total) * 100
    filled = int(length * (current / total))
    bar = '⬢' * filled + '⬡' * (length - filled)
    return (
        f'    ╭━━━[🤖**Compress Bot**]━━━╮\n'
        f'┠ [{bar}] {percent:.1f}%\n'
        f'┠ **Procesado**: {sizeof_fmt(current)}/{sizeof_fmt(total)}\n'
        f'┠ **Estado**: __#{proceso}__'
    )

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
        if isinstance(compression_key, str):
            if compression_key.endswith("_upload"):
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
                    f"╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                    f"┠⬇️ **Preparando descarga...** ⬇️\n"
                    f"┠📊 **Slot asignado:** {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
                    f"┠⏱ **Por favor espere...**\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
                    ])
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
        update_compression_progress(
            compression_id,
            stage_for_update,
            current,
            total,
            percentage * 100,
            file_name
        )
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
        ])
        try:
            await msg.edit(
                f"   {progress_bar}\n"
                f"┠ **Velocidad** {sizeof_fmt(speed)}/s\n"
                f"┠ **Tiempo transcurrido:** {elapsed_str}\n"
                f"┠ **Tiempo restante:** {remaining_str}\n"
                f"╰━━━━━━━━━━━━━━━━━━╯\n",
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editando mensaje de progreso: {e}")
            if compression_key in active_messages:
                del active_messages[compression_key]
    except asyncio.CancelledError as e:
        raise e
    except Exception as e:
        logger.error(f"Error en progress_callback: {e}", exc_info=True)

async def download_file_immediately(file_obj, file_name, compression_id, wait_msg, user_id, chat_id, original_message_id):
    try:
        register_cancelable_task(compression_id, "download", None, 
                                original_message_id=original_message_id, 
                                progress_message_id=wait_msg.id)
        update_compression_progress(compression_id, "download_starting", 0, 100, 0, file_name)
        queue_info = get_download_queue_info()
        queue_position = queue_info['waiting_count'] + 1
        waiting_text = (
            "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
            "┠⏳ **Preparando descarga...** ⏳\n"
            f"┠📊 **Posición en cola:** #{queue_position}\n"
            f"┠📈 **Slot disponibles:** {queue_info['current_downloads']}/{queue_info['max_downloads']}\n"
            f"┠⏱ **Esperando slot disponible...**\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯"
        )
        await wait_msg.edit_text(
            waiting_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
            ])
        )
        active_messages[compression_id] = wait_msg.id
        async with download_semaphore:
            global current_downloads
            current_downloads += 1
            update_compression_progress(compression_id, "download_starting", 0, 100, 0, file_name)
            starting_text = (
                "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                f"┠⬇️ **Iniciando descarga...** ⬇️\n"
                f"┠📊 **Slot asignado:** {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
                "┠⏱ **Por favor espere...**\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯"
            )
            await wait_msg.edit_text(
                starting_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
                ])
            )
            await asyncio.sleep(1)
            try:
                start_download_time = time.time()
                original_video_path = await app.download_media(
                    file_obj,
                    progress=progress_callback,
                    progress_args=(wait_msg, "DESCARGA", start_download_time)
                )
                if compression_id not in cancel_tasks:
                    if original_video_path and os.path.exists(original_video_path):
                        os.remove(original_video_path)
                    raise asyncio.CancelledError("Descarga cancelada")
                logger.info(f"Video descargado inmediatamente: {original_video_path}")
                await add_downloaded_video(user_id, original_video_path, file_name, compression_id)
                pending_col.delete_one({"compression_id": compression_id})
                downloaded_count = downloaded_videos_col.count_documents({})
                completion_text = (
                    "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                    "┠✅ **Video descargado** ✅\n"
                    f"┠📁 **Archivo:** `{file_name}`\n"
                    f"┠📊 **Posición en cola:** {downloaded_count}\n"
                    "┠🔄 **Agregado a la cola de compresión**\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━╯"
                )
                await wait_msg.edit_text(
                    completion_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
                    ])
                )
                await compression_processing_queue.put({
                    "compression_id": compression_id,
                    "user_id": user_id,
                    "original_video_path": original_video_path,
                    "file_name": file_name,
                    "chat_id": chat_id,
                    "original_message_id": original_message_id,
                    "wait_msg_id": wait_msg.id,
                    "wait_msg": wait_msg
                })
                return True
            except asyncio.CancelledError:
                logger.info(f"Descarga cancelada para compresión {compression_id}")
                temp_dir = tempfile.gettempdir()
                for filename in os.listdir(temp_dir):
                    if file_name in filename:
                        try:
                            temp_file = os.path.join(temp_dir, filename)
                            os.remove(temp_file)
                            logger.info(f"Archivo temporal eliminado: {temp_file}")
                        except:
                            pass
                cancel_text = (
                    "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                    "┠⛔ **Descarga cancelada** ⛔\n"
                    f"┠📁 **Archivo:** `{file_name}`\n"
                    "┠❌ **Operación interrumpida**\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━╯"
                )
                await wait_msg.edit_text(cancel_text)
                return False
            except Exception as e:
                logger.error(f"Error en descarga inmediata: {e}", exc_info=True)
                error_text = (
                    "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                    "┠❌ **Error en la descarga** ❌\n"
                    f"┠📁 **Archivo:** `{file_name}`\n"
                    f"┠⚠️ **Error:** {str(e)[:100]}\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━╯"
                )
                await wait_msg.edit_text(error_text)
                return False
            finally:
                current_downloads -= 1
                if compression_id in active_messages:
                    del active_messages[compression_id]
    except Exception as e:
        logger.error(f"Error en download_file_immediately: {e}", exc_info=True)
        return False

def get_download_queue_info():
    return {
        "current_downloads": current_downloads,
        "max_downloads": MAX_CONCURRENT_DOWNLOADS,
        "waiting_count": pending_col.count_documents({}) + downloaded_videos_col.count_documents({})
    }

async def show_waiting_message(wait_msg, file_name, compression_id, current_position):
    waiting_text = (
        "╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
        "┠⏳ **En cola de espera** ⏳\n"
        f"┠📁 **Archivo:** `{file_name}`\n"
        f"┠📊 **Posición en cola:** #{current_position}\n"
        f"┠📈 **Descargas activas:** {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
        "┠⏱ **Esperando slot disponible...**\n"
        "╰━━━━━━━━━━━━━━━━━━━━━╯"
    )
    await wait_msg.edit_text(
        waiting_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
        ])
    )

async def process_compression_queue():
    while True:
        try:
            task = await compression_processing_queue.get()
            video_data = downloaded_videos_col.find_one({"compression_id": task["compression_id"]})
            if not video_data:
                logger.info(f"Video cancelado, saltando: {task['file_name']}")
                compression_processing_queue.task_done()
                continue
            start_msg = await task["wait_msg"].edit("🗜️**Iniciando compresión**🎬")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor, 
                threading_compress_video_from_path, 
                task, 
                start_msg
            )
        except Exception as e:
            logger.error(f"Error procesando video de la cola: {e}", exc_info=True)
            try:
                await app.send_message(
                    task["chat_id"], 
                    f"⚠️ Error al procesar el video: {str(e)}"
                )
            except:
                pass
        finally:
            compression_processing_queue.task_done()

def threading_compress_video_from_path(task, start_msg):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(compress_video_from_path(task, start_msg))
    loop.close()

async def compress_video_from_path(task, start_msg):
    try:
        compression_id = task["compression_id"]
        user_id = task["user_id"]
        original_video_path = task["original_video_path"]
        file_name = task["file_name"]
        chat_id = task["chat_id"]
        original_message_id = task["original_message_id"]
        wait_msg = task["wait_msg"]
        if not os.path.exists(original_video_path):
            await wait_msg.edit_text("❌ **Error: Archivo no encontrado**")
            await remove_downloaded_video(compression_id)
            return
        user_video_settings = await get_user_video_settings(user_id)
        await add_active_compression(compression_id, user_id, None, file_name)
        progress_bar = create_progress_bar(0, 100, "COMPRESIÓN")
        msg = await app.send_message(
            chat_id=chat_id,
            text=f"   {progress_bar}\n┠ **Velocidad** 0.00B/s\n┠ **Tiempo transcurrido:** 00:00\n┠ **Tiempo restante:** 00:00\n╰━━━━━━━━━━━━━━━━━━╯\n",
            reply_to_message_id=original_message_id
        )
        active_messages[compression_id] = msg.id
        try:
            if start_msg:
                await start_msg.delete()
        except Exception:
            pass
        original_size = os.path.getsize(original_video_path)
        logger.info(f"Tamaño original: {original_size} bytes")
        await notify_group(app, await app.get_messages(chat_id, original_message_id), original_size, status="start")
        try:
            probe = ffmpeg.probe(original_video_path)
            dur_total = float(probe['format']['duration'])
            logger.info(f"Duración del video: {dur_total} segundos")
        except Exception as e:
            logger.error(f"Error obteniendo duración: {e}", exc_info=True)
            dur_total = 0
        compressed_video_path = f"{os.path.splitext(original_video_path)[0]}_compressed.mp4"
        logger.info(f"Ruta de compresión: {compressed_video_path}")
        drawtext_filter = f"drawtext=text='@compressbot_oficial_bot':x=w-tw-10:y=10:fontsize=20:fontcolor=white"
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
        logger.info(f"Comando FFmpeg: {' '.join(ffmpeg_command)}")
        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True, bufsize=1)
            register_cancelable_task(compression_id, "ffmpeg", process, 
                                    original_message_id=original_message_id, 
                                    progress_message_id=msg.id)
            register_ffmpeg_process(compression_id, process)
            update_compression_progress(compression_id, "compression", 0, 100, 0, file_name)
            last_percent = 0
            last_update_time = 0
            time_pattern = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
            while True:
                if compression_id not in cancel_tasks:
                    process.kill()
                    if compression_id in active_messages:
                        del active_messages[compression_id]
                    try:
                        await msg.delete()
                        await wait_msg.delete()
                    except:
                        pass
                    await send_auto_delete_message(
                        chat_id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    await cleanup_compression_data(compression_id)
                    unregister_cancelable_task(compression_id)
                    unregister_ffmpeg_process(compression_id)
                    remove_compression_progress(compression_id)
                    return
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
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
                        if percent > 0:
                            remaining_seconds = (elapsed_seconds / percent) * (100 - percent)
                        else:
                            remaining_seconds = 0
                        elapsed_str = format_time(elapsed_seconds)
                        remaining_str = format_time(remaining_seconds)
                        update_compression_progress(compression_id, "compression", current_time, dur_total, percent, file_name)
                        if percent - last_percent >= 5 or time.time() - last_update_time >= 5:
                            bar = create_compression_bar(percent)
                            cancel_button = InlineKeyboardMarkup([[
                                InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")
                            ]])
                            try:
                                await msg.edit(
                                    f"╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                                    f"┠🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
                                    f"┠**Progreso**: {bar}\n"
                                    f"┠**Tamaño**: {sizeof_fmt(compressed_size)}\n"
                                    f"┠**Tiempo transcurrido**: {elapsed_str}\n"
                                    f"┠**Tiempo restante**: {remaining_str}\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                                    reply_markup=cancel_button
                                )
                            except MessageNotModified:
                                pass
                            except Exception as e:
                                logger.error(f"Error editando mensaje de progreso: {e}")
                                if compression_id in active_messages:
                                    del active_messages[compression_id]
                            last_percent = percent
                            last_update_time = time.time()
            if compression_id not in cancel_tasks:
                if compressed_video_path and os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                await cleanup_compression_data(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)
                try:
                    await wait_msg.delete()
                    await msg.delete()
                except:
                    pass
                await send_auto_delete_message(
                    chat_id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
                return
            compressed_size = os.path.getsize(compressed_video_path)
            logger.info(f"Compresión completada. Tamaño comprimido: {compressed_size} bytes")
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
                logger.info(f"Duración del video comprimido: {duration} segundos")
            except Exception as e:
                logger.error(f"Error obteniendo duración comprimido: {e}", exc_info=True)
                duration = 0
            thumbnail_path = f"{compressed_video_path}_thumb.jpg"
            try:
                (
                    ffmpeg
                    .input(compressed_video_path, ss=duration//2 if duration > 0 else 0)
                    .filter('scale', 320, -1)
                    .output(thumbnail_path, vframes=1)
                    .overwrite_output()
                    .run(capture_stdout=True, capture_stderr=True)
                )
                logger.info(f"Miniatura generada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"Error generando miniatura: {e}", exc_info=True)
                thumbnail_path = None
            processing_time = datetime.datetime.now() - start_time
            processing_time_str = str(processing_time).split('.')[0]
            description = (
                "╭✠━━━━━━━━━━━━━━━━━━━━✠╮\n"
                f"┠➣🗜️**Vídeo comprimído**🎬\n┠➣**Tiempo transcurrido**: {processing_time_str}\n╰✠━━━━━━━━━━━━━━━━━━━━✠╯\n"
            )
            try:
                start_upload_time = time.time()
                register_cancelable_task(compression_id, "upload", None, 
                                        original_message_id=original_message_id, 
                                        progress_message_id=msg.id)
                update_compression_progress(compression_id, "upload", 0, 100, 0, file_name)
                if compression_id not in cancel_tasks:
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                    await cleanup_compression_data(compression_id)
                    unregister_cancelable_task(compression_id)
                    unregister_ffmpeg_process(compression_id)
                    remove_compression_progress(compression_id)
                    try:
                        await wait_msg.delete()
                        await msg.delete()
                    except:
                        pass
                    await send_auto_delete_message(
                        chat_id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    return
                if thumbnail_path and os.path.exists(thumbnail_path):
                    await send_protected_video(
                        chat_id=chat_id,
                        video=compressed_video_path,
                        caption=description,
                        thumb=thumbnail_path,
                        duration=duration,
                        reply_to_message_id=original_message_id,
                        progress=progress_callback,
                        progress_args=(msg, "SUBIDA", start_upload_time)
                    )
                else:
                    await send_protected_video(
                        chat_id=chat_id,
                        video=compressed_video_path,
                        caption=description,
                        duration=duration,
                        reply_to_message_id=original_message_id,
                        progress=progress_callback,
                        progress_args=(msg, "SUBIDA", start_upload_time)
                    )
                logger.info("✅ Video comprimido enviado")
                await notify_group(app, await app.get_messages(chat_id, original_message_id), original_size, compressed_size=compressed_size, status="done")
                users_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"compressed_videos": 1}},
                    upsert=True
                )
                try:
                    await wait_msg.delete()
                    logger.info("Mensaje de espera eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de espera: {e}")
                try:
                    await msg.delete()
                    logger.info("Mensaje de progreso eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de progreso: {e}")
            except Exception as e:
                logger.error(f"Error enviando video: {e}", exc_info=True)
                await app.send_message(chat_id=chat_id, text="⚠️ **Error al enviar el video comprimido**")
        except Exception as e:
            logger.error(f"Error en compresión: {e}", exc_info=True)
            await msg.delete()
            await app.send_message(chat_id=chat_id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            try:
                await cleanup_compression_data(compression_id)
                if compression_id in active_messages:
                    del active_messages[compression_id]
                for file_path in [original_video_path, compressed_video_path]:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Archivo temporal eliminado: {file_path}")
                if 'thumbnail_path' in locals() and thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                    logger.info(f"Miniatura eliminada: {thumbnail_path}")
                remove_compression_progress(compression_id)
            except Exception as e:
                logger.error(f"Error eliminando archivos temporales: {e}", exc_info=True)
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
        logger.error(f"Error creando barra de progreso: {e}", exc_info=True)
        return f"**Progreso**: {int(percent)}%"

@app.on_message(filters.command(["deleteall"]) & filters.user(admin_users))
async def delete_all_pending(client, message):
    result = pending_col.delete_many({})
    downloaded_result = downloaded_videos_col.delete_many({})
    await message.reply(
        f"**🗑️Cola eliminada.**\n"
        f"**➥Se eliminaron {result.deleted_count} elementos de la cola.**\n"
        f"**➥Se eliminaron {downloaded_result.deleted_count} videos descargados.**"
    )

@app.on_message(filters.regex(r"^/del_(\d+)$") & filters.user(admin_users))
async def delete_one_from_pending(client, message):
    match = message.text.strip().split("_")
    if len(match) != 2 or not match[1].isdigit():
        await message.reply("⚠️ Formato inválido. Usa `/del_1`, `/del_2`, etc.")
        return
    index = int(match[1]) - 1
    cola = list(pending_col.find().sort([("timestamp", 1)]))
    if index < 0 or index >= len(cola):
        await message.reply("⚠️ Número fuera de rango.")
        return
    eliminado = cola[index]
    pending_col.delete_one({"_id": eliminado["_id"]})
    file_name = eliminado.get("file_name", "¿?")
    user_id = eliminado["user_id"]
    tiempo = eliminado.get("timestamp")
    tiempo_str = tiempo.strftime("%Y-%m-d %H:%M:%S") if tiempo else "¿?"
    await message.reply(
        f"✅ Eliminado de la cola:\n"
        f"📁 {file_name}\n👤 ID: `{user_id}`\n⏰ {tiempo_str}"
    )

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
            task = {
                "compression_id": compression_id,
                "user_id": user_id,
                "original_video_path": file_path,
                "file_name": file_name,
                "chat_id": video.get("chat_id", user_id),
                "original_message_id": video.get("original_message_id", 0),
                "wait_msg_id": video.get("wait_msg_id", 0),
                "wait_msg": await app.send_message(user_id, f"🔄 Recuperando video descargado: {file_name}")
            }
            await compression_processing_queue.put(task)
        except Exception as e:
            logger.error(f"Error cargando video descargado: {e}")
    if not processing_tasks or all(task.done() for task in processing_tasks):
        processing_tasks = []
        for i in range(1):
            task = asyncio.create_task(process_compression_queue())
            processing_tasks.append(task)
        await msg.edit("✅ Procesamiento de cola iniciado con 1 worker")
    else:
        await msg.edit("✅ Los workers de procesamiento ya están activos.")

# ======================== INTERFAZ DE USUARIO ======================== #

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Settings"), KeyboardButton("📋 Planes")],
            [KeyboardButton("📊 Mi Plan"), KeyboardButton("ℹ️ Ayuda")],
            [KeyboardButton("👀 Ver Cola"), KeyboardButton("🗑️ Cancelar Cola")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
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
    await send_protected_message(
        message.chat.id, 
        "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️", 
        reply_markup=keyboard
    )

def get_plan_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 Estándar", callback_data="plan_standard")],
        [InlineKeyboardButton("💎 Pro", callback_data="plan_pro")],
        [InlineKeyboardButton("👑 Premium", callback_data="plan_premium")]
    ])

async def get_plan_menu(user_id: int):
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return (
            "**No tienes un plan activo.**\n\n"
            "Adquiere un plan para usar el bot.\n\n"
            "📋 **Selecciona un plan para más información:**"
        ), get_plan_menu_keyboard()
    plan_name = user["plan"].capitalize()
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠➣ **Tu plan actual**: {plan_name}\n"
        f"╰✠━━━━━━━━━━━━━━━━━━━━━━✠╯\n\n"
        "📋 **Selecciona un plan para más información:**"
    ), get_plan_menu_keyboard()

@app.on_message(filters.command("planes") & filters.private)
async def planes_command(client, message):
    try:
        texto, keyboard = await get_plan_menu(message.from_user.id)
        await send_protected_message(
            message.chat.id, 
            texto, 
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error en planes_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ Error al mostrar los planes"
        )

@app.on_message(filters.command("convert") & filters.private & filters.reply)
async def convert_command(client, message: Message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id):
            return
        if user_id in ban_users:
            logger.warning(f"Intento de uso por usuario baneado: {user_id}")
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_video")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n"
                "**Adquiere un plan para usar el bot.**\n\n",
                reply_markup=keyboard
            )
            return
        replied = message.reply_to_message
        if not replied or not replied.document:
            await send_protected_message(
                message.chat.id,
                "❌ **Debes responder a un documento que sea un vídeo.**"
            )
            return
        doc = replied.document
        file_name = doc.file_name or "video_sin_nombre"
        if not is_supported_video_file(file_name):
            await send_protected_message(
                message.chat.id,
                f"❌ **Formato no soportado.**\n"
                f"Extensiones válidas: {', '.join(SUPPORTED_VIDEO_EXTENSIONS)}"
            )
            return
        if await has_pending_confirmation(user_id):
            logger.info(f"Usuario {user_id} tiene confirmación pendiente, ignorando documento adicional")
            return
        if await check_user_limit(user_id):
            await send_protected_message(
                message.chat.id,
                f"⚠️ **Límite alcanzado**\n"
                f"Tu plan ha expirado.\n\n"
                "👨🏻‍💻**Contacta con @VirtualMix_Shop para renovar tu Plan**"
            )
            return
        queue_limit = await get_user_queue_limit(user_id)
        pending_count = pending_col.count_documents({"user_id": user_id})
        downloaded_count = await get_user_downloaded_count(user_id)
        total_pending = pending_count + downloaded_count
        if total_pending >= queue_limit:
            await send_protected_message(
                message.chat.id,
                f"Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\n"
                "Por favor espera a que se procesen antes de enviar más."
            )
            return
        confirmation_id = await create_confirmation(
            user_id,
            replied.chat.id,
            replied.id,
            doc.file_id,
            file_name
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Confirmar compresión 🟢", callback_data=f"confirm_{confirmation_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_{confirmation_id}")]
        ])
        await send_protected_message(
            message.chat.id,
            f"🎬 **Documento recibido para comprimir:** `{file_name}`\n\n"
            f"¿Deseas comprimir este video?",
            reply_to_message_id=replied.id,
            reply_markup=keyboard
        )
        logger.info(f"Solicitud de confirmación creada para documento de {user_id}: {file_name}")
    except Exception as e:
        logger.error(f"Error en convert_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "⚠️ **Error al procesar el comando /convert**"
        )

# ======================== MANEJADOR DE CALLBACKS ======================== #

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in admin_users:
        if MAINTENANCE_MODE:
            await callback_query.answer(
                "🔧 Bot en mantenimiento\n\nPor favor, espere a que termine.",
                show_alert=True
            )
            return
    config_map = {
        "general_v1": "resolution=854x480 crf=28 audio_bitrate=64k fps=22 preset=veryfast codec=libx264",
        "general_v2": "resolution=1280x720 crf=30 audio_bitrate=128k fps=22 preset=veryfast codec=libx264",
        "reels_v1": "resolution=420x720 crf=25 audio_bitrate=64k fps=30 preset=veryfast codec=libx264",
        "reels_v2": "resolution=420x720 crf=25 audio_bitrate=128k fps=30 preset=veryfast codec=libx264",
        "show_v1": "resolution=854x480 crf=32 audio_bitrate=64k fps=20 preset=veryfast codec=libx264",
        "show_v2": "resolution=1280x720 crf=34 audio_bitrate=128k fps=20 preset=veryfast codec=libx264",
        "anime_v1": "resolution=854x480 crf=32 audio_bitrate=64k fps=18 preset=veryfast codec=libx264",
        "anime_v2": "resolution=854x480 crf=25 audio_bitrate=128k fps=18 preset=veryfast codec=libx264"
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
    if callback_query.data == "refresh_status_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ver estas estadísticas", show_alert=True)
            return
        try:
            stats = get_status_stats()
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_status_stats")]
            ])
            await callback_query.message.edit_text(stats, reply_markup=keyboard)
            await callback_query.answer("✅ Estadísticas actualizadas")
        except Exception as e:
            logger.error(f"Error actualizando estadísticas del sistema: {e}")
            await callback_query.answer("❌ Error al actualizar estadísticas", show_alert=True)
        return
    elif callback_query.data == "refresh_admin_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ver estas estadísticas", show_alert=True)
            return
        try:
            pipeline = [
                {"$match": {"plan": {"$exists": True, "$ne": None}}},
                {"$group": {
                    "_id": "$plan",
                    "count": {"$sum": 1}
                }}
            ]
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
            response += f"⬇️ **Descargas en curso:** {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
            response += f"🔄 **Compresiones activas:** {total_active}\n\n"
            if total_active > 0:
                response += "📋 **Compresiones activas:**\n"
                for i, comp in enumerate(active_compr, 1):
                    comp_user_id = comp.get("user_id")
                    file_name = comp.get("file_name", "Sin nombre")
                    start_time = comp.get("start_time")
                    try:
                        user = await app.get_users(comp_user_id)
                        username = f"@{user.username}" if user.username else f"Usuario {comp_user_id}"
                    except:
                        username = f"Usuario {comp_user_id}"
                    if isinstance(start_time, datetime.datetime):
                        start_str = start_time.strftime("%H:%M:%S")
                    else:
                        start_str = "¿?"
                    response += f"{i}. {username} - `{file_name}` (⏰ {start_str})\n"
                response += "\n"
            response += "📝 **Distribución por Planes:**\n"
            plan_names = {
                "standard": "🧩 Estándar",
                "pro": "💎 Pro",
                "premium": "👑 Premium",
                "ultra": "🚀 Ultra"
            }
            for stat in stats:
                plan_type = stat["_id"]
                count = stat["count"]
                plan_name = plan_names.get(
                    plan_type, 
                    plan_type.capitalize() if plan_type else "❓ Desconocido"
                )
                response += (
                    f"\n{plan_name}:\n"
                    f"  👥 Usuarios: {count}\n"
                )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_admin_stats"),
                    InlineKeyboardButton("❌ Cerrar", callback_data="close_admin_stats")
                ]
            ])
            await callback_query.message.edit_text(response, reply_markup=keyboard)
            await callback_query.answer("✅ Estadísticas actualizadas")
        except Exception as e:
            logger.error(f"Error actualizando estadísticas de administrador: {e}")
            await callback_query.answer("❌ Error al actualizar estadísticas", show_alert=True)
        return
    elif callback_query.data == "close_admin_stats":
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden cerrar este mensaje", show_alert=True)
            return
        try:
            await callback_query.message.delete()
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"Error cerrando mensaje de estadísticas de administrador: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje", show_alert=True)
        return
    elif callback_query.data == "custom_quality_start":
        temp_custom_settings[user_id] = {}
        keyboard = get_resolution_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\n"
            "Selecciona la resolución del video:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data.startswith("custom_resolution_"):
        resolution_value = callback_query.data.replace("custom_resolution_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['resolution'] = resolution_value
        keyboard = get_resolution_keyboard(resolution_value)
        await callback_query.message.edit_text(
            f"⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\n"
            f"Selecciona la resolucion:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_next_crf":
        if user_id not in temp_custom_settings or 'resolution' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar una resolución primero.", show_alert=True)
            return
        keyboard = get_crf_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\n"
            "Selecciona el nivel de compresión CRF:\n"
            "➥Menor valor = mejor calidad\n(archivo más grande)\n➥Mayor valor = menor calidad\n(archivo más pequeño)",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_back_resolution":
        keyboard = get_resolution_keyboard(temp_custom_settings.get(user_id, {}).get('resolution'))
        message_text = "⚙️**CONFIGURAR CALIDAD - PASO 1/4**⚙️\n\nSelecciona la resolución del video:"
        await callback_query.message.edit_text(message_text, reply_markup=keyboard)
        return
    elif callback_query.data.startswith("custom_crf_"):
        crf_value = callback_query.data.replace("custom_crf_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['crf'] = crf_value
        keyboard = get_crf_keyboard(crf_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\n"
            "Selecciona el nivel de compresión CRF:\n"
            "➥Menor valor = mejor calidad\n(archivo más grande)\n➥Mayor valor = menor calidad\n(archivo más pequeño)",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_next_fps":
        if user_id not in temp_custom_settings or 'crf' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un CRF primero.", show_alert=True)
            return
        keyboard = get_fps_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\n"
            "Selecciona el FPS:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_back_crf":
        keyboard = get_crf_keyboard(temp_custom_settings.get(user_id, {}).get('crf'))
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 2/4**⚙️\n\n"
            "Selecciona el nivel de compresión CRF:\n"
            "➥Menor valor = mejor calidad\n(archivo más grande)\n➥Mayor valor = menor calidad\n(archivo más pequeño)",
            reply_markup=keyboard
        )
        return
    elif callback_query.data.startswith("custom_fps_"):
        fps_value = callback_query.data.replace("custom_fps_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['fps'] = fps_value
        keyboard = get_fps_keyboard(fps_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\n"
            "Selecciona el FPS:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_next_audio":
        if user_id not in temp_custom_settings or 'fps' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un FPS primero.", show_alert=True)
            return
        keyboard = get_audio_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 4/4**⚙️\n\n"
            "Selecciona la calidad de audio:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_back_fps":
        keyboard = get_fps_keyboard(temp_custom_settings.get(user_id, {}).get('fps'))
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 3/4**⚙️\n\n"
            "Selecciona el FPS:",
            reply_markup=keyboard
        )
        return
    elif callback_query.data.startswith("custom_audio_"):
        audio_value = callback_query.data.replace("custom_audio_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['audio_bitrate'] = audio_value
        keyboard = get_audio_keyboard(audio_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD - PASO 4/4**⚙️\n\n"
            "Selecciona la calidad de audio:\n\n➥Menor valor = mejor calidad\n(archivo más grande)\n➥Mayor valor = menor calidad\n(archivo más pequeño)",
            reply_markup=keyboard
        )
        return
    elif callback_query.data == "custom_finish":
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
            elif user_settings['resolution'] == '854x480':
                resolution_name = "480p"
            elif user_settings['resolution'] == '1280x720':
                resolution_name = "720p"
            confirmation_text = (
                f"✅ **CALIDAD PERSONALIZADA CONFIGURADA**\n\n"
                f"**Configuración aplicada:**\n"
                f"• **Resolución:** {resolution_name}\n"
                f"• **Compresión CRF:** {user_settings['crf']}\n"
                f"• **FPS:** {user_settings['fps']}\n"
                f"• **Audio:** {user_settings['audio_bitrate']}"
            )
            back_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver a Settings", callback_data="back_to_settings")]
            ])
            await callback_query.message.edit_text(
                confirmation_text,
                reply_markup=back_keyboard
            )
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
        return
    elif callback_query.data.startswith("cancel_task_"):
        compression_id = callback_query.data.split("_")[2]
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
                msg_to_delete = callback_query.message
                try:
                    await msg_to_delete.delete()
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de cancelación: {e}")
                if compression_id in active_messages:
                    del active_messages[compression_id]
                if f"{compression_id}_upload" in active_messages:
                    del active_messages[f"{compression_id}_upload"]
                await callback_query.answer("⛔ Compresión cancelada ⛔", show_alert=False)
                try:
                    msg = await send_protected_message(
                        callback_query.message.chat.id,
                        "⛔ **Descarga/Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                except:
                    msg = await send_protected_message(
                        callback_query.message.chat.id,
                        "⛔ **Descarga/Compresión cancelada** ⛔"
                    )
                asyncio.create_task(delete_message_after(msg, 3))
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
                        logger.error(f"Error eliminando mensaje de progreso: {e}")
                await callback_query.answer("⛔ Compresión cancelada ⛔", show_alert=False)
                try:
                    msg = await send_protected_message(
                        callback_query.message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                except:
                    msg = await send_protected_message(
                        callback_query.message.chat.id,
                        "⛔ **Compresión cancelada** ⛔"
                    )
                asyncio.create_task(delete_message_after(msg, 3))
            else:
                await callback_query.answer("⚠️ No se pudo cancelar la tarea", show_alert=True)
        return
    if callback_query.data == "refresh_queue":
        try:
            queue_text, queue_keyboard = await get_queue_status(user_id)
            await callback_query.message.edit_text(
                queue_text,
                reply_markup=queue_keyboard
            )
            await callback_query.answer("✅ Estado de la cola actualizado")
        except Exception as e:
            logger.error(f"Error actualizando cola: {e}")
            await callback_query.answer("⏳Procesando información⏳...")
        return
    elif callback_query.data == "close_queue":
        try:
            await callback_query.message.delete()
            try:
                message_id = callback_query.message.id
                await app.delete_messages(
                    callback_query.message.chat.id, 
                    [message_id - 1]
                )
            except Exception as e:
                logger.error(f"Error eliminando mensaje original de ver cola: {e}")
                try:
                    async for message in app.get_chat_history(callback_query.message.chat.id, limit=5):
                        if message.text and "👀 Ver Cola" in message.text:
                            await message.delete()
                            break
                except Exception as e2:
                    logger.error(f"Error alternativo eliminando mensaje ver cola: {e2}")
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"Error cerrando mensaje de cola: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje")
        return
    elif callback_query.data == "refresh_plan":
        try:
            user_id = callback_query.from_user.id
            plan_info, keyboard = await get_plan_info(user_id)
            await callback_query.message.edit_text(
                plan_info,
                reply_markup=keyboard
            )
            await callback_query.answer("✅ Información del plan actualizada")
        except Exception as e:
            logger.error(f"Error actualizando plan: {e}")
            await callback_query.answer("⏳Procesando información⏳...")
        return
    elif callback_query.data == "close_plan":
        try:
            await callback_query.message.delete()
            try:
                message_id = callback_query.message.id
                await app.delete_messages(
                    callback_query.message.chat.id, 
                    [message_id - 1]
                )
            except Exception as e:
                logger.error(f"Error eliminando mensaje original de mi plan: {e}")
                try:
                    async for message in app.get_chat_history(callback_query.message.chat.id, limit=5):
                        if message.text and "📊 Mi Plan" in message.text:
                            await message.delete()
                            break
                except Exception as e2:
                    logger.error(f"Error alternativo eliminando mensaje mi plan: {e2}")
            await callback_query.answer("✅ Mensaje cerrado")
        except Exception as e:
            logger.error(f"Error cerrando mensaje de plan: {e}")
            await callback_query.answer("❌ Error al cerrar el mensaje")
        return
    if callback_query.data.startswith("confirm_setdays_"):
        if callback_query.from_user.id not in admin_users:
            await callback_query.answer("⚠️ Solo los administradores pueden ejecutar esta acción", show_alert=True)
            return
        try:
            days = int(callback_query.data.split("_")[2])
            await callback_query.message.edit_text(f"🔄 **Agregando {days} día(s) a todos los usuarios...**\n\n⏳ Esto puede tomar varios minutos...")
            updated_count, failed_count, result_message = await add_days_to_all_users(days, callback_query.from_user.id)
            result_text = (
                f"✅ **Proceso de agregar días completado**\n\n"
                f"• **Días agregados**: {days}\n"
                f"• **Usuarios actualizados**: {updated_count}\n"
                f"• **Errores**: {failed_count}\n\n"
                f"{result_message}"
            )
            await callback_query.message.edit_text(result_text)
            await callback_query.answer("✅ Proceso completado")
        except Exception as e:
            logger.error(f"Error en confirm_setdays: {e}", exc_info=True)
            await callback_query.message.edit_text("❌ **Error al ejecutar el comando**")
            await callback_query.answer("❌ Error en el proceso")
        return
    elif callback_query.data == "cancel_setdays":
        await callback_query.message.edit_text("❌ **Operación cancelada**")
        await callback_query.answer("Operación cancelada")
        return
    if callback_query.data.startswith(("confirm_", "cancel_")):
        action, confirmation_id_str = callback_query.data.split('_', 1)
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
                await callback_query.answer(
                    f"⚠️ Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\n"
                    "Espera a que se procesen antes de enviar más.",
                    show_alert=True
                )
                await delete_confirmation(confirmation_id)
                return
            try:
                message = await app.get_messages(confirmation["chat_id"], confirmation["message_id"])
            except Exception as e:
                logger.error(f"Error obteniendo mensaje: {e}")
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
            queue_info = get_download_queue_info()
            queue_position = queue_info['waiting_count'] + 1
            wait_msg = await callback_query.message.edit_text(
                f"⏳ **Video agregado a la cola**\n\n"
                f"`{file_name}`\n\n"
                f"📊 **Posición en cola:** #{queue_position}\n"
                f"⏱ **Descargas activas:** {queue_info['current_downloads']}/{queue_info['max_downloads']}\n"
                f"⏳ **Estado:** Preparando descarga...",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")]
                ])
            )
            pending_col.insert_one({
                "user_id": user_id,
                "video_id": file_id,
                "file_name": file_name,
                "chat_id": message.chat.id,
                "message_id": message.id,
                "wait_message_id": wait_msg.id,
                "compression_id": compression_id,
                "timestamp": datetime.datetime.now()
            })
            asyncio.create_task(
                download_file_immediately(file_obj, file_name, compression_id, wait_msg, user_id, message.chat.id, message.id)
            )
            await delete_confirmation(confirmation_id)
            logger.info(f"Confirmación procesada para {user_id}: {file_name}")
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
    if callback_query.data.endswith("_menu"):
        quality_type = callback_query.data.replace("_menu", "")
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("V1 (audio normal y calidad media)", callback_data=f"{quality_type}_v1")],
            [InlineKeyboardButton("V2 (mejor audio y calidad alta)", callback_data=f"{quality_type}_v2")],
            [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
        ])
        await callback_query.message.edit_text(
            f"{title}\n\nSelecciona la calidad a usar:",
            reply_markup=keyboard
        )
        return
    if callback_query.data == "plan_back":
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error en plan_back: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al volver al menú de planes", show_alert=True)
        return
    if callback_query.data in ["show_plans_from_start", "show_plans_from_video"]:
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error mostrando planes desde callback: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al mostrar los planes", show_alert=True)
        return
    elif callback_query.data.startswith("plan_"):
        plan_type = callback_query.data.split("_")[1]
        user_id = callback_query.from_user.id
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back"),
             InlineKeyboardButton("📝 Contratar Plan", url="https://t.me/VirtualMix_Shop?text=Hola,+estoy+interesad@+en+un+plan+del+bot+de+comprimír+vídeos")]
        ])
        if plan_type == "standard":
            await callback_query.message.edit_text(
                "🧩**Plan Estándar**🧩\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n\n"
                "❌ **Desventajas:**\n• **No podá reenviar del bot**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **180Cup**💳 | **100Cup**📱\n• **Duración 7 dias**\n\n",
                reply_markup=back_keyboard
            )
        elif plan_type == "pro":
            await callback_query.message.edit_text(
                "💎**Plan Pro**💎\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Podá reenviar del bot**\n\n❌ **Desventajas**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **300Cup**💳 | **200Cup**📱\n• **Duración 15 dias**\n\n",
                reply_markup=back_keyboard
            )
        elif plan_type == "premium":
            await callback_query.message.edit_text(
                "👑**Plan Premium**👑\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Soporte prioritario 24/7**\n• **Podá reenviar del bot**\n"
                f"• **Múltiples videos en cola** (hasta {PREMIUM_QUEUE_LIMIT})\n\n"
                "• **Precio:** **500Cup**💳 | **300Cup**📱\n• **Duración 30 dias**\n\n",
                reply_markup=back_keyboard
            )
        return
    config = config_map.get(callback_query.data)
    if config:
        user_id = callback_query.from_user.id
        if await update_user_video_settings(user_id, config):
            back_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
            ])
            quality_name = quality_names.get(callback_query.data, "Calidad Desconocida")
            if callback_query.data.endswith("_v1"):
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            elif callback_query.data.endswith("_v2"):
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            else:
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            await callback_query.message.edit_text(
                message_text,
                reply_markup=back_keyboard
            )
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
    elif callback_query.data == "back_to_settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗜️ Compresión General", callback_data="general_menu")],
            [InlineKeyboardButton("📱 Videos en Vertical", callback_data="reels_menu")],
            [InlineKeyboardButton("📺 Shows|Calidad media", callback_data="show_menu")],
            [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime_menu")],
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data="custom_quality_start")]
        ])
        await callback_query.message.edit_text(
            "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️",
            reply_markup=keyboard
        )
    else:
        await callback_query.answer("Opción inválida.", show_alert=True)

# ======================== MANEJADOR DE START ======================== #

@app.on_message(filters.command("start"))
async def start_command(client, message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id, "start"):
            return
        if user_id in ban_users:
            logger.warning(f"Usuario baneado intentó usar /start: {user_id}")
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso al bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
            return
        image_path = "logo.jpg"
        caption = (
            "**🤖 Bot para comprimir videos**\n"
            "➣**Creado por** @InfiniteNetworkAdmin\n\n"
            "**¡Bienvenido!** Puedo reducir el tamaño de los vídeos hasta un 80% o más y se verán bien sin perder tanta calidad\nUsa los botones del menú para interactuar conmigo.\nSi tiene duda use el botón ℹ️ Ayuda\n\n"
            "**⚙️ Versión 28.5.1 F ⚙️**"
        )
        await send_protected_photo(
            chat_id=message.chat.id,
            photo=image_path,
            caption=caption,
            reply_markup=get_main_menu_keyboard()
        )
        logger.info(f"Comando /start ejecutado por {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en handle_start: {e}", exc_info=True)

# ======================== MANEJADOR DE MENÚ PRINCIPAL ======================== #

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
            support_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨🏻‍💻 Soporte", url="https://t.me/VirtualMix_Shop")]
            ])
            await send_protected_message(
                message.chat.id,
                "👨🏻‍💻 **Información**\n\n"
                "➣ **Configurar calidad**:\n• Usa el botón ⚙️ Settings\n"
                "➣ **Para comprimir un video**:\n• Envíalo directamente al bot\n"
                "➣ **Ver planes**:\n• Usa el botón 📋 Planes\n"
                "➣ **Ver tu estado**:\n• Usa el botón 📊 Mi Plan\n"
                "➣ **Usa** /start **para iniciar en el bot nuevamente o para actualizar**\n"
                "➣ **Ver cola de compresión**:\n• Usa el botón 👀 Ver Cola\n"
                "➣ **Cancelar videos de la cola**:\n• Usa el botón 🗑️ Cancelar Cola\n"
                "➣ **Para ver su configuración de compresión actual use**: /calidad\n"
                "➣ **Para ver el estado del bot use**: /estado\n➣ **Compresión de varios formatos de video en documento**:\n• Mande el documento(vídeo) y responda al vídeo con /convert para que lo empiece a procesar\n\n"
                "**NUEVO SISTEMA:**\n"
                "• Los videos se descargan inmediatamente y se agregarán a la cola de compresión\n"
                "• Progreso en tiempo real",
                reply_markup=support_keyboard
            )
        elif text == "👀 ver cola":
            await queue_command(client, message)
        elif text == "🗑️ cancelar cola":
            await cancel_queue_command(client, message)
        elif text == "/cancel":
            await cancel_command(client, message)
        else:
            await handle_message(client, message)
    except Exception as e:
        logger.error(f"Error en main_menu_handler: {e}", exc_info=True)

# ======================== NUEVOS COMANDOS DE ADMINISTRACIÓN ======================== #

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
                await app.send_message(
                    user_id,
                    "✅ **Tu acceso al bot ha sido restaurado.**\n\n"
                    "Ahora puedes volver a usar el bot."
                )
            except Exception as e:
                logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
        else:
            await message.reply(f"El usuario {user_id} no estaba baneado.")
        logger.info(f"Usuario desbaneado: {user_id} por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en unban_user_command: {e}", exc_info=True)
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
        banned_col.insert_one({
            "user_id": user_id,
            "banned_at": datetime.datetime.now()
        })
        pending_result = pending_col.delete_many({"user_id": user_id})
        downloaded_result = downloaded_videos_col.delete_many({"user_id": user_id})
        user_settings_col.delete_one({"user_id": user_id})
        await message.reply(
            f"Usuario {user_id} eliminado y baneado exitosamente.\n"
            f"🗑️ Tareas pendientes eliminadas: {pending_result.deleted_count}\n"
            f"🗑️ Videos descargados eliminados: {downloaded_result.deleted_count}"
        )
        logger.info(f"Usuario eliminado y baneado: {user_id} por admin {message.from_user.id}")
        try:
            await app.send_message(
                user_id,
                "🔒 **Tu acceso al bot ha sido revocado.**\n\n"
                "No podrás usar el bot hasta nuevo aviso."
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error en delete_user_command: {e}", exc_info=True)
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
            if isinstance(banned_at, datetime.datetime):
                banned_at_str = banned_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                banned_at_str = str(banned_at)
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   ⏰ Fecha: {banned_at_str}\n\n"
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en view_banned_users_command: {e}", exc_info=True)
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
        banned_col.insert_one({
            "user_id": ban_user_id,
            "banned_at": datetime.datetime.now()
        })
        user_settings_col.delete_one({"user_id": ban_user_id})
        downloaded_videos_col.delete_many({"user_id": ban_user_id})
        await message.reply(
            f"Usuario {ban_user_id} baneado y eliminado de la base de datos."
            if result.deleted_count > 0 else
            f"Usuario {ban_user_id} baneado (no estaba en la base de datos)."
        )
    except Exception as e:
        logger.error(f"Error en ban_or_delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("key") & filters.private)
async def key_command(client, message):
    try:
        user_id = message.from_user.id
        if user_id in ban_users:
            await send_protected_message(message.chat.id, "🚫 Tu acceso ha sido revocado.")
            return
        logger.info(f"Comando key recibido de {user_id}")
        if not message.text or len(message.text.split()) < 2:
            await send_protected_message(message.chat.id, "❌ Formato: /key <clave>")
            return
        key = message.text.split()[1].strip()
        now = datetime.datetime.now()
        key_data = temp_keys_col.find_one({
            "key": key,
            "used": False
        })
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
            await send_protected_message(
                message.chat.id,
                f"✅ **Plan {new_plan.capitalize()} activado!**\n"
                f"**Válido por {duration_text}**\n\n"
                f"Use el comando /start para iniciar en el bot"
            )
            logger.info(f"Plan actualizado a {new_plan} para {user_id} con clave {key}")
        else:
            await send_protected_message(message.chat.id, "❌ **Error al activar el plan. Contacta con el administrador.**")
    except Exception as e:
        logger.error(f"Error en key_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al procesar la solicitud de acceso**")

sent_messages = {}

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

# ======================== COMANDOS DE PLANES ======================== #

@app.on_message(filters.command("myplan") & filters.private)
async def my_plan_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠 Planes 💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
        else:
            plan_info, keyboard = await get_plan_info(user_id)
            await send_protected_message(
                message.chat.id, 
                plan_info,
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Error en my_plan_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ **Error al obtener información de tu plan**",
            reply_markup=get_main_menu_keyboard()
        )

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
        logger.error(f"Error en set_plan_command: {e}", exc_info=True)
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
            if isinstance(join_date, datetime.datetime):
                join_date = join_date.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(expires_at, datetime.datetime):
                expires_at = expires_at.strftime("%Y-%m-%d %H:%M:%S")
            await message.reply(
                f"👤**Usuario**: {username}\n"
                f"🆔 **ID**: `{user_id}`\n"
                f"📝 **Plan**: {plan_name}\n"
                f"🎬 **Videos comprimidos**: {compressed_videos}\n"
                f"📅 **Fecha de registro**: {join_date}\n"
                f"⏰ **Expira**: {expires_at}"
            )
        else:
            await message.reply("⚠️ Usuario no registrado o sin plan")
    except Exception as e:
        logger.error(f"Error en user_info_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("restuser") & filters.user(admin_users))
async def reset_all_users_command(client, message):
    try:
        result = users_col.delete_many({})
        user_settings_col.delete_many({})
        downloaded_videos_col.delete_many({})
        await message.reply(
            f"**Todos los usuarios han sido eliminados**\n"
            f"Usuarios eliminados: {result.deleted_count}\n"
            f"Videos descargados eliminados: {downloaded_videos_col.count_documents({})}"
        )
        logger.info(f"Todos los usuarios eliminados por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en reset_all_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuarios")

@app.on_message(filters.command("user") & filters.user(admin_users))
async def list_users_command(client, message):
    try:
        all_users = list(users_col.find({}))
        if not all_users:
            await message.reply("⛔**No hay usuarios registrados.**⛔")
            return
        response = "**Lista de Usuarios Registrados**\n\n"
        for i, user in enumerate(all_users, 1):
            user_id = user["user_id"]
            plan = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            try:
                user_info = await app.get_users(user_id)
                username = f"@{user_info.username}" if user_info.username else "Sin username"
            except:
                username = "Sin username"
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   📝 Plan: {plan}\n\n"
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en list_users_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al listar usuarios**")

@app.on_message(filters.command("admin") & filters.user(admin_users))
async def admin_stats_command(client, message):
    try:
        pipeline = [
            {"$match": {"plan": {"$exists": True, "$ne": None}}},
            {"$group": {
                "_id": "$plan",
                "count": {"$sum": 1}
            }}
        ]
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
        response += f"⬇️ **Descargas en curso:** {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}\n"
        response += f"🔄 **Compresiones activas:** {total_active}\n\n"
        if total_active > 0:
            response += "📋 **Compresiones activas:**\n"
            for i, comp in enumerate(active_compr, 1):
                comp_user_id = comp.get("user_id")
                file_name = comp.get("file_name", "Sin nombre")
                start_time = comp.get("start_time")
                try:
                    user = await app.get_users(comp_user_id)
                    username = f"@{user.username}" if user.username else f"Usuario {comp_user_id}"
                except:
                    username = f"Usuario {comp_user_id}"
                if isinstance(start_time, datetime.datetime):
                    start_str = start_time.strftime("%H:%M:%S")
                else:
                    start_str = "¿?"
                response += f"{i}. {username} - `{file_name}` (⏰ {start_str})\n"
            response += "\n"
        response += "📝 **Distribución por Planes:**\n"
        plan_names = {
            "standard": "🧩 Estándar",
            "pro": "💎 Pro",
            "premium": "👑 Premium",
            "ultra": "🚀 Ultra"
        }
        for stat in stats:
            plan_type = stat["_id"]
            count = stat["count"]
            plan_name = plan_names.get(
                plan_type, 
                plan_type.capitalize() if plan_type else "❓ Desconocido"
            )
            response += (
                f"\n{plan_name}:\n"
                f"  👥 Usuarios: {count}\n"
            )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Actualizar", callback_data="refresh_admin_stats"),
                InlineKeyboardButton("❌ Cerrar", callback_data="close_admin_stats")
            ]
        ])
        await message.reply(response, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error en admin_stats_command: {e}", exc_info=True)
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
        await app.send_message(
            admin_id,
            f"📤 **Iniciando difusión a {total_users} usuarios...**\n"
            f"⏱ Esto puede tomar varios minutos."
        )
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
                logger.error(f"Error enviando mensaje a {user_id}: {e}")
                failed += 1
        await app.send_message(
            admin_id,
            f"✅ **Difusión completada!**\n\n"
            f"👥 Total de usuarios: {total_users}\n"
            f"✅ Enviados correctamente: {success}\n"
            f"❌ Fallidos: {failed}"
        )
    except Exception as e:
        logger.error(f"Error en broadcast_message: {e}", exc_info=True)
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
        await message.reply(
            "📤 **Difusión iniciada!**\n"
            "⏱ Los mensajes se enviarán progresivamente a todos los usuarios.\n"
            "Recibirás un reporte final cuando se complete."
        )
    except Exception as e:
        logger.error(f"Error en broadcast_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al iniciar la difusión")

async def queue_command(client, message):
    user_id = message.from_user.id
    user_plan = await get_user_plan(user_id)
    if user_plan is None or user_plan.get("plan") is None:
        await send_protected_message(
            message.chat.id,
            "**Usted no tiene acceso para usar este bot.**\n\n"
            "Por favor, adquiera un plan para poder ver la cola de compresión."
        )
        return
    queue_status, keyboard = await get_queue_status(user_id)
    await send_protected_message(message.chat.id, queue_status, reply_markup=keyboard)

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
                logger.error(f"Error enviando mensaje de notificación a {user_id}: {e}")
                failed += 1
        return success, failed
    except Exception as e:
        logger.error(f"Error en notify_all_users: {e}", exc_info=True)
        return 0, 0

async def restart_bot():
    try:
        for compression_id, process in list(ffmpeg_processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
            except Exception as e:
                logger.error(f"Error terminando proceso FFmpeg para {compression_id}: {e}")
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
        logger.info(f"Eliminados {result.deleted_count} elementos de la cola")
        logger.info(f"Eliminados {downloaded_result.deleted_count} videos descargados")
        active_compressions_col.delete_many({})
        notification_text = (
            "🔔**Notificación:**\n\n"
            "El bot ha sido reiniciado\ntodos los procesos se han cancelado.\n\n✅ **Ahora puedes enviar nuevos videos para comprimir**."
        )
        success, failed = await notify_all_users(notification_text)
        try:
            await app.send_message(
                -4826894501,
                f"**Notificación de reinicio completada!**\n\n"
                f"✅ Enviados correctamente: {success}\n"
                f"❌ Fallidos: {failed}"
            )
        except Exception as e:
            logger.error(f"Error enviando notificación de reinicio al grupo: {e}")
        return True, success, failed
    except Exception as e:
        logger.error(f"Error en restart_bot: {e}", exc_info=True)
        return False, 0, 0

@app.on_message(filters.command("restart") & filters.user(admin_users))
async def restart_command(client, message):
    try:
        msg = await message.reply("🔄 Reiniciando bot...")
        success, notifications_sent, notifications_failed = await restart_bot()
        if success:
            await msg.edit(
                "**Bot reiniciado con éxito**\n\n"
                "✅ Todos los procesos activos cancelados\n"
                "✅ Cola de compresión vaciada\n"
                "✅ Videos descargados eliminados\n"
                "✅ Procesos FFmpeg terminados\n"
                "✅ Estado interno limpiado\n\n"
                f"📤 Notificaciones enviadas: {notifications_sent}\n"
                f"❌ Notificaciones fallidas: {notifications_failed}"
            )
        else:
            await msg.edit("⚠️ **Error al reiniciar el bot.**")
    except Exception as e:
        logger.error(f"Error en restart_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al ejecutar el comando de reinicio")

# ======================== COMANDOS PARA CONFIGURACIÓN PERSONALIZADA ======================== #

@app.on_message(filters.command(["calidad", "quality"]) & filters.private)
async def calidad_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso para usar este bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️"
            )
            return
        if len(message.text.split()) < 2:
            current_settings = await get_user_video_settings(user_id)
            resolution = current_settings['resolution']
            if 'x' in resolution:
                resolution_display = resolution.split('x')[1]
            else:
                resolution_display = resolution
            response = (
                "**Tu configuración actual de compresión:**\n\n"
                f"• **Resolución**: `{resolution_display}`\n"
                f"• **CRF**: `{current_settings['crf']}`\n"
                f"• **FPS**: `{current_settings['fps']}`\n"                
                f"• **Bitrate de audio**: `{current_settings['audio_bitrate']}`\n\n"
                "Para restablecer a la configuración por defecto, usa /resetcalidad"
            )
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
            await send_protected_message(
                message.chat.id,
                "❌ **Error al actualizar la configuración.**\n"
                "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
            )
    except Exception as e:
        logger.error(f"Error en calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al procesar el comando.**\n"
            "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
        )

@app.on_message(filters.command("resetcalidad") & filters.private)
async def reset_calidad_command(client, message):
    try:
        user_id = message.from_user.id
        await reset_user_video_settings(user_id)
        default_settings = await get_user_video_settings(user_id)
        resolution = default_settings['resolution']
        if 'x' in resolution:
            resolution_display = resolution.split('x')[1]
        else:
            resolution_display = resolution
        response = (
            "✅ **Configuración restablecida a los valores por defecto:**\n\n"
            f"• **resolución**: {resolution_display}\n"
            f"• **crf**: {default_settings['crf']}\n"
            f"• **fps**: {default_settings['fps']}\n"
            f"• **audio_bitrate**: {default_settings['audio_bitrate']}"
        )
        await send_protected_message(message.chat.id, response)
    except Exception as e:
        logger.error(f"Error en reset_calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al restablecer la configuración.**"
        )

# ======================== MANEJADORES PRINCIPALES ======================== #

@app.on_message(filters.video)
async def handle_video(client, message: Message):
    try:
        user_id = message.from_user.id
        if await check_maintenance_and_notify(user_id, message.chat.id):
            return
        if user_id in ban_users:
            logger.warning(f"Intento de uso por usuario baneado: {user_id}")
            return
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_video")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n"
                "**Adquiere un plan para usar el bot.**\n\n",
                reply_markup=keyboard
            )
            return
        if await has_pending_confirmation(user_id):
            logger.info(f"Usuario {user_id} tiene confirmación pendiente, ignorando video adicional")
            return
        if await check_user_limit(user_id):
            await send_protected_message(
                message.chat.id,
                f"⚠️ **Límite alcanzado**\n"
                f"Tu plan ha expirado.\n\n"
                "👨🏻‍💻**Contacta con @VirtualMix_Shop para renovar tu Plan**"
            )
            return
        queue_limit = await get_user_queue_limit(user_id)
        pending_count = pending_col.count_documents({"user_id": user_id})
        downloaded_count = await get_user_downloaded_count(user_id)
        total_pending = pending_count + downloaded_count
        if total_pending >= queue_limit:
            await send_protected_message(
                message.chat.id,
                f"Ya tienes {total_pending} videos en cola (límite: {queue_limit}).\n"
                "Por favor espera a que se procesen antes de enviar más."
            )
            return
        confirmation_id = await create_confirmation(
            user_id,
            message.chat.id,
            message.id,
            message.video.file_id,
            message.video.file_name
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Confirmar compresión 🟢", callback_data=f"confirm_{confirmation_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_{confirmation_id}")]
        ])
        await send_protected_message(
            message.chat.id,
            f"🎬 **Video recibido para comprimír:** `{message.video.file_name}`\n\n"
            f"¿Deseas comprimir este video?",
            reply_to_message_id=message.id,
            reply_markup=keyboard
        )
        logger.info(f"Solicitud de confirmación creada para {user_id}: {message.video.file_name}")
    except Exception as e:
        logger.error(f"Error en handle_video: {e}", exc_info=True)

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
        user_id = message.from_user.id
        if user_id in ban_users:
            return
        logger.info(f"Mensaje recibido de {user_id}: {text}")
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
                logger.warning(f"Intento no autorizado de banuser/deluser por {user_id}")
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
        # NUEVO: Comando watchdog
        elif text.startswith(('/watchdog', '.watchdog', '/whactdog', '.whactdog')):
            if user_id in admin_users:
                await watchdog_status_command(client, message)
        elif text.startswith(('/estado', '.estado')):
            await estado_command(client, message)
        elif text.startswith(('/man_on', '.man_on')):
            if user_id in admin_users:
                await maintenance_on_command(client, message)
        elif text.startswith(('/man_off', '.man_off')):
            if user_id in admin_users:
                await maintenance_off_command(client, message)
        elif text.startswith(('/convert', '.convert')):
            await send_protected_message(
                message.chat.id,
                "❌ **Debes responder a un documento que sea un vídeo con /convert.**"
            )
        if message.reply_to_message:
            original_message = sent_messages.get(message.reply_to_message.id)
            if original_message:
                user_id = original_message["user_id"]
                sender_info = f"Respuesta de @{message.from_user.username}" if message.from_user.username else f"Respuesta de user ID: {message.from_user.id}"
                await send_protected_message(user_id, f"{sender_info}: {message.text}")
                logger.info(f"Respuesta enviada a {user_id}")
    except Exception as e:
        logger.error(f"Error en handle_message: {e}", exc_info=True)

async def notify_group(client, message: Message, original_size: int, compressed_size: int = None, status: str = "start"):
    """
    Envía notificaciones al grupo de administración sobre el estado de los videos.
    Soporta mensajes de tipo video y documento.
    """
    try:
        group_id = -1003896005361  # ID del grupo (verificar si necesita prefijo -100)

        # Verificar que el bot sea miembro del grupo (opcional, pero útil para debug)
        try:
            await client.get_chat(group_id)
        except Exception as e:
            logger.error(f"El bot no puede acceder al grupo {group_id}: {e}")
            return

        user = message.from_user
        username = f"@{user.username}" if user.username else "Sin username"

        # Obtener nombre del archivo según el tipo de mensaje
        if message.video:
            file_name = message.video.file_name or "Sin nombre"
        elif message.document:
            file_name = message.document.file_name or "Sin nombre"
        else:
            file_name = "Desconocido"

        size_mb = original_size // (1024 * 1024)

        if status == "start":
            text = (
                "📤 **Nuevo video recibido para comprimir**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )
        elif status == "done" and compressed_size is not None:
            compressed_mb = compressed_size // (1024 * 1024)
            text = (
                "📥 **Video comprimido y enviado**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📉 **Tamaño comprimido:** {compressed_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )
        else:
            # status desconocido, no enviar mensaje
            return

        await client.send_message(chat_id=group_id, text=text)
        logger.info(f"Notificación enviada al grupo: {user.id} - {file_name} ({status})")

    except Exception as e:
        logger.error(f"Error enviando notificación al grupo: {e}", exc_info=True)

# ======================== WATCHDOG ======================== #

async def recover_pending_compressions():
    """Pone los videos descargados en la cola de compresión si no hay compresiones activas."""
    try:
        downloaded_count = downloaded_videos_col.count_documents({})
        if downloaded_count == 0:
            return
        active_count = active_compressions_col.count_documents({})
        if active_count > 0:
            return
        logger.info("Watchdog: No hay compresiones activas pero hay videos descargados. Recuperando...")
        downloaded_videos = list(downloaded_videos_col.find().sort("timestamp", 1))
        for video in downloaded_videos:
            compression_id = video["compression_id"]
            user_id = video["user_id"]
            file_path = video["file_path"]
            file_name = video["file_name"]
            task = {
                "compression_id": compression_id,
                "user_id": user_id,
                "original_video_path": file_path,
                "file_name": file_name,
                "chat_id": video.get("chat_id", user_id),
                "original_message_id": video.get("original_message_id", 0),
                "wait_msg_id": video.get("wait_msg_id", 0),
                "wait_msg": None
            }
            wait_msg_id = video.get("wait_msg_id")
            if wait_msg_id:
                try:
                    chat_id = video.get("chat_id", user_id)
                    wait_msg = await app.get_messages(chat_id, wait_msg_id)
                    task["wait_msg"] = wait_msg
                except:
                    task["wait_msg"] = None
            await compression_processing_queue.put(task)
            logger.info(f"Watchdog: Video {file_name} añadido a la cola de compresión.")
        global processing_tasks
        new_tasks = []
        for task in processing_tasks:
            if task.done():
                try:
                    exc = task.exception()
                    if exc:
                        logger.error(f"Worker terminó con excepción: {exc}")
                except:
                    pass
                new_task = asyncio.create_task(process_compression_queue())
                new_tasks.append(new_task)
            else:
                new_tasks.append(task)
        processing_tasks = new_tasks
        if not processing_tasks:
            logger.warning("No hay workers de procesamiento, creando uno nuevo.")
            processing_tasks.append(asyncio.create_task(process_compression_queue()))
    except Exception as e:
        logger.error(f"Error en recover_pending_compressions: {e}", exc_info=True)

async def watchdog_loop():
    global last_watchdog_run
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            logger.debug("Ejecutando watchdog...")
            await recover_pending_compressions()
            last_watchdog_run = datetime.datetime.now()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en watchdog_loop: {e}", exc_info=True)
            await asyncio.sleep(60)

# ======================== INICIO DEL BOT Y WORKERS ======================== #

async def start_workers():
    global processing_tasks
    processing_tasks = []
    for i in range(1):
        task = asyncio.create_task(process_compression_queue())
        processing_tasks.append(task)
    logger.info(f"Iniciados {len(processing_tasks)} workers de procesamiento")

async def main():
    await start_workers()
    asyncio.create_task(watchdog_loop())
    await app.start()
    bot_info = await app.get_me()
    logger.info(f"Bot iniciado: @{bot_info.username}")
    await asyncio.Event().wait()

try:
    logger.info("Iniciando el bot...")
    app.run(main())
except Exception as e:
    logger.critical(f"Error fatal al iniciar el bot: {e}", exc_info=True)