"""
Discord Bot: automatically detects Instagram Reels links in channels,
downloads the video, sends it to the Gemini API for a content summary,
and replies in the channel.

Required environment variables (.env):
    DISCORD_BOT_TOKEN=your discord bot token
    GEMINI_API_KEYS=comma-separated Gemini API keys (or GEMINI_API_KEY for one)

Install dependencies:
    pip install -U discord.py yt-dlp google-genai python-dotenv --break-system-packages

Run:
    python bot.py
"""

import os
import re
import json
import time
import random
import asyncio
import logging
import tempfile
from datetime import datetime, timezone
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
# Comma-separated list of Gemini API keys, tried in order: when one key's
# quota is exhausted the bot switches to the next, and only waits for a
# refill once every key is exhausted. Falls back to the single-key
# GEMINI_API_KEY for backward compatibility.
GEMINI_API_KEYS = [
    key.strip()
    for key in os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")).split(",")
    if key.strip()
]
if not GEMINI_API_KEYS:
    raise RuntimeError("Set GEMINI_API_KEYS (comma-separated) or GEMINI_API_KEY")

# Reels links only: /p/ posts are often photos with no video, so the bot
# ignores them entirely (no download attempt, no reply)
IG_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:reel|reels)/[A-Za-z0-9_\-]+/?[^\s]*)",
    re.IGNORECASE,
)

MAX_VIDEO_MB = 100  # Skip videos larger than this to avoid burning bandwidth/quota

# Optional Instagram session cookies (Netscape cookies.txt format). When the
# file exists, yt-dlp uses the logged-in session, which allows downloading
# age/audience-restricted reels. See README for how to export it.
IG_COOKIE_FILE = os.environ.get("IG_COOKIE_FILE", "/app/cookies/cookies.txt")

# Persistent record of already-analyzed reels, so the same reel is never sent
# to Gemini twice; instead the bot points at whoever shared it first.
SEEN_REELS_FILE = os.environ.get("SEEN_REELS_FILE", "/app/data/seen_reels.json")
GEMINI_MODEL = "gemini-flash-latest"  # Always points at the latest stable Flash; switch to gemini-pro-latest for higher accuracy

# Free-tier RPM is very low: only let one video hit Gemini at a time and make
# everyone else queue, so multiple links posted at once don't blow the quota
GEMINI_CONCURRENCY = 1
gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENCY)

# Retry settings for 503-style transient errors. 429 quota exhaustion is not
# capped: the bot waits for the quota to refill (using Google's suggested
# retry delay) and retries until it succeeds.
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 5  # Backoff grows exponentially: 5s, 10s, 20s, 40s...

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ig-reels-bot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# One client per key; uploads are tied to the key that made them, so a key
# switch restarts the whole upload → analyze pipeline on the new client
gemini_clients = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]

# Keys found to be denied/invalid (e.g. 403 PERMISSION_DENIED) are dropped
# from rotation until the bot restarts
disabled_gemini_keys: set[int] = set()


# The shortcode uniquely identifies a reel; tracking params like ?igsh=... vary
# per sender, so dedupe on the shortcode rather than the full URL
SHORTCODE_PATTERN = re.compile(
    r"instagram\.com/(?:reel|reels)/([A-Za-z0-9_\-]+)", re.IGNORECASE
)


def reel_key(url: str) -> str:
    match = SHORTCODE_PATTERN.search(url)
    return match.group(1) if match else url


