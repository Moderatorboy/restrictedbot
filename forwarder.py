"""
Telegram Bulk Forwarder — Resume via Saved Messages (100% Free)
================================================================
- Message ID 22 se 194 tak fetch karta hai
- Invite link se auto-join karta hai
- Progress Telegram "Saved Messages" mein save hoti hai (FREE!)
- Render restart pe Saved Messages se resume karta hai
- Videos/files apne channel mein bhejta hai

Env Vars: API_ID, API_HASH, SESSION_STRING, DEST_CHANNEL
"""

import asyncio
import os
import logging
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import UserAlreadyParticipantError, FloodWaitError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Hardcoded Config ─────────────────────────────────────────────────────────
INVITE_HASH  = "jc8pJlPJgcNkMzNl"
CHANNEL_ID   = int("-1002916915233")   # t.me/c/2916915233 → -100 prefix
MSG_START    = 22
MSG_END      = 194
DELAY        = 4
PROGRESS_TAG = "BULK_PROGRESS"   # Saved Messages mein is tag se dhundhega
# ────────────────────────────────────────────────────────────────────────────

# ── Env Vars ─────────────────────────────────────────────────────────────────
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
DEST_CHANNEL   = os.environ["DEST_CHANNEL"]
# ────────────────────────────────────────────────────────────────────────────

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


async def get_last_completed() -> int:
    """
    Telegram Saved Messages se last completed MSG ID padhta hai.
    Bilkul FREE — koi disk nahi chahiye!
    """
    try:
        async for msg in client.iter_messages("me", limit=30, search=PROGRESS_TAG):
            match = re.search(rf"{PROGRESS_TAG}:(\d+)", msg.text or "")
            if match:
                log.info(f"📂 Saved Messages se progress mili: MSG {match.group(1)}")
                return int(match.group(1))
    except Exception as e:
        log.warning(f"Progress read error: {e}")
    return MSG_START - 1


async def save_progress(msg_id: int):
    """
    Telegram Saved Messages mein progress save karta hai.
    Purana message delete karke naya bhejta hai — bilkul FREE!
    """
    try:
        async for msg in client.iter_messages("me", limit=30, search=PROGRESS_TAG):
            await msg.delete()
            break
        await client.send_message("me", f"{PROGRESS_TAG}:{msg_id}")
    except Exception as e:
        log.warning(f"Progress save error: {e}")


async def join_channel():
    """Invite link se channel join karta hai."""
    try:
        log.info("🔗 Channel join karne ki koshish...")
        await client(ImportChatInviteRequest(INVITE_HASH))
        log.info("✅ Channel join ho gaya!")
    except UserAlreadyParticipantError:
        log.info("✅ Pehle se joined hai — continue kar rahe hain.")
    except Exception as e:
        log.warning(f"⚠️ Join attempt: {e}")


def extract_video_meta(message):
    """
    Message se video ka title, duration, w/h, thumbnail extract karta hai.
    Returns dict with all available metadata.
    """
    meta = {
        "title": None,
        "duration": None,
        "width": None,
        "height": None,
        "thumb": None,       # thumbnail bytes
        "mime_type": None,
    }

    doc = message.document
    if not doc:
        return meta

    meta["mime_type"] = doc.mime_type

    # Document attributes mein title, duration, dimensions hote hain
    for attr in doc.attributes:
        cls_name = type(attr).__name__

        if cls_name == "DocumentAttributeVideo":
            meta["duration"] = getattr(attr, "duration", None)
            meta["width"]    = getattr(attr, "w", None)
            meta["height"]   = getattr(attr, "h", None)

        elif cls_name == "DocumentAttributeFilename":
            # Filename se title banao (extension hata ke)
            fname = getattr(attr, "file_name", "") or ""
            if fname:
                meta["title"] = os.path.splitext(fname)[0]

        elif cls_name == "DocumentAttributeAudio":
            meta["title"]    = getattr(attr, "title", None)
            meta["duration"] = getattr(attr, "duration", None)

    # Thumbnail — doc ke thumbs mein pehla wala
    if doc.thumbs:
        meta["thumb"] = doc.thumbs[-1]   # Sabse badi thumbnail

    return meta


