"""
Telegram Bulk Forwarder — Resume via Saved Messages (100% Free)
================================================================
- Message ID 22 se 194 tak fetch karta hai
- Invite link se auto-join karta hai
- Progress Telegram "Saved Messages" mein save hoti hai (FREE!)
- Render restart pe Saved Messages se resume karta hai
- Videos/files apne channel mein bhejta hai

Env Vars: API_ID, API_HASH, SESSION_STRING, DEST_CHANNEL

FIXES in this version:
  1. Proper startup error logging (crash reason ab dikhega)
  2. SESSION_STRING whitespace strip karta hai
  3. DEST_CHANNEL int/string dono handle karta hai
  4. FloodWait per-message retry loop (sirf ek baar nahi, jab tak wait khatam na ho)
  5. Temp files /tmp mein save hoti hain (Render ke read-only ./ se bachne ke liye)
  6. Graceful shutdown on KeyboardInterrupt
  7. Thumbnail download error alag se handle hota hai
"""

import asyncio
import os
import sys
import logging
import re
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

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,   # Render stdout pe dikhe
)
log = logging.getLogger(__name__)

# ── Hardcoded Config ──────────────────────────────────────────────────────────
INVITE_HASH  = "jc8pJlPJgcNkMzNl"
CHANNEL_ID   = int("-1002916915233")   # t.me/c/2916915233 → -100 prefix
MSG_START    = 22
MSG_END      = 194
DELAY        = 4          # seconds between messages (flood se bachne ke liye)
PROGRESS_TAG = "BULK_PROGRESS"
TMP_DIR      = "/tmp"     # Render pe safe writable directory
# ─────────────────────────────────────────────────────────────────────────────

# ── Env Vars (with validation) ────────────────────────────────────────────────
def get_env(key: str, cast=str) -> any:
    val = os.environ.get(key, "").strip()
    if not val:
        log.error(f"❌ Environment variable '{key}' missing ya empty hai!")
        sys.exit(1)
    try:
        return cast(val)
    except (ValueError, TypeError) as e:
        log.error(f"❌ '{key}' ka value invalid hai: {e}")
        sys.exit(1)

API_ID         = get_env("API_ID", int)
API_HASH       = get_env("API_HASH")
SESSION_STRING = get_env("SESSION_STRING")
DEST_CHANNEL_RAW = get_env("DEST_CHANNEL")

# DEST_CHANNEL int ya username dono ho sakta hai
try:
    DEST_CHANNEL = int(DEST_CHANNEL_RAW)
except ValueError:
    DEST_CHANNEL = DEST_CHANNEL_RAW   # @username string
# ─────────────────────────────────────────────────────────────────────────────

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


async def get_last_completed() -> int:
    """
    Telegram Saved Messages se last completed MSG ID padhta hai.
    Bilkul FREE — koi disk nahi chahiye!
    """
    try:
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            match = re.search(rf"{re.escape(PROGRESS_TAG)}:(\d+)", msg.text or "")
            if match:
                val = int(match.group(1))
                log.info(f"📂 Saved Messages se progress mili: MSG {val}")
                return val
    except Exception as e:
        log.warning(f"⚠️  Progress read error (fresh start se chalenge): {e}")
    return MSG_START - 1


async def save_progress(msg_id: int):
    """
    Telegram Saved Messages mein progress save karta hai.
    Purana progress message delete karke naya bhejta hai.
    """
    try:
        # Purane progress messages delete karo
        to_delete = []
        async for msg in client.iter_messages("me", limit=50, search=PROGRESS_TAG):
            to_delete.append(msg)
        for msg in to_delete:
            await msg.delete()
        # Naya save karo
        await client.send_message("me", f"{PROGRESS_TAG}:{msg_id}")
    except Exception as e:
        log.warning(f"⚠️  Progress save error: {e}")


async def join_channel():
    """Invite link se channel join karta hai."""
    try:
        log.info("🔗 Channel join karne ki koshish...")
        await client(ImportChatInviteRequest(INVITE_HASH))
        log.info("✅ Channel join ho gaya!")
    except UserAlreadyParticipantError:
        log.info("✅ Pehle se joined hai — continue kar rahe hain.")
    except Exception as e:
        log.warning(f"⚠️  Join attempt result: {e}")
        log.info("   (Agar already joined hai toh yeh error ignore kar sakte hain)")


def extract_video_meta(message) -> dict:
    """
    Message se video ka title, duration, w/h, thumbnail extract karta hai.
    """
    meta = {
        "title":     None,
        "duration":  None,
        "width":     None,
        "height":    None,
        "thumb":     None,
        "mime_type": None,
    }

    doc = message.document
    if not doc:
        return meta

    meta["mime_type"] = doc.mime_type

    for attr in doc.attributes:
        cls_name = type(attr).__name__

        if cls_name == "DocumentAttributeVideo":
            meta["duration"] = getattr(attr, "duration", None)
            meta["width"]    = getattr(attr, "w", None)
            meta["height"]   = getattr(attr, "h", None)

        elif cls_name == "DocumentAttributeFilename":
            fname = getattr(attr, "file_name", "") or ""
            if fname:
                meta["title"] = os.path.splitext(fname)[0]

        elif cls_name == "DocumentAttributeAudio":
            meta["title"]    = getattr(attr, "title", None)
            meta["duration"] = getattr(attr, "duration", None)

    if doc.thumbs:
        meta["thumb"] = doc.thumbs[-1]

    return meta


