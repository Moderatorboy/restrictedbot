"""
Telegram Bulk Forwarder
=======================
Env Vars: API_ID, API_HASH, SESSION_STRING, DEST_CHANNEL
"""

import asyncio
import os
import sys
import logging
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    UserAlreadyParticipantError,
    FloodWaitError,
    SessionPasswordNeededError,
    AuthKeyUnregisteredError,
    ApiIdInvalidError,
)

# ── Logging setup — SABSE PEHLE ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger(__name__)
# Telethon internal spam band karo
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("telethon.network").setLevel(logging.WARNING)
logging.getLogger("telethon.client").setLevel(logging.WARNING)

log.info(">>> Script load ho rahi hai...")

# ── Config ────────────────────────────────────────────────────────────────────
INVITE_HASH  = "jc8pJlPJgcNkMzNl"
CHANNEL_ID   = int("-1002916915233")
MSG_START    = 22
MSG_END      = 194
DELAY        = 4
PROGRESS_TAG = "BULK_PROGRESS"
TMP_DIR      = "/tmp"
PORT         = int(os.environ.get("PORT", 10000))

# ── Env Vars ──────────────────────────────────────────────────────────────────
def get_env(key, cast=str):
    val = os.environ.get(key, "").strip()
    if not val:
        log.error(f"MISSING ENV: {key}")
        sys.exit(1)
    try:
        return cast(val)
    except Exception as e:
        log.error(f"INVALID ENV {key}: {e}")
        sys.exit(1)

API_ID           = get_env("API_ID", int)
API_HASH         = get_env("API_HASH")
SESSION_STRING   = get_env("SESSION_STRING")
DEST_CHANNEL_RAW = get_env("DEST_CHANNEL")

try:
    DEST_CHANNEL = int(DEST_CHANNEL_RAW)
except ValueError:
    DEST_CHANNEL = DEST_CHANNEL_RAW

log.info(f">>> Config loaded. Dest={DEST_CHANNEL} Range={MSG_START}-{MSG_END}")

