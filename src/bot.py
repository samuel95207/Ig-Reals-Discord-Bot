"""
Discord Bot: 自動偵測頻道中的 Instagram Reels 連結，
下載影片後丟給 Gemini API 做內容摘要，並回覆到頻道。

需要環境變數 (.env)：
    DISCORD_BOT_TOKEN=你的 discord bot token
    GEMINI_API_KEY=你的 Gemini API key

安裝依賴：
    pip install -U discord.py yt-dlp google-genai python-dotenv --break-system-packages

執行：
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

# ---------- 基本設定 ----------

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# 也支援一般 instagram.com/p/ 貼文（有些 reel 會用 /p/ 格式），可自行增減
IG_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_\-]+/?[^\s]*)",
    re.IGNORECASE,
)

MAX_VIDEO_MB = 100  # 超過這個大小就跳過，避免佔用太多流量/額度
GEMINI_MODEL = "gemini-flash-latest"  # 永遠指向最新的穩定版 Flash，想要更準確可換成 gemini-pro-latest

# 免費層 RPM 很低，同一時間只讓一支影片跑 Gemini 請求，其他人排隊等
# 避免多人同時貼連結時瞬間炸開額度
GEMINI_CONCURRENCY = 1
gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENCY)

# 重試設定（針對 429 額度超限 / 503 暫時性錯誤）
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 5  # 每次重試間隔會指數成長：5s, 10s, 20s, 40s...

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ig-reels-bot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


class QuotaExceededError(Exception):
    """重試多次後仍然遇到 429/額度用盡，讓上層可以顯示友善訊息。"""
    pass


def _is_retryable_error(e: Exception) -> bool:
    """判斷是否為暫時性錯誤（額度超限 429、伺服器忙碌 503 等），值得重試。"""
    msg = str(e)
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code in (429, 503):
        return True
    return any(kw in msg for kw in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))


def _call_with_retry(func, *args, **kwargs):
    """對 Gemini API 呼叫做指數退避重試，最後仍失敗就拋出 QuotaExceededError。"""
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
                f"Gemini 請求遇到暫時性錯誤（第 {attempt + 1} 次），"
                f"{wait:.1f} 秒後重試: {e}"
            )
            time.sleep(wait)
    raise QuotaExceededError(str(last_error))


# ---------- 核心功能 ----------

def download_reel(url: str, out_dir: str) -> Path | None:
    """用 yt-dlp 下載 IG reel 影片，回傳檔案路徑。失敗回傳 None。"""
    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # 如果要抓需要登入的帳號內容，可以加上：
        # "cookiefile": "cookies.txt",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            return Path(filepath)
    except Exception as e:
        logger.error(f"yt-dlp 下載失敗: {e}")
        return None


def _upload_file(video_path: Path):
    return gemini_client.files.upload(file=str(video_path))


def _wait_for_processing(uploaded_file):
    """等待 Gemini 端處理完影片（轉檔/索引），通常幾秒到幾十秒。"""
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = gemini_client.files.get(name=uploaded_file.name)
    if uploaded_file.state.name == "FAILED":
        raise RuntimeError("Gemini 影片處理失敗")
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
    把影片上傳給 Gemini，請它看完影片後產生摘要（同步阻塞呼叫）。
    上傳、等待處理、產生內容這三步都各自包了重試機制，
    遇到 429（額度超限）或 503（伺服器忙碌）會自動退避重試；
    重試用盡則拋出 QuotaExceededError，由呼叫端顯示友善訊息。
    """
    uploaded_file = _call_with_retry(_upload_file, video_path)
    uploaded_file = _call_with_retry(_wait_for_processing, uploaded_file)
    response = _call_with_retry(_generate_summary, uploaded_file)

    # 清理雲端檔案，避免占用配額（這步失敗不影響摘要結果，不重試）
    try:
        gemini_client.files.delete(name=uploaded_file.name)
    except Exception:
        pass

    return response.text


# ---------- Discord 事件 ----------

@bot.event
async def on_ready():
    logger.info(f"已登入為 {bot.user}")


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
    status_msg = await message.reply(f"🔎 偵測到 Reels 連結，下載中...\n{url}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        # 下載影片（阻塞操作丟到 thread，避免卡住 event loop）
        video_path = await asyncio.to_thread(download_reel, url, tmp_dir)

        if video_path is None or not video_path.exists():
            await status_msg.edit(content=f"❌ 下載失敗，可能是私人帳號、連結失效或被 IG 擋下。\n{url}")
            return

        size_mb = video_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_VIDEO_MB:
            await status_msg.edit(content=f"⚠️ 影片大小 {size_mb:.1f}MB 超過上限，略過摘要。\n{url}")
            return

        # 免費層 RPM 很低，用 semaphore 讓多支影片排隊送 Gemini，
        # 避免多人同時貼連結時同時發request觸發 429
        if gemini_semaphore.locked():
            await status_msg.edit(content=f"⏳ 前面還有影片在分析，排隊等候中...\n{url}")

        async with gemini_semaphore:
            await status_msg.edit(content=f"🤖 AI 分析中（可能需要一點時間）...\n{url}")

            try:
                summary = await asyncio.to_thread(summarize_video_with_gemini, video_path)
            except QuotaExceededError:
                logger.warning(f"Gemini 額度已用盡: {url}")
                await status_msg.edit(
                    content=(
                        f"⏸️ 目前 Gemini 免費額度已用盡（RPM/RPD 上限），"
                        f"請稍後幾分鐘再試一次，或晚點重新貼連結。\n{url}"
                    )
                )
                return
            except Exception as e:
                logger.exception("Gemini 摘要失敗")
                await status_msg.edit(content=f"❌ AI 摘要失敗：{e}\n{url}")
                return

        # Discord 單則訊息上限 2000 字，超過就截斷
        if len(summary) > 1900:
            summary = summary[:1900] + "\n...(內容過長，已截斷)"

        embed = discord.Embed(
            title="📽️ Reels 摘要",
            description=summary,
            color=discord.Color.purple(),
        )
        embed.add_field(name="原始連結", value=url, inline=False)
        embed.set_footer(text=f"由 {GEMINI_MODEL} 產生摘要")

        await status_msg.edit(content=None, embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
