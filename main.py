#!/usr/bin/env python3
"""
Telegram TTS Bot – Simplified, Production Ready for Render
No admin commands, no developer ID, just TTS with voice selection.
"""

import sys
import os
import io
import json
import logging
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
from typing import Dict, List, Tuple, Any, Optional

# ========== FIX: dummy audioop for pydub ==========
if 'audioop' not in sys.modules:
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

from pydub import AudioSegment
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
    TELEGRAM_BOT_TOKEN = "8691786785:AAFQbqE8R1ZnULDOzVv0eKJ4XC2cCSsUGvU"
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    CHANNEL_LINK = "https://t.me/jorogamer"  # Your channel link
    DATABASE_URL = "sqlite+aiosqlite:///tts_bot.db"
    VOICES_DB_PATH = "voices_data.json"
    MAX_TEXT_LENGTH = 5000
    DEFAULT_FORMAT = "mp3"
    LOG_LEVEL = logging.INFO

logging.basicConfig(level=Config.LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TTSBot")

SELECTING_VOICE, WAITING_FOR_TEXT = range(2)
rate_limit_data = defaultdict(list)

# ========== DATABASE (SQLite) ==========
class Database:
    def __init__(self):
        self.db_path = "tts_bot.db"
    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.commit()
    async def add_user(self, user_id, username=None, first_name=None):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user_id, username, first_name))
            await conn.commit()
    async def get_user_settings(self, user_id):
        async with aiosqlite.connect(self.db_path) as conn:
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
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("UPDATE users SET settings = ? WHERE user_id = ?", (json.dumps(settings), user_id))
            await conn.commit()
    async def add_history_entry(self, user_id, text, voice):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT INTO history (user_id, text, voice) VALUES (?, ?, ?)", (user_id, text, voice))
            await conn.commit()

# ========== TTS ENGINE ==========
class UnifiedTTS:
    def __init__(self):
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
                        logger.info(f"Loaded {len(self.voices)} voices from cache")
                        return
            except: pass
        try:
            raw = await edge_tts.list_voices()
            self.voices = [{"Name": v["ShortName"], "ShortName": v["ShortName"], "Gender": v.get("Gender","Neutral"), "Locale": v.get("Locale","")} for v in raw]
            with open(self.voices_cache_path, "w") as f:
                json.dump({"edge_tts": {"voices": self.voices}}, f)
            logger.info(f"Fetched {len(self.voices)} voices")
        except Exception as e:
            logger.error(f"Voice fetch failed: {e}, using fallback")
            self.voices = [
                {"Name":"hi-IN-SwaraNeural","ShortName":"hi-IN-SwaraNeural","Gender":"Female","Locale":"hi-IN"},
                {"Name":"hi-IN-MadhurNeural","ShortName":"hi-IN-MadhurNeural","Gender":"Male","Locale":"hi-IN"},
                {"Name":"en-US-JennyNeural","ShortName":"en-US-JennyNeural","Gender":"Female","Locale":"en-US"}
            ]
    async def list_voices(self):
        if not self.voices:
            await self.load_voices()
        return self.voices
    async def synthesize(self, text, voice, output_format="mp3"):
        cache_key = hashlib.md5(f"{text}:{voice}".encode()).hexdigest()
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            comm = edge_tts.Communicate(text, voice)
            data = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    data.extend(chunk["data"])
            audio = bytes(data)
            self.cache[cache_key] = audio
            return audio
        except Exception as e:
            logger.error(f"Edge TTS failed: {e}, fallback to gTTS")
            lang = voice.split('-')[0] if '-' in voice else 'hi'
            loop = asyncio.get_running_loop()
            tts = await loop.run_in_executor(None, lambda: gtts.gTTS(text, lang=lang))
            fp = io.BytesIO()
            await loop.run_in_executor(None, lambda: tts.write_to_fp(fp))
            fp.seek(0)
            return fp.read()