# ── Global status ─────────────────────────────────────────────────────────────
status = {
    "msg_current": 0,
    "msg_total":   MSG_END - MSG_START + 1,
    "phase":       "Starting...",
    "file_done":   0,
    "file_total":  0,
    "file_pct":    0,
    "speed_kb":    0.0,
    "done":        False,
    "last_title":  "",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_size(b):
    if b >= 1024**3: return f"{b/1024**3:.1f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024:    return f"{b/1024:.1f} KB"
    return f"{b} B"

def make_bar(pct, width=20):
    f = int(width * pct / 100)
    return "█" * f + "░" * (width - f)

class ProgressCallback:
    def __init__(self, msg_id, phase, title=""):
        self.msg_id     = msg_id
        self.phase      = phase
        self.title      = title
        self.last_pct   = -1
        self.last_time  = time.time()
        self.last_bytes = 0

    def __call__(self, current, total):
        if total <= 0:
            return
        pct     = int(current * 100 / total)
        now     = time.time()
        elapsed = now - self.last_time
        spd_bps = (current - self.last_bytes) / elapsed if elapsed > 0 else 0
        spd_kb  = spd_bps / 1024

        status["phase"]      = f"{self.phase} MSG {self.msg_id}"
        status["file_done"]  = current
        status["file_total"] = total
        status["file_pct"]   = pct
        status["speed_kb"]   = round(spd_kb, 1)

        should_log = (pct % 5 == 0 and pct != self.last_pct) or (now - self.last_time >= 3)
        if should_log:
            bar     = make_bar(pct)
            eta_sec = int((total - current) / spd_bps) if spd_bps > 0 else 0
            eta_str = f"{eta_sec//60}m{eta_sec%60:02d}s" if eta_sec > 0 else "..."
            log.info(
                f"  {self.phase} [{bar}] {pct:3d}%  "
                f"{fmt_size(current)}/{fmt_size(total)}  "
                f"@ {spd_kb:.0f} KB/s  ETA {eta_str}"
                + (f"  [{self.title}]" if self.title else "")
            )
            self.last_pct   = pct
            self.last_time  = now
            self.last_bytes = current

# ── HTTP Status Page ──────────────────────────────────────────────────────────
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        s       = status
        fp      = s["file_pct"]
        mp      = int(s["msg_current"] * 100 / s["msg_total"]) if s["msg_total"] else 0
        body = (
            f"<!DOCTYPE html><html><head>"
            f'<meta http-equiv="refresh" content="5">'
            f"<style>body{{font-family:monospace;background:#111;color:#eee;padding:20px}}"
            f".g{{color:#0f0}}.d{{color:#aaa}}</style></head><body>"
            f"<h2>📡 Telegram Bulk Forwarder</h2>"
            f"<p><b>Messages:</b> {s['msg_current']} / {s['msg_total']} ({mp}%)</p>"
            f"<p class='g'>[{'█'*(mp//5)}{'░'*(20-mp//5)}] {mp}%</p><hr>"
            f"<p><b>Phase:</b> {s['phase']}"
            + (f" — {s['last_title']}" if s['last_title'] else "") +
            f"</p>"
            f"<p><b>File:</b> {fmt_size(s['file_done'])} / {fmt_size(s['file_total'])}</p>"
            f"<p class='g'>[{'█'*(fp//5)}{'░'*(20-fp//5)}] {fp}%</p>"
            f"<p class='d'>Speed: {s['speed_kb']} KB/s</p>"
            f"<p class='d'>Done: {'✅ Yes' if s['done'] else '⏳ Running...'}</p>"
            f"<p class='d'><small>Auto-refresh 5s</small></p>"
            f"</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), StatusHandler)
        log.info(f"🌐 HTTP server started on port {PORT}")
        server.serve_forever()
    except Exception as e:
        log.error(f"HTTP server error: {e}")

# ── Telegram helpers ──────────────────────────────────────────────────────────
async def get_last_completed(client):
    try:
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            m = re.search(rf"{re.escape(PROGRESS_TAG)}:(\d+)", msg.text or "")
            if m:
                val = int(m.group(1))
                log.info(f"📂 Resume point mila: MSG {val}")
                return val
    except Exception as e:
        log.warning(f"Progress read error: {e}")
    return MSG_START - 1


async def save_progress(client, msg_id):
    try:
        dels = []
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            dels.append(msg)
        for msg in dels:
            await msg.delete()
        await client.send_message("me", f"{PROGRESS_TAG}:{msg_id}")
    except Exception as e:
        log.warning(f"Progress save error: {e}")


async def join_channel(client):
    try:
        await client(ImportChatInviteRequest(INVITE_HASH))
        log.info("✅ Channel join ho gaya!")
    except UserAlreadyParticipantError:
        log.info("✅ Already joined.")
    except Exception as e:
        log.warning(f"Join: {e}")


def get_meta(message):
    meta = {"title": None, "duration": None, "thumb": None}
    doc = message.document
    if not doc:
        return meta
    for attr in doc.attributes:
        cn = type(attr).__name__
        if cn == "DocumentAttributeVideo":
            meta["duration"] = getattr(attr, "duration", None)
        elif cn == "DocumentAttributeFilename":
            fn = getattr(attr, "file_name", "") or ""
            if fn:
                meta["title"] = os.path.splitext(fn)[0]
        elif cn == "DocumentAttributeAudio":
            meta["title"]    = getattr(attr, "title", None)
            meta["duration"] = getattr(attr, "duration", None)
    if doc.thumbs:
        meta["thumb"] = doc.thumbs[-1]
    return meta


async def process_message(client, msg_id, current, total):
    fp = tp = None
    try:
        msg = await client.get_messages(CHANNEL_ID, ids=msg_id)

        if not msg:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — nahi mila, skip.")
            return True
        if not msg.media:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — media nahi, skip.")
            return True

        meta  = get_meta(msg)
        title = meta["title"] or ""
        status["last_title"] = title

        # Download
        log.info(f"[{current}/{total}] 📥 MSG {msg_id} download shuru" + (f" — '{title}'" if title else "") + "...")
        dl_cb = ProgressCallback(msg_id, "📥 DL", title)
        fp = await client.download_media(
            msg,
            file=os.path.join(TMP_DIR, f"vid_{msg_id}"),
            progress_callback=dl_cb,
        )
        if not fp:
            log.warning(f"[{current}/{total}] ⚠️  Download failed, skip.")
            return True
        log.info(f"[{current}/{total}] ✅ Download done — {fmt_size(os.path.getsize(fp))}")

        # Thumbnail
        if meta["thumb"]:
            try:
                tp = await client.download_media(msg, file=os.path.join(TMP_DIR, f"th_{msg_id}.jpg"), thumb=-1)
            except Exception:
                tp = None

        # Upload
        caption = msg.text or msg.message or ""
        log.info(f"[{current}/{total}] 📤 MSG {msg_id} upload shuru → {DEST_CHANNEL}...")
        ul_cb = ProgressCallback(msg_id, "📤 UL", title)
        await client.send_file(
            DEST_CHANNEL, fp,
            caption=caption,
            supports_streaming=True,
            attributes=msg.document.attributes if msg.document else [],
            thumb=tp,
            progress_callback=ul_cb,
        )
        log.info(
            f"[{current}/{total}] ✅ MSG {msg_id} COMPLETE!"
            + (f" | {title}" if title else "")
            + (f" | {meta['duration']}s" if meta["duration"] else "")
        )
        status["phase"] = f"✅ MSG {msg_id} done"
        return True

    except FloodWaitError as e:
        log.warning(f"[{current}/{total}] ⏳ FloodWait {e.seconds}s...")
        status["phase"] = f"⏳ FloodWait {e.seconds}s"
        await asyncio.sleep(e.seconds + 5)
        return False

    except Exception as e:
        log.error(f"[{current}/{total}] ❌ MSG {msg_id} error: {type(e).__name__}: {e}")
        status["phase"] = f"❌ MSG {msg_id} error"
        return True

    finally:
        for p in [fp, tp]:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass


async def main():
    log.info("=" * 55)
    log.info("🚀 Forwarder starting...")
    log.info(f"   Source : {CHANNEL_ID}")
    log.info(f"   Dest   : {DEST_CHANNEL}")
    log.info(f"   Range  : {MSG_START} → {MSG_END}")
    log.info("=" * 55)

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    try:
        await client.start()
    except ApiIdInvalidError:
        log.error("❌ API_ID/API_HASH galat!")
        sys.exit(1)
    except AuthKeyUnregisteredError:
        log.error("❌ SESSION_STRING expire! Naya banao.")
        sys.exit(1)
    except SessionPasswordNeededError:
        log.error("❌ 2FA on hai.")
        sys.exit(1)
    except Exception as e:
        log.error(f"❌ Login failed: {type(e).__name__}: {e}")
        sys.exit(1)

    async with client:
        me = await client.get_me()
        log.info(f"✅ Logged in: {me.first_name} (@{me.username})")

        await join_channel(client)

        last_done    = await get_last_completed(client)
        start_from   = last_done + 1
        total_range  = MSG_END - MSG_START + 1
        already_done = max(0, last_done - MSG_START + 1)
        status["msg_total"] = total_range

        if start_from > MSG_END:
            log.info("🎉 Sab complete pehle se!")
            status["done"] = True
            await asyncio.sleep(86400 * 7)
            return

        if already_done > 0:
            log.info(f"🔄 Resume: MSG {start_from} se ({already_done}/{total_range} done)")
        else:
            log.info(f"🆕 Fresh start: {MSG_START} → {MSG_END}")

        log.info(f"📊 Baki: {MSG_END - start_from + 1} messages")
        log.info("=" * 55)

        success = 0
        current = already_done + 1

        for msg_id in range(start_from, MSG_END + 1):
            status["msg_current"] = current
            while True:
                if await process_message(client, msg_id, current, total_range):
                    break
            await save_progress(client, msg_id)
            success += 1
            current += 1
            if msg_id < MSG_END:
                await asyncio.sleep(DELAY)

        log.info("=" * 55)
        log.info(f"🎉 COMPLETE! {success}/{total_range} forwarded.")
        log.info("=" * 55)
        status["done"] = True
        status["msg_current"] = total_range
        status["phase"] = "🎉 All done!"
        await asyncio.sleep(86400 * 7)


if __name__ == "__main__":
    # HTTP server PEHLE start karo — port bind hone ke liye
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()
    # Thoda wait karo taaki port bind ho jaye Render scan se pehle
    time.sleep(1)
    log.info(">>> HTTP server ready, Telegram start ho raha hai...")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("⛔ Stopped by user.")
    except Exception as e:
        log.error(f"💥 Fatal: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
