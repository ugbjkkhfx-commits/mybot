import os
import re
import json
import time
import random
import tempfile
import logging
import traceback
import urllib.request
import threading
import sqlite3
from contextlib import contextmanager
import telebot
from telebot import types
import yt_dlp
from keep_alive import keep_alive

# --- الإعدادات ---
BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
CHANNEL_ID = -1003872259900
DB_PATH = "users.db"
BATCH_SIZE = 20

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- قاعدة البيانات ---
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try: yield conn
    finally: conn.close()

def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users_db (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT);
        """)

def register_user_db(user):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users_db (id, username, first_name) VALUES (?, ?, ?)", 
                     (user.id, user.username or "", user.first_name or ""))

# --- الواجهة والأزرار ---
def _build_main_keyboard(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("📥 تحميل دفعة واحدة"), types.KeyboardButton("🚀 استنساخ كامل"))
    if chat_id == ADMIN_ID:
        markup.add(types.KeyboardButton("📊 لوحة التحكم"), types.KeyboardButton("📡 إذاعة رسالة"))
    return markup

# --- الجلسات والتحميل ---
sessions = {}
def get_session(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {"profile_url": None, "state": "waiting"}
    return sessions[chat_id]

def normalise_profile_input(text):
    text = text.strip()
    if "tiktok.com" in text:
        m = re.search(r"(https?://(?:www\.)?tiktok\.com/@[\w.]+)", text)
        return m.group(1) if m else None
    return None

# --- المعالجات ---
@bot.message_handler(commands=["start"])
def start(message):
    register_user_db(message.from_user)
    bot.send_message(message.chat.id, "أهلاً بك! استخدم الأزرار للتحميل أو استنساخ الحسابات.", 
                     reply_markup=_build_main_keyboard(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "📥 تحميل دفعة واحدة")
def handle_batch(message):
    bot.reply_to(message, "أرسل روابط الفيديوهات دفعة واحدة وسأقوم بتحميلها لك 📥")

@bot.message_handler(func=lambda m: m.text == "🚀 استنساخ كامل")
def handle_full_clone(message):
    session = get_session(message.chat.id)
    if not session.get("profile_url"):
        bot.send_message(message.chat.id, "⚠️ أرسل رابط حساب تيك توك أولاً لتسجيله.")
        return
    bot.send_message(message.chat.id, "🚀 بدء عملية استنساخ الحساب (نظام 20 في 20)...")

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_text(message):
    # تسجيل الرابط إذا كان بروفايل
    profile_url = normalise_profile_input(message.text)
    if profile_url:
        session = get_session(message.chat.id)
        session["profile_url"] = profile_url
        bot.send_message(message.chat.id, f"✅ تم تسجيل الحساب: {profile_url}\nيمكنك الآن الضغط على زر (استنساخ كامل).")
    else:
        # هنا يتم استدعاء دالة التحميل الفردي
        bot.reply_to(message, "⏳ جارِ التحميل...")

# --- التشغيل ---
if __name__ == "__main__":
    os.makedirs("downloads", exist_ok=True)
    init_db()
    keep_alive()
    logger.info("البوت يعمل الآن...")
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            time.sleep(5)
