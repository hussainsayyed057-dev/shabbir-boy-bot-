"""
Demon Shadow Secretary Bot — Premium style
Env: BOT_TOKEN, WEBHOOK_SECRET, OWNER_ID, RENDER_EXTERNAL_URL
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
DEFAULT_COOLDOWN = 60
MAX_SLOTS = 20

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
RAW_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "_", RAW_SECRET)

OWNER_ID = 0
_raw = os.environ.get("OWNER_ID", "").strip()
if _raw.isdigit(): OWNER_ID = int(_raw)

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/") or FALLBACK_URL.strip().rstrip("/")

if not BOT_TOKEN: raise RuntimeError("Missing BOT_TOKEN")
if not WEBHOOK_SECRET: raise RuntimeError("Missing WEBHOOK_SECRET")
if not OWNER_ID: raise RuntimeError("Missing OWNER_ID")

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
            intro_sent INTEGER NOT NULL DEFAULT 0,
            last_ts REAL NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS processed_updates (
            update_id INTEGER PRIMARY KEY
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS auto_replies (
            slot INTEGER PRIMARY KEY, text TEXT NOT NULL
        )""")
        # Gali targets — username + uid dono track
        db.execute("""CREATE TABLE IF NOT EXISTS gali_targets (
            target_uid INTEGER,
            target_username TEXT,
            slot INTEGER,
            text TEXT NOT NULL,
            PRIMARY KEY (target_uid, target_username, slot)
        )""")
        # Users jo bot ko DM kar chuke hain — unhe DM karne ke liye
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")

        if db.execute("SELECT COUNT(*) c FROM auto_replies").fetchone()["c"] == 0:
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (1,?)",
                ("☠️ 𝐇𝐄𝐋𝐋𝐎, 𝐒𝐓𝐑𝐀𝐍𝐆𝐄𝐑. 🖤\n\n𝐈'𝐦 𝐒𝐡𝐚𝐛𝐛𝐢𝐫'𝐬 𝐩𝐞𝐫𝐬𝐨𝐧𝐚𝐥 𝐛𝐨𝐭. 🕶️\n𝐖𝐡𝐨 𝐚𝐦 𝐈 𝐬𝐩𝐞𝐚𝐤𝐢𝐧𝐠 𝐰𝐢𝐭𝐡?",))
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (2,?)",
                ("🌑 𝐒𝐇𝐀𝐁𝐁𝐈𝐑 𝐈𝐒 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄. 🥀\n\n𝐇𝐞'𝐬 𝐛𝐮𝐬𝐲 𝐭𝐨𝐝𝐚𝐲 𝐚𝐧𝐝 𝐰𝐢𝐥𝐥 𝐛𝐞 𝐚𝐰𝐚𝐲 𝐟𝐨𝐫 𝐭𝐡𝐞 𝐧𝐞𝐱𝐭 𝐟𝐞𝐰 𝐝𝐚𝐲𝐬. 𝐋𝐞𝐚𝐯𝐞 𝐲𝐨𝐮𝐫 𝐦𝐞𝐬𝐬𝐚𝐠𝐞 𝐡𝐞𝐫𝐞; 𝐡𝐞'𝐥𝐥 𝐠𝐞𝐭 𝐛𝐚𝐜𝐤 𝐭𝐨 𝐲𝐨𝐮 𝐰𝐡𝐞𝐧 𝐡𝐞'𝐬 𝐚𝐯𝐚𝐢𝐥𝐚𝐛𝐥𝐞. 🖤",))
            db.execute("INSERT INTO auto_replies(slot,text) VALUES (3,?)",
                ("📩 𝐌𝐄𝐒𝐒𝐀𝐆𝐄 𝐑𝐄𝐂𝐄𝐈𝐕𝐄𝐃. 🖤\n\n𝐒𝐡𝐚𝐛𝐛𝐢𝐫 𝐢𝐬 𝐚𝐰𝐚𝐲 𝐟𝐨𝐫 𝐚 𝐟𝐞𝐰 𝐝𝐚𝐲𝐬. 𝐋𝐞𝐚𝐯𝐞 𝐲𝐨𝐮𝐫 𝐦𝐞𝐬𝐬𝐚𝐠𝐞 𝐡𝐞𝐫𝐞, 𝐚𝐧𝐝 𝐡𝐞'𝐥𝐥 𝐫𝐞𝐩𝐥𝐲 𝐰𝐡𝐞𝐧 𝐡𝐞'𝐬 𝐚𝐯𝐚𝐢𝐥𝐚𝐛𝐥𝐞. 🌑",))

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

