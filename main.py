import asyncio
import logging
import sys
import os
import json
import datetime
from pyrogram import Client, filters, idle
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
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- 2. DATABASE (MA'LUMOTLAR OMBORI) ---
DB_NAME = "bot_data.db"

async def create_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                reminder_text TEXT,
                remind_at TIMESTAMP,
                is_completed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            ("auto_reply", "1")
        )
        await db.commit()

async def add_user(user_id, full_name, username):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (id, full_name, username) VALUES (?, ?, ?)',
            (user_id, full_name, username)
        )
        await db.commit()

async def add_message_to_history(user_id, role, content):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)',
            (user_id, role, content)
        )
        await db.commit()

async def get_history(user_id, limit=10):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
            (user_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT value FROM settings WHERE key = ?', (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def update_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, value)
        )
        await db.commit()

async def add_reminder(user_id, reminder_text, remind_at):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO reminders (user_id, reminder_text, remind_at) VALUES (?, ?, ?)',
            (user_id, reminder_text, remind_at)
        )
        await db.commit()

async def get_pending_reminders():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            '''SELECT r.id, r.user_id, r.reminder_text, r.remind_at, u.full_name
               FROM reminders r
               LEFT JOIN users u ON r.user_id = u.id
               WHERE r.is_completed = 0 AND r.remind_at <= datetime("now")'''
        ) as cursor:
            return await cursor.fetchall()

async def mark_reminder_completed(reminder_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'UPDATE reminders SET is_completed = 1 WHERE id = ?',
            (reminder_id,)
        )
        await db.commit()

# --- 3. AI XIZMATI (GEMINI) ---
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = 'gemini-2.5-flash'
SYSTEM_INSTRUCTION = (
    "Siz oddiy insonsiz, ismingiz yo'q. Sizning xojayiningiz bor. "
    "Mijozlar bilan tabiiy va insoniy ohangda gaplashasiz. "
    "AI ekanligingizni ASLO bildirmang. "
    "Javoblar qisqa va Telegramga mos bo'lsin."
)

def generate_client_reply(history: list, new_message: str, user_name: str) -> str:
    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(f"Mijoz: {user_name}\n\n" + SYSTEM_INSTRUCTION)]
        )
    ]
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(
            types.Content(role=role, parts=[types.Part.from_text(msg["content"])])
        )
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(new_message)])
    )
    try:
        response = ai_client.models.generate_content(model=MODEL_ID, contents=contents)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini xatosi: {e}")
        return "Tez orada javob beraman."

def analyze_chat(history: list, current_message: str) -> dict:
    chat_text = "\n".join([
        f"{'Mijoz' if m['role'] == 'user' else 'Siz'}: {m['content']}"
        for m in history
    ]) + f"\nMijoz: {current_message}"

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = (
        f"Hozirgi vaqt: {now_str}\n"
        f"Suhbat:\n{chat_text}\n\n"
        "Faqat JSON qaytaring (boshqa matn yo'q):\n"
        '{"needs_summary": bool, "summary_text": "str", '
        '"has_reminder": bool, "reminder_time": "YYYY-MM-DD HH:MM:00", "reminder_text": "str"}'
    )
    try:
        res = ai_client.models.generate_content(model=MODEL_ID, contents=prompt).text
        cleaned = res.replace('```json', '').replace('```', '').strip()
        data = json.loads(cleaned)
        # reminder_time mavjudligini tekshirish
        if data.get("has_reminder") and not data.get("reminder_time"):
            data["has_reminder"] = False
        return data
    except Exception as e:
        logger.error(f"analyze_chat xatosi: {e}")
        return {"needs_summary": False, "has_reminder": False}

# --- 4. BOTLARNI SOZLASH ---
# Railway'da SESSION_STRING majburiy — file session ishlamaydi
if not SESSION_STRING:
    logger.warning("SESSION_STRING yo'q! Userbot file session bilan ishga tushadi (local uchun).")

