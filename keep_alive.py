from flask import Flask
from threading import Thread

app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive!"


def run():
    app.run(host='0.0.0.0', port=8080)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

    # Install integrations only after bot.py has constructed its TeleBot and
    # registered the existing handlers. The existing Flask service is unchanged.
    try:
        import sys
        import owner_backup_integration

        module = sys.modules.get("__main__")
        if module is None or not hasattr(module, "bot"):
            module = sys.modules.get("bot")
        if module is not None:
            owner_backup_integration.install(module)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not install owner/backup integration")
