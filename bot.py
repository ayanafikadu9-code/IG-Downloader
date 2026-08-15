
#!/usr/bin/env python3
"""
Instagram Reels/Video Downloader Telegram Bot
Same architecture as the TikTok bot: Flask ad-verification webhook,
SQLite storage, Telegram Stars premium, colored buttons (Bot API 9.4),
full EN/AM/OM localization, /stats admin command.
Scope: Reels and regular video posts only — Stories are NOT supported
(they require a logged-in session and expire in 24h, out of scope here).
"""

import os
import re
import json
import time
import sqlite3
import secrets
import threading
import subprocess
from typing import Optional

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable required.")

BOT_USERNAME = os.getenv("BOT_USERNAME", "ig_downloadbot")
HOST = os.getenv("HOST", "").rstrip("/")
AD_PAGE_URL = os.getenv("AD_PAGE_URL", "").strip()

# Primary: a hosted API that returns a direct video URL for a Reel/post
# (e.g. a self-hosted or demo instance of Okramjimmy/Instagram-reels-downloader).
# Falls back to yt-dlp if this isn't set or fails.
IG_API_URL = os.getenv("IG_API_URL", "").strip()
IG_API_KEY = os.getenv("IG_API_KEY", "").strip()

# Comma-separated Telegram user IDs allowed to use /stats
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DB_FILE = os.getenv("DB_FILE", "bot_data.db")
_db_lock = threading.Lock()

# Reels and regular posts/videos only. Deliberately excludes /stories/.
IG_RE = re.compile(r"instagram\.com/(reel|reels|p|tv)/", re.IGNORECASE)
IG_STORY_RE = re.compile(r"instagram\.com/stories/", re.IGNORECASE)

flask_app = Flask(__name__)
CORS(flask_app, resources={r"/*": {"origins": "*"}})

