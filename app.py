"""
Demon Shadow Secretary Bot — Telegram Business + Group Raid
Python 3.11+ | Flask + Requests

Required environment variables:
  BOT_TOKEN       BotFather token
  WEBHOOK_SECRET  Random secret (only A-Z, a-z, 0-9, _ and - allowed)
  OWNER_ID        Tumhara Telegram user ID (numeric)
  RENDER_EXTERNAL_URL  (Optional) Render service URL

Run command on Render:
  gunicorn main:app
"""

import os
import re
import hmac
import time
import random
import sqlite3
import logging
import threading
from pathlib import Path

import requests
from flask import Flask, request, jsonify, abort

# -------------------- Configuration --------------------

FALLBACK_URL = "https://your-service.onrender.com"

GALI_TRIGGERS = ["gali", "gaali", "gali de", "gali kar"]
RAID_TRIGGERS = ["raid", "raid kar", "raid de"]

DEFAULT_RAID_COUNT = 15
DEFAULT_RAID_DELAY = 0.4
DEFAULT_COOLDOWN = 1800  # 30 min

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RAW_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "_", RAW_SECRET)

OWNER_ID = 0
_owner_raw = os.environ.get("OWNER_ID", "").strip()
if _owner_raw.isdigit():
    OWNER_ID = int(_owner_raw)

BASE_URL = (
    os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    or FALLBACK_URL.strip().rstrip("/")
)

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing WEBHOOK_SECRET environment variable.")
if not OWNER_ID:
    raise RuntimeError("Missing or invalid OWNER_ID environment variable.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/tmp/demon_shadow_bot.sqlite3")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demon-shadow-bot")

BOT_START_TIME = time.time()

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
                message_count INTEGER NOT NULL DEFAULT 0,
                intro_sent INTEGER NOT NULL DEFAULT 0,
                session_count INTEGER NOT NULL DEFAULT 0,
                last_ts REAL NOT NULL DEFAULT 0
            )
        """)
        for col, defval in [
            ("intro_sent", "INTEGER NOT NULL DEFAULT 0"),
            ("session_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_ts", "REAL NOT NULL DEFAULT 0"),
        ]:
            try:
                db.execute(f"ALTER TABLE chat_state ADD COLUMN {col} {defval}")
            except sqlite3.OperationalError:
                pass

        db.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS custom_replies (
                keyword TEXT PRIMARY KEY,
                reply TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS gali_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id INTEGER,
                target_username TEXT,
                trigger_word TEXT NOT NULL,
                reply TEXT NOT NULL,
                UNIQUE(target_user_id, target_username, trigger_word, reply)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS business_chats (
                chat_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


init_db()


def get_setting(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as db:
        db.execute("""
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def get_raid_count():
    try:
        return int(get_setting("raid_count", DEFAULT_RAID_COUNT))
    except Exception:
        return DEFAULT_RAID_COUNT


def get_raid_delay():
    try:
        return float(get_setting("raid_delay", DEFAULT_RAID_DELAY))
    except Exception:
        return DEFAULT_RAID_DELAY


def get_cooldown():
    try:
        return int(get_setting("cooldown", DEFAULT_COOLDOWN))
    except Exception:
        return DEFAULT_COOLDOWN


def get_notify_enabled():
    return get_setting("notify", "1") == "1"


def record_update(update_id):
    with get_db() as db:
        try:
            db.execute("INSERT INTO processed_updates(update_id) VALUES (?)", (update_id,))
            return True
        except sqlite3.IntegrityError:
            return False


def ensure_chat_row(chat_id):
    with get_db() as db:
        db.execute("""
            INSERT INTO chat_state(chat_id) VALUES (?)
            ON CONFLICT(chat_id) DO NOTHING
        """, (chat_id,))


