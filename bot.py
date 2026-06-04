import telebot
from telebot import types
import yt_dlp
import os

BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
bot = telebot.TeleBot(BOT_TOKEN)

# --- دالة الأزرار المدمجة (Inline) ---
def get_inline_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch"),
        types.InlineKeyboardButton("👤 استنساخ الحساب", callback_data="clone")
    )
    if user_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton("📊 لوحة التحكم", callback_data="panel"),
            types.InlineKeyboardButton("📡 إذاعة رسالة", callback_data="broadcast")
        )
    return markup

# --- البداية ---
@bot.message_handler(commands=['start'])
def start(message):
    text = "مرحباً بك! أرسل رابط الفيديو مباشرة وسأقوم بتحميله لك."
    bot.send_message(message.chat.id, text, reply_markup=get_inline_keyboard(message.from_user.id))

# --- وظيفة التحميل الفعلي (yt-dlp) ---
def download_video(url, chat_id):
    bot.send_message(chat_id, "⏳ جاري المعالجة...")
    ydl_opts = {'format': 'best', 'outtmpl': '%(id)s.%(ext)s'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        
        with open(filename, 'rb') as video:
            bot.send_video(chat_id, video)
        os.remove(filename) # حذف الملف بعد الإرسال لتوفير مساحة
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ: {e}")

# --- معالجة الروابط والنصوص ---
@bot.message_handler(func=lambda m: True)
def handle_messages(message):
    if "tiktok.com" in message.text or "instagram.com" in message.text:
        download_video(message.text, message.chat.id)
    else:
        bot.reply_to(message, "أرسل رابط فيديو صحيح للبدء.")

# --- معالجة الأزرار (Inline) ---
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.data == "batch":
        bot.send_message(call.message.chat.id, "أرسل الروابط دفعة واحدة في رسالة.")
    elif call.data == "clone":
        bot.send_message(call.message.chat.id, "أرسل رابط الحساب.")
    elif call.data == "panel" and call.from_user.id == ADMIN_ID:
        bot.send_message(call.message.chat.id, "📊 لوحة التحكم: (أضف أوامرك هنا)")

bot.infinity_polling()
