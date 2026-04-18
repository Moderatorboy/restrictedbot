"""
Telegram Bulk Forwarder — With Download/Upload Progress Bar
============================================================
- Download aur Upload ka live % dikhta hai logs mein
- Render Web Service compatible (HTTP status page)
- Resume via Saved Messages

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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Hardcoded Config ──────────────────────────────────────────────────────────
INVITE_HASH  = "jc8pJlPJgcNkMzNl"
CHANNEL_ID   = int("-1002916915233")
MSG_START    = 22
MSG_END      = 194
DELAY        = 4
PROGRESS_TAG = "BULK_PROGRESS"
TMP_DIR      = "/tmp"
PORT         = int(os.environ.get("PORT", 10000))
# ─────────────────────────────────────────────────────────────────────────────

# ── Env Vars ──────────────────────────────────────────────────────────────────
def get_env(key: str, cast=str):
    val = os.environ.get(key, "").strip()
    if not val:
        log.error(f"❌ Environment variable '{key}' missing ya empty hai!")
        sys.exit(1)
    try:
        return cast(val)
    except (ValueError, TypeError) as e:
        log.error(f"❌ '{key}' ka value invalid hai: {e}")
        sys.exit(1)

API_ID           = get_env("API_ID", int)
API_HASH         = get_env("API_HASH")
SESSION_STRING   = get_env("SESSION_STRING")
DEST_CHANNEL_RAW = get_env("DEST_CHANNEL")

try:
    DEST_CHANNEL = int(DEST_CHANNEL_RAW)
except ValueError:
    DEST_CHANNEL = DEST_CHANNEL_RAW
# ─────────────────────────────────────────────────────────────────────────────

# ── Global Status (HTTP page ke liye) ─────────────────────────────────────────
status = {
    "msg_current": 0,
    "msg_total":   MSG_END - MSG_START + 1,
    "msg_id":      0,
    "phase":       "Starting...",   # Downloading / Uploading / Done
    "file_done":   0,               # bytes
    "file_total":  0,               # bytes
    "file_pct":    0,               # 0-100
    "speed_kb":    0.0,             # KB/s
    "done":        False,
    "last_title":  "",
}

# ── Progress Bar Helper ───────────────────────────────────────────────────────
def fmt_size(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b/1024**3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b/1024**2:.1f} MB"
    if b >= 1024:
        return f"{b/1024:.1f} KB"
    return f"{b} B"

def make_bar(pct: int, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)

class ProgressCallback:
    """
    Telethon progress callback — download aur upload dono ke liye.
    Har 5% ya har 3 second pe log print karta hai.
    """
    def __init__(self, msg_id: int, phase: str, title: str = ""):
        self.msg_id     = msg_id
        self.phase      = phase   # "📥 DL" ya "📤 UL"
        self.title      = title
        self.last_pct   = -1
        self.last_time  = time.time()
        self.last_bytes = 0
        self.start_time = time.time()

    def __call__(self, current: int, total: int):
        if total <= 0:
            return

        pct       = int(current * 100 / total)
        now       = time.time()
        elapsed   = now - self.last_time
        speed_bps = (current - self.last_bytes) / elapsed if elapsed > 0 else 0
        speed_kb  = speed_bps / 1024

        # Status update karo (HTTP page ke liye)
        status["phase"]      = f"{self.phase} MSG {self.msg_id}"
        status["file_done"]  = current
        status["file_total"] = total
        status["file_pct"]   = pct
        status["speed_kb"]   = round(speed_kb, 1)

        # Log sirf jab 5% change ho ya 3 sec guzre
        if pct != self.last_pct and (pct % 5 == 0 or pct >= 99) or (now - self.last_time >= 3):
            bar = make_bar(pct)
            eta_sec = int((total - current) / speed_bps) if speed_bps > 0 else 0
            eta_str = f"{eta_sec//60}m{eta_sec%60:02d}s" if eta_sec > 0 else "..."

            log.info(
                f"  {self.phase} [{bar}] {pct:3d}% "
                f"{fmt_size(current)}/{fmt_size(total)} "
                f"@ {speed_kb:.0f} KB/s  ETA {eta_str}"
                + (f"  [{self.title}]" if self.title else "")
            )
            self.last_pct   = pct
            self.last_time  = now
            self.last_bytes = current

# ── HTTP Status Page ──────────────────────────────────────────────────────────
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        s = status
        pct      = s["file_pct"]
        bar_html = "█" * (pct // 5) + "░" * (20 - pct // 5)
        msg_pct  = int(s["msg_current"] * 100 / s["msg_total"]) if s["msg_total"] else 0

        body = f"""<!DOCTYPE html>