def get_chat_state(chat_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None


def bump_message_count(chat_id):
    with get_db() as db:
        db.execute("""
            INSERT INTO chat_state(chat_id, message_count) VALUES (?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET message_count = message_count + 1
        """, (chat_id,))


def remember_business_chat(chat_id):
    with get_db() as db:
        db.execute("""
            INSERT INTO business_chats(chat_id, last_seen)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP
        """, (chat_id,))


def all_business_chat_ids():
    with get_db() as db:
        return [r["chat_id"] for r in db.execute("SELECT chat_id FROM business_chats").fetchall()]


def get_custom_replies():
    with get_db() as db:
        return [(r["keyword"], r["reply"]) for r in db.execute(
            "SELECT keyword, reply FROM custom_replies").fetchall()]


def set_custom_reply(keyword, reply):
    with get_db() as db:
        db.execute("""
            INSERT INTO custom_replies(keyword, reply) VALUES (?, ?)
            ON CONFLICT(keyword) DO UPDATE SET reply = excluded.reply
        """, (keyword, reply))


def del_custom_reply(keyword):
    with get_db() as db:
        return db.execute("DELETE FROM custom_replies WHERE keyword = ?", (keyword,)).rowcount > 0


def clear_custom():
    with get_db() as db:
        return db.execute("DELETE FROM custom_replies").rowcount


def parse_target(token):
    token = token.strip()
    if token.startswith("@") and len(token) > 1:
        return None, token[1:].lower()
    if token.lstrip("-").isdigit():
        return int(token), None
    return None, token.lower()


def add_gali_rule(target_id, target_username, trigger_word, reply):
    with get_db() as db:
        db.execute("""
            INSERT OR IGNORE INTO gali_rules(target_user_id, target_username, trigger_word, reply)
            VALUES (?, ?, ?, ?)
        """, (target_id, target_username, trigger_word.lower(), reply))


def del_gali_rule(target_id, target_username):
    with get_db() as db:
        if target_id is not None:
            return db.execute(
                "DELETE FROM gali_rules WHERE target_user_id = ?", (target_id,)
            ).rowcount
        return db.execute(
            "DELETE FROM gali_rules WHERE LOWER(target_username) = ?", (target_username.lower(),)
        ).rowcount


def list_gali_rules(target_id=None, target_username=None):
    with get_db() as db:
        if target_id is not None:
            rows = db.execute(
                "SELECT * FROM gali_rules WHERE target_user_id = ?", (target_id,)
            ).fetchall()
        elif target_username is not None:
            rows = db.execute(
                "SELECT * FROM gali_rules WHERE LOWER(target_username) = ?", (target_username.lower(),)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM gali_rules ORDER BY target_username").fetchall()
        return [dict(r) for r in rows]


def gali_rules_for_target(target_id=None, target_username=None):
    with get_db() as db:
        query = "SELECT * FROM gali_rules WHERE 1=1"
        params = []
        if target_id is not None:
            query += " AND target_user_id = ?"
            params.append(target_id)
        if target_username:
            query += " AND LOWER(target_username) = ?"
            params.append(target_username.lower())
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def clear_gali_rules():
    with get_db() as db:
        return db.execute("DELETE FROM gali_rules").rowcount


# -------------------- Default replies --------------------

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


# -------------------- Owner notification --------------------

def notify_owner(user, chat_id, message):
    """DM owner in English whenever a business message arrives."""
    if not get_notify_enabled():
        return

    from_user = (message.get("from") or {})
    first = from_user.get("first_name") or ""
    last = from_user.get("last_name") or ""
    full_name = (first + " " + last).strip() or "Unknown"

    username = from_user.get("username")
    user_id = from_user.get("id") or chat_id

    text = message.get("text") or message.get("caption") or ""
    if not text:
        # Detect media type
        if message.get("photo"):
            text = "[photo]"
        elif message.get("video"):
            text = "[video]"
        elif message.get("voice"):
            text = "[voice]"
        elif message.get("audio"):
            text = "[audio]"
        elif message.get("document"):
            text = "[document]"
        elif message.get("sticker"):
            text = "[sticker]"
        elif message.get("location"):
            text = "[location]"
        else:
            text = "[non-text message]"

    # Truncate long messages
    if len(text) > 500:
        text = text[:500] + "... (truncated)"

    uname_line = f"@{username}" if username else "—"

    notification = (
        "🔔 **NEW MESSAGE RECEIVED** 🖤\n\n"
        f"👤 **Name:** {full_name}\n"
        f"🆔 **User ID:** `{user_id}`\n"
        f"💬 **Username:** {uname_line}\n"
        f"📥 **Chat ID:** `{chat_id}`\n\n"
        f"📝 **Message:**\n{text}"
    )

    try:
        telegram_post("sendMessage", {
            "chat_id": OWNER_ID,
            "text": notification,
            "parse_mode": "Markdown",
        })
    except Exception:
        logger.exception("Owner notification failed")


# -------------------- Smart reply logic --------------------

def decide_business_reply(message, chat_id):
    text = (message.get("text") or message.get("caption") or "").strip().lower()
    if text:
        normalized = re.sub(r"\s+", " ", text)
        for kw, reply in get_custom_replies():
            if kw and kw in normalized:
                return reply

    ensure_chat_row(chat_id)
    state = get_chat_state(chat_id) or {}
    intro_sent = state.get("intro_sent", 0)
    session_count = state.get("session_count", 0)
    last_ts = state.get("last_ts", 0)
    now = time.time()

    cooldown = get_cooldown()

    if last_ts == 0 or (now - last_ts) >= cooldown:
        session_count = 0

    session_count += 1
    reply = None

    if not intro_sent:
        if session_count == 1:
            reply = INTRO_REPLY
            intro_sent = 1
        elif session_count == 2:
            reply = BUSY_REPLY
        elif session_count == 3:
            reply = THIRD_REPLY
    else:
        if session_count == 1:
            reply = BUSY_REPLY

    with get_db() as db:
        db.execute("""
            UPDATE chat_state
            SET intro_sent = ?, session_count = ?, last_ts = ?
            WHERE chat_id = ?
        """, (intro_sent, session_count, now, chat_id))

    return reply


# -------------------- Mention / trigger parsing --------------------

def extract_mentions(message):
    mentions = []
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    for ent in entities:
        etype = ent.get("type")
        offset = ent.get("offset", 0)
        length = ent.get("length", 0)
        if etype == "mention":
            chunk = text[offset:offset + length]
            uname = chunk.lstrip("@").lower()
            if uname:
                mentions.append((None, uname))
        elif etype == "text_mention":
            u = ent.get("user") or {}
            uid = u.get("id")
            uname = (u.get("username") or "").lower()
            mentions.append((uid, uname or None))
    rtm = message.get("reply_to_message")
    if rtm:
        u = rtm.get("from") or {}
        uid = u.get("id")
        uname = (u.get("username") or "").lower()
        if uid or uname:
            mentions.append((uid, uname or None))
    seen = set()
    out = []
    for m in mentions:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def text_has_trigger(text, triggers):
    t = (text or "").lower()
    return any(w in t for w in triggers)


# -------------------- Raid engine --------------------

def tag_for(target_id, target_username):
    if target_username:
        return f"@{target_username}", None
    if target_id:
        return f'<a href="tg://user?id={target_id}">user</a>', "HTML"
    return None, None


def run_raid(chat_id, target_id, target_username, reply_to_id=None):
    rules = []
    if target_id:
        rules = gali_rules_for_target(target_id=target_id)
    if not rules and target_username:
        rules = gali_rules_for_target(target_username=target_username)
    if not rules:
        return False

    tag, parse_mode = tag_for(target_id, target_username)
    if not tag:
        return False

    count = get_raid_count()
    delay = get_raid_delay()

    for i in range(count):
        rule = random.choice(rules)
        text = f"{tag} {rule['reply']}"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if i == 0 and reply_to_id:
            payload["reply_to_message_id"] = reply_to_id
            payload["allow_sending_without_reply"] = True
        try:
            telegram_post("sendMessage", payload)
        except Exception:
            logger.exception("Raid message failed")
        time.sleep(delay)
    return True


def run_single_gali(chat_id, target_id, target_username, reply_to_id=None):
    rules = []
    if target_id:
        rules = gali_rules_for_target(target_id=target_id)
    if not rules and target_username:
        rules = gali_rules_for_target(target_username=target_username)
    if not rules:
        return False

    rule = random.choice(rules)
    tag, parse_mode = tag_for(target_id, target_username)
    if not tag:
        return False

    text = f"{tag} {rule['reply']}"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id
        payload["allow_sending_without_reply"] = True
    try:
        telegram_post("sendMessage", payload)
        return True
    except Exception:
        logger.exception("Single gali failed")
        return False


def handle_group_message(message):
    sender_id = (message.get("from") or {}).get("id")
    if sender_id != OWNER_ID:
        return False

    text = (message.get("text") or message.get("caption") or "")
    if not text:
        return False

    is_gali = text_has_trigger(text, GALI_TRIGGERS)
    is_raid = text_has_trigger(text, RAID_TRIGGERS)
    if not (is_gali or is_raid):
        return False

    mentions = extract_mentions(message)
    if not mentions:
        return False

    chat_id = (message.get("chat") or {}).get("id")
    reply_to_id = message.get("message_id")

    for uid, uname in mentions:
        if uid == OWNER_ID:
            continue
        if is_raid:
            threading.Thread(
                target=run_raid,
                args=(chat_id, uid, uname, reply_to_id),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=run_single_gali,
                args=(chat_id, uid, uname, reply_to_id),
                daemon=True,
            ).start()
    return True


# -------------------- Telegram API helpers --------------------

def telegram_post(method, payload):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {data}")
    return data


def send_business_reply(connection_id, chat_id, text):
    telegram_post("sendMessage", {
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "text": text,
    })


def send_dm(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    telegram_post("sendMessage", payload)


def human_uptime():
    secs = int(time.time() - BOT_START_TIME)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    return f"{d}d {h}h {m}m {s}s"


def human_cooldown(sec):
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


# -------------------- Owner DM commands --------------------

HELP_TEXT = (
    "☠️ **Demon Shadow Bot — Commands** 🖤\n\n"
    "**Basics**\n"
    "/alive — Uptime + settings\n"
    "/start, /ping, /help\n\n"
    "**Notifications**\n"
    "/notify on   → message alerts ON\n"
    "/notify off  → message alerts OFF\n\n"
    "**Auto-reply flow**\n"
    "New user:\n"
    "  msg1 → Intro\n"
    "  msg2 → Busy\n"
    "  msg3 → Message received\n"
    "  msg4+ → silent\n"
    "Returning (after cooldown):\n"
    "  msg1 → Busy, then silent\n\n"
    "**Cooldown**\n"
    "/setcooldown SECONDS (60-86400, default 1800)\n\n"
    "**Custom keyword replies**\n"
    "/setreply keyword | reply\n"
    "/delreply keyword\n"
    "/list\n\n"
    "**Targeted Gali (DM)**\n"
    "`/gali @user trigger | reply`\n"
    "Bulk:\n`/gali @user`\n`trigger | reply`\n`trigger | reply`\n\n"
    "/delgali @user, /listgali [@user], /cleargali\n\n"
    "**Raid settings**\n"
    "/raidcount N, /raiddelay X\n\n"
    "**GC me use**\n"
    "`@target gali` → 1 gali\n"
    "`@target raid` → 🔥 RAID\n\n"
    "**Chats**\n"
    "/chats, /broadcast <text>, /clearall"
)


def parse_bulk_lines(lines):
    pairs = []
    for line in lines:
        if "|" not in line:
            continue
        kw, rep = line.split("|", 1)
        kw, rep = kw.strip().lower(), rep.strip()
        if kw and rep:
            pairs.append((kw, rep))
    return pairs


def extract_target_from_command(arg):
    m = re.match(r"^(@\S+|\-?\d+)\s+(.+)$", arg.strip(), flags=re.S)
    if not m:
        return None
    target_token = m.group(1)
    rest = m.group(2)
    if "|" not in rest:
        return None
    kw, rep = rest.split("|", 1)
    kw, rep = kw.strip().lower(), rep.strip()
    if not kw or not rep:
        return None
    tid, tuname = parse_target(target_token)
    return tid, tuname, kw, rep


def handle_owner_command(msg):
    sender_id = (msg.get("from") or {}).get("id")
    if sender_id != OWNER_ID:
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        send_dm(OWNER_ID, "☠️ Demon Shadow Bot online. 🖤\n/alive, /help try karo.")
        return

    if cmd == "/alive":
        send_dm(OWNER_ID,
                f"🖤 𝐁𝐎𝐓 𝐈𝐒 𝐀𝐋𝐈𝐕𝐄 🌑\n\n"
                f"⏱ Uptime: {human_uptime()}\n"
                f"🕐 Cooldown: {human_cooldown(get_cooldown())}\n"
                f"🔔 Notifications: {'ON' if get_notify_enabled() else 'OFF'}\n"
                f"🔥 Raid: {get_raid_count()} msgs @ {get_raid_delay()}s\n"
                f"📋 Custom replies: {len(get_custom_replies())}\n"
                f"🔥 Gali rules: {len(list_gali_rules())}")
        return

    if cmd == "/ping":
        send_dm(OWNER_ID, "🏓 Pong! Alive. 🖤")
        return

    if cmd == "/help":
        send_dm(OWNER_ID, HELP_TEXT)
        return

    if cmd == "/notify":
        val = arg.lower()
        if val == "on":
            set_setting("notify", "1")
            send_dm(OWNER_ID, "🔔 Notifications ON.")
        elif val == "off":
            set_setting("notify", "0")
            send_dm(OWNER_ID, "🔕 Notifications OFF.")
        else:
            send_dm(OWNER_ID, f"Format: `/notify on` ya `/notify off`.\nCurrent: {'ON' if get_notify_enabled() else 'OFF'}")
        return

    if cmd == "/setcooldown":
        if not arg.isdigit() or int(arg) < 60 or int(arg) > 86400:
            send_dm(OWNER_ID,
                    f"Format: `/setcooldown SECONDS` (60-86400).\n"
                    f"Current: {get_cooldown()}s ({human_cooldown(get_cooldown())})")
            return
        set_setting("cooldown", int(arg))
        send_dm(OWNER_ID, f"✅ Cooldown set: {int(arg)}s ({human_cooldown(int(arg))})")
        return

    if cmd == "/raidcount":
        if not arg.isdigit() or int(arg) < 1 or int(arg) > 100:
            send_dm(OWNER_ID, f"Format: `/raidcount N` (1-100). Current: {get_raid_count()}")
            return
        set_setting("raid_count", int(arg))
        send_dm(OWNER_ID, f"✅ Raid count set: {int(arg)}")
        return

    if cmd == "/raiddelay":
        try:
            v = float(arg)
            if v < 0.1 or v > 5:
                raise ValueError
        except Exception:
            send_dm(OWNER_ID, f"Format: `/raiddelay X` (0.1-5). Current: {get_raid_delay()}")
            return
        set_setting("raid_delay", v)
        send_dm(OWNER_ID, f"✅ Raid delay set: {v}s")
        return

    if cmd == "/setreply":
        if not arg:
            send_dm(OWNER_ID, "Format: `/setreply keyword | reply`")
            return
        pairs = parse_bulk_lines(arg.split("\n"))
        if not pairs:
            send_dm(OWNER_ID, "❌ Koi valid line nahi. `keyword | reply`")
            return
        for kw, rep in pairs:
            set_custom_reply(kw, rep)
        send_dm(OWNER_ID, f"✅ {len(pairs)} custom reply save.")
        return

    if cmd == "/delreply":
        if not arg:
            send_dm(OWNER_ID, "Format: `/delreply keyword`")
            return
        if del_custom_reply(arg.strip().lower()):
            send_dm(OWNER_ID, f"🗑️ `{arg}` delete.")
        else:
            send_dm(OWNER_ID, f"❌ `{arg}` nahi mila.")
        return

    if cmd == "/list":
        rows = get_custom_replies()
        if not rows:
            send_dm(OWNER_ID, "📭 Koi custom reply nahi.")
            return
        body = "\n".join(f"• `{k}` → {v}" for k, v in rows)
        send_dm(OWNER_ID, f"📋 Custom ({len(rows)})\n\n{body}")
        return

    if cmd == "/gali":
        if not arg:
            send_dm(OWNER_ID,
                    "Format:\n`/gali @user trigger | reply`\n\nBulk:\n"
                    "`/gali @user`\n`trigger | reply`\n`trigger | reply`")
            return

        lines = arg.split("\n")
        first_line = lines[0].strip()

        m_target = re.match(r"^(@\S+|\-?\d+)\s*$", first_line)
        if m_target and len(lines) > 1:
            tid, tuname = parse_target(m_target.group(1))
            pairs = parse_bulk_lines(lines[1:])
            if not pairs:
                send_dm(OWNER_ID, "❌ Bulk me `trigger | reply` line chahiye.")
                return
            for kw, rep in pairs:
                add_gali_rule(tid, tuname, kw, rep)
            label = f"@{tuname}" if tuname else f"ID {tid}"
            send_dm(OWNER_ID, f"🔥 {len(pairs)} gali add for {label}.")
            return

        parsed = extract_target_from_command(arg)
        if not parsed:
            send_dm(OWNER_ID, "❌ Format: `/gali @user trigger | reply`")
            return
        tid, tuname, kw, rep = parsed
        add_gali_rule(tid, tuname, kw, rep)
        label = f"@{tuname}" if tuname else f"ID {tid}"
        send_dm(OWNER_ID, f"🔥 Gali set for {label} — trigger: `{kw}`")
        return

    if cmd == "/delgali":
        if not arg:
            send_dm(OWNER_ID, "Format: `/delgali @user`")
            return
        tid, tuname = parse_target(arg.strip())
        n = del_gali_rule(tid, tuname)
        label = f"@{tuname}" if tuname else f"ID {tid}"
        send_dm(OWNER_ID, f"🗑️ {n} gali rules delete for {label}." if n else f"❌ {label} me kuch nahi mila.")
        return

    if cmd == "/listgali":
        if arg:
            tid, tuname = parse_target(arg.strip())
            rows = list_gali_rules(tid, tuname)
        else:
            rows = list_gali_rules()
        if not rows:
            send_dm(OWNER_ID, "📭 Koi gali rule nahi.")
            return
        lines = []
        for r in rows:
            tgt = f"@{r['target_username']}" if r["target_username"] else f"ID {r['target_user_id']}"
            lines.append(f"• {tgt} | `{r['trigger_word']}` → {r['reply']}")
        send_dm(OWNER_ID, f"🔥 Gali rules ({len(rows)})\n\n" + "\n".join(lines))
        return

    if cmd == "/cleargali":
        n = clear_gali_rules()
        send_dm(OWNER_ID, f"🧹 {n} gali rules delete.")
        return

    if cmd == "/clearall":
        a = clear_custom()
        b = clear_gali_rules()
        send_dm(OWNER_ID, f"🧹 Cleared: {a} custom + {b} gali.")
        return

    if cmd == "/chats":
        chats = all_business_chat_ids()
        if not chats:
            send_dm(OWNER_ID, "📭 Koi business chat nahi.")
            return
        body = "\n".join(f"• `{c}`" for c in chats[:50])
        send_dm(OWNER_ID, f"💬 Business chats ({len(chats)})\n\n{body}")
        return

    if cmd == "/broadcast":
        if not arg:
            send_dm(OWNER_ID, "Format: `/broadcast text`")
            return
        chats = all_business_chat_ids()
        sent = failed = 0
        for cid in chats:
            try:
                send_dm(cid, arg)
                sent += 1
            except Exception:
                failed += 1
        send_dm(OWNER_ID, f"📢 Sent: {sent}, Failed: {failed}")
        return

    send_dm(OWNER_ID, "❓ Unknown command. /help dekho.")


# -------------------- Webhook routes --------------------

@app.get("/")
def health_check():
    return "☠️ Demon Shadow Bot is running. 🖤", 200


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.post("/webhook")
def webhook():
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
        abort(403)

    update = request.get_json(silent=True) or {}

    dm = update.get("message")
    if dm:
        chat = dm.get("chat") or {}
        chat_type = chat.get("type")

        if chat_type == "private":
            try:
                handle_owner_command(dm)
            except Exception:
                logger.exception("Owner command failed")
            return jsonify(ok=True), 200

        if chat_type in ("group", "supergroup"):
            try:
                handle_group_message(dm)
            except Exception:
                logger.exception("Group handler failed")
            return jsonify(ok=True), 200

    message = update.get("business_message")
    if not message:
        return jsonify(ok=True), 200

    update_id = update.get("update_id")
    chat = message.get("chat") or {}
    connection_id = message.get("business_connection_id")

    if chat.get("type") != "private" or not connection_id:
        return jsonify(ok=True), 200
    if message.get("is_outgoing", False):
        return jsonify(ok=True), 200

    chat_id = chat.get("id")
    if chat_id is None or update_id is None:
        return jsonify(ok=True), 200

    if not record_update(update_id):
        return jsonify(ok=True), 200

    remember_business_chat(chat_id)
    bump_message_count(chat_id)

    # 👇 Owner ko notify karo (English me)
    threading.Thread(
        target=notify_owner,
        args=(None, chat_id, message),
        daemon=True,
    ).start()

    answer = decide_business_reply(message, chat_id)
    if answer is None:
        logger.info("Silent mode: chat_id=%s", chat_id)
        return jsonify(ok=True), 200

    try:
        send_business_reply(connection_id, chat_id, answer)
    except (requests.RequestException, RuntimeError):
        logger.exception("Reply failed for chat_id=%s", chat_id)
        return jsonify(ok=False, error="reply_failed"), 200

    return jsonify(ok=True), 200


# -------------------- Register webhook --------------------

if BASE_URL:
    try:
        result = telegram_post("setWebhook", {
            "url": f"{BASE_URL}/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": [
                "business_connection",
                "business_message",
                "message",
            ],
        })
        logger.info("Webhook configured: %s (url=%s/webhook)", result.get("ok"), BASE_URL)
    except Exception:
        logger.exception("Could not register webhook.")
else:
    logger.warning("BASE_URL empty; webhook skipped.")