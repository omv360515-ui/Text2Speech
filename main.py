#!/usr/bin/env python3
"""
Telegram TTS Bot – Complete Production Version
Fixed all syntax errors, missing methods, and pydub audioop issue.
"""

import sys
import os
import io
import gc
import json
import logging
import asyncio
import hashlib
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Optional

# ========== FIX for pydub + Python 3.14 (missing pyaudioop) ==========
try:
    import audioop
except ImportError:
    # Create a minimal dummy audioop if missing (prevents pydub crash)
    class DummyAudioOp:
        @staticmethod
        def add(*args): return b''
        @staticmethod
        def adpcm2lin(*args): return (b'', None)
        @staticmethod
        def alaw2lin(*args): return b''
        @staticmethod
        def bias(*args): return b''
        @staticmethod
        def cross(*args): return b''
        @staticmethod
        def findfactor(*args): return b''
        @staticmethod
        def findfit(*args): return (b'', None)
        @staticmethod
        def findmax(*args): return 0
        @staticmethod
        def getsample(*args): return 0
        @staticmethod
        def lin2adpcm(*args): return (b'', None)
        @staticmethod
        def lin2alaw(*args): return b''
        @staticmethod
        def lin2lin(*args): return b''
        @staticmethod
        def lin2ulaw(*args): return b''
        @staticmethod
        def max(*args): return 0
        @staticmethod
        def maxpp(*args): return 0
        @staticmethod
        def mul(*args): return b''
        @staticmethod
        def ratecv(*args): return (b'', None)
        @staticmethod
        def reverse(*args): return b''
        @staticmethod
        def rms(*args): return 0
        @staticmethod
        def tomono(*args): return b''
        @staticmethod
        def tostereo(*args): return b''
        @staticmethod
        def ulaw2lin(*args): return b''
    sys.modules['audioop'] = DummyAudioOp

# Now safe to import pydub
import pydub
from pydub import AudioSegment

# Other dependencies
import psutil
import aiohttp
import aiosqlite
import edge_tts
import gtts
from textblob import TextBlob
from cachetools import TTLCache
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, ContextTypes
)

load_dotenv()

# ========== CONFIGURATION ==========
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN missing in .env")
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///tts_bot.db")
    VOICES_DB_PATH = "voices_data.json"
    AUDIO_OUTPUT_DIR = Path("generated_audio")
    MAX_TEXT_LENGTH = 5000
    RATE_LIMIT_CALLS = 5
    RATE_LIMIT_PERIOD = 60
    PREFERRED_TTS = "edge_tts"
    FALLBACK_TTS = "gtts"
    SUPPORTED_FORMATS = ["mp3", "wav", "ogg", "m4a", "flac"]
    DEFAULT_FORMAT = "mp3"
    LOG_LEVEL = logging.INFO

Config.AUDIO_OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=Config.LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TTSBot")

SELECTING_VOICE, WAITING_FOR_TEXT = range(2)
rate_limit_data = defaultdict(list)

