"""
Demon Shadow Secretary Bot — Telegram Business auto-replies
Python 3.11+ | Flask + Requests

Required environment variables:
  BOT_TOKEN       BotFather token (keep secret)
  WEBHOOK_SECRET  Random secret (only A-Z, a-z, 0-9, _ and - allowed)
  RENDER_EXTERNAL_URL  (Optional) Your Render service URL
                       If not set, FALLBACK_URL below is used.

Run command on Render:
  gunicorn main:app
"""

import os
import re
import hmac
import sqlite3
import logging
from pathlib import Path

import requests
from flask import Flask, request, jsonify, abort

# -------------------- Configuration --------------------

# 👇 YAHAN APNA RENDER URL DAALO (fallback ke liye)
FALLBACK_URL = "https://your-service.onrender.com"

# 👇 Bot sirf itne messages tak reply karega (uske baad chup)
MAX_REPLIES_PER_CHAT = 3

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RAW_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

# Auto-sanitize: sirf allowed characters rakho
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "_", RAW_SECRET)

BASE_URL = (
    os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    or FALLBACK_URL.strip().rstrip("/")
)

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing WEBHOOK_SECRET environment variable.")

if not BASE_URL or BASE_URL == "https://your-service.onrender.com":
    raise RuntimeError(
        "RENDER_EXTERNAL_URL is not set AND FALLBACK_URL is not edited. "
        "Set the env var OR edit FALLBACK_URL in the code."
    )

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/tmp/demon_shadow_bot.sqlite3")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demon-shadow-bot")

# -------------------- Database --------------------

def get_db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                message_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY
            )
        """)


init_db()


def record_update_and_count(update_id, chat_id):
    """Return (is_new_update, message_count); avoids duplicate replies on webhook retries."""
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO processed_updates(update_id) VALUES (?)",
                (update_id,),
            )
        except sqlite3.IntegrityError:
            row = db.execute(
                "SELECT message_count FROM chat_state WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return False, row["message_count"] if row else 0

        db.execute("""
            INSERT INTO chat_state(chat_id, message_count)
            VALUES (?, 1)
            ON CONFLICT(chat_id)
            DO UPDATE SET message_count = message_count + 1
        """, (chat_id,))
        row = db.execute(
            "SELECT message_count FROM chat_state WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return True, row["message_count"]


# -------------------- Reply rules --------------------

INTRO_REPLY = (
    "☠️ 𝐇𝐄𝐋𝐋𝐎, 𝐒𝐓𝐑𝐀𝐍𝐆𝐄𝐑. 🖤\n\n"
    "I'm Shabbir's personal bot. 🕶️\n"
    "Who am I speaking with?"
)

BUSY_REPLY = (
    "🌑 𝐒𝐇𝐀𝐁𝐁𝐈𝐑 𝐈𝐒 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄. 🥀\n\n"
    "He's busy today and will be away for the next few days. "
    "Leave your message here; he'll get back to you when he's available. 🖤"
)

THIRD_REPLY = (
    "📩 𝐌𝐄𝐒𝐒𝐀𝐆𝐄 𝐑𝐄𝐂𝐄𝐈𝐕𝐄𝐃. 🖤\n\n"
    "Shabbir is away for a few days. Leave your message here, "
    "and he'll reply when he's available. 🌑"
)


def choose_reply(message, count):
    """
    Returns reply text for the first MAX_REPLIES_PER_CHAT messages.
    After that returns None → bot stays silent.
    """
    # Chup ho jao agar limit paar ho gayi
    if count > MAX_REPLIES_PER_CHAT:
        return None

    # 1st message
    if count == 1:
        return INTRO_REPLY

    # 2nd message
    if count == 2:
        return BUSY_REPLY

    # 3rd message
    return THIRD_REPLY


# -------------------- Telegram API helpers --------------------

def telegram_post(method, payload):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {data}")
    return data


def send_business_reply(connection_id, chat_id, text):
    telegram_post("sendMessage", {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "text": text,
    })


# -------------------- Webhook routes --------------------

@app.get("/")
def health_check():
    return "☠️ Demon Shadow Bot is running. 🖤", 200


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.post("/webhook")
def webhook():
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
        abort(403)

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")
    message = update.get("business_message")

    if not message:
        return jsonify(ok=True), 200

    chat = message.get("chat") or {}
    connection_id = message.get("business_connection_id")

    if chat.get("type") != "private" or not connection_id:
        return jsonify(ok=True), 200

    if message.get("is_outgoing", False):
        return jsonify(ok=True), 200

    chat_id = chat.get("id")
    if chat_id is None or update_id is None:
        return jsonify(ok=True), 200

    is_new, count = record_update_and_count(update_id, chat_id)
    if not is_new:
        return jsonify(ok=True), 200

    answer = choose_reply(message, count)

    # 👇 Bot chup rahega agar limit paar ho gayi
    if answer is None:
        logger.info(
            "Silent mode: chat_id=%s already got %s replies (limit=%s)",
            chat_id, count, MAX_REPLIES_PER_CHAT
        )
        return jsonify(ok=True), 200

    try:
        send_business_reply(connection_id, chat_id, answer)
    except (requests.RequestException, RuntimeError):
        logger.exception("Reply failed for chat_id=%s", chat_id)
        return jsonify(ok=False, error="reply_failed"), 200

    return jsonify(ok=True), 200


# Register the webhook when the app starts.
if BASE_URL:
    try:
        result = telegram_post("setWebhook", {
            "url": f"{BASE_URL}/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["business_connection", "business_message"],
        })
        logger.info("Telegram webhook configured: %s (url=%s/webhook)", result.get("ok"), BASE_URL)
    except Exception:
        logger.exception("Could not register webhook. Check BOT_TOKEN and BASE_URL.")
else:
    logger.warning("BASE_URL is empty; webhook registration skipped.")