async def process_message(msg_id: int, current: int, total: int) -> bool:
    """
    Ek message fetch karke destination channel mein bhejta hai.
    Returns True = success ya permanent skip
    Returns False = retry chahiye (FloodWait)
    """
    file_path  = None
    thumb_path = None

    try:
        message = await client.get_messages(CHANNEL_ID, ids=msg_id)

        if not message:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — exist nahi karta, skip.")
            return True

        if not message.media:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — media nahi hai, skip.")
            return True

        meta  = extract_video_meta(message)
        title = meta["title"]

        log.info(
            f"[{current}/{total}] 📥 Downloading MSG {msg_id}"
            + (f" — '{title}'" if title else "") + "..."
        )

        # /tmp mein save karo (Render pe safe hai)
        file_path = await client.download_media(
            message,
            file=os.path.join(TMP_DIR, f"vid_{msg_id}")
        )

        if not file_path:
            log.warning(f"[{current}/{total}] ⚠️  MSG {msg_id} — download failed, skip.")
            return True

        # Thumbnail download (optional — na mile toh chalega)
        if meta["thumb"]:
            try:
                thumb_path = await client.download_media(
                    message,
                    file=os.path.join(TMP_DIR, f"thumb_{msg_id}.jpg"),
                    thumb=-1
                )
            except Exception as thumb_err:
                log.debug(f"Thumbnail download skip: {thumb_err}")
                thumb_path = None

        caption = message.text or message.message or ""

        log.info(f"[{current}/{total}] 📤 Uploading MSG {msg_id} → {DEST_CHANNEL}...")

        await client.send_file(
            DEST_CHANNEL,
            file_path,
            caption=caption,
            supports_streaming=True,
            attributes=message.document.attributes if message.document else [],
            thumb=thumb_path,
        )

        log.info(
            f"[{current}/{total}] ✅ MSG {msg_id} done!"
            + (f" | 🎬 {title}"            if title            else "")
            + (f" | ⏱ {meta['duration']}s" if meta["duration"] else "")
        )
        return True

    except FloodWaitError as e:
        log.warning(
            f"[{current}/{total}] ⏳ FloodWait {e.seconds}s — wait kar rahe hain..."
        )
        await asyncio.sleep(e.seconds + 5)
        return False   # caller retry karega

    except Exception as e:
        log.error(f"[{current}/{total}] ❌ MSG {msg_id} error: {type(e).__name__}: {e}")
        return True    # skip and move on

    finally:
        for path in [file_path, thumb_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


async def main():
    log.info("=" * 55)
    log.info("🚀 Telegram Bulk Forwarder starting...")
    log.info(f"   Source channel : {CHANNEL_ID}")
    log.info(f"   Dest channel   : {DEST_CHANNEL}")
    log.info(f"   Message range  : {MSG_START} → {MSG_END}")
    log.info("=" * 55)

    # ── Client start with proper error messages ──
    try:
        await client.start()
    except ApiIdInvalidError:
        log.error("❌ API_ID ya API_HASH galat hai! my.telegram.org se check karo.")
        sys.exit(1)
    except AuthKeyUnregisteredError:
        log.error("❌ SESSION_STRING expire ho gaya hai! Naya session generate karo.")
        sys.exit(1)
    except SessionPasswordNeededError:
        log.error("❌ Account mein 2FA enabled hai. Session generate karte waqt password dena hoga.")
        sys.exit(1)
    except Exception as e:
        log.error(f"❌ Login failed: {type(e).__name__}: {e}")
        sys.exit(1)

    me = await client.get_me()
    log.info(f"✅ Logged in as: {me.first_name} (@{me.username})")

    # Channel join karo
    await join_channel()

    # Resume point check karo
    last_done  = await get_last_completed()
    start_from = last_done + 1

    total_range  = MSG_END - MSG_START + 1
    already_done = max(0, last_done - MSG_START + 1)

    if start_from > MSG_END:
        log.info("🎉 Sab pehle se complete ho chuka hai! Kuch kaam nahi bacha.")
        return

    if already_done > 0:
        log.info(
            f"🔄 Resume ho raha hai MSG {start_from} se "
            f"(pehle {already_done}/{total_range} already done)"
        )
    else:
        log.info(f"🆕 Fresh start — MSG {MSG_START} se {MSG_END} tak")

    remaining = MSG_END - start_from + 1
    log.info(f"📊 Remaining: {remaining} messages")
    log.info("=" * 55)

    success_count = 0
    skip_count    = 0
    current       = already_done + 1

    for msg_id in range(start_from, MSG_END + 1):

        # Retry loop — FloodWait ke baad same message dobara try karo
        while True:
            result = await process_message(msg_id, current, total_range)
            if result:
                break   # success ya permanent skip
            # result = False means FloodWait → loop dobara chalega

        await save_progress(msg_id)
        success_count += 1
        current       += 1

        if msg_id < MSG_END:
            await asyncio.sleep(DELAY)

    log.info("=" * 55)
    log.info("🎉 Bulk forward COMPLETE!")
    log.info(f"   ✅ Processed : {success_count}")
    log.info(f"   📦 Total     : {total_range}")
    log.info("=" * 55)


if __name__ == "__main__":
    try:
        with client:
            client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("⛔ User ne stop kiya. Abhi tak ki progress save ho chuki hai.")
    except Exception as e:
        log.error(f"💥 Fatal error: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