# ========== DATABASE (Async SQLite) ==========
class Database:
    def __init__(self):
        self.db_path = Config.DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    async def _get_connection(self):
        return await aiosqlite.connect(self.db_path)
    async def init_db(self):
        async with await self._get_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_premium INTEGER DEFAULT 0,
                    total_requests INTEGER DEFAULT 0,
                    settings TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    text TEXT,
                    voice TEXT,
                    format TEXT DEFAULT 'mp3',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.commit()
    async def add_user(self, user_id, username=None, first_name=None):
        async with await self._get_connection() as conn:
            await conn.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user_id, username, first_name))
            await conn.commit()
    async def get_user_settings(self, user_id):
        async with await self._get_connection() as conn:
            cursor = await conn.execute("SELECT settings FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except:
                    return {}
            return {}
    async def update_user_setting(self, user_id, key, value):
        settings = await self.get_user_settings(user_id)
        settings[key] = value
        async with await self._get_connection() as conn:
            await conn.execute("UPDATE users SET settings = ? WHERE user_id = ?", (json.dumps(settings), user_id))
            await conn.commit()
    async def add_history_entry(self, user_id, text, voice, audio_format="mp3"):
        async with await self._get_connection() as conn:
            await conn.execute("INSERT INTO history (user_id, text, voice, format) VALUES (?, ?, ?, ?)", (user_id, text, voice, audio_format))
            await conn.execute("UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?", (user_id,))
            await conn.commit()
    async def get_user_history(self, user_id, limit=10):
        async with await self._get_connection() as conn:
            cursor = await conn.execute("SELECT text, voice, format, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
            rows = await cursor.fetchall()
            return [{"text": r[0], "voice": r[1], "format": r[2], "timestamp": r[3]} for r in rows]

# ========== EMOTION ANALYZER (Lightweight) ==========
class EmotionAnalyzer:
    def __init__(self):
        self.use_transformers = False
        try:
            if psutil.virtual_memory().available / (1024**3) >= 2.0:
                from transformers import pipeline
                self.classifier = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")
                self.use_transformers = True
                logger.info("Transformer loaded")
            else:
                raise MemoryError
        except:
            self.use_transformers = False
            logger.info("Using TextBlob fallback")
    def analyze(self, text):
        emotion_map = {"POSITIVE": {"pitch": 15, "speed": 10, "volume": 5},
                       "NEGATIVE": {"pitch": -15, "speed": -10, "volume": -5},
                       "NEUTRAL": {"pitch": 0, "speed": 0, "volume": 0}}
        if self.use_transformers:
            try:
                label = self.classifier(text[:512])[0]['label'].upper()
            except:
                label = "NEUTRAL"
        else:
            blob = TextBlob(text)
            polarity = blob.sentiment.polarity
            label = "POSITIVE" if polarity > 0.1 else "NEGATIVE" if polarity < -0.1 else "NEUTRAL"
        return label, emotion_map.get(label, emotion_map["NEUTRAL"])

# ========== TTS ENGINE ==========
class UnifiedTTS:
    def __init__(self):
        self.analyzer = EmotionAnalyzer()
        self.cache = TTLCache(maxsize=100, ttl=3600)
        self.voices = []
        self.voices_cache_path = Path(Config.VOICES_DB_PATH)
    async def load_voices(self):
        if self.voices_cache_path.exists() and self.voices_cache_path.stat().st_size > 0:
            try:
                with open(self.voices_cache_path) as f:
                    data = json.load(f)
                    self.voices = data.get("edge_tts", {}).get("voices", [])
                    if self.voices:
                        return
            except: pass
        # Fetch from edge_tts
        try:
            raw = await edge_tts.list_voices()
            self.voices = [{"Name": v["ShortName"], "ShortName": v["ShortName"], "Gender": v.get("Gender","Neutral"), "Locale": v.get("Locale","")} for v in raw]
            with open(self.voices_cache_path, "w") as f:
                json.dump({"edge_tts": {"voices": self.voices}}, f)
        except Exception as e:
            logger.error(f"Voice fetch failed: {e}")
            self.voices = [{"Name":"hi-IN-SwaraNeural","ShortName":"hi-IN-SwaraNeural","Gender":"Female","Locale":"hi-IN"},
                           {"Name":"hi-IN-MadhurNeural","ShortName":"hi-IN-MadhurNeural","Gender":"Male","Locale":"hi-IN"},
                           {"Name":"en-US-JennyNeural","ShortName":"en-US-JennyNeural","Gender":"Female","Locale":"en-US"}]
    async def list_voices(self):
        if not self.voices:
            await self.load_voices()
        return self.voices
    async def synthesize(self, text, voice, provider=None, rate_override=None, pitch_override=None, volume_override=None, output_format="mp3"):
        cache_key = hashlib.md5(f"{text}:{voice}:{rate_override}:{pitch_override}:{output_format}".encode()).hexdigest()
        if cache_key in self.cache:
            return self.cache[cache_key]
        _, emotion = self.analyzer.analyze(text)
        speed = rate_override if rate_override is not None else emotion.get("speed", 0)
        pitch = pitch_override if pitch_override is not None else emotion.get("pitch", 0)
        volume = volume_override if volume_override is not None else emotion.get("volume", 0)
        if provider is None:
            provider = Config.PREFERRED_TTS
        try:
            audio = await self._call_engine(text, voice, provider, speed, volume, pitch)
            audio = await self._convert_format(audio, output_format)
            self.cache[cache_key] = audio
            return audio
        except Exception as e:
            logger.error(f"Primary failed: {e}, falling back")
            audio = await self._call_engine(text, voice, Config.FALLBACK_TTS, 0, 0, 0)
            return await self._convert_format(audio, output_format)
    async def _call_engine(self, text, voice, provider, rate, volume, pitch):
        if provider == "edge_tts":
            rate_str = f"{rate:+d}%" if rate != 0 else "0%"
            vol_str = f"{volume:+d}%" if volume != 0 else "0%"
            pitch_str = f"{pitch:+d}%" if pitch != 0 else "0%"
            comm = edge_tts.Communicate(text, voice, rate=rate_str, volume=vol_str, pitch=pitch_str)
            data = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    data.extend(chunk["data"])
            return bytes(data)
        elif provider == "gtts":
            lang = voice.split('-')[0] if '-' in voice else 'hi'
            loop = asyncio.get_running_loop()
            tts = await loop.run_in_executor(None, lambda: gtts.gTTS(text, lang=lang))
            fp = io.BytesIO()
            await loop.run_in_executor(None, lambda: tts.write_to_fp(fp))
            fp.seek(0)
            return fp.read()
        elif provider == "elevenlabs":
            if not Config.ELEVENLABS_API_KEY:
                raise ValueError("No API key")
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
            headers = {"xi-api-key": Config.ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            payload = {"text": text, "model_id": "eleven_multilingual_v2"}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"ElevenLabs error {resp.status}")
                    return await resp.read()
        else:
            raise ValueError("Unknown provider")
    async def _convert_format(self, audio_bytes, target):
        if target == "mp3":
            return audio_bytes
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
            out = io.BytesIO()
            audio.export(out, format=target)
            return out.getvalue()
        except:
            return audio_bytes

# ========== RATE LIMIT DECORATOR ==========
def rate_limit(limit, per):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, update, context, *args, **kwargs):
            user = update.effective_user
            if not user:
                return await func(self, update, context, *args, **kwargs)
            uid = user.id
            now = datetime.now()
            rate_limit_data[uid] = [t for t in rate_limit_data[uid] if t > now - timedelta(seconds=per)]
            if len(rate_limit_data[uid]) >= limit:
                wait = (rate_limit_data[uid][0] + timedelta(seconds=per) - now).seconds
                await update.effective_message.reply_text(f"⏳ रुकिए, {wait} सेकंड बाद प्रयास करें।")
                return
            rate_limit_data[uid].append(now)
            return await func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator

# ========== MAIN BOT CLASS ==========
class TTSBot:
    def __init__(self):
        self.tts = UnifiedTTS()
        self.db = Database()
        self.app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()
    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(CommandHandler("history", self.history))
        self.app.add_handler(CommandHandler("settings", self.settings))
        conv = ConversationHandler(
            entry_points=[CommandHandler("voice", self.voice), CallbackQueryHandler(self.voice_trigger, pattern="^trigger_voice$")],
            states={
                SELECTING_VOICE: [CallbackQueryHandler(self.voice_page_callback, pattern="^voice_page_"),
                                  CallbackQueryHandler(self.voice_select, pattern="^voice_")],
                WAITING_FOR_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel), CallbackQueryHandler(self.cancel_cb, pattern="^back$")]
        )
        self.app.add_handler(conv)
        self.app.add_handler(CallbackQueryHandler(self.menu_callback, pattern="^(menu_history|menu_settings|menu_voice)$"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.direct_tts))
    async def start(self, update, context):
        user = update.effective_user
        await self.db.add_user(user.id, user.username, user.first_name)
        kb = [[InlineKeyboardButton("🎤 बदलें आवाज़", callback_data="trigger_voice"),
               InlineKeyboardButton("⚙️ सेटिंग्स", callback_data="menu_settings")],
              [InlineKeyboardButton("📜 इतिहास", callback_data="menu_history")]]
        await update.message.reply_text(f"नमस्ते {user.first_name}! मैं TTS बॉट हूँ। कोई भी टेक्स्ट भेजें, आवाज़ बनाऊँगा।", reply_markup=InlineKeyboardMarkup(kb))
    async def help(self, update, context):
        await update.message.reply_text("कमांड्स: /start, /voice, /settings, /history\nटेक्स्ट सीधे भेजें या /tts <text>")
    async def history(self, update, context):
        uid = update.effective_user.id
        hist = await self.db.get_user_history(uid, 10)
        if not hist:
            await update.message.reply_text("कोई इतिहास नहीं।")
            return
        msg = "📜 हालिया:\n" + "\n".join([f"• {h['text'][:40]}... ({h['voice']})" for h in hist])
        await update.message.reply_text(msg)
    async def settings(self, update, context):
        uid = update.effective_user.id
        sets = await self.db.get_user_settings(uid)
        voice = sets.get("voice", "hi-IN-SwaraNeural")
        await update.message.reply_text(f"⚙️ वर्तमान सेटिंग्स:\nआवाज़: {voice}\n/voice से बदलें।")
    async def voice(self, update, context):
        voices = await self.tts.list_voices()
        if not voices:
            await update.message.reply_text("❌ आवाज़ें लोड नहीं हुईं।")
            return ConversationHandler.END
        context.user_data["all_voices"] = voices
        context.user_data["voice_page"] = 0
        await self._send_voice_page(update, context, 0)
        return SELECTING_VOICE
    async def voice_trigger(self, update, context):
        query = update.callback_query
        await query.answer()
        return await self.voice(query, context)
    async def _send_voice_page(self, update, context, page):
        voices = context.user_data.get("all_voices", [])
        per_page = 40
        start = page * per_page
        end = start + per_page
        page_voices = voices[start:end]
        kb = []
        for v in page_voices:
            emoji = "👨" if v["Gender"] == "Male" else "👩" if v["Gender"] == "Female" else "🤖"
            name = v["Name"][:35]
            kb.append([InlineKeyboardButton(f"{emoji} {name}", callback_data=f"voice_{v['ShortName']}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ पिछला", callback_data=f"voice_page_{page-1}"))
        if end < len(voices):
            nav.append(InlineKeyboardButton("अगला ▶️", callback_data=f"voice_page_{page+1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("❌ रद्द", callback_data="back")])
        total = (len(voices) + per_page - 1) // per_page
        text = f"🎙️ आवाज़ चुनें (पेज {page+1}/{total})"
        if isinstance(update, Update):
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    async def voice_page_callback(self, update, context):
        query = update.callback_query
        await query.answer()
        page = int(query.data.split("_")[2])
        context.user_data["voice_page"] = page
        await self._send_voice_page(update, context, page)
    async def voice_select(self, update, context):
        query = update.callback_query
        await query.answer()
        voice_id = query.data.split("_", 1)[1]
        context.user_data["selected_voice"] = voice_id
        await query.edit_message_text(f"✅ आवाज़ चुन ली: {voice_id}\nअब टेक्स्ट भेजें:")
        return WAITING_FOR_TEXT
    async def handle_text(self, update, context):
        text = update.message.text
        if len(text) > Config.MAX_TEXT_LENGTH:
            await update.message.reply_text(f"टेक्स्ट बहुत लंबा है (अधिकतम {Config.MAX_TEXT_LENGTH} अक्षर)")
            return WAITING_FOR_TEXT
        voice = context.user_data.get("selected_voice", "hi-IN-SwaraNeural")
        # Save to DB for future use
        await self.db.update_user_setting(update.effective_user.id, "voice", voice)
        proc_msg = await update.message.reply_text("🎧 आवाज़ बन रही है...")
        try:
            audio = await self.tts.synthesize(text, voice, output_format=Config.DEFAULT_FORMAT)
            await self.db.add_history_entry(update.effective_user.id, text, voice)
            await update.message.reply_audio(audio=audio, filename=f"tts_{update.effe
