"""Production entry point: `python run.py` serves the app with Waitress and runs the daily reminder check."""

import logging
import os

from waitress import serve

from ingatmaintain import create_app
from ingatmaintain.reminders import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()

if __name__ == "__main__":
    if app.config["REMINDER_SCHEDULER"]:
        start_scheduler(app)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    logging.getLogger(__name__).info("IngatMaintain listening on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=8)
