"""
Discord Bot: automatically detects Instagram Reels links in channels,
downloads the video, sends it to the Gemini API for a content summary,
and replies in the channel.

Required environment variables (.env):
    DISCORD_BOT_TOKEN=your discord bot token
    GEMINI_API_KEY=your Gemini API key

Install dependencies:
    pip install -U discord.py yt-dlp google-genai python-dotenv --break-system-packages

Run:
    python bot.py
"""

import os
import re
import time
import random
import asyncio
import logging
import tempfile
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

# ---------- Basic setup ----------

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Also matches regular instagram.com/p/ posts (some reels use the /p/ format);
# adjust as needed
IG_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_\-]+/?[^\s]*)",
    re.IGNORECASE,
)

MAX_VIDEO_MB = 100  # Skip videos larger than this to avoid burning bandwidth/quota
GEMINI_MODEL = "gemini-flash-latest"  # Always points at the latest stable Flash; switch to gemini-pro-latest for higher accuracy

# Free-tier RPM is very low: only let one video hit Gemini at a time and make
# everyone else queue, so multiple links posted at once don't blow the quota
GEMINI_CONCURRENCY = 1
gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENCY)

# Retry settings (for 429 quota-exceeded / 503 transient errors)
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 5  # Backoff grows exponentially: 5s, 10s, 20s, 40s...

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ig-reels-bot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


class QuotaExceededError(Exception):
    """Still hitting 429/quota exhaustion after all retries; lets the caller show a friendly message."""
    pass


def _is_retryable_error(e: Exception) -> bool:
    """Check whether an error is transient (429 quota exceeded, 503 server busy, etc.) and worth retrying."""
    msg = str(e)
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code in (429, 503):
        return True
    return any(kw in msg for kw in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))


def _call_with_retry(func, *args, **kwargs):
    """Call the Gemini API with exponential backoff; raise QuotaExceededError if all retries fail."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if not _is_retryable_error(e) or attempt == MAX_RETRIES:
                raise
            wait = BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 2)
            logger.warning(
                f"Gemini request hit a transient error (attempt {attempt + 1}), "
                f"retrying in {wait:.1f}s: {e}"
            )
            time.sleep(wait)
    raise QuotaExceededError(str(last_error))


# ---------- Core features ----------

def download_reel(url: str, out_dir: str) -> Path | None:
    """Download an IG reel with yt-dlp and return the file path, or None on failure."""
    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # To fetch content that requires login, add:
        # "cookiefile": "cookies.txt",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            return Path(filepath)
    except Exception as e:
        logger.error(f"yt-dlp download failed: {e}")
        return None


def _upload_file(video_path: Path):
    return gemini_client.files.upload(file=str(video_path))


def _wait_for_processing(uploaded_file):
    """Wait for Gemini to finish processing the video (transcode/index), usually seconds to tens of seconds."""
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = gemini_client.files.get(name=uploaded_file.name)
    if uploaded_file.state.name == "FAILED":
        raise RuntimeError("Gemini video processing failed")
    return uploaded_file


def _generate_summary(uploaded_file):
    prompt = (
        "請完整看過這支短影片（包含畫面內容與聲音對話），"
        "用繁體中文寫一份簡短摘要，包含：\n"
        "1. 影片主題／內容重點（2-4 句話）\n"
        "2. 如果有講話或字幕，摘要重點訊息\n"
        "3. 影片的整體語氣或風格（例如：教學、搞笑、開箱、劇情等）\n"
        "請用條列式呈現，不要太長。"
    )
    return gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[uploaded_file, prompt],
    )


def summarize_video_with_gemini(video_path: Path) -> str:
    """
    Upload the video to Gemini and ask it to produce a summary (synchronous, blocking).
    Upload, processing wait, and generation each have their own retry wrapper:
    429 (quota exceeded) and 503 (server busy) trigger automatic backoff retries;
    once retries are exhausted, QuotaExceededError is raised for the caller
    to show a friendly message.
    """
    uploaded_file = _call_with_retry(_upload_file, video_path)
    uploaded_file = _call_with_retry(_wait_for_processing, uploaded_file)
    response = _call_with_retry(_generate_summary, uploaded_file)

    # Clean up the cloud file to free quota (failure here doesn't affect the
    # summary, so no retry)
    try:
        gemini_client.files.delete(name=uploaded_file.name)
    except Exception:
        pass

    return response.text


# ---------- Discord events ----------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    urls = IG_URL_PATTERN.findall(message.content)
    if not urls:
        await bot.process_commands(message)
        return

    for url in urls:
        asyncio.create_task(handle_reel(message, url))

    await bot.process_commands(message)


async def handle_reel(message: discord.Message, url: str):
    status_msg = await message.reply(f"🔎 Reels link detected, downloading...\n{url}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Download the video (blocking work goes to a thread so the event loop stays free)
        video_path = await asyncio.to_thread(download_reel, url, tmp_dir)

        if video_path is None or not video_path.exists():
            await status_msg.edit(content=f"❌ Download failed — possibly a private account, dead link, or blocked by IG.\n{url}")
            return

        size_mb = video_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_VIDEO_MB:
            await status_msg.edit(content=f"⚠️ Video is {size_mb:.1f}MB, over the size limit — skipping summary.\n{url}")
            return

        # Free-tier RPM is very low: the semaphore makes videos queue for Gemini
        # so simultaneous links don't fire concurrent requests and trigger 429s
        if gemini_semaphore.locked():
            await status_msg.edit(content=f"⏳ Another video is still being analyzed, waiting in queue...\n{url}")

        async with gemini_semaphore:
            await status_msg.edit(content=f"🤖 AI is analyzing (this may take a moment)...\n{url}")

            try:
                summary = await asyncio.to_thread(summarize_video_with_gemini, video_path)
            except QuotaExceededError:
                logger.warning(f"Gemini quota exhausted: {url}")
                await status_msg.edit(
                    content=(
                        f"⏸️ Gemini free-tier quota is exhausted (RPM/RPD limit). "
                        f"Please try again in a few minutes, or repost the link later.\n{url}"
                    )
                )
                return
            except Exception as e:
                logger.exception("Gemini summarization failed")
                await status_msg.edit(content=f"❌ AI summary failed: {e}\n{url}")
                return

        # Discord messages cap at 2000 characters; truncate if needed
        if len(summary) > 1900:
            summary = summary[:1900] + "\n...(content too long, truncated)"

        embed = discord.Embed(
            title="📽️ Reels Summary",
            description=summary,
            color=discord.Color.purple(),
        )
        embed.add_field(name="Original link", value=url, inline=False)
        embed.set_footer(text=f"Summary generated by {GEMINI_MODEL}")

        await status_msg.edit(content=None, embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