def record_update(uid):
    with get_db() as db:
        try:
            db.execute("INSERT INTO processed_updates(update_id) VALUES(?)", (uid,))
            return True
        except sqlite3.IntegrityError:
            return False

# ---- Auto replies ----
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

# ---- Users tracking ----
def remember_user(from_user, chat_id):
    if not from_user: return
    uid = from_user.get("id")
    if not uid: return
    with get_db() as db:
        db.execute("""INSERT INTO users(user_id, chat_id, username, first_name, last_name, last_seen)
            VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=CURRENT_TIMESTAMP
        """, (uid, chat_id, from_user.get("username"), from_user.get("first_name"), from_user.get("last_name")))

def find_user(uid=None, username=None):
    with get_db() as db:
        if uid:
            r = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        elif username:
            r = db.execute("SELECT * FROM users WHERE LOWER(username)=?", (username.lower(),)).fetchone()
        else:
            return None
        return dict(r) if r else None

# ---- Gali targets ----
def set_gali(uid, username, slot, text):
    with get_db() as db:
        db.execute("""INSERT INTO gali_targets(target_uid, target_username, slot, text)
            VALUES(?,?,?,?)
            ON CONFLICT(target_uid, target_username, slot) DO UPDATE SET text=excluded.text
        """, (uid, (username or "").lower() or None, slot, text))

def del_gali_target(uid, username):
    with get_db() as db:
        if uid:
            return db.execute("DELETE FROM gali_targets WHERE target_uid=?", (uid,)).rowcount
        if username:
            return db.execute("DELETE FROM gali_targets WHERE LOWER(target_username)=?", (username.lower(),)).rowcount
        return 0

def list_gali_targets(uid=None, username=None):
    with get_db() as db:
        if uid:
            rows = db.execute("SELECT * FROM gali_targets WHERE target_uid=? ORDER BY slot", (uid,)).fetchall()
        elif username:
            rows = db.execute("SELECT * FROM gali_targets WHERE LOWER(target_username)=? ORDER BY slot", (username.lower(),)).fetchall()
        else:
            rows = db.execute("SELECT * FROM gali_targets ORDER BY target_username, slot").fetchall()
        return [dict(r) for r in rows]

def get_galis_for(uid=None, username=None):
    with get_db() as db:
        if uid:
            rows = db.execute("SELECT text FROM gali_targets WHERE target_uid=?", (uid,)).fetchall()
            if rows: return [r["text"] for r in rows]
        if username:
            rows = db.execute("SELECT text FROM gali_targets WHERE LOWER(target_username)=?", (username.lower(),)).fetchall()
            return [r["text"] for r in rows]
        return []

def clear_gali():
    with get_db() as db:
        return db.execute("DELETE FROM gali_targets").rowcount

# -------------------- Reply logic --------------------
def decide_reply(chat_id):
    """
    Flow:
      First ever msg → intro (slot 1)
      Same session 2nd msg → busy (slot 2)
      Same session 3rd msg → msg received (slot 3)
      4th+ → silent
      Cooldown (60s) baad → naya session shuru, intro skip (agar pehle bhej diya)
    """
    now = time.time()
    cooldown = get_cooldown()

    with get_db() as db:
        row = db.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            db.execute("INSERT INTO chat_state(chat_id) VALUES(?)", (chat_id,))
            session_count, intro_sent, last_ts = 0, 0, 0
        else:
            session_count = row["session_count"]
            intro_sent = row["intro_sent"]
            last_ts = row["last_ts"]

        # New session (after cooldown)
        if last_ts == 0 or (now - last_ts) >= cooldown:
            session_count = 0

        session_count += 1

        # Decide slot
        if not intro_sent:
            reply = get_auto_reply(1)
            intro_sent = 1
        else:
            # Intro already sent — start from slot 2
            effective = session_count + 1  # because slot 1 is skipped
            reply = get_auto_reply(effective)

        db.execute("""UPDATE chat_state SET session_count=?, intro_sent=?, last_ts=?
            WHERE chat_id=?""", (session_count, intro_sent, now, chat_id))
    return reply

