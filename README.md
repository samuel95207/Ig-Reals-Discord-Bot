# IG Reels 摘要 Discord Bot

頻道裡有人貼 Instagram Reels 連結時，機器人會自動下載影片、丟給 Gemini 看完整支影片（畫面＋聲音），並回覆重點摘要。

## 安裝

```bash
pip install -r requirements.txt --break-system-packages
```

另外需要系統裝 `ffmpeg`（yt-dlp 轉檔會用到）：

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## 設定

1. 複製 `.env.example` 為 `.env`，填入：
   - `DISCORD_BOT_TOKEN`：到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Bot 取得
   - `GEMINI_API_KEY`：到 [Google AI Studio](https://aistudio.google.com/apikey) 取得

2. 在 Discord Developer Portal 的 Bot 設定裡，打開 **Message Content Intent**（必須，否則抓不到訊息內容）。

3. 邀請 bot 進伺服器時，至少要給以下權限：
   - View Channels
   - Send Messages
   - Read Message History
   - Embed Links

## 執行

```bash
python src/bot.py
```

## 運作流程

1. 監聽頻道訊息，用正則抓 `instagram.com/reel/`、`/reels/`、`/p/` 開頭的連結
2. 用 `yt-dlp` 下載影片到暫存資料夾
3. 把影片檔上傳到 Gemini File API
4. 請 Gemini（預設 `gemini-2.5-flash`）看完整支影片，產生繁中摘要
5. 用 Embed 格式回覆到原訊息串

## 額度保護機制

免費層的 RPM/RPD 上限偏低，且 Google 官方額度會隨帳號等級動態調整（實際數字請以 [AI Studio 的即時額度頁面](https://aistudio.google.com) 為準，網路上的文章數字不一定準確）。程式內建了兩層保護：

1. **請求佇列**：`GEMINI_CONCURRENCY = 1`，同一時間只有一支影片會送給 Gemini，其他人的連結會排隊等候（bot 會回覆「排隊中」提示），避免多人同時貼連結時瞬間炸開 RPM 額度。
2. **自動重試 + 指數退避**：遇到 429（額度超限）或 503（伺服器忙碌）會自動等待後重試（5s → 10s → 20s → 40s，最多 4 次）。若重試用盡仍失敗，會回覆友善訊息請使用者稍後再試，而不是直接噴錯誤訊息。

如果社群較活躍、常常同時有多人貼連結，可以考慮：
- 把 `GEMINI_CONCURRENCY` 調高（但免費層本身 RPM 就低，調高佇列平行數幫助有限，主要還是受限於帳號等級的額度）
- 升級到付費層（不受免費層的低 RPM/RPD 限制，且資料不會被用於訓練）

## 已知限制 / 注意事項

- **私人帳號 / 需要登入的內容抓不到**：如果要支援，需要在 `download_reel()` 的 `ydl_opts` 加上 `cookiefile`，用你自己登入 IG 的瀏覽器 cookies（有帳號被鎖的風險，請自行評估）。
- **IG 反爬蟲會不定期讓 yt-dlp 失效**：遇到大量下載失敗時，通常是 yt-dlp 版本過舊，跑 `pip install -U yt-dlp` 更新即可。
- **抓取 Instagram 內容可能違反其服務條款**，此工具僅供技術示範與個人/私人社群使用，請自行評估法律與帳號風險，不建議用於大規模或商業用途。
- **影片大小上限**：預設超過 100MB 的影片會跳過摘要（`MAX_VIDEO_MB` 可調），避免佔用過多流量與 Gemini 額度。
- **費用**：Gemini API 依用量計費，長影片或高頻率使用請留意額度與費用。
- 若要改用 `gemini-2.5-pro` 以取得更精確的摘要，把 `bot.py` 中的 `GEMINI_MODEL` 改掉即可，但費用較高、速度較慢。
