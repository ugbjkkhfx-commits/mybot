import telebot
from telebot import types
import yt_dlp
import os

# بياناتك
BOT_TOKEN = "8131991575:AAGCjGh5dRX0vJXojsC9VgOZez0-RDRT3fM"
ADMIN_ID = 1520960859
bot = telebot.TeleBot(BOT_TOKEN)

# --- قواعد البيانات ---
users_data = set() # لحفظ المستخدمين (إحصائيات)

# --- دالة إرسال الأزرار المدمجة ---
def get_main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📥 تحميل دفعة واحدة", callback_data="batch_down"),
        types.InlineKeyboardButton("👤 استنساخ حساب", callback_data="clone_acc")
    )
    if user_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton("📊 لوحة الأدمن", callback_data="admin_panel"),
            types.InlineKeyboardButton("📡 إذاعة", callback_data="broadcast")
        )
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    users_data.add(message.chat.id)
    bot.send_message(message.chat.id, "أهلاً بك! استخدم الأزرار أدناه:", reply_markup=get_main_menu(message.from_user.id))

# --- منطق الاستنساخ والتحميل ---
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == "batch_down":
        bot.send_message(call.message.chat.id, "أرسل الروابط (رابط واحد في كل سطر) وسأحملها جميعاً.")
    elif call.data == "clone_acc":
        bot.send_message(call.message.chat.id, "أرسل 'اسم المستخدم' ورابط الحساب، سأقوم باستنساخه.")
    elif call.data == "admin_panel":
        if call.from_user.id == ADMIN_ID:
            bot.send_message(call.message.chat.id, f"عدد المستخدمين: {len(users_data)}")
    elif call.data == "broadcast":
        if call.from_user.id == ADMIN_ID:
            bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد إذاعتها للجميع.")

# --- معالجة الروابط (التحميل) ---
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    # إذا كانت رسالة نصية بسيطة
    if message.text.startswith("http"):
        links = message.text.split('\n')
        for link in links:
            if "tiktok" in link or "instagram" in link:
                bot.reply_to(message, f"⏳ جاري تحميل: {link}")
                # هنا يتم وضع منطق yt-dlp للتحميل
    
    # إذا كانت رسالة إذاعة من الأدمن
    if message.from_user.id == ADMIN_ID and message.reply_to_message:
        for user in users_data:
            try: bot.send_message(user, message.text)
            except: pass

bot.infinity_polling()