# -------------------- GC: mentions --------------------
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

def tag_for(uid, uname):
    if uname: return f"@{uname}", None
    if uid: return f'<a href="tg://user?id={uid}">user</a>', "HTML"
    return None, None

# -------------------- Raid / gali in GC --------------------
def run_raid(chat_id, uid, uname, reply_to_id=None):
    texts = get_galis_for(uid=uid, username=uname)
    if not texts: return
    tag, pm = tag_for(uid, uname)
    if not tag: return
    count, delay = get_raid_count(), get_raid_delay()
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
    texts = get_galis_for(uid=uid, username=uname)
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
    if sender_id != OWNER_ID: return False
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

# -------------------- Gali DM engine --------------------
def send_gali_dm(target_uid, target_username, count=1):
    """Bot target ke UID pe DM karega gali."""
    texts = get_galis_for(uid=target_uid, username=target_username)
    if not texts:
        return 0, "no_gali"
    user = find_user(uid=target_uid) or find_user(username=target_username)
    if not user:
        return 0, "not_found"
    chat_id = user.get("chat_id") or user.get("user_id")
    if not chat_id:
        return 0, "no_chat"
    sent = 0
    for i in range(count):
        t = random.choice(texts)
        try:
            telegram_post("sendMessage", {"chat_id": chat_id, "text": t})
            sent += 1
        except Exception:
            logger.exception("Gali DM failed")
            break
        if count > 1:
            time.sleep(get_raid_delay())
    return sent, "ok"

# -------------------- Telegram API --------------------
def telegram_post(method, payload):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {data}")
    return data

def send_reply(chat_id, text):
    telegram_post("sendMessage", {"chat_id": chat_id, "text": text})

def send_business_reply(conn_id, chat_id, text):
    telegram_post("sendMessage", {"business_connection_id": conn_id, "chat_id": chat_id, "text": text})

