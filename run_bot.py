"""Production entry point that installs channel setup before polling starts."""
import logging
import time

import bot as downloader_bot
import channel_setup

channel_setup.install(downloader_bot)

if __name__ == "__main__":
    downloader_bot.keep_alive()
    downloader_bot.bot.set_my_commands([
        downloader_bot.telebot.types.BotCommand("start", "رسالة البدء"),
        downloader_bot.telebot.types.BotCommand("help", "المساعدة"),
        downloader_bot.telebot.types.BotCommand("stats", "إحصائيات المالك"),
        downloader_bot.telebot.types.BotCommand("owner", "لوحة المالك"),
    ])
    downloader_bot.logger.info("Bot is running")
    while True:
        try:
            downloader_bot.bot.infinity_polling(
                timeout=20,
                long_polling_timeout=10,
                allowed_updates=["message", "callback_query", "chat_member"],
                logger_level=logging.WARNING,
            )
        except Exception as exc:
            downloader_bot.logger.error("Polling crashed; restarting in five seconds: %s", exc)
            time.sleep(5)