<html><head>
<meta http-equiv="refresh" content="5">
<style>
  body {{ font-family: monospace; background: #111; color: #eee; padding: 20px; }}
  .bar {{ color: #0f0; font-size: 1.2em; }}
  .info {{ color: #aaa; }}
  h2 {{ color: #fff; }}
</style>
</head><body>
<h2>📡 Telegram Bulk Forwarder</h2>
<p><b>Messages:</b> {s['msg_current']} / {s['msg_total']} &nbsp; ({msg_pct}%)</p>
<p class="bar">[{'█'*(msg_pct//5)}{'░'*(20-msg_pct//5)}] {msg_pct}%</p>
<hr>
<p><b>Current:</b> {s['phase']}{' — ' + s['last_title'] if s['last_title'] else ''}</p>
<p><b>File:</b> {fmt_size(s['file_done'])} / {fmt_size(s['file_total'])}</p>
<p class="bar">[{bar_html}] {pct}%</p>
<p class="info">Speed: {s['speed_kb']} KB/s</p>
<p class="info">Done: {'✅ Yes' if s['done'] else '⏳ Running...'}</p>
<p class="info"><small>Auto-refresh every 5s</small></p>
</body></html>""".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def start_http_server():
    server = HTTPServer(("0.0.0.0", PORT), StatusHandler)
    log.info(f"🌐 Status page: port {PORT}")
    server.serve_forever()

# ─────────────────────────────────────────────────────────────────────────────

async def get_last_completed(client: TelegramClient) -> int:
    try:
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            match = re.search(rf"{re.escape(PROGRESS_TAG)}:(\d+)", msg.text or "")
            if match:
                val = int(match.group(1))
                log.info(f"📂 Saved Messages se progress mili: MSG {val}")
                return val
    except Exception as e:
        log.warning(f"⚠️  Progress read error (fresh start): {e}")
    return MSG_START - 1


async def save_progress(client: TelegramClient, msg_id: int):
    try:
        to_delete = []
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            to_delete.append(msg)
        for msg in to_delete:
            await msg.delete()
        await client.send_message("me", f"{PROGRESS_TAG}:{msg_id}")
    except Exception as e:
        log.warning(f"⚠️  Progress save error: {e}")


async def join_channel(client: TelegramClient):
    try:
        log.info("🔗 Channel join karne ki koshish...")
        await client(ImportChatInviteRequest(INVITE_HASH))
        log.info("✅ Channel join ho gaya!")
    except UserAlreadyParticipantError:
        log.info("✅ Pehle se joined hai — continue.")
    except Exception as e:
        log.warning(f"⚠️  Join result: {e}")


def extract_video_meta(message) -> dict:
    meta = {"title": None, "duration": None, "width": None,
            "height": None, "thumb": None, "mime_type": None}
    doc = message.document
    if not doc:
        return meta
    meta["mime_type"] = doc.mime_type
    for attr in doc.attributes:
        cls = type(attr).__name__
        if cls == "DocumentAttributeVideo":
            meta["duration"] = getattr(attr, "duration", None)
            meta["width"]    = getattr(attr, "w", None)
            meta["height"]   = getattr(attr, "h", None)
        elif cls == "DocumentAttributeFilename":
            fname = getattr(attr, "file_name", "") or ""
            if fname:
                meta["title"] = os.path.splitext(fname)[0]
        elif cls == "DocumentAttributeAudio":
            meta["title"]    = getattr(attr, "title", None)
            meta["duration"] = getattr(attr, "duration", None)
    if doc.thumbs:
        meta["thumb"] = doc.thumbs[-1]
    return meta


async def process_message(client: TelegramClient, msg_id: int,
                           current: int, total: int) -> bool:
    file_path  = None
    thumb_path = None
    try:
        message = await client.get_messages(CHANNEL_ID, ids=msg_id)

        if not message:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — exist nahi, skip.")
            return True
        if not message.media:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — media nahi, skip.")
            return True

        meta  = extract_video_meta(message)
        title = meta["title"] or ""
        status["last_title"] = title
        status["msg_id"]     = msg_id

        # ── DOWNLOAD with progress ──
        log.info(f"[{current}/{total}] 📥 MSG {msg_id} download shuru"
                 + (f" — '{title}'" if title else "") + "...")

        dl_cb = ProgressCallback(msg_id, "📥 DL", title)
        file_path = await client.download_media(
            message,
            file=os.path.join(TMP_DIR, f"vid_{msg_id}"),
            progress_callback=dl_cb,
        )

        if not file_path:
            log.warning(f"[{current}/{total}] ⚠️  MSG {msg_id} — download failed, skip.")
            return True

        log.info(f"[{current}/{total}] ✅ Download done — {fmt_size(os.path.getsize(file_path))}")

        # Thumbnail
        if meta["thumb"]:
            try:
                thumb_path = await client.download_media(
                    message,
                    file=os.path.join(TMP_DIR, f"thumb_{msg_id}.jpg"),
                    thumb=-1
                )
            except Exception:
                thumb_path = None

        caption = message.text or message.message or ""

        # ── UPLOAD with progress ──
        log.info(f"[{current}/{total}] 📤 MSG {msg_id} upload shuru → {DEST_CHANNEL}...")

        ul_cb = ProgressCallback(msg_id, "📤 UL", title)
        await client.send_file(
            DEST_CHANNEL,
            file_path,
            caption=caption,
            supports_streaming=True,
            attributes=message.document.attributes if message.document else [],
            thumb=thumb_path,
            progress_callback=ul_cb,
        )

        log.info(
            f"[{current}/{total}] ✅ MSG {msg_id} COMPLETE!"
            + (f" | 🎬 {title}"             if title            else "")
            + (f" | ⏱ {meta['duration']}s"  if meta["duration"] else "")
        )
        status["phase"] = f"✅ MSG {msg_id} done"
        return True

    except FloodWaitError as e:
        log.warning(f"[{current}/{total}] ⏳ FloodWait {e.seconds}s — waiting...")
        status["phase"] = f"⏳ FloodWait {e.seconds}s"
        await asyncio.sleep(e.seconds + 5)
        return False

    except Exception as e:
        log.error(f"[{current}/{total}] ❌ MSG {msg_id} — {type(e).__name__}: {e}")
        status["phase"] = f"❌ MSG {msg_id} error"
        return True

    finally:
        for path in [file_path, thumb_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


async def main():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    log.info("=" * 55)
    log.info("🚀 Telegram Bulk Forwarder starting...")
    log.info(f"   Source  : {CHANNEL_ID}")
    log.info(f"   Dest    : {DEST_CHANNEL}")
    log.info(f"   Range   : MSG {MSG_START} → {MSG_END}")
    log.info("=" * 55)

    try:
        await client.start()
    except ApiIdInvalidError:
        log.error("❌ API_ID ya API_HASH galat hai!")
        sys.exit(1)
    except AuthKeyUnregisteredError:
        log.error("❌ SESSION_STRING expire ho gaya! Naya generate karo.")
        sys.exit(1)
    except SessionPasswordNeededError:
        log.error("❌ 2FA enabled hai.")
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
            log.info("🎉 Sab complete! Kuch bacha nahi.")
            status["done"]  = True
            status["phase"] = "🎉 All done!"
            await asyncio.sleep(86400 * 7)
            return

        if already_done > 0:
            log.info(f"🔄 Resume: MSG {start_from} se ({already_done}/{total_range} done)")
        else:
            log.info(f"🆕 Fresh start: MSG {MSG_START} → {MSG_END}")

        log.info(f"📊 Remaining: {MSG_END - start_from + 1} messages")
        log.info("=" * 55)

        success_count = 0
        current       = already_done + 1

        for msg_id in range(start_from, MSG_END + 1):
            status["msg_current"] = current

            while True:
                result = await process_message(client, msg_id, current, total_range)
                if result:
                    break

            await save_progress(client, msg_id)
            success_count += 1
            current       += 1

            if msg_id < MSG_END:
                await asyncio.sleep(DELAY)

        log.info("=" * 55)
        log.info(f"🎉 COMPLETE! {success_count}/{total_range} messages forwarded.")
        log.info("=" * 55)

        status["done"]        = True
        status["msg_current"] = total_range
        status["phase"]       = "🎉 All done!"
        await asyncio.sleep(86400 * 7)


if __name__ == "__main__":
    threading.Thread(target=start_http_server, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("⛔ Stopped.")
    except Exception as e:
        log.error(f"💥 Fatal: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
