import os
import io
import gc
import json
import logging
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Optional

# Third-Party Dependencies
import psutil
import aiohttp
import aiosqlite
import edge_tts
import gtts
from pydub import AudioSegment
from textblob import TextBlob
from cachetools import TTLCache
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, ContextTypes
)

# Load environment variables
load_dotenv()

# =====================================================================
# ⚙️ CENTRAL CONFIGURATION LAYER
# =====================================================================
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing in your environment/.env file.")
    
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///tts_bot.db")
    VOICES_DB_PATH = "voices_data.json"
    AUDIO_OUTPUT_DIR = Path("generated_audio")
    
    MAX_TEXT_LENGTH = 5000
    RATE_LIMIT_CALLS = 5
    RATE_LIMIT_PERIOD = 60  # Seconds
    
    PREFERRED_TTS = "edge_tts"
    FALLBACK_TTS = "gtts"
    SUPPORTED_FORMATS = ["mp3", "wav", "ogg", "m4a", "flac"]
    DEFAULT_FORMAT = "mp3"
    LOG_LEVEL = logging.INFO

Config.AUDIO_OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=Config.LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TTSMasterBot")

# Conversation States
SELECTING_VOICE, WAITING_FOR_TEXT = range(2)
rate_limit_data = defaultdict(list)