# ============ DATABASE ============
def init_db():
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'en',
                last_ig_url TEXT,
                is_lifetime_premium BOOLEAN DEFAULT 0,
                pass_expires_at INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ad_jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER,
                chat_id INTEGER,
                ig_url TEXT,
                status TEXT,
                verified BOOLEAN DEFAULT 0,
                created_at INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                mode TEXT,
                created_at INTEGER
            )
        """)
        conn.commit()
        conn.close()

def _db_exec(query, params=(), fetchone=False, fetchall=False):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c = conn.cursor()
        c.execute(query, params)
        result = c.fetchone() if fetchone else (c.fetchall() if fetchall else None)
        conn.commit()
        conn.close()
        return result

def set_user_language(user_id: int, lang: str):
    _db_exec(
        "INSERT INTO users (user_id, language) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
        (user_id, lang)
    )

def get_user(user_id: int) -> dict:
    row = _db_exec(
        "SELECT user_id, language, last_ig_url, is_lifetime_premium, pass_expires_at FROM users WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    if not row:
        _db_exec("INSERT OR IGNORE INTO users (user_id, language) VALUES (?, ?)", (user_id, "en"))
        return {"user_id": user_id, "language": "en", "last_ig_url": None, "is_lifetime_premium": False, "pass_expires_at": None}
    return {
        "user_id": row[0],
        "language": row[1] or "en",
        "last_ig_url": row[2],
        "is_lifetime_premium": bool(row[3]),
        "pass_expires_at": row[4]
    }

def set_user_ig_url(user_id: int, url: str):
    _db_exec("UPDATE users SET last_ig_url=? WHERE user_id=?", (url, user_id))

def set_lifetime_premium(user_id: int):
    _db_exec("UPDATE users SET is_lifetime_premium=1 WHERE user_id=?", (user_id,))

def grant_temporary_pass(user_id: int, duration_hours: int = 24):
    expires = int(time.time()) + (duration_hours * 3600)
    _db_exec("UPDATE users SET pass_expires_at=? WHERE user_id=?", (expires, user_id))

def user_has_access(user_id: int) -> bool:
    u = get_user(user_id)
    if u.get("is_lifetime_premium"):
        return True
    expires = u.get("pass_expires_at")
    if expires and int(time.time()) < expires:
        return True
    return False

def create_ad_job(user_id: int, chat_id: int, ig_url: str) -> str:
    job_id = secrets.token_hex(16)
    _db_exec(
        "INSERT INTO ad_jobs (job_id, user_id, chat_id, ig_url, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, user_id, chat_id, ig_url, "pending", int(time.time()))
    )
    return job_id

def get_ad_job(job_id: str) -> dict:
    row = _db_exec(
        "SELECT job_id, user_id, chat_id, ig_url, status, verified, created_at FROM ad_jobs WHERE job_id=?",
        (job_id,),
        fetchone=True
    )
    if not row:
        return None
    return {
        "job_id": row[0],
        "user_id": row[1],
        "chat_id": row[2],
        "ig_url": row[3],
        "status": row[4],
        "verified": bool(row[5]),
        "created_at": row[6]
    }

def mark_job_verified(job_id: str):
    _db_exec("UPDATE ad_jobs SET verified=1, status='verified' WHERE job_id=?", (job_id,))

def log_download(user_id: int, mode: str):
    _db_exec(
        "INSERT INTO downloads (user_id, mode, created_at) VALUES (?, ?, ?)",
        (user_id, mode, int(time.time()))
    )

def get_bot_stats() -> dict:
    total_users = _db_exec("SELECT COUNT(*) FROM users", fetchone=True)[0]
    premium_users = _db_exec("SELECT COUNT(*) FROM users WHERE is_lifetime_premium=1", fetchone=True)[0]
    ad_views = _db_exec("SELECT COUNT(*) FROM ad_jobs WHERE verified=1", fetchone=True)[0]
    unique_ad_watchers = _db_exec("SELECT COUNT(DISTINCT user_id) FROM ad_jobs WHERE verified=1", fetchone=True)[0]
    total_downloads = _db_exec("SELECT COUNT(*) FROM downloads", fetchone=True)[0]
    day_ago = int(time.time()) - 86400
    week_ago = int(time.time()) - (86400 * 7)
    month_ago = int(time.time()) - (86400 * 30)
    year_ago = int(time.time()) - (86400 * 365)
    downloads_today = _db_exec("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (day_ago,), fetchone=True)[0]
    downloads_week = _db_exec("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (week_ago,), fetchone=True)[0]
    downloads_month = _db_exec("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (month_ago,), fetchone=True)[0]
    downloads_year = _db_exec("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (year_ago,), fetchone=True)[0]
    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "ad_views": ad_views,
        "unique_ad_watchers": unique_ad_watchers,
        "total_downloads": total_downloads,
        "downloads_today": downloads_today,
        "downloads_week": downloads_week,
        "downloads_month": downloads_month,
        "downloads_year": downloads_year,
    }

# ============ TELEGRAM API HELPERS ============
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_telegram_message(chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup.to_json()
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def answer_callback_query(callback_query_id: str, text: Optional[str] = None, alert: bool = False):
    url = f"{TELEGRAM_API_BASE}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    payload["show_alert"] = alert
    requests.post(url, json=payload, timeout=15)

def send_file_via_bot(chat_id: int, file_path: str, file_type: str = "video", caption: Optional[str] = None):
    method = "sendVideo" if file_type == "video" else "sendAudio"
    url = f"{TELEGRAM_API_BASE}/{method}"
    with open(file_path, "rb") as fh:
        files = {file_type: fh}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = requests.post(url, data=data, files=files, timeout=180)
        r.raise_for_status()
        return r.json()

def send_photo_album(chat_id: int, image_urls: list, caption: Optional[str] = None):
    """Send up to 10 images as a single Telegram album — used for
    Instagram carousel/photo posts, which have no video stream."""
    url = f"{TELEGRAM_API_BASE}/sendMediaGroup"
    media = []
    for i, img_url in enumerate(image_urls[:10]):
        item = {"type": "photo", "media": img_url}
        if i == 0 and caption:
            item["caption"] = caption
        media.append(item)
    r = requests.post(url, json={"chat_id": chat_id, "media": media}, timeout=60)
    r.raise_for_status()
    return r.json()

# ============ LOCALIZED STRINGS (EN / AM / OM) ============
LANG_STRINGS = {
    "en": {
        "lang_set": "✅ Language set to <b>English</b>.\n\nNow send your Instagram Reel or video link!",
        "send_link": "❌ Please send a valid Instagram Reel or video link (not a Story — those aren't supported).",
        "story_not_supported": "❌ Stories aren't supported. Please send a Reel or video post link instead.",
        "quality_prompt": "🎉 <b>Ad completed!</b> Tap below to get your video:",
        "premium_success": "⭐ <b>Lifetime Premium Activated!</b> Enjoy unlimited downloads without ads forever.",
        "processing": "⏳ Downloading your video, please wait...",
        "ad_gate_msg": "To continue, watch a short ad or buy premium:",
        "cancelled_msg": "❌ Action cancelled. Send a new Instagram link whenever you're ready.",
        "no_link_found": "❌ No Instagram link found.",
        "must_watch_ad": "⚠️ You must watch the ad or buy premium first!",
        "opening_checkout": "Opening checkout...",
        "premium_title": "Lifetime Premium Pass",
        "premium_desc": "Unlock lifetime unlimited downloads with zero ads!",
        "btn_watch_ad": "👉 Watch Ad",
        "btn_buy_premium": "⭐ Buy Lifetime Premium (100 ⭐)",
        "btn_cancel": "❌ Cancel",
        "btn_get_video": "📥 Download Video",
        "btn_get_audio": "🎵 Audio Only (MP3)",
        "btn_start_over": "◀️ Cancel / Start Over",
        "btn_lang_en": "🇬🇧 English",
        "btn_lang_am": "🇪🇹 Amharic (አማርኛ)",
        "btn_lang_om": "🌍 Afaan Oromoo",
    },
    "am": {
        "lang_set": "✅ ቋንቋዎ ወደ <b>አማርኛ</b> ተቀይሯል።\n\nአሁን የInstagram Reel ወይም ቪዲዮ ሊንክ ይላኩ!",
        "send_link": "❌ እባክዎ ትክክለኛ የInstagram Reel ወይም ቪዲዮ ሊንክ ይላኩ (Story አይደገፍም)።",
        "story_not_supported": "❌ Story አይደገፍም። እባክዎ የReel ወይም ቪዲዮ ፖስት ሊንክ ይላኩ።",
        "quality_prompt": "🎉 <b>ማስታወቂያው ተጠናቋል!</b> ቪዲዮዎን ለማግኘት ከታች ይንኩ:",
        "premium_success": "⭐ <b>የልዩ ዕድል (Lifetime) ፕሪሚየም ነቅቷል!</b> ያለ ማስታወቂያ ለዘላለም ያውርዱ።",
        "processing": "⏳ ቪዲዮዎ እየተወረደ ነው, እባክዎ ይጠብቁ...",
        "ad_gate_msg": "ለመቀጠል፣ አጭር ማስታወቂያ ይመልከቱ ወይም ፕሪሚየም ይግዙ:",
        "cancelled_msg": "❌ ተሰርዟል። ዝግጁ ሲሆኑ አዲስ የInstagram ሊንክ ይላኩ።",
        "no_link_found": "❌ የInstagram ሊንክ አልተገኘም።",
        "must_watch_ad": "⚠️ መጀመሪያ ማስታወቂያውን መመልከት ወይም ፕሪሚየም መግዛት አለብዎት!",
        "opening_checkout": "ክፍያ በመክፈት ላይ...",
        "premium_title": "የልዩ ዕድል (Lifetime) ፕሪሚየም",
        "premium_desc": "ያለ ምንም ማስታወቂያ ለዘላለም ያልተገደበ ማውረድ ይክፈቱ!",
        "btn_watch_ad": "👉 ማስታወቂያ ይመልከቱ",
        "btn_buy_premium": "⭐ የልዩ ዕድል ፕሪሚየም ይግዙ (100 ⭐)",
        "btn_cancel": "❌ ሰርዝ",
        "btn_get_video": "📥 ቪዲዮ ያውርዱ",
        "btn_get_audio": "🎵 ድምፅ ብቻ (MP3)",
        "btn_start_over": "◀️ ሰርዝ / እንደገና ጀምር",
        "btn_lang_en": "🇬🇧 English",
        "btn_lang_am": "🇪🇹 Amharic (አማርኛ)",
        "btn_lang_om": "🌍 Afaan Oromoo",
    },
    "om": {
        "lang_set": "✅ Afaan <b>Afaan Oromoo</b> tti jijjiirameera.\n\nAmma linki Reel ykn viidiyoo Instagram ergaa!",
        "send_link": "❌ Maaloo linki Reel ykn viidiyoo Instagram sirrii ergaa (Story hin deeggaramu).",
        "story_not_supported": "❌ Story hin deeggaramu. Maaloo linki Reel ykn viidiyoo poostii ergaa.",
        "quality_prompt": "🎉 <b>Beeksifni xumurameera!</b> Viidiyoo keessan argachuuf gadii tuqaa:",
        "premium_success": "⭐ <b>Preemiyamii Bara Guutuu (Lifetime) hojjeteera!</b> Beeksisa malee bilisaan buufadhaa.",
        "processing": "⏳ Viidiyoon buufamaa jira, maaloo eegaa...",
        "ad_gate_msg": "Itti fufuuf, beeksisa gabaabaa ilaali ykn piriimiyamii bitaa:",
        "cancelled_msg": "❌ Haqameera. Yeroo qophooftan linki Instagram haaraa ergaa.",
        "no_link_found": "❌ Linki Instagram hin argamne.",
        "must_watch_ad": "⚠️ Dura beeksisa ilaaluu ykn piriimiyamii bituu qabdu!",
        "opening_checkout": "Kaffaltii banaa jira...",
        "premium_title": "Piriimiyamii Bara Guutuu",
        "premium_desc": "Beeksisa tokko malee buufata bara guutuu bilisaan banaa!",
        "btn_watch_ad": "👉 Beeksisa Ilaali",
        "btn_buy_premium": "⭐ Piriimiyamii Bara Guutuu Bitaa (100 ⭐)",
        "btn_cancel": "❌ Haqi",
        "btn_get_video": "📥 Viidiyoo Buufadhu",
        "btn_get_audio": "🎵 Sagalee Qofa (MP3)",
        "btn_start_over": "◀️ Haqi / Irra Deebi'ii Jalqabi",
        "btn_lang_en": "🇬🇧 English",
        "btn_lang_am": "🇪🇹 Amharic (አማርኛ)",
        "btn_lang_om": "🌍 Afaan Oromoo",
    },
}


def strings_for(lang: str) -> dict:
    return LANG_STRINGS.get(lang, LANG_STRINGS["en"])


# ============ KEYBOARDS ============
def make_language_keyboard():
    s = LANG_STRINGS["en"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_lang_en"], callback_data="lang_en", style="primary")],
        [InlineKeyboardButton(s["btn_lang_am"], callback_data="lang_am", style="success")],
        [InlineKeyboardButton(s["btn_lang_om"], callback_data="lang_om", style="primary")],
    ])

def make_ad_gate_keyboard(user_id: int, job_id: str, lang: str):
    s = strings_for(lang)
    ad_url = f"{AD_PAGE_URL}?user_id={user_id}&job_id={job_id}" if AD_PAGE_URL else "https://example.com"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_watch_ad"], url=ad_url, style="danger")],
        [InlineKeyboardButton(s["btn_buy_premium"], callback_data="buy_lifetime", style="primary")],
        [InlineKeyboardButton(s["btn_cancel"], callback_data="cancel", style="danger")],
    ])

def make_download_keyboard(lang: str):
    s = strings_for(lang)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["btn_get_video"], callback_data="get_video", style="success")],
        [InlineKeyboardButton(s["btn_get_audio"], callback_data="get_audio", style="primary")],
        [InlineKeyboardButton(s["btn_start_over"], callback_data="cancel", style="danger")],
    ])

# ============ DOWNLOAD HANDLERS ============
def fetch_ig_json(ig_url: str) -> Optional[dict]:
    if not IG_API_URL:
        return None
    try:
        params = {"postUrl": ig_url, "url": ig_url}
        headers = {"Authorization": f"Bearer {IG_API_KEY}"} if IG_API_KEY else {}
        resp = requests.get(IG_API_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def find_video_url(obj):
    """Recursively scan a JSON structure for a direct video URL. Written
    defensively since the exact response shape of free/demo IG downloader
    APIs can vary or change without notice."""
    if isinstance(obj, str) and obj.startswith("http") and (".mp4" in obj or "video" in obj.lower()):
        return obj
    if isinstance(obj, dict):
        for key in ("videoUrl", "video_url", "download_url", "url", "downloadUrl"):
            if key in obj and isinstance(obj[key], str) and obj[key].startswith("http"):
                return obj[key]
        for v in obj.values():
            found = find_video_url(v)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = find_video_url(item)
            if found:
                return found
    return None

def find_image_urls(obj) -> list:
    """Recursively scan a JSON structure for likely image URLs — used to
    detect Instagram carousel/photo posts, whatever shape the API's
    response comes back in (e.g. data.images[], media[].url)."""
    found = []
    if isinstance(obj, str):
        if obj.startswith("http") and any(ext in obj.lower() for ext in (".jpg", ".jpeg", ".webp", ".png")):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(find_image_urls(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_image_urls(item))
    return found

def fetch_ig_info(ig_url: str) -> Optional[dict]:
    """Fallback photo-post detection via yt-dlp -j when IG_API_URL isn't
    set or doesn't return images. Less reliable than a dedicated API."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-j", ig_url],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout.strip().splitlines()[0])
    except Exception:
        return None

