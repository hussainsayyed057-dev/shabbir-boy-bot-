"""
Demon Shadow Secretary Bot
Python 3.11+ | Flask + Requests

Env vars:
  BOT_TOKEN, WEBHOOK_SECRET, OWNER_ID, RENDER_EXTERNAL_URL (optional)

Run: gunicorn main:app
"""

import os, re, hmac, time, random, sqlite3, logging, threading
from pathlib import Path
import requests
from flask import Flask, request, jsonify, abort

# -------------------- Config --------------------
FALLBACK_URL = "https://your-service.onrender.com"
GALI_TRIGGERS = ["gali", "gaali", "gali de", "gali kar"]
RAID_TRIGGERS = ["raid", "raid kar", "raid de"]
DEFAULT_RAID_COUNT = 15
DEFAULT_RAID_DELAY = 0.4
DEFAULT_COOLDOWN = 1800
MAX_SLOTS = 20

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RAW_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "_", RAW_SECRET)

OWNER_ID = 0
_raw = os.environ.get("OWNER_ID", "").strip()
if _raw.isdigit():
    OWNER_ID = int(_raw)

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/") or FALLBACK_URL.strip().rstrip("/")

if not BOT_TOKEN: raise RuntimeError("Missing BOT_TOKEN")
if not WEBHOOK_SECRET: raise RuntimeError("Missing WEBHOOK_SECRET")
if not OWNER_ID: raise RuntimeError("Missing OWNER_ID (numeric Telegram user id)")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/tmp/demon_shadow_bot.sqlite3")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demon-shadow-bot")
BOT_START_TIME = time.time()

