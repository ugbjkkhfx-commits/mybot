"""Single production entry point for the Telegram bot."""
import fcntl
import logging
import os
import time

import bot as downloader_bot
import channel_setup
import runtime_fixes

POLLING_LOCK_PATH = os.environ.get("BOT_POLLING_LOCK", "/tmp/mybot-telegram-polling.lock")
_polling_lock_handle = None


def acquire_polling_lock() -> None:
    """Prevent two bot pollers in the same host/container from consuming updates."""
    global _polling_lock_handle
    _polling_lock_handle = open(POLLING_LOCK_PATH, "w")
    try:
        fcntl.flock(_polling_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(
            "Another bot polling process already holds the local polling lock; "
            "run only one bot replica for this Telegram token"
        ) from exc


def main() -> None:
    acquire_polling_lock()
    channel_setup.install(downloader_bot)
    runtime_fixes.install(downloader_bot)
    downloader_bot.keep_alive()
    downloader_bot.logger.info("Bot is running with one local polling process")
    while True:
        try:
            downloader_bot.bot.infinity_polling(
                timeout=20,
                long_polling_timeout=10,
                allowed_updates=["message", "callback_query", "chat_member"],
                logger_level=logging.WARNING,
            )
        except Exception:
            downloader_bot.logger.exception("Polling failed; restarting in five seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
