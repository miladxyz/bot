import asyncio
import html
import logging
import os
import shutil
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
MAX_DURATION = 1500          # seconds; skip anything longer than ~25 min
MAX_UPLOAD_BYTES = 49 * 1024 * 1024   # Bot API caps uploads at 50 MB
AUDIO_QUALITY = "192"        # mp3 kbps

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")


# --------------------------------------------------------------------------- #
# yt-dlp helpers (blocking — always call these via asyncio.to_thread)
# --------------------------------------------------------------------------- #

SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",   # metadata only, no per-video page fetch
    "skip_download": True,
}


def yt_search(query: str, limit: int = RESULTS_PER_SEARCH) -> list[dict]:
    with YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [e for e in (info or {}).get("entries", []) if e]


def yt_download(video_id: str, outdir: str) -> tuple[Path, dict]:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(outdir) / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_QUALITY,
            },
            {"key": "FFmpegMetadata"},      # writes title/artist ID3 tags
            {"key": "EmbedThumbnail"},      # cover art
        ],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=True
        )

    mp3 = Path(outdir) / f"{info['id']}.mp3"
    if not mp3.exists():                    # fallback if naming differs
        candidates = list(Path(outdir).glob("*.mp3"))
        if not candidates:
            raise FileNotFoundError("conversion produced no mp3")
        mp3 = candidates[0]
    return mp3, info


def fmt_duration(seconds) -> str:
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎵 Send me a song name or a YouTube link and I'll send it back as audio.\n\n"
        "Example: <code>bohemian rhapsody</code>",
        parse_mode="HTML",
    )


async def handle_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()[:200]
    if not query:
        return

    status = await update.message.reply_text(f"🔎 Searching for <i>{html.escape(query)}</i>…",
                                             parse_mode="HTML")
    try:
        results = await asyncio.to_thread(yt_search, query)
    except Exception:
        log.exception("search failed")
        await status.edit_text("❌ Search failed. Try again in a moment.")
        return

    results = [r for r in results if not r.get("duration")
               or r["duration"] <= MAX_DURATION][:RESULTS_PER_SEARCH]

    if not results:
        await status.edit_text("Nothing found. Try different wording.")
        return

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
        "\n".join(lines) + "\n\nTap a number to download:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([rows]),
        disable_web_page_preview=True,
    )


async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Working on it…")

    video_id = q.data.split(":", 1)[1]
    await q.edit_message_text("⬇️ Downloading & converting…")
    await ctx.bot.send_chat_action(q.message.chat_id, ChatAction.UPLOAD_VOICE)

    tmp = tempfile.mkdtemp(prefix="musicbot_")
    try:
        path, info = await asyncio.to_thread(yt_download, video_id, tmp)

        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            await q.edit_message_text(
                f"⚠️ That file is {size / 1024 / 1024:.0f} MB — over Telegram's "
                "50 MB bot limit. Pick a shorter version."
            )
            return

        await q.edit_message_text("⬆️ Uploading…")
        with path.open("rb") as fh:
            await ctx.bot.send_audio(
                chat_id=q.message.chat_id,
                audio=fh,
                title=(info.get("track") or info.get("title", "Unknown"))[:64],
                performer=(info.get("artist") or info.get("uploader") or "Unknown")[:64],
                duration=int(info.get("duration") or 0),
                filename=f"{info.get('title', 'audio')[:60]}.mp3",
                read_timeout=120,
                write_timeout=120,
            )
        await q.delete_message()

    except Exception:
        log.exception("download failed for %s", video_id)
        await q.edit_message_text("❌ Couldn't fetch that one. Try another result.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=ctx.error)


# --------------------------------------------------------------------------- #

def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)     # serve several users at once
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    app.add_error_handler(on_error)

    log.info("Bot running. Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