async def process_message(msg_id: int, current: int, total: int) -> bool:
    """
    Ek message fetch karta hai — title, thumbnail, duration ke saath upload karta hai.
    Returns True agar success ya skip, False agar retry chahiye.
    """
    file_path    = None
    thumb_path   = None

    try:
        message = await client.get_messages(CHANNEL_ID, ids=msg_id)

        if not message:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — exist nahi karta, skip.")
            return True

        if not message.media:
            log.info(f"[{current}/{total}] ⏭  MSG {msg_id} — media nahi hai, skip.")
            return True

        # Video metadata extract karo
        meta = extract_video_meta(message)
        title    = meta["title"]
        duration = meta["duration"]
        width    = meta["width"]
        height   = meta["height"]

        log.info(f"[{current}/{total}] 📥 Downloading MSG {msg_id}"
                 + (f" — '{title}'" if title else "") + "...")

        # Video file download
        file_path = await client.download_media(message, file=f"./temp_vid_{msg_id}")

        if not file_path:
            log.warning(f"[{current}/{total}] ⚠️  MSG {msg_id} — download failed, skip.")
            return True

        # Thumbnail download (agar available hai)
        if meta["thumb"]:
            try:
                thumb_path = await client.download_media(
                    message,
                    file=f"./temp_thumb_{msg_id}.jpg",
                    thumb=-1    # Best quality thumbnail
                )
            except Exception:
                thumb_path = None   # Thumbnail na mile toh bhi chalega

        caption = message.text or message.message or ""

        log.info(f"[{current}/{total}] 📤 Uploading MSG {msg_id} to {DEST_CHANNEL}...")

        await client.send_file(
            DEST_CHANNEL,
            file_path,
            caption=caption,
            supports_streaming=True,
            # ── Ye sab original jaisi dikhegi video ──
            attributes=message.document.attributes if message.document else [],
            thumb=thumb_path,       # Original thumbnail
        )

        log.info(f"[{current}/{total}] ✅ MSG {msg_id} done!"
                 + (f" | 🎬 {title}" if title else "")
                 + (f" | ⏱ {duration}s" if duration else ""))
        return True

    except FloodWaitError as e:
        log.warning(f"[{current}/{total}] ⏳ FloodWait {e.seconds}s — wait kar rahe hain...")
        await asyncio.sleep(e.seconds + 5)
        return False   # Retry

    except Exception as e:
        log.error(f"[{current}/{total}] ❌ MSG {msg_id} error: {e}")
        return True    # Skip and move on

    finally:
        for path in [file_path, thumb_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass


async def main():
    await client.start()
    me = await client.get_me()
    log.info(f"✅ Logged in as: {me.first_name} (@{me.username})")

    # Channel join karo
    await join_channel()

    # Resume point check karo (Saved Messages se)
    last_done = await get_last_completed()
    start_from = last_done + 1

    total_range = MSG_END - MSG_START + 1
    already_done = last_done - MSG_START + 1 if last_done >= MSG_START else 0

    if start_from > MSG_END:
        log.info("🎉 Sab pehle se complete ho chuka hai! Kuch kaam nahi bacha.")
        return

    if already_done > 0:
        log.info(f"🔄 Resume ho raha hai MSG {start_from} se (pehle {already_done} already done)")
    else:
        log.info(f"🚀 Fresh start — MSG {MSG_START} se {MSG_END} tak")

    log.info(f"📊 Total range: {total_range} messages | Remaining: {MSG_END - start_from + 1}")
    log.info("=" * 55)

    success_count = 0
    skip_count = 0
    current = already_done + 1

    for msg_id in range(start_from, MSG_END + 1):
        result = await process_message(msg_id, current, total_range)

        if result:
            await save_progress(msg_id)   # ← Saved Messages mein save
            success_count += 1
            current += 1
            if msg_id < MSG_END:
                await asyncio.sleep(DELAY)
        else:
            # FloodWait ya retry case — same msg dobara try karo
            log.info(f"🔁 MSG {msg_id} retry ho raha hai...")
            result2 = await process_message(msg_id, current, total_range)
            if result2:
                await save_progress(msg_id)
                success_count += 1
                current += 1
                await asyncio.sleep(DELAY)
            else:
                skip_count += 1
                current += 1

    log.info("=" * 55)
    log.info("🎉 Bulk forward COMPLETE!")
    log.info(f"   ✅ Processed : {success_count}")
    log.info(f"   ⏭  Skipped   : {skip_count}")
    log.info(f"   📦 Total     : {total_range}")
    log.info("=" * 55)


with client:
    client.loop.run_until_complete(main())