def send_dm(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    telegram_post("sendMessage", payload)

def human_uptime():
    s = int(time.time() - BOT_START_TIME)
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return f"{d}d {h}h {m}m {s}s"

# -------------------- Owner commands --------------------
HELP_TEXT = (
    "☠️ **Demon Shadow Bot** 🖤\n\n"
    "**Basics**\n"
    "/alive, /myid, /ping, /help\n\n"
    "**Auto replies** (3 default)\n"
    "`/setreply 1 <text>`\n"
    "`/setreply 2 <text>`\n"
    "`/setreply 3 <text>`\n"
    "`/delreply N`, `/listreply`, `/clearreply`\n\n"
    "**Gali targets** (username + UID dono)\n"
    "`/gali @user <text>` — @user ki gali\n"
    "`/gali 12345 <text>` — UID se gali\n"
    "`/delgali @user` or `/delgali 12345`\n"
    "`/listgali [@user|12345]`\n"
    "`/cleargali`\n\n"
    "**Gali DM bhejo** (bot khud target ko DM karega)\n"
    "`/dmgali @user` — 1 gali DM\n"
    "`/raid @user` — raid DM (15 msgs)\n"
    "`/dmgali 12345` or `/raid 12345`\n\n"
    "**GC**\n"
    "`@target gali` → 1 gali GC me\n"
    "`@target raid` → raid GC me\n\n"
    "**Settings**\n"
    "/setcooldown SECS, /raidcount N, /raiddelay X"
)

def parse_target(token):
    token = token.strip()
    if token.startswith("@") and len(token) > 1:
        return None, token[1:].lower()
    if token.lstrip("-").isdigit():
        return int(token), None
    return None, token.lower()

def handle_owner_command(msg):
    sender_id = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text: return
    parts = text.split(None, 2)
    cmd = parts[0].lower().split("@")[0]

    if cmd == "/myid":
        send_dm(chat_id, f"🆔 Your ID: `{sender_id}`\nChat ID: `{chat_id}`", parse_mode="Markdown")
        return
    if sender_id != OWNER_ID: return

    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        send_dm(OWNER_ID, "☠️ Demon Shadow Bot online. 🖤\n/help dekho."); return
    if cmd == "/ping":
        send_dm(OWNER_ID, "🏓 Pong! 🖤"); return
    if cmd == "/help":
        send_dm(OWNER_ID, HELP_TEXT); return
    if cmd == "/alive":
        send_dm(OWNER_ID,
            f"🖤 𝐁𝐎𝐓 𝐈𝐒 𝐀𝐋𝐈𝐕𝐄 🌑\n\n"
            f"⏱ Uptime: {human_uptime()}\n"
            f"🕐 Cooldown: {get_cooldown()}s\n"
            f"📋 Auto replies: {len(list_auto_replies())}\n"
            f"🔥 Gali targets: {len(list_gali_targets())}")
        return

    # /setreply
    if cmd == "/setreply":
        m = re.match(r"^(\d+)\s+(.+)$", text[len(parts[0]):].strip(), re.S)
        if not m:
            send_dm(OWNER_ID, "Format: `/setreply <slot> <text>`"); return
        slot, txt = int(m.group(1)), m.group(2).strip()
        if not (1 <= slot <= MAX_SLOTS):
            send_dm(OWNER_ID, f"Slot 1-{MAX_SLOTS}"); return
        set_auto_reply(slot, txt)
        send_dm(OWNER_ID, f"✅ Auto reply slot {slot} set.")
        return

    if cmd == "/delreply":
        if not arg.isdigit(): send_dm(OWNER_ID, "Format: `/delreply <slot>`"); return
        if del_auto_reply(int(arg)): send_dm(OWNER_ID, f"🗑️ Slot {arg} deleted.")
        else: send_dm(OWNER_ID, f"❌ Slot {arg} empty.")
        return

    if cmd == "/listreply":
        rows = list_auto_replies()
        if not rows: send_dm(OWNER_ID, "📭 Koi auto reply nahi."); return
        body = "\n\n".join(f"*Slot {r['slot']}*\n{r['text']}" for r in rows)
        send_dm(OWNER_ID, f"📋 Auto replies ({len(rows)})\n\n{body}")
        return

    if cmd in ("/clearreply", "/cleartreply"):
        n = clear_auto_replies()
        send_dm(OWNER_ID, f"🧹 {n} cleared."); return

    # /gali <target> <text>
    if cmd == "/gali":
        m = re.match(r"^(@\S+|\-?\d+)\s+(.+)$", text[len(parts[0]):].strip(), re.S)
        if not m:
            send_dm(OWNER_ID, "Format:\n`/gali @user <text>`\n`/gali 12345 <text>`"); return
        tgt, txt = m.group(1), m.group(2).strip()
        uid, uname = parse_target(tgt)
        # slot = next available
        existing = list_gali_targets(uid=uid, username=uname)
        slot = max([r["slot"] or 0 for r in existing], default=0) + 1
        set_gali(uid, uname, slot, txt)
        label = f"@{uname}" if uname else f"UID {uid}"
        send_dm(OWNER_ID, f"🔥 Gali #{slot} add for {label}")
        return

    if cmd == "/delgali":
        if not arg:
            send_dm(OWNER_ID, "Format: `/delgali @user` or `/delgali 12345`"); return
        uid, uname = parse_target(arg)
        n = del_gali_target(uid, uname)
        label = f"@{uname}" if uname else f"UID {uid}"
        send_dm(OWNER_ID, f"🗑️ {n} gali delete for {label}" if n else f"❌ {label} me kuch nahi.")
        return

    if cmd == "/listgali":
        if arg:
            uid, uname = parse_target(arg)
            rows = list_gali_targets(uid=uid, username=uname)
        else:
            rows = list_gali_targets()
        if not rows: send_dm(OWNER_ID, "📭 Koi gali nahi."); return
        lines = []
        for r in rows:
            tgt = f"@{r['target_username']}" if r['target_username'] else f"UID {r['target_uid']}"
            lines.append(f"• {tgt} #{r['slot']}: {r['text']}")
        body = "\n".join(lines)
        send_dm(OWNER_ID, f"🔥 Gali targets ({len(rows)})\n\n{body}")
        return

    if cmd == "/cleargali":
        n = clear_gali()
        send_dm(OWNER_ID, f"🧹 {n} gali cleared."); return

    # /dmgali — DM target with 1 gali
    if cmd == "/dmgali":
        if not arg: send_dm(OWNER_ID, "Format: `/dmgali @user` or `/dmgali 12345`"); return
        uid, uname = parse_target(arg)
        sent, status = send_gali_dm(uid, uname, count=1)
        if sent: send_dm(OWNER_ID, f"✅ {sent} gali DM bheji.")
        else: send_dm(OWNER_ID, f"❌ Failed: {status}")
        return

    # /raid — DM target with multiple gali
    if cmd == "/raid":
        if not arg: send_dm(OWNER_ID, "Format: `/raid @user` or `/raid 12345`"); return
        uid, uname = parse_target(arg)
        n = get_raid_count()
        sent, status = send_gali_dm(uid, uname, count=n)
        if sent: send_dm(OWNER_ID, f"🔥 Raid sent: {sent}/{n} gali DM.")
        else: send_dm(OWNER_ID, f"❌ Failed: {status}")
        return

    # settings
    if cmd == "/raidcount":
        if not arg.isdigit() or not (1 <= int(arg) <= 100):
            send_dm(OWNER_ID, f"Format: `/raidcount N`. Current: {get_raid_count()}"); return
        set_setting("raid_count", int(arg)); send_dm(OWNER_ID, f"✅ Raid count: {arg}"); return

    if cmd == "/raiddelay":
        try:
            v = float(arg)
            if not (0.1 <= v <= 5): raise ValueError
        except:
            send_dm(OWNER_ID, f"Format: `/raiddelay X`. Current: {get_raid_delay()}"); return
        set_setting("raid_delay", v); send_dm(OWNER_ID, f"✅ Raid delay: {v}s"); return

    if cmd == "/setcooldown":
        if not arg.isdigit() or not (10 <= int(arg) <= 86400):
            send_dm(OWNER_ID, f"Format: `/setcooldown SECS`. Current: {get_cooldown()}s"); return
        set_setting("cooldown", int(arg)); send_dm(OWNER_ID, f"✅ Cooldown: {arg}s"); return

    send_dm(OWNER_ID, "❓ Unknown. /help dekho.")

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

    # ---- Normal message ----
    m = update.get("message")
    if m:
        chat = m.get("chat") or {}
        ct = chat.get("type")
        sender_id = (m.get("from") or {}).get("id")
        chat_id = chat.get("id")

        # Owner DM commands
        if ct == "private" and sender_id == OWNER_ID:
            try: handle_owner_command(m)
            except Exception: logger.exception("Owner cmd failed")
            return jsonify(ok=True), 200

        # Any user DM → auto-reply
        if ct == "private" and sender_id:
            uid = update.get("update_id")
            if uid is None or not record_update(uid):
                return jsonify(ok=True), 200
            remember_user(m.get("from"), chat_id)
            answer = decide_reply(chat_id)
            if answer:
                try: send_reply(chat_id, answer)
                except Exception: logger.exception("Reply failed chat=%s", chat_id)
            return jsonify(ok=True), 200

        # Group
        if ct in ("group", "supergroup"):
            try: handle_group_message(m)
            except Exception: logger.exception("Group handler failed")
            return jsonify(ok=True), 200

    # ---- Business message ----
    bm = update.get("business_message")
    if bm:
        uid = update.get("update_id")
        chat = bm.get("chat") or {}
        conn_id = bm.get("business_connection_id")
        if chat.get("type") == "private" and conn_id and not bm.get("is_outgoing", False):
            chat_id = chat.get("id")
            if chat_id is None or uid is None: return jsonify(ok=True), 200
            if not record_update(uid): return jsonify(ok=True), 200
            remember_user(bm.get("from"), chat_id)
            answer = decide_reply(chat_id)
            if answer:
                try: send_business_reply(conn_id, chat_id, answer)
                except Exception: logger.exception("Biz reply failed chat=%s", chat_id)
        return jsonify(ok=True), 200

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