# -------------------- DB --------------------
def get_db():
    c = sqlite3.connect(DATABASE_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            session_count INTEGER NOT NULL DEFAULT 0,
            last_ts REAL NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS processed_updates (
            update_id INTEGER PRIMARY KEY
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS auto_replies (
            slot INTEGER PRIMARY KEY, text TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS gali_replies (
            slot INTEGER PRIMARY KEY, text TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS business_chats (
            chat_id INTEGER PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        # Default 3 auto replies seed
        if db.execute("SELECT COUNT(*) c FROM auto_replies").fetchone()["c"] == 0:
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (1,?)",
                ("☠️ 𝐇𝐄𝐋𝐋𝐎, 𝐒𝐓𝐑𝐀𝐍𝐆𝐄𝐑. 🖤\n\nI'm Shabbir's personal bot. 🕶️\nWho am I speaking with?",))
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (2,?)",
                ("🌑 𝐒𝐇𝐀𝐁𝐁𝐈𝐑 𝐈𝐒 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄. 🥀\n\nHe's busy today and will be away for the next few days. Leave your message here; he'll get back to you when he's available. 🖤",))
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (3,?)",
                ("📩 𝐌𝐄𝐒𝐒𝐀𝐆𝐄 𝐑𝐄𝐂𝐄𝐈𝐕𝐄𝐃. 🖤\n\nShabbir is away for a few days. Leave your message here, and he'll reply when he's available. 🌑",))

init_db()

def get_setting(k, d=None):
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return r["value"] if r else d

def set_setting(k, v):
    with get_db() as db:
        db.execute("""INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (k, str(v)))

def get_raid_count():
    try: return int(get_setting("raid_count", DEFAULT_RAID_COUNT))
    except: return DEFAULT_RAID_COUNT

def get_raid_delay():
    try: return float(get_setting("raid_delay", DEFAULT_RAID_DELAY))
    except: return DEFAULT_RAID_DELAY

def get_cooldown():
    try: return int(get_setting("cooldown", DEFAULT_COOLDOWN))
    except: return DEFAULT_COOLDOWN

def get_notify():
    return get_setting("notify", "1") == "1"

def record_update(uid):
    with get_db() as db:
        try:
            db.execute("INSERT INTO processed_updates(update_id) VALUES(?)", (uid,))
            return True
        except sqlite3.IntegrityError:
            return False

def remember_business_chat(cid):
    with get_db() as db:
        db.execute("""INSERT INTO business_chats(chat_id,last_seen) VALUES(?,CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""", (cid,))

def all_business_chat_ids():
    with get_db() as db:
        return [r["chat_id"] for r in db.execute("SELECT chat_id FROM business_chats").fetchall()]

# ---- Auto reply slots ----
def list_auto_replies():
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT slot, text FROM auto_replies ORDER BY slot").fetchall()]

def set_auto_reply(slot, text):
    with get_db() as db:
        db.execute("""INSERT INTO auto_replies(slot,text) VALUES(?,?)
            ON CONFLICT(slot) DO UPDATE SET text=excluded.text""", (slot, text))

def del_auto_reply(slot):
    with get_db() as db:
        return db.execute("DELETE FROM auto_replies WHERE slot=?", (slot,)).rowcount > 0

def clear_auto_replies():
    with get_db() as db:
        return db.execute("DELETE FROM auto_replies").rowcount

def get_auto_reply(slot):
    with get_db() as db:
        r = db.execute("SELECT text FROM auto_replies WHERE slot=?", (slot,)).fetchone()
        return r["text"] if r else None

# ---- Gali slots ----
def list_gali_replies():
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT slot, text FROM gali_replies ORDER BY slot").fetchall()]

def set_gali_reply(slot, text):
    with get_db() as db:
        db.execute("""INSERT INTO gali_replies(slot,text) VALUES(?,?)
            ON CONFLICT(slot) DO UPDATE SET text=excluded.text""", (slot, text))

def del_gali_reply(slot):
    with get_db() as db:
        return db.execute("DELETE FROM gali_replies WHERE slot=?", (slot,)).rowcount > 0

def clear_gali_replies():
    with get_db() as db:
        return db.execute("DELETE FROM gali_replies").rowcount

def get_all_gali_texts():
    with get_db() as db:
        return [r["text"] for r in db.execute("SELECT text FROM gali_replies ORDER BY slot").fetchall()]

# -------------------- Reply logic (business chat) --------------------
def decide_business_reply(chat_id):
    """
    Session flow:
      msg1 → slot 1
      msg2 → slot 2
      msg3 → slot 3
      msg4+ → silent
      After cooldown → reset counter
    """
    now = time.time()
    cooldown = get_cooldown()

    with get_db() as db:
        row = db.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            db.execute("INSERT INTO chat_state(chat_id) VALUES(?)", (chat_id,))
            session_count = 0
            last_ts = 0
        else:
            session_count = row["session_count"]
            last_ts = row["last_ts"]

        # Cooldown reset
        if last_ts == 0 or (now - last_ts) >= cooldown:
            session_count = 0

        session_count += 1
        reply = get_auto_reply(session_count)

        db.execute("""UPDATE chat_state SET session_count=?, last_ts=? WHERE chat_id=?""",
                   (session_count, now, chat_id))

    return reply

# -------------------- Owner notification --------------------
def notify_owner(chat_id, message):
    if not get_notify(): return
    fu = message.get("from") or {}
    first = fu.get("first_name") or ""
    last = fu.get("last_name") or ""
    full_name = (first + " " + last).strip() or "Unknown"
    username = fu.get("username")
    user_id = fu.get("id") or chat_id

    text = message.get("text") or message.get("caption") or ""
    if not text:
        for k in ["photo","video","voice","audio","document","sticker","location","contact","animation","video_note"]:
            if message.get(k):
                text = f"[{k}]"
                break
        if not text: text = "[non-text message]"
    if len(text) > 500: text = text[:500] + "... (truncated)"

    uname_line = f"@{username}" if username else "—"

    note = (
        "🔔 **NEW MESSAGE RECEIVED** 🖤\n\n"
        f"👤 **Name:** {full_name}\n"
        f"🆔 **User ID:** `{user_id}`\n"
        f"💬 **Username:** {uname_line}\n"
        f"📥 **Chat ID:** `{chat_id}`\n\n"
        f"📝 **Message:**\n{text}"
    )
    try:
        telegram_post("sendMessage", {"chat_id": OWNER_ID, "text": note, "parse_mode": "Markdown"})
    except Exception:
        logger.exception("Owner notify failed")

# -------------------- Mention parsing --------------------
def extract_mentions(message):
    mentions = []
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    for ent in entities:
        et, off, ln = ent.get("type"), ent.get("offset", 0), ent.get("length", 0)
        if et == "mention":
            u = text[off:off+ln].lstrip("@").lower()
            if u: mentions.append((None, u))
        elif et == "text_mention":
            us = ent.get("user") or {}
            mentions.append((us.get("id"), (us.get("username") or "").lower() or None))
    rtm = message.get("reply_to_message")
    if rtm:
        us = rtm.get("from") or {}
        if us.get("id") or us.get("username"):
            mentions.append((us.get("id"), (us.get("username") or "").lower() or None))
    seen, out = set(), []
    for m in mentions:
        if m not in seen:
            seen.add(m); out.append(m)
    return out

def text_has(text, triggers):
    t = (text or "").lower()
    return any(w in t for w in triggers)

# -------------------- Raid engine --------------------
def tag_for(uid, uname):
    if uname: return f"@{uname}", None
    if uid: return f'<a href="tg://user?id={uid}">user</a>', "HTML"
    return None, None

def run_raid(chat_id, uid, uname, reply_to_id=None):
    texts = get_all_gali_texts()
    if not texts: return
    tag, pm = tag_for(uid, uname)
    if not tag: return
    count = get_raid_count(); delay = get_raid_delay()
    for i in range(count):
        t = random.choice(texts)
        payload = {"chat_id": chat_id, "text": f"{tag} {t}"}
        if pm: payload["parse_mode"] = pm
        if i == 0 and reply_to_id:
            payload["reply_to_message_id"] = reply_to_id
            payload["allow_sending_without_reply"] = True
        try: telegram_post("sendMessage", payload)
        except Exception: logger.exception("Raid failed")
        time.sleep(delay)

def run_single_gali(chat_id, uid, uname, reply_to_id=None):
    texts = get_all_gali_texts()
    if not texts: return
    tag, pm = tag_for(uid, uname)
    if not tag: return
    t = random.choice(texts)
    payload = {"chat_id": chat_id, "text": f"{tag} {t}"}
    if pm: payload["parse_mode"] = pm
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id
        payload["allow_sending_without_reply"] = True
    try: telegram_post("sendMessage", payload)
    except Exception: logger.exception("Single gali failed")

def handle_group_message(message):
    sender_id = (message.get("from") or {}).get("id")
    if sender_id != OWNER_ID: return False  # Only owner can trigger

    text = message.get("text") or message.get("caption") or ""
    if not text: return False

    is_gali = text_has(text, GALI_TRIGGERS)
    is_raid = text_has(text, RAID_TRIGGERS)
    if not (is_gali or is_raid): return False

    mentions = extract_mentions(message)
    if not mentions: return False

    chat_id = (message.get("chat") or {}).get("id")
    reply_to_id = message.get("message_id")

    for uid, uname in mentions:
        if uid == OWNER_ID: continue
        if is_raid:
            threading.Thread(target=run_raid, args=(chat_id, uid, uname, reply_to_id), daemon=True).start()
        else:
            threading.Thread(target=run_single_gali, args=(chat_id, uid, uname, reply_to_id), daemon=True).start()
    return True

# -------------------- Telegram API --------------------
def telegram_post(method, payload):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {data}")
    return data

def send_business_reply(conn_id, chat_id, text):
    telegram_post("sendMessage", {
        "business_connection_id": conn_id, "chat_id": chat_id, "text": text,
    })

def send_dm(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    telegram_post("sendMessage", payload)

def human_uptime():
    s = int(time.time() - BOT_START_TIME)
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return f"{d}d {h}h {m}m {s}s"

def human_cd(sec):
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec//60}m {sec%60}s"
    return f"{sec//3600}h {(sec%3600)//60}m"

# -------------------- Owner DM commands --------------------
HELP_TEXT = (
    "☠️ **Demon Shadow Bot — Commands** 🖤\n\n"
    "**Basics**\n"
    "/alive — Status\n"
    "/myid — Apna Telegram ID dekhne ke liye\n"
    "/ping, /help\n\n"
    "**Auto-reply slots (business chat)**\n"
    "`/setreply 1 <text>` — slot 1 ka reply\n"
    "`/setreply 2 <text>` — slot 2\n"
    "`/setreply N <text>` — N=1..20\n"
    "`/delreply N`\n"
    "`/listreply`\n\n"
    "**Gali slots (GC me use hote hain)**\n"
    "`/setgali 1 <text>`\n"
    "`/setgali N <text>` — N=1..20\n"
    "`/delgali N`\n"
    "`/listgali`\n"
    "`/cleargali`\n\n"
    "**Raid settings**\n"
    "/raidcount N, /raiddelay X\n\n"
    "**Cooldown**\n"
    "/setcooldown SECONDS\n\n"
    "**Notifications**\n"
    "/notify on|off\n\n"
    "**GC me use**\n"
    "`@target gali` → 1 random gali\n"
    "`@target raid` → 🔥 RAID (N times)\n\n"
    "**Chats**\n"
    "/chats, /broadcast <text>"
)

def handle_owner_command(msg):
    sender_id = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()

    # Debug: har DM log karo
    logger.info("DM from id=%s chat=%s text=%r", sender_id, chat_id, text[:80])

    if not text: return
    parts = text.split(None, 2)
    cmd = parts[0].lower().split("@")[0]

    # /myid — kisi bhi user ke liye (debug)
    if cmd == "/myid":
        send_dm(chat_id, f"🆔 Your Telegram ID: `{sender_id}`\nChat ID: `{chat_id}`", parse_mode="Markdown")
        return

    # Baaki sab commands sirf OWNER ke liye
    if sender_id != OWNER_ID:
        logger.info("Ignored DM from non-owner id=%s (OWNER_ID=%s)", sender_id, OWNER_ID)
        return

    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        send_dm(OWNER_ID, "☠️ Demon Shadow Bot online. 🖤\n/help, /alive try karo.")
        return
    if cmd == "/ping":
        send_dm(OWNER_ID, "🏓 Pong! 🖤"); return
    if cmd == "/help":
        send_dm(OWNER_ID, HELP_TEXT); return
    if cmd == "/alive":
        send_dm(OWNER_ID,
            f"🖤 𝐁𝐎𝐓 𝐈𝐒 𝐀𝐋𝐈𝐕𝐄 🌑\n\n"
            f"⏱ Uptime: {human_uptime()}\n"
            f"🕐 Cooldown: {human_cd(get_cooldown())}\n"
            f"🔔 Notify: {'ON' if get_notify() else 'OFF'}\n"
            f"🔥 Raid: {get_raid_count()} @ {get_raid_delay()}s\n"
            f"📋 Auto replies: {len(list_auto_replies())}\n"
            f"🔥 Gali slots: {len(list_gali_replies())}")
        return

    # /setreply <slot> <text>
    if cmd == "/setreply":
        m = re.match(r"^(\d+)\s+(.+)$", text[len(parts[0]):].strip(), re.S)
        if not m:
            send_dm(OWNER_ID, "Format: `/setreply <slot> <text>`\nExample: `/setreply 1 Hello boss`", parse_mode="Markdown")
            return
        slot = int(m.group(1)); txt = m.group(2).strip()
        if slot < 1 or slot > MAX_SLOTS:
            send_dm(OWNER_ID, f"Slot 1-{MAX_SLOTS} ke beech hona chahiye.")
            return
        set_auto_reply(slot, txt)
        send_dm(OWNER_ID, f"✅ Auto reply slot {slot} set.")
        return

    if cmd == "/delreply":
        if not arg.isdigit():
            send_dm(OWNER_ID, "Format: `/delreply <slot>`")
            return
        if del_auto_reply(int(arg)):
            send_dm(OWNER_ID, f"🗑️ Slot {arg} deleted.")
        else:
            send_dm(OWNER_ID, f"❌ Slot {arg} me kuch nahi.")
        return

    if cmd == "/listreply":
        rows = list_auto_replies()
        if not rows:
            send_dm(OWNER_ID, "📭 Koi auto reply nahi.")
            return
        body = "\n\n".join(f"*Slot {r['slot']}*\n{r['text']}" for r in rows)
        send_dm(OWNER_ID, f"📋 Auto replies ({len(rows)})\n\n{body}")
        return

    if cmd == "/cleartreply" or cmd == "/clearreply":
        n = clear_auto_replies()
        send_dm(OWNER_ID, f"🧹 {n} auto replies cleared.")
        return

    # /setgali <slot> <text>
    if cmd == "/setgali":
        m = re.match(r"^(\d+)\s+(.+)$", text[len(parts[0]):].strip(), re.S)
        if not m:
            send_dm(OWNER_ID, "Format: `/setgali <slot> <text>`\nExample: `/setgali 1 chup rah bhosdi`", parse_mode="Markdown")
            return
        slot = int(m.group(1)); txt = m.group(2).strip()
        if slot < 1 or slot > MAX_SLOTS:
            send_dm(OWNER_ID, f"Slot 1-{MAX_SLOTS} ke beech hona chahiye.")
            return
        set_gali_reply(slot, txt)
        send_dm(OWNER_ID, f"🔥 Gali slot {slot} set.")
        return

    if cmd == "/delgali":
        if not arg.isdigit():
            send_dm(OWNER_ID, "Format: `/delgali <slot>`")
            return
        if del_gali_reply(int(arg)):
            send_dm(OWNER_ID, f"🗑️ Gali slot {arg} deleted.")
        else:
            send_dm(OWNER_ID, f"❌ Gali slot {arg} me kuch nahi.")
        return

    if cmd == "/listgali":
        rows = list_gali_replies()
        if not rows:
            send_dm(OWNER_ID, "📭 Koi gali nahi.")
            return
        body = "\n".join(f"*{r['slot']}.* {r['text']}" for r in rows)
        send_dm(OWNER_ID, f"🔥 Gali slots ({len(rows)})\n\n{body}")
        return

    if cmd == "/cleargali":
        n = clear_gali_replies()
        send_dm(OWNER_ID, f"🧹 {n} gali cleared.")
        return

    if cmd == "/raidcount":
        if not arg.isdigit() or not (1 <= int(arg) <= 100):
            send_dm(OWNER_ID, f"Format: `/raidcount N` (1-100). Current: {get_raid_count()}")
            return
        set_setting("raid_count", int(arg))
        send_dm(OWNER_ID, f"✅ Raid count: {arg}")
        return

    if cmd == "/raiddelay":
        try:
            v = float(arg)
            if not (0.1 <= v <= 5): raise ValueError
        except:
            send_dm(OWNER_ID, f"Format: `/raiddelay X` (0.1-5). Current: {get_raid_delay()}")
            return
        set_setting("raid_delay", v)
        send_dm(OWNER_ID, f"✅ Raid delay: {v}s")
        return

    if cmd == "/setcooldown":
        if not arg.isdigit() or not (60 <= int(arg) <= 86400):
            send_dm(OWNER_ID, f"Format: `/setcooldown SECS` (60-86400). Current: {get_cooldown()}s")
            return
        set_setting("cooldown", int(arg))
        send_dm(OWNER_ID, f"✅ Cooldown: {arg}s ({human_cd(int(arg))})")
        return

    if cmd == "/notify":
        v = arg.lower()
        if v == "on":
            set_setting("notify", "1"); send_dm(OWNER_ID, "🔔 Notifications ON.")
        elif v == "off":
            set_setting("notify", "0"); send_dm(OWNER_ID, "🔕 Notifications OFF.")
        else:
            send_dm(OWNER_ID, f"Format: `/notify on|off`. Current: {'ON' if get_notify() else 'OFF'}")
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
            send_dm(OWNER_ID, "Format: `/broadcast <text>`")
            return
        chats = all_business_chat_ids()
        sent = failed = 0
        for cid in chats:
            try: send_dm(cid, arg); sent += 1
            except: failed += 1
        send_dm(OWNER_ID, f"📢 Sent: {sent}, Failed: {failed}")
        return

    send_dm(OWNER_ID, "❓ Unknown command. /help dekho.")

# -------------------- Webhook --------------------
@app.get("/")
def health_check():
    return "☠️ Demon Shadow Bot is running. 🖤", 200

@app.get("/health")
def health():
    return jsonify(status="ok"), 200

@app.post("/webhook")
def webhook():
    recv = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(recv, WEBHOOK_SECRET):
        abort(403)

    update = request.get_json(silent=True) or {}

    # Normal message (DM / group)
    m = update.get("message")
    if m:
        chat = m.get("chat") or {}
        ct = chat.get("type")
        if ct == "private":
            try: handle_owner_command(m)
            except Exception: logger.exception("Owner cmd failed")
            return jsonify(ok=True), 200
        if ct in ("group", "supergroup"):
            try: handle_group_message(m)
            except Exception: logger.exception("Group handler failed")
            return jsonify(ok=True), 200

    # Business message
    bm = update.get("business_message")
    if not bm:
        return jsonify(ok=True), 200

    uid = update.get("update_id")
    chat = bm.get("chat") or {}
    conn_id = bm.get("business_connection_id")

    if chat.get("type") != "private" or not conn_id:
        return jsonify(ok=True), 200
    if bm.get("is_outgoing", False):
        return jsonify(ok=True), 200

    chat_id = chat.get("id")
    if chat_id is None or uid is None:
        return jsonify(ok=True), 200

    if not record_update(uid):
        return jsonify(ok=True), 200

    remember_business_chat(chat_id)

    # Notify owner
    threading.Thread(target=notify_owner, args=(chat_id, bm), daemon=True).start()

    # Auto reply
    answer = decide_business_reply(chat_id)
    if answer is None:
        logger.info("Silent: chat=%s", chat_id)
        return jsonify(ok=True), 200

    try:
        send_business_reply(conn_id, chat_id, answer)
    except Exception:
        logger.exception("Reply failed for chat=%s", chat_id)
        return jsonify(ok=False, error="reply_failed"), 200

    return jsonify(ok=True), 200

# -------------------- Register webhook --------------------
if BASE_URL:
    try:
        r = telegram_post("setWebhook", {
            "url": f"{BASE_URL}/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["business_connection", "business_message", "message"],
        })
        logger.info("Webhook set: %s url=%s/webhook", r.get("ok"), BASE_URL)
    except Exception:
        logger.exception("Webhook registration failed")
else:
    logger.warning("BASE_URL empty; webhook skipped.")