# =====================================================================
# 🗄️ ASYNC DATABASE CONTROLLER (aiosqlite)
# =====================================================================
class Database:
    def __init__(self):
        self.db_path = Config.DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    
    async def _get_connection(self) -> aiosqlite.Connection:
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            await conn.commit()
            logger.info("Database initialized successfully.")
    
    async def add_user(self, user_id: int, username: str = None, first_name: str = None):
        async with await self._get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, username, first_name)
            )
            await conn.commit()
    
    async def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        async with await self._get_connection() as conn:
            cursor = await conn.execute("SELECT settings FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except:
                    return {}
            return {}
    
    async def update_user_setting(self, user_id: int, key: str, value: Any):
        settings = await self.get_user_settings(user_id)
        settings[key] = value
        async with await self._get_connection() as conn:
            await conn.execute(
                "UPDATE users SET settings = ? WHERE user_id = ?",
                (json.dumps(settings), user_id)
            )
            await conn.commit()
    
    async def add_history_entry(self, user_id: int, text: str, voice: str, audio_format: str = "mp3"):
        async with await self._get_connection() as conn:
            await conn.execute(
                "INSERT INTO history (user_id, text, voice, format) VALUES (?, ?, ?, ?)",
                (user_id, text, voice, audio_format)
            )
            await conn.execute(
                "UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?",
                (user_id,)
            )
            await conn.commit()
    
    async def get_user_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        async with await self._get_connection() as conn:
            cursor = await conn.execute(
                "SELECT text, voice, format, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            )
            rows = await cursor.fetchall()
            history = []
            for row in rows:
                history.append({
                    "text": row[0], "voice": row[1],
                    "format": row[2], "timestamp": row[3]
                })
            return history

# =====================================================================
# 🎭 HYBRID EMOTION ANALYZER ENGINE
# =====================================================================
class EmotionAnalyzer:
    def __init__(self):
        self.classifier = None
        self.use_transformers = False
        self._init_classifier()
    
    def _init_classifier(self):
        try:
            available_ram = psutil.virtual_memory().available / (1024**3)
            if available_ram < 2.0:
                logger.warning(f"Low RAM ({available_ram:.1f}GB). Shifting core to TextBlob fallback mode.")
                raise MemoryError("Insufficient RAM for deep learning model.")
            
            from transformers import pipeline
            self.classifier = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")
            self.use_transformers = True
            logger.info("DistilBERT Sentiment Architecture loaded successfully.")
        except (ImportError, MemoryError, Exception) as e:
            logger.warning(f"Transformers pipeline bypassed: {e}. TextBlob active.")
            self.use_transformers = False
            gc.collect()
    
    def analyze(self, text: str) -> Tuple[str, Dict[str, int]]:
        emotion_map = {
            "POSITIVE": {"pitch_delta": 20, "speed_delta": 15, "volume_delta": 10},
            "NEGATIVE": {"pitch_delta": -20, "speed_delta": -15, "volume_delta": -10},
            "NEUTRAL": {"pitch_delta": 0, "speed_delta": 0, "volume_delta": 0}
        }
        
        if self.use_transformers and self.classifier:
            try:
                result = self.classifier(text[:512])[0]
                label = result['label'].upper()
            except Exception:
                label = "NEUTRAL"
        else:
            blob = TextBlob(text)
            polarity = blob.sentiment.polarity
            if polarity > 0.1:
                label = "POSITIVE"
            elif polarity < -0.1:
                label = "NEGATIVE"
            else:
                label = "NEUTRAL"
        
        params = emotion_map.get(label, emotion_map["NEUTRAL"])
        return label, params

# =====================================================================
# 🧠 UNIFIED TTS ENGINE WITH CACHING & PAGINATION REGISTRY
# =====================================================================
class UnifiedTTS:
    def __init__(self):
        self.analyzer = EmotionAnalyzer()
        self.cache = TTLCache(maxsize=150, ttl=3600)
        self.voices_cache_path = Path(Config.VOICES_DB_PATH)
        self.voices = []
    
    async def load_or_fetch_voices(self):
        """Asynchronously load from cache or fetch live from Edge TTS (FIXED)."""
        if self.voices_cache_path.exists() and self.voices_cache_path.stat().st_size > 0:
            try:
                with open(self.voices_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.voices = data.get("edge_tts", {}).get("voices", [])
                    if self.voices:
                        logger.info(f"Loaded {len(self.voices)} voices cleanly from JSON cache.")
                        return
            except Exception as e:
                logger.warning(f"Cache system read failed: {e}")
        
        logger.info("Fetching voice registry from remote Edge TTS grid...")
        try:
            raw_voices = await edge_tts.list_voices()
            self.voices = []
            for v in raw_voices:
                self.voices.append({
                    "Name": v.get("ShortName", "Unknown"),
                    "ShortName": v.get("ShortName", "Unknown"),
                    "Gender": v.get("Gender", "Neutral"),
                    "Locale": v.get("Locale", "Unknown")
                })
            
            cache_data = {"edge_tts": {"voices": self.voices}}
            with open(self.voices_cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            logger.info(f"Successfully cached {len(self.voices)} premium voices.")
        except Exception as e:
            logger.error(f"Failed to pull voices from cloud network: {e}. Setting up baseline array.")
            self.voices = [
                {"Name": "hi-IN-SwaraNeural", "ShortName": "hi-IN-SwaraNeural", "Gender": "Female", "Locale": "hi-IN"},
                {"Name": "hi-IN-MadhurNeural", "ShortName": "hi-IN-MadhurNeural", "Gender": "Male", "Locale": "hi-IN"},
                {"Name": "en-US-JennyNeural", "ShortName": "en-US-JennyNeural", "Gender": "Female", "Locale": "en-US"}
            ]

    async def list_voices(self, provider: str = "edge_tts") -> List[Dict]:
        if provider == "edge_tts":
            if not self.voices:
                await self.load_or_fetch_voices()
            return self.voices
        return []

    async def synthesize(self, text: str, voice: str, provider: str = None,
                         rate_override: int = None, pitch_override: int = None,
                         volume_override: int = None, output_format: str = "mp3") -> bytes:
        
        cache_key = hashlib.md5(f"{text}:{voice}:{rate_override}:{pitch_override}:{output_format}".encode()).hexdigest()
        if cache_key in self.cache:
            logger.info("Cache hit! Extracting audio array from RAM buffer.")
            return self.cache[cache_key]

        _, emotion_params = self.analyzer.analyze(text)
        
        final_speed = rate_override if rate_override is not None else emotion_params["speed_delta"]
        final_pitch = pitch_override if pitch_override is not None else emotion_params["pitch_delta"]
        final_volume = volume_override if volume_override is not None else emotion_params["volume_delta"]

        if provider is None:
            provider = Config.PREFERRED_TTS

        try:
            audio = await self._call_tts_engine(text, voice, provider, final_speed, final_volume, final_pitch)
            audio = await self._convert_format(audio, output_format)
            self.cache[cache_key] = audio
            return audio
        except Exception as e:
            logger.error(f"Primary cluster failed: {e}. Shifting context load to fallback engine.")
            try:
                audio = await self._call_tts_engine(text, voice, Config.FALLBACK_TTS, 0, 0, 0)
                return await self._convert_format(audio, output_format)
            except Exception as severe_err:
                logger.critical(f"All available rendering blocks are entirely exhausted: {severe_err}")
                raise RuntimeError("Audio pipeline execution failed completely.")

    async def _call_tts_engine(self, text: str, voice: str, provider: str, rate: int, volume: int, pitch: int) -> bytes:
        if provider == "edge_tts":
            rate_str = f"{rate:+d}%" if rate != 0 else "0%"
            volume_str = f"{volume:+d}%" if volume != 0 else "0%"
            pitch_str = f"{pitch:+d}%" if pitch != 0 else "0%"
            
            communicate = edge_tts.Communicate(text, voice, rate=rate_str, volume=volume_str, pitch=pitch_str)
            audio_data = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data.extend(chunk["data"])
            return bytes(audio_data)
            
        elif provider == "gtts":
            lang = voice.split('-')[0] if '-' in voice else 'hi'
            loop = asyncio.get_running_loop()
            tts = await loop.run_in_executor(None, lambda: gtts.gTTS(text=text, lang=lang))
            audio_fp = io.BytesIO()
            await loop.run_in_executor(None, lambda: tts.write_to_fp(audio_fp))
            audio_fp.seek(0)
            return audio_fp.read()
            
        elif provider == "elevenlabs":
            if not Config.ELEVENLABS_API_KEY:
                raise ValueError("Elevenlabs authentication matrix token missing.")
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
            headers = {"xi-api-key": Config.ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            payload = {"text": text, "model_id": "eleven_multilingual_v2"}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Elevenlabs pipeline returned error flag: {resp.status}")
                    return await resp.read()
        else:
            raise ValueError(f"Provider token {provider} invalid.")

    async def _convert_format(self, audio_data: bytes, target_format: str) -> bytes:
        if target_format == "mp3":
            return audio_data
        try:
            audio = AudioSegment.from_file(io.BytesIO(audio_data), format="mp3")
            output = io.BytesIO()
            audio.export(output, format=target_format)
            return output.getvalue()
        except Exception as e:
            logger.warning(f"Transcoding layer skipped: {e}. Outputting default mp3 buffer.")
            return audio_data

# =====================================================================
# ⚙️ UTILITIES & SECURITY LAYER (Rate Limiting)
# =====================================================================
def rate_limit(limit: int, per: int):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            if not update.effective_user or not update.effective_message:
                return await func(self, update, context, *args, **kwargs)
                
            user_id = update.effective_user.id
            now = datetime.now()
            
            rate_limit_data[user_id] = [
                t for t in rate_limit_data[user_id]
                if t > now - timedelta(seconds=per)
            ]
            
            if len(rate_limit_data[user_id]) >= limit:
                wait_time = (rate_limit_data[user_id][0] + timedelta(seconds=per) - now).seconds
                await update.effective_message.reply_text(
                    f"⏳ *स्पैम सुरक्षा अलर्ट!*\nकृपया थोड़ा रुकें, {wait_time} सेकंड बाद दोबारा प्रयास करें।",
                    parse_mode="Markdown"
                )
                return
            
            rate_limit_data[user_id].append(now)
            return await func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator

from functools import wraps

# =====================================================================
# 🤖 CORE BOT UI HANDLERS & ORCHESTRATION LAYER (bot.py logic merged)
# =====================================================================
class TTSBot:
    def __init__(self):
        self.tts = UnifiedTTS()
        self.db = Database()
        self.app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()
    
    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("languages", self.languages_command))
        self.app.add_handler(CommandHandler("history", self.history_command))
        self.app.add_handler(CommandHandler("settings", self.settings_command))
        
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("voice", self.voice_command),
                CallbackQueryHandler(self.voice_workflow_callback, pattern="^trigger_voice_ui$")
            ],
            states={
                SELECTING_VOICE: [
                    CallbackQueryHandler(self.callback_handler, pattern="^voice_page_"),
                    CallbackQueryHandler(self.voice_selected, pattern="^voice_")
                ],
                WAITING_FOR_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation), CallbackQueryHandler(self.cancel_callback, pattern="^back$")]
        )
        self.app.app_context
        self.app.add_handler(conv_handler)
        self.app.add_handler(CallbackQueryHandler(self.general_callbacks, pattern="^(ui_back_home|ui_show_hist|ui_show_settings)$"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_direct_text))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self.db.add_user(user.id, user.username, user.first_name)
        
        keyboard = [
            [InlineKeyboardButton("🎙️ चेंज वॉयस", callback_data="trigger_voice_ui"),
             InlineKeyboardButton("⚙️ सेटिंग्स", callback_data="ui_show_settings")],
            [InlineKeyboardButton("📊 हिस्ट्री", callback_data="ui_show_hist")]
        ]
        await update.message.reply_text(
            f"👋 स्वागत है {user.first_name}!\n\n"
            "मैं एक इंटेलिजेंट *AI Text-to-Speech (TTS)* बॉट हूँ।\n"
            "मुझे कोई भी टेक्स्ट मैसेज भेजें, मैं उसका मूड एनालाइज करके उसे रियल साउंडिंग वॉइस में बदल दूँगा।",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 *उपलब्ध कमांड्स गाइड:*\n\n"
            "/start - होम डैशबोर्ड खोलें\n"
            "/voice - 300+ भाषाओं और आवाज़ों की लिस्ट बदलें\n"
            "/settings - आपकी मौजूदा एक्टिव कॉन्फ़िगरेशन\n"
            "/history - आपके द्वारा बनाए गए पिछले ऑडियो ट्रैक\n\n"
            "💡 *शॉर्टकट:* बिना किसी कमांड के सीधे टेक्स्ट टाइप करें, बोट तुरंत ऑडियो जेनरेट कर देगा।",
            parse_mode="Markdown"
        )

    async def voice_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        voices = await self.tts.list_voices("edge_tts")
        if not voices:
            await update.message.reply_text("