def _load_seen_reels() -> dict:
    try:
        with open(SEEN_REELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Failed to load {SEEN_REELS_FILE}, starting empty: {e}")
        return {}


def _save_seen_reels():
    try:
        os.makedirs(os.path.dirname(SEEN_REELS_FILE), exist_ok=True)
        tmp_path = SEEN_REELS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(seen_reels, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SEEN_REELS_FILE)
    except Exception as e:
        logger.error(f"Failed to save {SEEN_REELS_FILE}: {e}")


seen_reels: dict = _load_seen_reels()

# Reels currently being analyzed, so the same reel posted twice in quick
# succession doesn't get processed twice (all mutation happens on the event
# loop, so no lock is needed)
in_progress_reels: set[str] = set()


def _is_quota_error(e: Exception) -> bool:
    """429 / RESOURCE_EXHAUSTED: out of quota, will succeed again once it refills."""
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code == 429:
        return True
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _is_transient_error(e: Exception) -> bool:
    """503 / UNAVAILABLE: server busy, worth a few quick retries."""
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code == 503:
        return True
    msg = str(e)
    return "503" in msg or "UNAVAILABLE" in msg


def _parse_retry_delay_seconds(e: Exception) -> float | None:
    """Extract Google's suggested retry delay from a 429 error, if present."""
    msg = str(e)
    match = re.search(r"[Rr]etry in ([0-9.]+)s", msg)
    if match:
        return float(match.group(1))
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?([0-9.]+)s", msg)
    if match:
        return float(match.group(1))
    return None


class QuotaExhaustedError(Exception):
    """One key's quota is exhausted; the caller switches keys or waits for a refill."""

    def __init__(self, original: Exception):
        super().__init__(str(original))
        self.retry_delay = _parse_retry_delay_seconds(original)


class KeyUnusableError(Exception):
    """The key or its project is denied/invalid; drop it from rotation."""


def _is_key_unusable_error(e: Exception) -> bool:
    """403 PERMISSION_DENIED / invalid API key: permanent for this key, unlike quota."""
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code == 403:
        return True
    msg = str(e)
    return any(kw in msg for kw in ("PERMISSION_DENIED", "API_KEY_INVALID", "API key not valid"))


async def _call_with_retry(func, *args):
    """
    Run a blocking Gemini call in a thread, retrying automatically.
    503-style transient errors back off exponentially up to MAX_RETRIES.
    429 quota exhaustion raises QuotaExhaustedError so the caller can
    switch to a backup key (or wait for a refill when none are left).
    """
    transient_attempts = 0
    while True:
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as e:
            if _is_quota_error(e):
                raise QuotaExhaustedError(e) from e
            if _is_key_unusable_error(e):
                raise KeyUnusableError(str(e)) from e
            if _is_transient_error(e) and transient_attempts < MAX_RETRIES:
                transient_attempts += 1
                wait = BASE_BACKOFF_SECONDS * (2 ** (transient_attempts - 1)) + random.uniform(0, 2)
                logger.warning(
                    f"Gemini request hit a transient error (attempt {transient_attempts}), "
                    f"retrying in {wait:.1f}s: {e}"
                )
                await asyncio.sleep(wait)
            else:
                raise


# ---------- Core features ----------

def _ydl_download(url: str, out_dir: str, use_cookies: bool) -> Path:
    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if use_cookies:
        ydl_opts["cookiefile"] = IG_COOKIE_FILE
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def download_reel(url: str, out_dir: str) -> Path | None:
    """
    Download an IG reel with yt-dlp and return the file path, or None on failure.
    Tries an anonymous session first so the logged-in account isn't burned on
    public reels; only when that fails (age/audience-restricted, login-walled)
    does it retry with the session cookies.
    """
    try:
        return _ydl_download(url, out_dir, use_cookies=False)
    except Exception as e:
        anon_error = e

    if not os.path.exists(IG_COOKIE_FILE):
        logger.error(f"yt-dlp anonymous download failed (no cookie file to fall back on): {anon_error}")
        return None

    logger.info(f"Anonymous download failed, retrying with logged-in session: {anon_error}")
    try:
        return _ydl_download(url, out_dir, use_cookies=True)
    except Exception as e:
        logger.error(f"yt-dlp download failed with logged-in session too: {e}")
        return None


def _upload_file(client, video_path: Path):
    return client.files.upload(file=str(video_path))


def _wait_for_processing(client, uploaded_file):
    """Wait for Gemini to finish processing the video (transcode/index), usually seconds to tens of seconds."""
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)
    if uploaded_file.state.name == "FAILED":
        raise RuntimeError("Gemini video processing failed")
    return uploaded_file


def _generate_summary(client, uploaded_file):
    prompt = (
        "請完整看過這支短影片（包含畫面內容與聲音對話），"
        "用繁體中文寫一段簡短摘要，描述影片的主題與內容重點，"
        "如果有講話或字幕請帶到關鍵訊息，並點出影片的整體語氣或風格"
        "（例如：教學、搞笑、開箱、劇情等）。"
        "請以自然的短文呈現，不要使用條列式，用兩三句話即可，"
        "整體輸出總長度嚴格控制在大約 100 字以內。"
    )
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[uploaded_file, prompt],
    )


async def _summarize_with_client(client, video_path: Path) -> str:
    """Run the full upload → process → summarize pipeline on one client/key."""
    uploaded_file = await _call_with_retry(_upload_file, client, video_path)
    try:
        uploaded_file = await _call_with_retry(_wait_for_processing, client, uploaded_file)
        response = await _call_with_retry(_generate_summary, client, uploaded_file)
    finally:
        # Clean up the cloud file to free quota (failure here doesn't affect
        # the summary, so no retry)
        try:
            await asyncio.to_thread(client.files.delete, name=uploaded_file.name)
        except Exception:
            pass
    return response.text


