import telebot
from telebot import types
import sqlite3

BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
bot = telebot.TeleBot(BOT_TOKEN)

# --- دالة بناء الأزرار المدمجة داخل الرسالة ---
def get_inline_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    # أزرار للجميع
    markup.add(
        types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch"),
        types.InlineKeyboardButton("🚀 استنساخ كامل", callback_data="clone")
    )
    # أزرار الأدمن (تظهر لك فقط)
    if user_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton("📊 لوحة التحكم", callback_data="panel"),
            types.InlineKeyboardButton("📡 إذاعة رسالة", callback_data="broadcast")
        )
    return markup

# --- رسالة الترحيب مع الأزرار ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "مرحباً بك في بوت التحميل! 📥\n\n"
        "ما يمكنك تحميله:\n"
        "🎵 تيك توك | 📷 إنستغرام | 📖 فيسبوك | 📌 بينتريست\n\n"
        "استخدم الأزرار أدناه للبدء:"
    )
    # هنا تظهر الأزرار داخل الرسالة كما طلبت
    bot.send_message(
        message.chat.id, 
        welcome_text, 
        reply_markup=get_inline_keyboard(message.from_user.id)
    )

# --- معالجة الضغط على الأزرار ---
@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    user_id = call.from_user.id
    
    if call.data == "batch":
        bot.answer_callback_query(call.id, "جاري البدء...")
        bot.send_message(call.message.chat.id, "أرسل روابط الفيديوهات دفعة واحدة 📥")
        
    elif call.data == "clone":
        bot.answer_callback_query(call.id, "جاري البدء...")
        bot.send_message(call.message.chat.id, "أرسل رابط الحساب أولاً للبدء في الاستنساخ 🚀")
        
    elif call.data == "panel" and user_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "أهلاً أدمن")
        bot.send_message(call.message.chat.id, "📊 لوحة التحكم: هنا ستظهر الإحصائيات.")
        
    elif call.data in ["panel", "broadcast"] and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⚠️ هذه الخاصية للأدمن فقط!")

bot.infinity_polling()
