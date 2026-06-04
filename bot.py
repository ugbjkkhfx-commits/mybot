import telebot
from telebot import types
import sqlite3

BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
BOT_NAME = "محمل الوسائط الشامل"

bot = telebot.TeleBot(BOT_TOKEN)

# --- إعداد قاعدة البيانات لتخزين نص الترحيب ---
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
cursor.execute('INSERT OR IGNORE INTO config VALUES ("welcome_msg", "مرحباً بك في البوت! أرسل رابطك للتحميل.")')
conn.commit()

def get_welcome_text():
    cursor.execute('SELECT value FROM config WHERE key="welcome_msg"')
    return cursor.fetchone()[0]

# --- الكيبورد ---
def get_main_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("📥 تحميل دفعة واحدة"), types.KeyboardButton("استنساخ الحساب 👤"))
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("📊 لوحة التحكم"))
    return markup

# --- البداية ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, get_welcome_text(), reply_markup=get_main_keyboard(message.from_user.id))

# --- لوحة التحكم ---
@bot.message_handler(func=lambda m: m.text == "📊 لوحة التحكم" and m.from_user.id == ADMIN_ID)
def admin_panel(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 تغيير رسالة الترحيب", callback_data="edit_welcome"))
    bot.reply_to(message, "⚙️ مرحباً أدمن، اختر ما تريد تعديله:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "edit_welcome")
def edit_welcome_step1(call):
    msg = bot.send_message(call.message.chat.id, "أرسل نص الترحيب الجديد الآن:")
    bot.register_next_step_handler(msg, save_new_welcome)

def save_new_welcome(message):
    cursor.execute('UPDATE config SET value=? WHERE key="welcome_msg"', (message.text,))
    conn.commit()
    bot.reply_to(message, "✅ تم تحديث رسالة الترحيب بنجاح!")

# --- معالجة البوت ---
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.text == "📥 تحميل دفعة واحدة":
        bot.reply_to(message, "أرسل الروابط في رسالة واحدة (كل رابط في سطر).")
    elif message.text == "👤 استنساخ استنساخ":
        bot.reply_to(message, "أرسل الحساب الحساب.")
    # باقي منطق التحميل...

bot.infinity_polling()