# ========== RATE LIMIT ==========
def rate_limit(limit, per):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, update, context, *args, **kwargs):
            user = update.effective_user
            if not user:
                return await func(self, update, context, *args, **kwargs)
            msg = update.effective_message or (update.callback_query and update.callback_query.message)
            if not msg:
                return await func(self, update, context, *args, **kwargs)
            uid = user.id
            now = datetime.now()
            rate_limit_data[uid] = [t for t in rate_limit_data[uid] if t > now - timedelta(seconds=per)]
            if len(rate_limit_data[uid]) >= limit:
                wait = (rate_limit_data[uid][0] + timedelta(seconds=per) - now).seconds
                await msg.reply_text(f"⏳ रुकिए, {wait} सेकंड बाद प्रयास करें।")
                return
            rate_limit_data[uid].append(now)
            return await func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator

# ========== MAIN BOT ==========
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
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.direct_tts), group=1)
    
    async def start(self, update, context):
        user = update.effective_user
        await self.db.add_user(user.id, user.username, user.first_name)
        kb = [
            [InlineKeyboardButton("🎤 बदलें आवाज़", callback_data="trigger_voice")],
            [InlineKeyboardButton("📜 इतिहास", callback_data="menu_history")],
            [InlineKeyboardButton("📢 चैनल ज्वाइन करें", url=Config.CHANNEL_LINK)]
        ]
        await update.message.reply_text(f"नमस्ते {user.first_name}! मैं TTS बॉट हूँ। कोई भी टेक्स्ट भेजें, आवाज़ बनाऊँगा।", reply_markup=InlineKeyboardMarkup(kb))
    
    async def help(self, update, context):
        await update.message.reply_text("कमांड्स: /start, /voice, /history\nटेक्स्ट सीधे भेजें।")
    
    async def history(self, update, context):
        uid = update.effective_user.id
        async with aiosqlite.connect("tts_bot.db") as conn:
            cursor = await conn.execute("SELECT text, voice, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (uid,))
            rows = await cursor.fetchall()
        if not rows:
            await update.message.reply_text("कोई इतिहास नहीं।")
            return
        msg = "📜 हालिया:\n" + "\n".join([f"• {r[0][:40]}... ({r[1]})" for r in rows])
        await update.message.reply_text(msg)
    
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
            await update.message.reply_text(f"टेक्स्ट बहुत लंबा है (max {Config.MAX_TEXT_LENGTH})")
            return WAITING_FOR_TEXT
        voice = context.user_data.get("selected_voice", "hi-IN-SwaraNeural")
        await self.db.update_user_setting(update.effective_user.id, "voice", voice)
        proc = await update.message.reply_text("🎧 आवाज़ बन रही है...")
        try:
            audio = await self.tts.synthesize(text, voice)
            await self.db.add_history_entry(update.effective_user.id, text, voice)
            await update.message.reply_audio(audio=audio, filename=f"tts.mp3")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
        finally:
            await proc.delete()
        return ConversationHandler.END
    
    async def direct_tts(self, update, context):
        if context.user_data.get("state") == SELECTING_VOICE:
            return
        text = update.message.text
        if text.startswith('/'):
            return
        uid = update.effective_user.id
        sets = await self.db.get_user_settings(uid)
        voice = sets.get("voice", "hi-IN-SwaraNeural")
        proc = await update.message.reply_text("🎧 प्रोसेसिंग...")
        try:
            audio = await self.tts.synthesize(text, voice)
            await self.db.add_history_entry(uid, text, voice)
            await update.message.reply_audio(audio=audio, filename="speech.mp3")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
        finally:
            await proc.delete()
    
    async def cancel(self, update, context):
        await update.message.reply_text("❌ रद्द। /start")
        return ConversationHandler.END
    
    async def cancel_cb(self, update, context):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("रद्द। /start")
        return ConversationHandler.END
    
    async def run(self):
        await self.tts.load_voices()
        await self.db.init_db()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Bot is running...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        bot = TTSBot()
        asyncio.run(bot.run())
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