def extract_photo_urls_from_info(info: dict) -> list:
    urls = []
    entries = info.get("entries")
    if entries:
        for e in entries:
            u = e.get("url")
            if not u and e.get("thumbnails"):
                thumbs = e["thumbnails"]
                if thumbs:
                    u = thumbs[-1].get("url")
            if not u:
                u = e.get("thumbnail")
            if u:
                urls.append(u)
    if not urls and info.get("thumbnails"):
        urls = [t["url"] for t in info["thumbnails"] if t.get("url")]
    return urls

def download_via_yt_dlp(ig_url: str, out_path: str):
    cmd = ["yt-dlp", "-o", out_path, "-f", "best[ext=mp4]/best", ig_url]
    subprocess.check_call(cmd, timeout=600)

def download_audio_via_yt_dlp(ig_url: str, out_path: str):
    # NOTE: audio extraction requires ffmpeg to be present on the host.
    # If this fails with a command-not-found / conversion error on Render,
    # ffmpeg isn't installed in the environment — same dependency the
    # TikTok bot needs for its MP3 option.
    cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", out_path, ig_url]
    subprocess.check_call(cmd, timeout=600)

def process_download_job(chat_id: int, user_id: int, ig_url: str, mode: str = "video"):
    try:
        tmp_filename = None

        if mode == "audio":
            # No dedicated audio endpoint from the IG API — always extract
            # via yt-dlp for audio requests.
            out_path = f"/tmp/{user_id}_{int(time.time())}.%(ext)s"
            download_audio_via_yt_dlp(ig_url, out_path)
            found = out_path.replace(".%(ext)s", ".mp3")
            if os.path.exists(found):
                send_file_via_bot(chat_id, found, file_type="audio", caption="✅ Here is your audio (MP3)!")
                tmp_filename = found
        else:
            raw_api_data = fetch_ig_json(ig_url)
            dl_url = find_video_url(raw_api_data) if raw_api_data else None

            # Check for a carousel/photo post FIRST (before attempting a
            # video download at all) if the API is configured.
            if not dl_url and raw_api_data:
                image_urls = find_image_urls(raw_api_data)
                if image_urls:
                    send_photo_album(chat_id, image_urls, caption="✅ Here are your photos!")
                    log_download(user_id, "photo")
                    return

            if dl_url:
                tmp_filename = f"/tmp/{user_id}_{int(time.time())}.mp4"
                with requests.get(dl_url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(tmp_filename, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                fh.write(chunk)
                send_file_via_bot(chat_id, tmp_filename, file_type="video", caption="✅ Here is your video!")
            else:
                out_path = f"/tmp/{user_id}_{int(time.time())}.mp4"
                try:
                    download_via_yt_dlp(ig_url, out_path)
                except subprocess.CalledProcessError:
                    # No video stream — likely a photo/carousel post. Try
                    # yt-dlp's own (less reliable) photo detection before
                    # giving up.
                    info = fetch_ig_info(ig_url)
                    if info:
                        photo_urls = extract_photo_urls_from_info(info)
                        if photo_urls:
                            send_photo_album(chat_id, photo_urls, caption="✅ Here are your photos!")
                            log_download(user_id, "photo")
                            return
                    send_telegram_message(
                        chat_id,
                        "❌ Couldn't find a video for this link. If this is a photo/carousel "
                        "post, photo downloads are still experimental and may not work for every post yet."
                    )
                    return

                if os.path.exists(out_path):
                    send_file_via_bot(chat_id, out_path, file_type="video", caption="✅ Here is your video!")
                    tmp_filename = out_path

        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)
            log_download(user_id, mode)
    except Exception as e:
        try:
            send_telegram_message(chat_id, f"❌ Error processing download: {str(e)[:100]}")
        except Exception:
            pass

# ============ BOT HANDLERS ============
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id not in ADMIN_IDS:
        return

    s = get_bot_stats()
    msg = (
        "📊 <b>Bot Stats</b>\n\n"
        f"👥 Total users: <b>{s['total_users']}</b>\n"
        f"⭐ Premium users: <b>{s['premium_users']}</b>\n\n"
        f"👁️ Ad views (all-time): <b>{s['ad_views']}</b>\n"
        f"👁️ Unique users who watched an ad: <b>{s['unique_ad_watchers']}</b>\n\n"
        f"📥 Total downloads delivered: <b>{s['total_downloads']}</b>\n\n"
        f"📈 Downloads — 24h: <b>{s['downloads_today']}</b> · 7d: <b>{s['downloads_week']}</b> · 30d: <b>{s['downloads_month']}</b> · 365d: <b>{s['downloads_year']}</b>"
    )
    send_telegram_message(chat_id, msg)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    get_user(user_id)

    parts = update.message.text.split(maxsplit=1) if update.message and update.message.text else []
    payload = parts[1] if len(parts) > 1 else None
    if payload and payload.startswith("ad_verified"):
        return

    send_telegram_message(
        chat_id,
        "🌐 <b>Please choose your language / እባክዎ ቋንቋ ይምረጡ / Afaan filadhu:</b>",
        reply_markup=make_language_keyboard()
    )

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id

    user = get_user(user_id)
    lang = user.get("language", "en")
    s = strings_for(lang)

    if data.startswith("lang_"):
        lang_code = data.split("_", 1)[1]
        set_user_language(user_id, lang_code)
        new_s = strings_for(lang_code)
        answer_callback_query(query.id, "✅")
        send_telegram_message(chat_id, new_s["lang_set"])
        return

    if data == "cancel":
        answer_callback_query(query.id, s["btn_cancel"])
        try:
            query.edit_message_text(s["cancelled_msg"])
        except Exception:
            pass
        return

    if data == "buy_lifetime":
        answer_callback_query(query.id, s["opening_checkout"])
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=s["premium_title"],
            description=s["premium_desc"],
            payload="lifetime_premium_pass",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(s["premium_title"], 100)]
        )
        return

    if data in ("get_video", "get_audio"):
        if not user_has_access(user_id):
            answer_callback_query(query.id, s["must_watch_ad"], alert=True)
            return

        ig_url = user.get("last_ig_url")
        if not ig_url:
            answer_callback_query(query.id, s["no_link_found"], alert=True)
            return

        mode = "audio" if data == "get_audio" else "video"
        answer_callback_query(query.id, s["processing"])
        threading.Thread(target=process_download_job, args=(chat_id, user_id, ig_url, mode), daemon=True).start()
        return

    answer_callback_query(query.id, "")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    set_lifetime_premium(user_id)
    user = get_user(user_id)
    lang = user.get("language", "en")
    s = strings_for(lang)

    send_telegram_message(
        chat_id,
        s["premium_success"] + "\n\n" + s["quality_prompt"],
        reply_markup=make_download_keyboard(lang)
    )

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    user = get_user(user_id)
    lang = user.get("language", "en")
    s = strings_for(lang)

    if IG_STORY_RE.search(text):
        send_telegram_message(chat_id, s["story_not_supported"])
        return

    if IG_RE.search(text):
        set_user_ig_url(user_id, text)

        if user_has_access(user_id):
            send_telegram_message(
                chat_id,
                s["quality_prompt"],
                reply_markup=make_download_keyboard(lang)
            )
        else:
            job_id = create_ad_job(user_id, chat_id, text)
            keyboard = make_ad_gate_keyboard(user_id, job_id, lang)
            send_telegram_message(chat_id, s["ad_gate_msg"], reply_markup=keyboard)
        return

    send_telegram_message(chat_id, s["send_link"])

# ============ FLASK ENDPOINTS ============
@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})

@flask_app.route("/verify_ad", methods=["POST"])
def verify_ad():
    """Called automatically by the ad page after the ad timer finishes"""
    try:
        data = request.get_json() or {}
        job_id = data.get("job_id")
        user_id = data.get("user_id")

        job = get_ad_job(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404

        mark_job_verified(job_id)
        grant_temporary_pass(int(user_id), duration_hours=24)

        chat_id = job["chat_id"]
        user = get_user(int(user_id))
        lang = user.get("language", "en")
        s = strings_for(lang)

        send_telegram_message(
            chat_id,
            s["quality_prompt"],
            reply_markup=make_download_keyboard(lang)
        )

        return jsonify({"success": True, "message": "Ad verified & download button sent!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def run_flask():
    port = int(os.getenv("PORT", "5000"))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    print("🤖 Instagram Downloader Bot started successfully!")
    application.run_polling()

if __name__ == "__main__":
    main()
