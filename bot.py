import telebot
from telebot import types

# ضع التوكن الخاص بك هنا
BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859

bot = telebot.TeleBot(BOT_TOKEN)

# --- دالة الأزرار المدمجة (داخل الرسالة) ---
def get_inline_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # أزرار للجميع (داخل الرسالة)
    markup.add(
        types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch"),
        types.InlineKeyboardButton("استنساخ الحساب 👤", callback_data="clone")
    )
    
    # أزرار الأدمن (تظهر لك فقط داخل رسالتك)
    if user_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton("📊 لوحة التحكم", callback_data="panel"),
            types.InlineKeyboardButton("📡 إذاعة رسالة", callback_data="broadcast")
        )
    return markup

# --- رسالة الترحيب ---
@bot.message_handler(commands=['start'])
def start(message):
    welcome_text = (
        "مرحباً بك في بوت التحميل! 📥\n\n"
        "أرسل رابط أي فيديو وسأقوم بتحميله لك فوراً.\n"
        "يمكنك استخدام الأزرار أدناه للعمليات الكبيرة:"
    )
    # الأزرار ستظهر الآن داخل الرسالة كما طلبت
    bot.send_message(
        message.chat.id, 
        welcome_text, 
        reply_markup=get_inline_keyboard(message.from_user.id)
    )

# --- معالجة الأزرار (بدون أي اشتراك إجباري) ---
@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    # لا يوجد هنا أي تحقق من اشتراك!
    if call.data == "batch":
        bot.answer_callback_query(call.id, "أرسل الروابط دفعة واحدة")
        bot.send_message(call.message.chat.id, "📥 أرسل الروابط في رسالة واحدة.")
    elif call.data == "clone":
        bot.answer_callback_query(call.id, "جاري الاستنساخ")
        bot.send_message(call.message.chat.id, "👤 أرسل رابط الحساب.")
    elif call.data in ["panel", "broadcast"]:
        if call.from_user.id == ADMIN_ID:
            bot.answer_callback_query(call.id, "أهلاً أدمن")
        else:
            bot.answer_callback_query(call.id, "هذه الميزة للإدارة فقط", show_alert=True)

bot.infinity_polling()
