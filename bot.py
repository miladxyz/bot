"""
Telegram music bot — search a song, tap a result (or reply with its number),
get it back as an audio file.

Termux setup:
    pkg install python ffmpeg nodejs
    pip install -U python-telegram-bot yt-dlp
    export BOT_TOKEN="123456:ABC..."
    python bot.py
"""

import asyncio
import html
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ["BOT_TOKEN"]
RESULTS_PER_SEARCH = 5
MAX_DURATION = 1500                    # seconds; skip anything over ~25 min
MAX_UPLOAD_BYTES = 49 * 1024 * 1024    # Bot API caps uploads at 50 MB
AUDIO_QUALITY = "192"                  # mp3 kbps
DEBUG_ERRORS = True                    # show real error text in chat

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")


# --------------------------------------------------------------------------- #
# Startup guards
# --------------------------------------------------------------------------- #

def acquire_single_instance_lock():
    """Bind an abstract unix socket. Freed automatically when the process dies."""
    lock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        lock.bind("\0musicbot_lock")
    except OSError:
        raise SystemExit(
            "Another instance is already running.\n"
            "Kill it first:  pkill -f bot.py   (also check: tmux ls)"
        )
    return lock


def check_ffmpeg():
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise SystemExit(
            f"Missing: {', '.join(missing)}\n"
            "Install with:  pkg install ffmpeg   (Termux)  /  apt install ffmpeg"
        )


# --------------------------------------------------------------------------- #
# yt-dlp helpers (blocking — always call via asyncio.to_thread)
# --------------------------------------------------------------------------- #

SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",      # metadata only, fast
    "skip_download": True,
    "source_address": "0.0.0.0",        # force IPv4 — YouTube blocks IPv6 harder
}

DOWNLOAD_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,                 # keeps the log readable
    "source_address": "0.0.0.0",
    "retries": 3,
    "extractor_args": {"youtube": {"formats": ["missing_pot"]}},
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": AUDIO_QUALITY,
        },
        {"key": "FFmpegMetadata"},      # ID3 title/artist tags
    ],
}


def yt_search(query: str, limit: int = RESULTS_PER_SEARCH) -> list[dict]:
    with YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [e for e in (info or {}).get("entries", []) if e]


def yt_download(video_id: str, outdir: str) -> tuple[Path, dict]:
    opts = dict(DOWNLOAD_OPTS)
    opts["outtmpl"] = str(Path(outdir) / "%(id)s.%(ext)s")

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=True
        )

    mp3 = Path(outdir) / f"{info['id']}.mp3"
    if not mp3.exists():
        candidates = list(Path(outdir).glob("*.mp3"))
        if not candidates:
            raise FileNotFoundError("conversion produced no mp3")
        mp3 = candidates[0]
    return mp3, info


def fmt_duration(seconds) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎵 Send me a song name and I'll send it back as audio.\n\n"
        "Pick a result by tapping a button, or just reply with its number."
    )


async def do_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    status = await update.message.reply_text(
        f"🔎 Searching for <i>{html.escape(query)}</i>…", parse_mode="HTML"
    )
    try:
        results = await asyncio.to_thread(yt_search, query)
    except Exception as e:
        log.exception("search failed")
        await status.edit_text(f"❌ Search failed: {type(e).__name__}")
        return

    results = [
        r for r in results
        if not r.get("duration") or r["duration"] <= MAX_DURATION
    ][:RESULTS_PER_SEARCH]

    if not results:
        await status.edit_text("Nothing found. Try different wording.")
        return

    # remember for the number-reply shortcut
    ctx.user_data["last_results"] = [r["id"] for r in results]

    rows, lines = [], []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Unknown")
        uploader = r.get("uploader") or r.get("channel") or "—"
        lines.append(
            f"<b>{i}.</b> {html.escape(title[:70])}\n"
            f"    <i>{html.escape(uploader[:40])} · {fmt_duration(r.get('duration'))}</i>"
        )
        rows.append(InlineKeyboardButton(str(i), callback_data=f"dl:{r['id']}"))

    await status.edit_text(
        "\n".join(lines) + "\n\nTap a number (or reply with it):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([rows]),
        disable_web_page_preview=True,
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()[:200]
    if not text:
        return

    # bare number → a pick from the last search, not a new query
    last = ctx.user_data.get("last_results")
    if text.isdigit() and last and 1 <= int(text) <= len(last):
        await deliver(update, ctx, last[int(text) - 1], update.message.chat_id)
        return

    await do_search(update, ctx, text)


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Working on it…")
    await deliver(update, ctx, q.data.split(":", 1)[1], q.message.chat_id, query=q)


async def deliver(update, ctx, video_id: str, chat_id: int, query=None) -> None:
    """Download video_id and send it to chat_id. `query` = callback to edit, if any."""
    status = None

    async def say(text: str):
        nonlocal status
        if query is not None:
            await query.edit_message_text(text)
        elif status is None:
            status = await update.message.reply_text(text)
        else:
            await status.edit_text(text)

    await say("⬇️ Downloading & converting…")
    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)

    tmp = tempfile.mkdtemp(prefix="musicbot_")
    try:
        path, info = await asyncio.to_thread(yt_download, video_id, tmp)

        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            await say(
                f"⚠️ {size / 1024 / 1024:.0f} MB — over Telegram's 50 MB bot limit. "
                "Pick a shorter version."
            )
            return

        await say("⬆️ Uploading…")
        with path.open("rb") as fh:
            await ctx.bot.send_audio(
                chat_id=chat_id,
                audio=fh,
                title=(info.get("track") or info.get("title", "Unknown"))[:64],
                performer=(info.get("artist") or info.get("uploader") or "Unknown")[:64],
                duration=int(info.get("duration") or 0),
                filename=f"{info.get('title', 'audio')[:60]}.mp3",
                read_timeout=180,
                write_timeout=180,
            )

        if query is not None:
            await query.delete_message()
        elif status is not None:
            await status.delete()

    except Exception as e:
        log.exception("download failed for %s", video_id)
        msg = (
            f"❌ Failed: {type(e).__name__}: {str(e)[:300]}"
            if DEBUG_ERRORS
            else "❌ Couldn't fetch that one. Try another result."
        )
        try:
            await say(msg)
        except Exception:
            await ctx.bot.send_message(chat_id, msg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Conflict

    if isinstance(ctx.error, Conflict):
        log.error(
            "Conflict: another instance is polling this token. "
            "Run: pkill -f bot.py  (and check tmux ls)"
        )
        return
    log.error("unhandled error", exc_info=ctx.error)


# --------------------------------------------------------------------------- #

def main() -> None:
    _lock = acquire_single_instance_lock()   # held for the process lifetime
    check_ffmpeg()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    log.info("Bot running. Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
