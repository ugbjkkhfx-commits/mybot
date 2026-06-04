import telebot
from telebot import types
import sqlite3

BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
bot = telebot.TeleBot(BOT_TOKEN)

# --- دالة بناء الأزرار الذكية ---
def get_user_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    # أزرار للجميع
    markup.add(
        types.KeyboardButton("📥 تحميل دفعة واحدة"),
        types.KeyboardButton("🚀 استنساخ كامل")
    )
    # شرط الأدمن (لن تظهر هذه الأزرار لأي شخص غيرك)
    if user_id == ADMIN_ID:
        markup.add(
            types.KeyboardButton("📊 لوحة التحكم"),
            types.KeyboardButton("📡 إذاعة رسالة")
        )
    return markup

# --- رسالة الترحيب ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "مرحباً بك في بوت التحميل! 📥\n\n"
        "ما يمكنك تحميله:\n"
        "🎵 تيك توك\n📷 إنستغرام\n📖 فيسبوك\n📌 بينتريست\n\n"
        "استخدم الأزرار أدناه للبدء:"
    )
    bot.send_message(
        message.chat.id, 
        welcome_text, 
        reply_markup=get_user_keyboard(message.from_user.id)
    )

# --- منع ظهور أزرار الأدمن للمستخدمين ---
@bot.message_handler(func=lambda m: m.text in ["📊 لوحة التحكم", "📡 إذاعة رسالة"])
def admin_only(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⚠️ عذراً، هذه الخاصية للأدمن فقط.")
    else:
        # هنا تضع منطق لوحة التحكم الخاصة بك
        bot.reply_to(message, "أهلاً بك يا أدمن في لوحة التحكم.")

# --- باقي منطق البوت (التحميل والاستنساخ) ---
@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    if message.text == "📥 تحميل دفعة واحدة":
        bot.reply_to(message, "أرسل الروابط وسأبدأ التحميل.")
    elif message.text == "🚀 استنساخ كامل":
        bot.reply_to(message, "أرسل رابط الحساب أولاً.")

bot.infinity_polling()
