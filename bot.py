import telebot
from telebot import types

# هذا التوكن الخاص بك
BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859

bot = telebot.TeleBot(BOT_TOKEN)

# --- الكيبورد الوحيد في البوت ---
def get_keyboard(user_id):
    # ننشئ لوحة مفاتيح فارغة
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    
    # الأزرار العامة
    markup.add(
        types.KeyboardButton("📥 تحميل دفعة واحدة"),
        types.KeyboardButton("👤 استنساخ استنساخ")
    )
    
    # الأزرار الإدارية (لن تضاف إلا للأدمن فقط)
    if user_id == ADMIN_ID:
        markup.add(
            types.KeyboardButton("📊 لوحة التحكم"),
            types.KeyboardButton("📡 إذاعة رسالة")
        )
    return markup

# --- رسالة الترحيب ---
@bot.message_handler(commands=['start'])
def start(message):
    # نقوم بإرسال الكيبورد الخاص بهذا المستخدم تحديداً
    bot.send_message(
        message.chat.id, 
        "مرحباً بك! أرسل الرابط مباشرة للتحميل.", 
        reply_markup=get_keyboard(message.from_user.id)
    )

# --- معالجة الأزرار (حماية الأزرار الإدارية) ---
@bot.message_handler(func=lambda m: True)
def handle_msg(message):
    if message.text in ["📊 لوحة التحكم", "📡 إذاعة رسالة"]:
        if message.from_user.id != ADMIN_ID:
            # إذا ضغط مستخدم عادي على زر إدارة، لا يفعل البوت شيئاً أو يتجاهله
            return
        else:
            bot.reply_to(message, "مرحباً أدمن.")
    else:
        # هنا يكمل باقي عمل البوت الخاص بك
        pass

bot.infinity_polling()
