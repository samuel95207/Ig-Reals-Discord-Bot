# IG Reels Summary Discord Bot

When someone posts an Instagram Reels link in a channel, the bot automatically downloads the video, has Gemini watch the entire clip (visuals + audio), and replies with a summary of the key points.

## Installation

```bash
pip install -r requirements.txt --break-system-packages
```

You also need `ffmpeg` installed on the system (yt-dlp uses it for transcoding):

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## Docker

```bash
docker compose up -d --build
```

Set `DISCORD_BOT_TOKEN` and `GEMINI_API_KEYS` in a `.env` file (see `.env.example`) or as environment variables before starting.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_BOT_TOKEN`: create a bot at the [Discord Developer Portal](https://discord.com/developers/applications)
   - `GEMINI_API_KEYS`: one or more keys from [Google AI Studio](https://aistudio.google.com/apikey), comma-separated. Keys are tried in order: when one's quota is exhausted the bot switches to the next, and only waits for a refill when all are exhausted. (`GEMINI_API_KEY` with a single key still works.)

2. In the Discord Developer Portal, under your app's Bot settings, enable the **Message Content Intent** (required — without it the bot cannot read message content and will fail to start).

3. When inviting the bot to your server, grant at least these permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Embed Links

## Run

```bash
python src/bot.py
```

## How it works

1. Listens to channel messages and matches `instagram.com/reel/`, `/reels/`, and `/p/` links with a regex
2. Downloads the video to a temp directory with `yt-dlp`
3. Uploads the video file to the Gemini File API
4. Asks Gemini (default `gemini-flash-latest`) to watch the whole video and produce a Traditional Chinese summary
5. Replies to the original message thread with an embed

## Quota protection

Free-tier RPM/RPD limits are low, and Google adjusts quotas dynamically per account tier (check the [live quota page in AI Studio](https://aistudio.google.com) for actual numbers — figures found in articles online are often outdated). The bot has two built-in layers of protection:

1. **Request queue**: `GEMINI_CONCURRENCY = 1` means only one video is sent to Gemini at a time; other links wait in line (the bot replies with a "queued" notice), so several links posted at once can't blow through the RPM quota instantly.
2. **Automatic retry with exponential backoff**: on 429 (quota exceeded) or 503 (server busy) errors, the bot waits and retries (5s → 10s → 20s → 40s, up to 4 times). If retries run out, it replies with a friendly "try again later" message instead of a raw error.

If your community is active and often posts several links at once, consider:
- Raising `GEMINI_CONCURRENCY` (though free-tier RPM is the real bottleneck, so this helps only so much)
- Upgrading to the paid tier (no low free-tier RPM/RPD caps, and data is not used for training)

## Known limitations / notes

- **Private accounts / login-gated content can't be fetched**: to support them, add a `cookiefile` to `ydl_opts` in `download_reel()` using browser cookies from your own logged-in IG session (risk of account lockout — evaluate for yourself).
- **IG anti-scraping periodically breaks yt-dlp**: if downloads suddenly start failing en masse, your yt-dlp is usually outdated; run `pip install -U yt-dlp`.
- **Scraping Instagram content may violate its Terms of Service**. This tool is for technical demonstration and personal/private community use only; assess the legal and account risks yourself, and avoid large-scale or commercial use.
- **Video size cap**: videos over 100MB are skipped by default (`MAX_VIDEO_MB` is adjustable) to avoid excessive bandwidth and Gemini quota usage.
- **Cost**: the Gemini API bills by usage; watch your quota and costs with long videos or heavy traffic.
- To get more accurate summaries with `gemini-pro-latest`, change `GEMINI_MODEL` in `src/bot.py` — higher cost and slower responses.