user_app = Client(
    "my_account",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING if SESSION_STRING else None
)
bot_app = Client(
    "control_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

MY_ID = None
ai_sent_message_ids = set()

# --- 5. ESLATMALAR TIZIMI (SCHEDULER) ---
async def check_reminders():
    try:
        reminders = await get_pending_reminders()
        for r in reminders:
            reminder_id, user_id, reminder_text, remind_at, full_name = r
            text = (
                f"⏰ **ESLATMA!**\n"
                f"👤 Mijoz: {full_name or user_id}\n"
                f"📅 Vaqt: {remind_at}\n"
                f"📝 Eslatma: {reminder_text}"
            )
            try:
                await bot_app.send_message(ADMIN_ID, text)
                await mark_reminder_completed(reminder_id)
            except Exception as e:
                logger.error(f"Eslatma yuborishda xato: {e}")
    except Exception as e:
        logger.error(f"check_reminders xatosi: {e}")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=1)
    scheduler.start()
    return scheduler

# --- 6. HANDLERLAR ---
@user_app.on_message(filters.private & ~filters.me)
async def handle_user_message(client: Client, message: Message):
    if message.from_user is None:
        return
    if message.from_user.id == ADMIN_ID:
        return

    auto_reply = await get_setting("auto_reply")
    if auto_reply == "0":
        return

    await add_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username
    )
    history = await get_history(message.from_user.id)
    msg_text = message.text or "[Media]"

    reply_text = generate_client_reply(history, msg_text, message.from_user.first_name)
    try:
        sent_msg = await message.reply(reply_text)
        ai_sent_message_ids.add(sent_msg.id)
    except Exception as e:
        logger.error(f"Reply yuborishda xato: {e}")
        return

    await add_message_to_history(message.from_user.id, "user", msg_text)
    await add_message_to_history(message.from_user.id, "assistant", reply_text)

    analysis = analyze_chat(history, msg_text)

    if analysis.get("needs_summary") and analysis.get("summary_text"):
        try:
            await bot_app.send_message(
                ADMIN_ID,
                f"📩 **Mijozdan xabar:**\n"
                f"👤 {message.from_user.full_name}\n"
                f"📝 Xulosa: {analysis['summary_text']}"
            )
        except Exception as e:
            logger.error(f"Summary yuborishda xato: {e}")

    if analysis.get("has_reminder") and analysis.get("reminder_time") and analysis.get("reminder_text"):
        try:
            await add_reminder(
                message.from_user.id,
                analysis["reminder_text"],
                analysis["reminder_time"]
            )
            await bot_app.send_message(
                ADMIN_ID,
                f"📅 **Yangi reja!**\n"
                f"👤 {message.from_user.full_name}\n"
                f"📝 {analysis['reminder_text']}\n"
                f"⏰ Vaqt: {analysis['reminder_time']}"
            )
        except Exception as e:
            logger.error(f"Reminder saqlashda xato: {e}")

@bot_app.on_message(filters.command("start") & filters.private)
async def bot_start(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.reply("Xush kelibsiz Xojayin! Men sizning yordamchingizman.")

@bot_app.on_message(filters.command("auto_on") & filters.private)
async def auto_on(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await update_setting("auto_reply", "1")
    await message.reply("✅ AI Avto-javob yoqildi.")

@bot_app.on_message(filters.command("auto_off") & filters.private)
async def auto_off(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await update_setting("auto_reply", "0")
    await message.reply("❌ AI Avto-javob o'chirildi.")

@bot_app.on_message(filters.command("status") & filters.private)
async def status(client: Client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    auto = await get_setting("auto_reply")
    state = "✅ Yoqiq" if auto == "1" else "❌ O'chiq"
    await message.reply(f"🤖 Tizim holati\nAvto-javob: {state}")

# --- 7. ASOSIY ISHGA TUSHIRISH ---
async def main():
    await create_tables()
    logger.info("Database tayyor!")

    apps = []

    if BOT_TOKEN:
        await bot_app.start()
        logger.info("Boshqaruv boti ishga tushdi.")
        apps.append(bot_app)
        try:
            await bot_app.send_message(ADMIN_ID, "🚀 Tizim ishga tushdi!")
        except Exception as e:
            logger.warning(f"Start xabarini yuborib bo'lmadi: {e}")

    await user_app.start()
    me = await user_app.get_me()
    global MY_ID
    MY_ID = me.id
    apps.append(user_app)
    logger.info(f"Akkauntga ulandi: {me.first_name}")

    scheduler = start_scheduler()
    logger.info("Scheduler ishga tushdi. Tayyor!")

    await idle()

    scheduler.shutdown()
    await user_app.stop()
    if BOT_TOKEN:
        await bot_app.stop()
    logger.info("Tizim to'xtatildi.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("To'xtatildi.")
    except Exception as e:
        logger.error(f"Kritik xato: {e}")
        sys.exit(1)
