import asyncio
import logging
import sys
import os
import json
import datetime
from pyrogram import Client, filters, idle, compose
from pyrogram.types import Message
from google import genai
from google.genai import types
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite

# --- 1. SOZLAMALAR (CONFIG) ---
load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID", 551853004))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")

# Windows konsol encoding muammosini hal qilish
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# Python 3.10+ event loop muammosini hal qilish
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

logging.basicConfig(level=logging.INFO)

# --- 2. DATABASE (MA'LUMOTLAR OMBORI) ---
DB_NAME = "bot_data.db"

async def create_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, reminder_text TEXT, remind_at TIMESTAMP, is_completed INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ("auto_reply", "1"))
        await db.commit()

async def add_user(user_id, full_name, username):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT OR IGNORE INTO users (id, full_name, username) VALUES (?, ?, ?)', (user_id, full_name, username))
        await db.commit()

async def add_message_to_history(user_id, role, content):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)', (user_id, role, content))
        await db.commit()

async def get_history(user_id, limit=10):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?', (user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def update_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
        await db.commit()

async def add_reminder(user_id, reminder_text, remind_at):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO reminders (user_id, reminder_text, remind_at) VALUES (?, ?, ?)', (user_id, reminder_text, remind_at))
        await db.commit()

async def get_pending_reminders():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT r.id, r.user_id, r.reminder_text, r.remind_at, u.full_name FROM reminders r LEFT JOIN users u ON r.user_id = u.id WHERE r.is_completed = 0 AND r.remind_at <= datetime("now", "localtime")') as cursor:
            return await cursor.fetchall()