async def summarize_video_with_gemini(video_path: Path, status_msg, url: str) -> str:
    """
    Upload the video to Gemini and ask it to produce a summary.
    Keys are tried in order: when one's quota is exhausted the next takes
    over (restarting the pipeline, since uploads are key-scoped). Once every
    key is exhausted, wait for the quota to refill — using Google's suggested
    retry delay when available — and start over, for as long as it takes.
    """
    round_num = 0
    while True:
        usable = [i for i in range(len(gemini_clients)) if i not in disabled_gemini_keys]
        if not usable:
            raise RuntimeError(
                "All Gemini API keys are denied or invalid — check them in Google AI Studio"
            )

        retry_delays = []
        for idx in usable:
            try:
                return await _summarize_with_client(gemini_clients[idx], video_path)
            except QuotaExhaustedError as e:
                retry_delays.append(e.retry_delay)
                logger.warning(f"Gemini key #{idx + 1} quota exhausted, trying next key")
            except KeyUnusableError as e:
                disabled_gemini_keys.add(idx)
                logger.error(
                    f"Gemini key #{idx + 1} is denied/invalid, dropping it from rotation: {e}"
                )

        if not retry_delays:
            # Every key we tried this round was just disabled; the check at the
            # top of the loop decides whether anything is left
            continue

        round_num += 1
        known_delays = [d for d in retry_delays if d]
        wait = (min(known_delays) if known_delays else min(60.0 * round_num, 300.0)) + random.uniform(1, 3)
        logger.warning(f"All Gemini keys exhausted (round {round_num}), waiting {wait:.0f}s for a refill")
        if status_msg is not None:
            try:
                await status_msg.edit(
                    content=(
                        f"⏸️ All Gemini API keys are out of quota — waiting {int(wait)}s for a "
                        f"refill, then retrying automatically (attempt {round_num})...\n{url}"
                    )
                )
            except discord.HTTPException:
                pass
        await asyncio.sleep(wait)
        if status_msg is not None:
            try:
                await status_msg.edit(content=f"🤖 AI is analyzing (this may take a moment)...\n{url}")
            except discord.HTTPException:
                pass


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


def _duplicate_embed(record: dict) -> discord.Embed:
    posted_at = ""
    try:
        ts = int(datetime.fromisoformat(record["timestamp"]).timestamp())
        posted_at = f" <t:{ts}:R>"
    except Exception:
        pass
    embed = discord.Embed(
        title="🔁 This reel was already shared",
        description=(
            f"First shared by <@{record['author_id']}>{posted_at} — "
            f"[jump to the original message]({record['jump_url']})"
        ),
        color=discord.Color.orange(),
    )
    summary = record.get("summary")
    if summary:
        # Embed field values cap at 1024 characters
        if len(summary) > 1000:
            summary = summary[:1000] + "\n...(truncated)"
        embed.add_field(name="Earlier summary", value=summary, inline=False)
    return embed


async def handle_reel(message: discord.Message, url: str):
    key = reel_key(url)

    record = seen_reels.get(key)
    if record:
        logger.info(f"Skipping already-analyzed reel {key} (first shared by {record.get('author_name')})")
        await message.reply(embed=_duplicate_embed(record), mention_author=False)
        return

    if key in in_progress_reels:
        await message.reply(
            f"⏳ This reel was just posted and is already being analyzed — the summary will appear above.\n{url}",
            mention_author=False,
        )
        return

    in_progress_reels.add(key)
    try:
        await _analyze_reel(message, url, key)
    finally:
        in_progress_reels.discard(key)


async def _analyze_reel(message: discord.Message, url: str, key: str):
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
        # so simultaneous links don't fire concurrent requests and trigger 429s.
        # A quota wait deliberately holds the semaphore — nothing behind it
        # could succeed without quota either.
        if gemini_semaphore.locked():
            await status_msg.edit(content=f"⏳ Another video is still being analyzed, waiting in queue...\n{url}")

        async with gemini_semaphore:
            await status_msg.edit(content=f"🤖 AI is analyzing (this may take a moment)...\n{url}")

            try:
                summary = await summarize_video_with_gemini(video_path, status_msg, url)
            except Exception as e:
                logger.exception("Gemini summarization failed")
                await status_msg.edit(content=f"❌ AI summary failed: {e}\n{url}")
                return

        # Record the successful analysis so this reel is never re-analyzed
        seen_reels[key] = {
            "url": url,
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "jump_url": message.jump_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
        }
        _save_seen_reels()

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