async def mark_reminder_completed(reminder_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE reminders SET is_completed = 1 WHERE id = ?', (reminder_id,))
        await db.commit()

# --- 3. AI XIZMATI (GEMINI) ---
try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logging.error(f"Gemini Clientni ishga tushirishda xatolik: {e}")
    ai_client = None

MODEL_ID = 'gemini-2.0-flash' # Tuzatildi: gemini-2.5 mavjud emas
SYSTEM_INSTRUCTION = """Siz oddiy insonsiz, ismingiz yo'q. Sizning xojayiningiz bor. Mijozlar bilan tabiiy va insoniy ohangda gaplashasiz. AI ekanligingizni ASLO bildirmang. Javoblar qisqa va Telegramga mos bo'lsin."""

def generate_client_reply(history: list, new_message: str, user_name: str) -> str:
    if not ai_client: return "Tez orada javob beraman."
    contents = [types.Content(role="user", parts=[types.Part.from_text(f"Mijoz: {user_name}\n\n" + SYSTEM_INSTRUCTION)])]
    for msg in history:
        contents.append(types.Content(role="model" if msg["role"] == "assistant" else "user", parts=[types.Part.from_text(msg["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(new_message)]))
    try:
        return ai_client.models.generate_content(model=MODEL_ID, contents=contents).text.strip()
    except Exception as e:
        logging.error(f"AI javob generatsiyasida xatolik: {e}")
        return "Tez orada javob beraman."

def analyze_chat(history: list, current_message: str) -> dict:
    if not ai_client: return {"needs_summary": False, "has_reminder": False}
    chat_text = "\n".join([f"{'Mijoz' if m['role']=='user' else 'Siz'}: {m['content']}" for m in history]) + f"\nMijoz: {current_message}"
    prompt = f"Hozirgi vaqt: {datetime.datetime.now()}\nSuhbat: {chat_text}\nJSON qaytaring: needs_summary(bool), summary_text(str), has_reminder(bool), reminder_time(YYYY-MM-DD HH:MM:00), reminder_text(str)"
    try:
        res = ai_client.models.generate_content(model=MODEL_ID, contents=prompt).text
        return json.loads(res.replace('```json', '').replace('```', '').strip())
    except Exception as e:
        logging.error(f"Chat analizida xatolik: {e}")
        return {"needs_summary": False, "has_reminder": False}

# --- 4. BOTLARNI SOZLASH ---
if not SESSION_STRING:
    logging.warning("⚠️ SESSION_STRING topilmadi! Railway'da userbot ishlamasligi mumkin.")

user_app = Client("my_account", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING) if SESSION_STRING else Client("my_account", api_id=API_ID, api_hash=API_HASH)
bot_app = Client("control_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

MY_ID = None
admin_active_chats = {} # user_id -> instruction_mode
ai_sent_message_ids = set()

# --- 5. ESLATMALAR TIZIMI (SCHEDULER) ---
async def check_reminders():
    try:
        reminders = await get_pending_reminders()
        for r in reminders:
            text = f"⏰ **ESLATMA!**\n👤 Mijoz: {r[4]}\n📅 Vaqt: {r[3]}\n📝 Eslatma: {r[2]}"
            try:
                await bot_app.send_message(ADMIN_ID, text)
                await mark_reminder_completed(r[0])
            except Exception as e:
                logging.error(f"Eslatma yuborishda xatolik: {e}")
    except Exception as e:
        logging.error(f"Check remindersda xatolik: {e}")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=1)
    scheduler.start()

# --- 6. HANDLERLAR (USERBOT & BOT) ---
@user_app.on_message(filters.private & ~filters.me)
async def handle_user_message(client, message: Message):
    try:
        if message.from_user.id == ADMIN_ID: return
        auto_reply = await get_setting("auto_reply")
        if auto_reply == "0": return
        
        await add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
        history = await get_history(message.from_user.id)
        
        reply_text = generate_client_reply(history, message.text or "[Media]", message.from_user.first_name)
        sent_msg = await message.reply(reply_text)
        ai_sent_message_ids.add(sent_msg.id)
        
        await add_message_to_history(message.from_user.id, "user", message.text or "[Media]")
        await add_message_to_history(message.from_user.id, "assistant", reply_text)
        
        analysis = analyze_chat(history, message.text or "[Media]")
        if analysis.get("needs_summary"):
            await bot_app.send_message(ADMIN_ID, f"📩 **Mijozdan xabar:**\n👤 {message.from_user.full_name}\n📝 Xulosa: {analysis['summary_text']}\n\nUnga javob yuborish uchun ustiga bosing.")
        if analysis.get("has_reminder"):
            await add_reminder(message.from_user.id, analysis["reminder_text"], analysis["reminder_time"])
            await bot_app.send_message(ADMIN_ID, f"📅 **Yangi reja!**\n👤 {message.from_user.full_name}\n📝 {analysis['reminder_text']}\n⏰ Vaqt: {analysis['reminder_time']}")
    except Exception as e:
        logging.error(f"User message handlerda xatolik: {e}")

@bot_app.on_message(filters.command("start"))
async def bot_start(client, message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.reply("Xush kelibsiz Xojayin! Men sizning yordamchingizman.")

@bot_app.on_message(filters.command("auto_on"))
async def auto_on(client, message: Message):
    if message.from_user.id != ADMIN_ID: return
    await update_setting("auto_reply", "1")
    await message.reply("✅ AI Avto-javob yoqildi.")

@bot_app.on_message(filters.command("auto_off"))
async def auto_off(client, message: Message):
    if message.from_user.id != ADMIN_ID: return
    await update_setting("auto_reply", "0")
    await message.reply("❌ AI Avto-javob o'chirildi.")

# --- 7. ASOSIY ISHGA TUSHIRISH ---
async def main():
    try:
        await create_tables()
        logging.info("Database tayyor!")
        
        if BOT_TOKEN:
            await bot_app.start()
            bot_me = await bot_app.get_me()
            logging.info(f"Boshqaruv boti ishga tushdi: @{bot_me.username}")
            try:
                await bot_app.send_message(ADMIN_ID, "🚀 Tizim ishga tushdi!")
            except Exception as e:
                logging.error(f"Admin-ga start xabari yuborib bo'lmadi: {e}")
        
        if not SESSION_STRING:
            logging.error("❌ XATOLIK: SESSION_STRING kiritilmagan! Railway'da userbot ulanib bo'lmaydi.")
            if not sys.stdin.isatty(): # Agar terminal bo'lmasa
                logging.error("Terminal mavjud emas, login qilib bo'lmaydi. To'xtatildi.")
                return

        await user_app.start()
        me = await user_app.get_me()
        global MY_ID
        MY_ID = me.id
        logging.info(f"Akkauntga ulandi: {me.first_name} (ID: {me.id})")
        
        start_scheduler()
        logging.info("🚀 Hammasi tayyor!")
        await idle()
        
    except Exception as e:
        logging.error(f"Asosiy jarayonda xatolik: {e}")
    finally:
        if user_app.is_connected: await user_app.stop()
        if bot_app.is_connected: await bot_app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot to'xtatildi.")
    except Exception as e:
        logging.critical(f"Kutilmagan xatolik: {e}")

