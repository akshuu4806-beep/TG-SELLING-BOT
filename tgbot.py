from dotenv import load_dotenv
load_dotenv()

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import re
import os
import gc
import qrcode
from google import genai
import json
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
import pycountry
from io import BytesIO
from datetime import datetime, timedelta, timezone
from telethon.tl.types import UpdateBotChatInviteRequester, UpdateChannelParticipant
from telethon import TelegramClient, events, Button, functions, types
from telethon.tl.functions.channels import GetParticipantRequest, GetFullChannelRequest
from telethon.tl.functions.messages import GetHistoryRequest, GetFullChatRequest, CheckChatInviteRequest
from telethon.tl.types import ChannelParticipantsSearch, InputPeerChannel, InputPeerChat
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    UserNotParticipantError,
    UserDeactivatedBanError,
    UserIsBlockedError,
    PeerIdInvalidError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    MessageNotModifiedError,
)
from pymongo import MongoClient

# ================= CONFIGURATION =================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_IDS = [int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()]

UPI_ID = os.getenv("UPI_ID", "")

# Force Join Setup
CHANNEL_USERNAME = "itz_darkX"
GROUP_USERNAME = "-1003944403417"

# Groups Configuration
OTP_GROUP_ID = -1003944403417          # Main OTP Group
PAYMENT_LOG_GROUP = -1004338467560     # Payment Verification Group ID

CHANNEL_LINK = "https://t.me/itz_darkX"
GROUP_LINK = "https://t.me/+Vhaq8wgS8cRkZjhl"

# MongoDB Connection String
MONGO_URI = os.getenv("MONGO_URI")

# MongoDB Database Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["tg_store_bot"]

# Collections
users_col = db["users"]
accounts_col = db["accounts"]
chats_col = db["chats"]
settings_col = db["settings"]
db["forced_chats"].create_index("chat_id", unique=True)

# Temporary runtime states
user_states = {}
login_states = {}

# Telethon bot client (session string optional for bot)
bot = TelegramClient("StoreBot", API_ID, API_HASH)

# Gemini API Keys
raw_api_keys = os.getenv("API_KEYS", "")
API_KEYS = [k.strip() for k in raw_api_keys.split(",") if k.strip()]

if not API_KEYS:
    raise ValueError("❌ Error: API_KEYS environment variable is missing!")

current_key_index = 0
ai_client = genai.Client(api_key=API_KEYS[current_key_index])

# ================= SESSION LOCK MANAGER =================
_session_locks: dict = {}
_busy_phones: set = set()


def get_formatted_time():
    now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    date_str = now.strftime("%d-%m-%Y")
    time_str = now.strftime("%I:%M:%S %p")
    return date_str, time_str


def mask_upi_id(upi: str) -> str:
    if not upi or "@" not in upi:
        return "N/A"
    parts = upi.split("@")
    name = parts[0]
    domain = parts[1]
    if len(name) <= 3:
        masked_name = name[0] + "*" * (len(name) - 1)
    else:
        masked_name = name[:2] + "*" * (len(name) - 4) + name[-2:]
    return f"{masked_name}@{domain}"


def _detect_image_mime(data: bytes) -> str:
    """Image ke actual magic bytes se correct MIME type detect karo."""
    if len(data) < 12:
        return "image/jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/jpeg"


async def extract_ss_details(photo_bytes: bytes) -> dict:
    global current_key_index, ai_client

    if not photo_bytes or len(photo_bytes) < 500:
        print(f"🚨 [AI] Image too small: {len(photo_bytes) if photo_bytes else 0} bytes")
        return {"is_valid_ss": False, "upi_txn_id": "N/A", "customer_upi": "N/A",
                "date": "N/A", "time": "N/A", "amount": None, "receiver": "N/A",
                "_ai_status": "image_too_small"}

    mime_type = _detect_image_mime(photo_bytes)
    print(f"🖼️ [AI] MIME: {mime_type} | Size: {len(photo_bytes)} bytes")

    prompt = (
        "You are reading a UPI payment screenshot from an Indian payment app "
        "(GPay, PhonePe, Paytm, BHIM, etc.).\n\n"
        "Extract these fields from visible text:\n"
        "- upi_txn_id: UPI transaction ID / UTR / Ref No (12-digit number, look near 'UTR' or 'Txn ID')\n"
        "- customer_upi: Sender UPI ID (like name@oksbi, 9876543210@ybl)\n"
        "- date: Payment date in YYYY-MM-DD format\n"
        "- time: Payment time in HH:MM:SS AM/PM format\n"
        "- amount: Numeric amount only (no ₹, no commas)\n"
        "- receiver: Receiver UPI ID or merchant name\n\n"
        "Use null if field not visible. Do NOT make up values.\n"
        "Respond ONLY with raw JSON (no markdown fences):\n"
        '{"upi_txn_id": null, "customer_upi": null, "date": null, "time": null, "amount": null, "receiver": null}'
    )

    last_error = "unknown"
    
    # ✅ FIX 1: Set correct current models as fallback
    # These are based on the API error messages and latest documentation.
    models_to_try = ['gemini-3.8-flash', 'gemini-3.1-pro-preview', 'gemini-3-flash-preview']
    
    print(f"ℹ️ Using model list: {models_to_try}")

    # Loop through the models we have
    for model_name in models_to_try:
        try:
            # 🎯 KEY FIX: Use SYNC API via to_thread (as before, this was correct)
            response = await asyncio.to_thread(
                ai_client.models.generate_content,
                model=model_name,
                contents=[
                    prompt,
                    genai.types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg")
                ]
            )

            raw = None
            try:
                raw = response.text
            except Exception:
                pass
            if not raw:
                try:
                    raw = response.candidates[0].content.parts[0].text
                except Exception:
                    raw = None

            if not raw:
                last_error = f"empty_{model_name}"
                print(f"⚠️ [{model_name}] empty response")
                continue

            print(f"🔎 RAW AI [{model_name}] key{current_key_index}: {raw[:400]}")

            cleaned = raw.strip().replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            if not cleaned.startswith("{"):
                m = re.search(r"\{.*\}", cleaned, re.DOTALL)
                if m:
                    cleaned = m.group(0)

            data = json.loads(cleaned)

            # Normalize nulls
            for k in ["upi_txn_id", "customer_upi", "date", "time", "receiver"]:
                v = data.get(k)
                if v is None or str(v).strip().lower() in ["", "null", "none", "n/a",
                                                          "not visible", "not found",
                                                          "not readable", "unknown", "na"]:
                    data[k] = "N/A"

            data["is_valid_ss"] = all(
                data.get(k) not in (None, "N/A")
                for k in ["upi_txn_id", "date", "time"]
            )
            data["_ai_status"] = "success"

            print(f"✅ AI success [{model_name}] key{current_key_index} → "
                  f"txn={data['upi_txn_id']}, date={data['date']}, "
                  f"time={data['time']}, amt={data.get('amount')}")
            return data

        except Exception as e:
            err_msg = str(e)
            last_error = f"{model_name}:{err_msg[:80]}"
            print(f"❌ AI ERR [{model_name}] key{current_key_index}: {err_msg[:250]}")

            if any(x in err_msg for x in ["429", "RESOURCE_EXHAUSTED",
                                          "quota", "rate limit"]):
                current_key_index = (current_key_index + 1) % len(API_KEYS)
                ai_client = genai.Client(api_key=API_KEYS[current_key_index])
                print(f"🔄 Switched to key {current_key_index}")

            await asyncio.sleep(0.5)

    print(f"🚨 AI FAILED COMPLETELY — last_error={last_error}")
    return {"is_valid_ss": False, "upi_txn_id": "N/A", "customer_upi": "N/A",
            "date": "N/A", "time": "N/A", "amount": None, "receiver": "N/A",
            "_ai_status": last_error}

def parse_ss_datetime(date_str: str, time_str: str):
    if not date_str or not time_str:
        return None
    try:
        combined = f"{date_str} {time_str}"
        for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(combined, fmt)
            except ValueError:
                continue
    except Exception as e:
        print(f"Datetime parse error: {e}")
    return None

async def cleanup_user_login(user_id: int):
    if user_id in login_states:
        session_data = login_states.pop(user_id, None)
        if session_data:
            acc = session_data.get("account", {})
            target_phone = acc.get("phone")
            if target_phone:
                _busy_phones.discard(target_phone)
            client_obj = session_data.get("client")
            if client_obj:
                await safe_disconnect(client_obj)

async def safe_edit(event, text, **kwargs):
    """event.edit() wrapper — same message edit hone pe error nahi dega."""
    try:
        return await event.edit(text, **kwargs)
    except MessageNotModifiedError:
        return None
    except Exception as e:
        # "message to edit not found" ya "message can't be edited" — silently ignore
        err = str(e).lower()
        if "not modified" in err or "can't be edited" in err or "message to edit not found" in err:
            return None
        raise

def _lock_for(name: str) -> asyncio.Lock:
    if name not in _session_locks:
        _session_locks[name] = asyncio.Lock()
    return _session_locks[name]


async def safe_disconnect(client):
    if not client:
        return
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception as e:
        print(f"disconnect err: {e}")

async def safe_logout(client):
    """Bot ke session ko REAL Telegram server se TERMINATE karo (disconnect nahi)."""
    if not client:
        return
    try:
        if not client.is_connected():
            try:
                await client.connect()
            except Exception as e:
                print(f"connect-before-logout failed: {e}")
                return

        try:
            await client.log_out()      # ✅ REAL termination — AuthKey invalidate
            print("🔒 [LOGOUT] Bot session terminated on Telegram servers")
        except Exception as e:
            print(f"⚠️ [LOGOUT] log_out failed: {e} — falling back to disconnect")
            try:
                await client.disconnect()
            except Exception:
                pass
    except Exception as e:
        print(f"safe_logout err: {e}")

def get_available_countries(acc_type: str = "fresh"):
    pipeline = [
        {
            "$match": {
                "sold": False,
                "$or": [
                    {"type": acc_type},
                    {"type": {"$exists": False}} if acc_type == "fresh" else {"_id": None}
                ]
            }
        },
        {
            "$group": {
                "_id": "$country_name",
                "count": {"$sum": 1},
                "code": {"$first": "$country_code"}
            }
        }
    ]
    return list(accounts_col.aggregate(pipeline))


def mask_phone_number(phone: str, country_code: str = None) -> str:
    phone_clean = re.sub(r'[\s\-\(\)]', '', phone)
    if not phone_clean.startswith('+'):
        phone_clean = '+' + phone_clean

    cc = country_code.strip() if country_code else ""
    if not cc.startswith('+'):
        cc = '+' + cc
    cc = cc.strip()

    if not cc or cc == '+':
        if phone_clean.startswith('+91'):
            cc = '+91'
        elif phone_clean.startswith('+1'):
            cc = '+1'
        else:
            cc = phone_clean[:3] if len(phone_clean) > 10 else phone_clean[:2]

    last_three = phone_clean[-3:]
    hidden_len = len(phone_clean) - len(cc) - 3
    if hidden_len < 1:
        return f"{cc}{'*' * 3}{last_three}"
    return f"{cc}{'*' * hidden_len}{last_three}"


def get_flag_emoji(country_name: str) -> str:
    custom_flags = {"usa": "🇺🇸", "us": "🇺🇸", "uk": "🇬🇧", "uae": "🇦🇪", "india": "🇮🇳"}
    clean_name = country_name.strip().lower()
    if clean_name in custom_flags:
        return custom_flags[clean_name]
    try:
        country = pycountry.countries.search_fuzzy(country_name)[0]
        iso = country.alpha_2.upper()
        return chr(0x1F1E6 + ord(iso[0]) - ord('A')) + chr(0x1F1E6 + ord(iso[1]) - ord('A'))
    except Exception:
        return "🌍"


def extract_otp(text: str) -> str:
    match = re.search(r'\b\d{5,6}\b', text)
    if match:
        return match.group(0)
    return "NOT-FOUND"


def get_user(user_id, referrer_id=None):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "wallet": 0.0,
            "total_spent": 0.0,
            "total_deposited": 0.0,
            "referral_profit": 0.0,
            "referred_by": referrer_id,
            "start_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        users_col.insert_one(user)
    return user

def styled_inline(text, data, color="primary"):
    """Inline button with background color."""
    btn = Button.inline(text, data)
    btn.style = types.KeyboardButtonStyle(
        bg_primary=(color == "primary"),
        bg_success=(color == "success"),
        bg_danger=(color == "danger")
    )
    return btn


def styled_url(text, url, color="primary"):
    """URL button with background color."""
    btn = Button.url(text, url)
    btn.style = types.KeyboardButtonStyle(
        bg_primary=(color == "primary"),
        bg_success=(color == "success"),
        bg_danger=(color == "danger")
    )
    return btn

def get_state(user_id):
    if user_id not in user_states:
        user_states[user_id] = {"state": None, "deposit_amount": 0.0}
    return user_states[user_id]


async def cleanup_session(session_path: str, active_client=None):
    lock = _lock_for(session_path)
    async with lock:
        if active_client:
            await safe_disconnect(active_client)
        gc.collect()
        await asyncio.sleep(0.2)
        full_path = f"{session_path}.session"
        if os.path.exists(full_path):
            for attempt in range(5):
                try:
                    os.remove(full_path)
                    break
                except Exception:
                    if attempt == 4:
                        print(f"Could not remove locked session file (giving up): {full_path}")
                    await asyncio.sleep(0.5)


# ================= KEYBOARDS =================
from telethon.tl import types

def main_keyboard():
    return types.ReplyKeyboardMarkup(
        rows=[
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(
                        text="💳 Deposit Money",
                        type=types.ButtonTypeDefault(), # 👈 Ye zaroori tha
                        style=types.KeyboardButtonStyle(bg_primary=True)
                    ),
                    types.KeyboardButton(
                        text="🛒 Buy Account",
                        type=types.ButtonTypeDefault(),
                        style=types.KeyboardButtonStyle(bg_success=True)
                    )
                ]
            ),
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(
                        text="👤 Profile",
                        type=types.ButtonTypeDefault(),
                        style=types.KeyboardButtonStyle(bg_primary=True)
                    ),
                    types.KeyboardButton(
                        text="📊 Stats",
                        type=types.ButtonTypeDefault(),
                        style=types.KeyboardButtonStyle(bg_success=True)
                    )
                ]
            ),
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(
                        text="🛠️ Support",
                        type=types.ButtonTypeDefault(),
                        style=types.KeyboardButtonStyle(bg_danger=True)
                    )
                ]
            )
        ],
        resize=True,
        single_use=False,       # 👈 Buttons hide rahenge
        selective=False,
        persistent=False       # 👈 Grid icon dikhega
    )

def logout_keyboard():
    return [[styled_inline("🔴 Logout Bot Session", b"customer_logout", "danger")]]

def force_join_keyboard():
    return [
        [styled_url("📢 Channel", CHANNEL_LINK, "primary")],
        [styled_url("💬 Otp Group", GROUP_LINK, "success")],
        [styled_inline("✅ Verify / Check Join", b"check_force_join", "success")]
    ]

async def get_force_join_channels():
    config = settings_col.find_one({"type": "force_join"})
    if config and "channels" in config:
        return config["channels"]
    return [
        {"username": CHANNEL_USERNAME, "link": CHANNEL_LINK, "title": "📢 Channel"},
        {"username": GROUP_USERNAME, "link": GROUP_LINK, "title": "💬 Otp Group"}
    ]

async def build_force_join_keyboard():
    channels = await get_force_join_channels()
    buttons = []
    for idx, ch in enumerate(channels, 1):
        title = ch.get("title") or f"📢 Join Channel {idx}"
        buttons.append([styled_url(title, ch["link"], "primary")])
    buttons.append([styled_inline("✅ Verify / Check Join", b"check_force_join", "success")])
    return buttons

@bot.on(events.Raw(types=UpdateBotChatInviteRequester))
async def on_join_request_raw(event):
    """Jab koi user join request bheje, use DB me save karo."""
    try:
        # Peer se chat_id nikalna
        peer = event.peer
        chat_id = None
        if hasattr(peer, "channel_id"):
            chat_id = int(f"-100{peer.channel_id}")
        elif hasattr(peer, "chat_id"):
            chat_id = int(f"-{peer.chat_id}")

        if chat_id is None:
            return

        db["join_requests"].update_one(
            {"user_id": event.user_id, "chat_id": chat_id},
            {
                "$set": {
                    "user_id": event.user_id,
                    "chat_id": chat_id,
                    "requested_at": datetime.now(),
                    "status": "pending"
                }
            },
            upsert=True
        )
        print(f"📥 Join request captured: user={event.user_id}, chat={chat_id}")
    except Exception as e:
        print(f"⚠️ Join request capture error: {e}")

@bot.on(events.Raw(types=UpdateChannelParticipant))
async def on_member_leave_raw(event):
    """Jab koi user leave/kick ho, pending record delete karo."""
    try:
        # Sirf leave/kick events (pehle tha, ab nahi hai)
        if event.prev_participant and not event.new_participant:
            user_id = event.user_id
            chat_id = int(f"-100{event.channel_id}")

            deleted = db["join_requests"].delete_one({
                "user_id": user_id,
                "chat_id": chat_id
            })

            if deleted.deleted_count > 0:
                print(f"🗑️ Pending cleared (leave): user={user_id}, chat={chat_id}")
    except Exception as e:
        print(f"⚠️ Leave event error: {e}")
        
async def is_user_joined(client: TelegramClient, user_id: int) -> bool:
    status_config = settings_col.find_one({"type": "force_join_status"})
    if status_config and status_config.get("status") == "off":
        return True

    channels = await get_force_join_channels()
    for ch in channels:
        target = str(ch.get("username", "")).strip()
        link = str(ch.get("link", "")).strip()
        chat_identifier = None

        if target.startswith("-100") or (target.startswith("-") and target[1:].isdigit()) or target.isdigit():
            try:
                chat_identifier = int(target)
            except ValueError:
                chat_identifier = target
        elif target.startswith("@"):
            chat_identifier = target
        elif "t.me/" in target:
            clean_part = target.split("t.me/")[-1].replace("/", "")
            if clean_part.startswith("+") or "joinchat" in clean_part:
                chat_identifier = link if ("t.me/" in link and not link.startswith("+")) else target
            else:
                chat_identifier = "@" + clean_part
        else:
            chat_identifier = "@" + target if not target.startswith("+") else target

        is_verified = False
        entity = None

        # ✅ Entity resolve fail ho to BLOCK mat karo, skip karo
        try:
            entity = await client.get_entity(chat_identifier)
        except Exception as e:
            print(f"⚠️ Entity resolve failed for {chat_identifier}: {e} — skipping")
            continue

        # ✅ Sirf DEFINITE "not joined" pe block, unknown errors pe skip
        try:
            await client(functions.channels.GetParticipantRequest(entity, user_id))
            is_verified = True

            # ✅ Member hai → purana pending delete karo
            # Iska matlab: agla verify fail hua toh dobara request deni padegi
            _cleanup_id = None
            if hasattr(entity, "id"):
                if isinstance(entity, types.Channel):
                    _cleanup_id = int(f"-100{entity.id}")
                elif isinstance(entity, types.Chat):
                    _cleanup_id = int(f"-{entity.id}")

            if _cleanup_id is not None:
                deleted = db["join_requests"].delete_one({
                    "user_id": user_id,
                    "chat_id": _cleanup_id
                })
                if deleted.deleted_count > 0:
                    print(f"🧹 Pending cleared after verify: user={user_id}, chat={_cleanup_id}")

        except UserNotParticipantError:

            # ✅ Member nahi hai — check karo pending join request hai kya
            is_verified = False

            # Chat ka numeric ID nikaalna
            numeric_id = None
            if hasattr(entity, "id"):
                if isinstance(entity, types.Channel):
                    numeric_id = int(f"-100{entity.id}")
                elif isinstance(entity, types.Chat):
                    numeric_id = int(f"-{entity.id}")

            if numeric_id is not None:
                pending = db["join_requests"].find_one({
                    "user_id": user_id,
                    "chat_id": numeric_id,
                    "status": "pending"
                })
                if pending:
                    print(f"✅ Pending request found: user={user_id}, chat={numeric_id}")
                    is_verified = True

        except Exception as e:
            print(f"⚠️ Participant check error for {chat_identifier}: {e} — skipping")
            continue

        # Fallback check
        if not is_verified and link and link != target:
            try:
                fallback_target = link.split("t.me/")[-1].replace("/", "")
                if not fallback_target.startswith("+"):
                    fb_entity = await client.get_entity("@" + fallback_target)
                    try:
                        await client(functions.channels.GetParticipantRequest(fb_entity, user_id))
                        is_verified = True
                    except UserNotParticipantError:
                        pass
                    except Exception:
                        pass
            except Exception:
                pass

        # ✅ Sirf genuinely "not joined" pe block
        if not is_verified:
            return False

    return True

# ================= BOT ON/OFF SYSTEM =================
def get_bot_status():
    """Returns (is_off, message)."""
    config = settings_col.find_one({"type": "bot_status"})
    if config and config.get("status") == "off":
        return True, config.get("message", "🔴 Bot is currently under maintenance. Please try again later.")
    return False, None


@bot.on(events.NewMessage(pattern=r"^/botoff"))
async def bot_off_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2:
        await event.reply(
            "⚠️ <b>Format:</b>\n"
            "<code>/botoff &lt;your message&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/botoff Bot is under maintenance. Back in 1 hour.</code>",
            parse_mode='html'
        )
        return
    off_msg = input_text[1].strip()

    # ✅ 1. Save to DB
    settings_col.update_one(
        {"type": "bot_status"},
        {"$set": {"status": "off", "message": off_msg, "off_at": datetime.now()}},
        upsert=True
    )

    # ✅ 2. Owner ko QUOTE me confirmation (reply_to = command message)
    status_msg = await event.reply(
        f"🔴 <b>Bot OFF Ho Gaya!</b>\n\n"
        f"📝 <b>Message:</b>\n"
        f"<blockquote>{off_msg}</blockquote>\n\n"
        f"📢 Broadcasting to all users...",
        parse_mode='html',
        reply_to=event.message   # 👈 QUOTE
    )

    # ✅ 3. Reply-based media support (agar owner ne kisi message ko reply karke /botoff bheja)
    replied = await event.get_reply_message()

    # ✅ 4. SABHI USERS KO BROADCAST
    all_users = list(users_col.find({}, {"user_id": 1}))
    success = 0
    failed = 0

    for u in all_users:
        uid = u.get("user_id")
        if not uid:
            continue
        try:
            # Text message (quoted style)
            await bot.send_message(
                uid,
                f"🔴 <b>Bot is Offline</b>\n\n"
                f"<blockquote>{off_msg}</blockquote>",
                parse_mode='html'
            )
            # Agar owner ne koi message reply karke /botoff bheja hai, toh woh bhi forward karo
            if replied:
                try:
                    await replied.forward_to(uid)
                except Exception:
                    pass
            success += 1
            await asyncio.sleep(0.05)   # Rate limit safe
        except Exception as e:
            # Blocked/deactivated users — ignore
            failed += 1

    # ✅ 5. Confirmation UPDATE karo with broadcast results
    try:
        await status_msg.edit(
            f"🔴 <b>Bot OFF Ho Gaya!</b>\n\n"
            f"📝 <b>Message:</b>\n"
            f"<blockquote>{off_msg}</blockquote>\n\n"
            f"✅ <b>Broadcast Complete!</b>\n"
            f"🟢 <b>Delivered:</b> <code>{success}</code>\n"
            f"🔴 <b>Failed:</b> <code>{failed}</code>\n"
            f"📊 <b>Total:</b> <code>{len(all_users)}</code>",
            parse_mode='html'
        )
    except Exception as e:
        print(f"Edit status error: {e}")

@bot.on(events.NewMessage(pattern=r"^/boton"))
async def bot_on_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    
    input_text = event.text.split(maxsplit=1)
    on_msg = input_text[1].strip() if len(input_text) > 1 else None
    
    update_data = {"status": "on", "on_at": datetime.now()}
    if on_msg:
        update_data["on_message"] = on_msg
    
    settings_col.update_one(
        {"type": "bot_status"},
        {"$set": update_data},
        upsert=True
    )
    
    if on_msg:
        await event.reply(
            f"🟢 <b>Bot ON Ho Gaya!</b>\n\n"
            f"📝 <b>Users ko yeh message broadcast hoga:</b>\n"
            f"<blockquote>{on_msg}</blockquote>\n\n"
            f"📢 Broadcasting to all users...",
            parse_mode='html'
        )
        
        # Broadcast "bot is back online" message to all users
        all_users = list(users_col.find({}, {"user_id": 1}))
        success = 0
        failed = 0
        for u in all_users:
            uid = u.get("user_id")
            if not uid:
                continue
            try:
                await bot.send_message(
                    uid,
                    f"🟢 <b>Bot is back online!</b>\n\n"
                    f"<blockquote>{on_msg}</blockquote>",
                    parse_mode='html'
                )
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        
        await event.reply(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"🟢 <b>Delivered:</b> <code>{success}</code>\n"
            f"🔴 <b>Failed:</b> <code>{failed}</code>\n"
            f"📊 <b>Total:</b> <code>{len(all_users)}</code>",
            parse_mode='html'
        )
    else:
        await event.reply(
            "🟢 <b>Bot ON Ho Gaya!</b>\n\n"
            "✅ Ab sabhi users bot use kar sakte hain.\n\n"
            "💡 <i>Tip: <code>/boton &lt;message&gt;</code> se users ko notification bhi bhej sakte hain.</i>",
            parse_mode='html'
        )

@bot.on(events.NewMessage(pattern=r"^/botstatus"))
async def bot_status_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    is_off, off_msg = get_bot_status()
    if is_off:
        await event.reply(
            f"🔴 <b>Bot Status:</b> OFF\n\n"
            f"📝 <b>Current Message:</b>\n"
            f"<blockquote>{off_msg}</blockquote>",
            parse_mode='html'
        )
    else:
        await event.reply("🟢 <b>Bot Status:</b> ON\n\n✅ Sab users use kar sakte hain.", parse_mode='html')

# ================= PREMIUM EMOJI HELPER =================
# Apni emoji IDs yahan paste karo (@EmojiIDBot se)
PREMIUM_EMOJI_IDS = {
    "💎": "5309958691854754293",
    "👋": "5368324170671202286",
    "🎁": "5368324170671202286",
    "🔗": "5368324170671202286",
    "⚡": "5368324170671202286",
    "🟢": "",
    "🔵": "",
    "🟡": "",
}


def pe(emoji: str, is_premium: bool) -> str:
    if is_premium:
        emoji_id = PREMIUM_EMOJI_IDS.get(emoji, "")
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji

def get_support_usernames():
    config = settings_col.find_one({"type": "support_settings"})
    if config and "usernames" in config:
        return config["usernames"]
    return ["ll_its_rudransh_ll"]  # Default username (agar koi set nahi hai)

def apply_premium_emojis_to_text(text: str, is_premium: bool) -> str:
    """Custom message ke emojis ko premium emoji tags me convert karo"""
    text = re.sub(r'<tg-emoji[^>]*>([^<]+)</tg-emoji>', r'\1', text)
    if not is_premium:
        return text
    result = text
    for emoji, emoji_id in PREMIUM_EMOJI_IDS.items():
        if emoji_id and emoji in result:
            result = result.replace(
                emoji,
                f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
            )
    return result
# ========================================================
async def process_telegram_message(user_id, msg, session_data):
    text = msg.text or ""
    acc = session_data["account"]
    masked_num = mask_phone_number(acc["phone"], acc.get('country_code'))
    two_fa = acc.get("2fa_password") or "None"
    
    otp_code = extract_otp(text)
    user = users_col.find_one({"user_id": user_id})
    is_owner = (user_id in OWNER_IDS)
    
    # 1. Agar OTP aaya
    if otp_code != "NOT-FOUND":
        session_data["last_otp_time"] = datetime.now().timestamp() 
        session_data["otp_received_at"] = datetime.now().timestamp()  
        session_data["warning_count"] = 0 
        if not session_data.get("is_logged_in", False):
            if not is_owner:
                new_wallet = float(user.get("wallet", 0)) - float(acc["price"])
                users_col.update_one({"user_id": int(user_id)}, {"$set": {"wallet": new_wallet}, "$inc": {"total_spent": float(acc["price"])}})
            accounts_col.update_one({"phone": acc["phone"]}, {"$set": {"sold": True, "buyer_id": int(user_id)}})
            session_data["is_logged_in"] = True
            
        # 👇 Naya message nahi bhejenge, purane number wale message ko edit karenge
        msg_id = session_data.get("msg_id")
        otp_text = (
            f"📩 **Login Verification Code:**\n\n"
            f"📱 **Phone:** `{masked_num}`\n"
            f"🔑 **OTP:** `{otp_code}`\n"
            f"🔐 **2FA Password:** `{two_fa}`\n\n"
            f"💡 *Agar code expire ho jaye, toh Telegram me 'Send Code' dabakar yahan 'Check New Messages' par click karein.*"
        )
        
        if msg_id:
            try:
                await bot.edit_message(
                    user_id, msg_id, otp_text,
                    buttons=[[styled_inline("🔄 Check New Messages", b"fetch_otp", "success")]]
                )
                print(f"✅ [OTP] Message edited (id={msg_id})")
            except Exception as e:
                print(f"⚠️ [OTP] Edit failed: {e} — sending new")
                sent_new = await bot.send_message(
                    user_id, otp_text,
                    buttons=[[styled_inline("🔄 Check New Messages", b"fetch_otp", "success")]]
                )
                # ✅ CRITICAL FIX: msg_id update karo taaki success edit sahi message pe ho
                session_data["msg_id"] = sent_new.id
                print(f"📝 [OTP] New message sent (id={sent_new.id}) — msg_id updated")
        else:
            # msg_id hi nahi tha — send new + save
            sent_new = await bot.send_message(
                user_id, otp_text,
                buttons=[[styled_inline("🔄 Check New Messages", b"fetch_otp", "success")]]
            )
            session_data["msg_id"] = sent_new.id
            print(f"📝 [OTP] No msg_id — sent new (id={sent_new.id}) — msg_id saved")
        
     # 2. Agar "New Login" alert aaya (Login successful) — Option B: Auto-Disconnect
    else:
        # ---- Login Alert Detection (ROBUST) ----
        text_lower = text.lower()
        is_login_alert = (
            "new login" in text_lower or
            "naya login" in text_lower or
            "login into your account" in text_lower or
            "login successful" in text_lower or
            "logged in" in text_lower or
            "signed in" in text_lower or
            "new sign-in" in text_lower or
            "new signin" in text_lower or
            "device added" in text_lower or
            "active sessions" in text_lower or
            ("login" in text_lower and "device" in text_lower) or
            ("login" in text_lower and "terminate" in text_lower) or
            ("login" in text_lower and "session" in text_lower)
        )

        if not is_login_alert:
            # Normal SMS — send to user
            await bot.send_message(
                user_id,
                f"📩 **New Message Received:**\n\n"
                f"📱 **Phone:** `{masked_num}`\n"
                f"💬 **Message:** `{text[:500]}`",
                buttons=[
                    [styled_inline("🔄 Check New Messages", b"fetch_otp", "success")],
                    [styled_inline("🔴 Logout Bot Session", b"customer_logout", "danger")]
                ]
            )
            return

        # ============ SUCCESS BRANCH ============
        print(f"🎉 [LOGIN] Success detected for user={user_id}")

        if session_data.get("session_terminated"):
            print(f"⚠️ [LOGIN] Already terminated, skipping")
            return

        session_data["session_terminated"] = True
        session_data["login_verified"] = True

        user_client_now = session_data.get("client")
        if user_client_now:
            await safe_logout(user_client_now)

        if not session_data.get("is_logged_in", False):
            if not is_owner:
                new_wallet = float(user.get("wallet", 0)) - float(acc["price"])
                users_col.update_one({"user_id": int(user_id)}, {"$set": {"wallet": new_wallet}, "$inc": {"total_spent": float(acc["price"])}})
            accounts_col.update_one({"phone": acc["phone"]}, {"$set": {"sold": True, "buyer_id": int(user_id)}})
            session_data["is_logged_in"] = True

        # ✅ Purchase log ALAG karo — `purchase_logged` flag se (double send nahi hoga)
        if not session_data.get("purchase_logged"):
            session_data["purchase_logged"] = True

            try:
                tg_user = await bot.get_entity(user_id)
                user_mention = tg_user.first_name or "User"
                username_str = f"@{tg_user.username}" if tg_user.username else "N/A"
            except Exception:
                user_mention = "User"
                username_str = "N/A"

            acc_price = acc.get("price", 0.0)
            try:
                me = await bot.get_me()
                bot_username = me.username
            except Exception:
                bot_username = "N/A"

            group_msg = (
                f"🎉 **New Purchase Successful**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"📱 **Account Number:** `{masked_num}`\n"
                f"💰 **Price:** ₹{acc_price}\n\n"
                f"✨ **Thanks for purchase!**\n\n"
                f"🤖 **Bot:** @{bot_username}"
            )
            try:
                await bot.send_message(OTP_GROUP_ID, group_msg, link_preview=False)
                print(f"✅ [LOGIN] Purchase log sent")
            except Exception as ge:
                print(f"Purchase log send err: {ge}")

        # ---- Send SUCCESS message (multiple fallbacks) ----
        msg_id = session_data.get("msg_id")
        success_text = (
            f"🎉 **Login Successful Detected!**\n\n"
            f"📱 **Phone:** `{masked_num}`\n\n"
            f"✅ Aapne successfully Telegram me login kar liya hai!\n"
            f"🔐 **2FA Password:** `{two_fa}`\n\n"
            f"*🔒 Bot ka session Telegram server se TERMINATE kar diya gaya hai. "
            f"Ab 'Active Sessions' me sirf aapka device dikhega.*"
        )        
        sent_ok = False
        if msg_id:
            for attempt in range(3):
                try:
                    await bot.edit_message(user_id, msg_id, success_text, buttons=[])
                    sent_ok = True
                    print(f"✅ [LOGIN] Success edited (attempt {attempt+1})")
                    break
                except MessageNotModifiedError:
                    sent_ok = True
                    break
                except Exception as e:
                    err_lower = str(e).lower()
                    print(f"⚠️ [LOGIN] Edit attempt {attempt+1} failed: {e}")

                    # ✅ Same fallback — reply markup invalid hone pe buttons hata ke edit
                    if "reply markup" in err_lower or "reply_markup" in err_lower or "markup is invalid" in err_lower:
                        try:
                            await bot.edit_message(user_id, msg_id, success_text)
                            sent_ok = True
                            print(f"✅ [LOGIN] Success edited (kept old buttons) attempt {attempt+1}")
                            break
                        except Exception as e2:
                            print(f"⚠️ [LOGIN] Fallback edit also failed: {e2}")

                    await asyncio.sleep(1)

        if not sent_ok:
            # Sirf last resort — rare case (message delete ho gaya, etc.)
            try:
                await bot.send_message(user_id, success_text)
                print(f"⚠️ [LOGIN] Last resort: sent new message")
            except Exception as e:
                print(f"🚨 [LOGIN] send also failed: {e}")

        # Disconnect and cleanup
        if user_id in login_states:
            del login_states[user_id]

async def get_active_sessions_hash(client):
    """Account ke saare active sessions ke hashes ki set return karta hai."""
    try:
        result = await client(functions.account.GetAuthorizationsRequest())
        return {auth.hash for auth in result.authorizations}
    except Exception as e:
        print(f"⚠️ [SESSION] GetAuthorizations failed: {e}")
        return None


async def send_login_success_message(user_id, session_data):
    """Login success message bhejo (edit + fallback)."""
    acc = session_data.get("account", {})
    masked_num = mask_phone_number(acc.get("phone", ""), acc.get("country_code"))
    two_fa = acc.get("2fa_password") or "None"

    success_text = (
        f"🎉 **Login Successful Detected!**\n\n"
        f"📱 **Phone:** `{masked_num}`\n\n"
        f"✅ Aapne successfully Telegram me login kar liya hai!\n"
        f"🔐 **2FA Password:** `{two_fa}`\n\n"
        f"*🔒 Bot ka session Telegram server se TERMINATE kar diya gaya hai. "
        f"Ab 'Active Sessions' me sirf aapka device dikhega.*"
    )

    msg_id = session_data.get("msg_id")
    sent_ok = False
    if msg_id:
        for attempt in range(3):
            try:
                await bot.edit_message(user_id, msg_id, success_text, buttons=[])
                sent_ok = True
                print(f"✅ [LOGIN] Success edited with cleared buttons (attempt {attempt+1})")
                break
            except MessageNotModifiedError:
                sent_ok = True
                break
            except Exception as e:
                err_lower = str(e).lower()
                print(f"⚠️ [LOGIN] Edit attempt {attempt+1} failed: {e}")

                # ✅ FIX: Agar "reply markup is invalid" hai — buttons=[] hata ke edit karo
                if "reply markup" in err_lower or "reply_markup" in err_lower or "markup is invalid" in err_lower:
                    try:
                        await bot.edit_message(user_id, msg_id, success_text)
                        sent_ok = True
                        print(f"✅ [LOGIN] Success edited (kept old buttons) attempt {attempt+1}")
                        break
                    except Exception as e2:
                        print(f"⚠️ [LOGIN] Fallback edit also failed: {e2}")

                await asyncio.sleep(1)

    if not sent_ok:
        try:
            await bot.send_message(user_id, success_text)
            print(f"⚠️ [LOGIN] Last resort: sent new message")
        except Exception as e:
            print(f"🚨 [LOGIN] send also failed: {e}")

async def background_message_checker():
    print("🔄 Background Message Checker Started (15s loop)...")
    while True:
        await asyncio.sleep(15)
        if not login_states:
            continue

        for user_id, session_data in list(login_states.items()):
            user_client = session_data.get("client")
            if not user_client:
                continue

            # Auto-reconnect if disconnected
            if not user_client.is_connected():
                try:
                    await user_client.connect()
                    print(f"🔌 Reconnected for user={user_id}")
                except Exception as e:
                    print(f"❌ Reconnect failed for user={user_id}: {e}")
                    continue

            try:
                # ─── 1. MESSAGES FETCH (OTP / SMS ke liye) ───
                last_msg_id = session_data.get("last_msg_id", 0)
                messages = await user_client.get_messages(777000, limit=5, min_id=last_msg_id)

                if messages:
                    for msg in reversed(messages):
                        msg_ts = msg.date.timestamp() if msg.date else 0
                        if msg_ts < session_data.get("purchase_time", 0):
                            continue
                        session_data["last_msg_id"] = msg.id
                        await process_telegram_message(user_id, msg, session_data)

                # Agar message se login detect ho gaya (SMS aa gaya) → session check skip
                if session_data.get("login_verified") or session_data.get("session_terminated"):
                    continue

                # ─── 2. NAYA: ACTIVE SESSIONS SE LOGIN DETECT ───
                # Sirf tab check karo jab OTP aa chuka ho (wallet deducted, is_logged_in=True)
                if session_data.get("is_logged_in", False):
                    current_sessions = await get_active_sessions_hash(user_client)

                    if current_sessions is not None:
                        # Pehli baar → baseline save karo (kya-kya pehle se logged in hai)
                        if "baseline_sessions" not in session_data:
                            session_data["baseline_sessions"] = current_sessions
                            print(f"📸 [SESSION] Baseline saved for user={user_id} ({len(current_sessions)} devices)")
                        else:
                            baseline = session_data["baseline_sessions"]
                            new_devices = current_sessions - baseline

                            if new_devices:
                                print(f"🎉 [SESSION] NEW LOGIN DETECTED for user={user_id}! New hashes: {new_devices}")

                                # Mark verified + terminated
                                session_data["login_verified"] = True
                                session_data["session_terminated"] = True

                                try:
                                    await safe_logout(user_client)
                                except Exception as e:
                                    print(f"⚠️ [SESSION] early log_out err: {e}")

                                if not session_data.get("purchase_logged"):
                                    session_data["purchase_logged"] = True
                                    try:
                                        tg_user = await bot.get_entity(user_id)
                                        user_mention = tg_user.first_name or "User"
                                        username_str = f"@{tg_user.username}" if tg_user.username else "N/A"
                                    except Exception:
                                        user_mention = "User"
                                        username_str = "N/A"

                                    acc = session_data.get("account", {})
                                    acc_price = acc.get("price", 0.0)
                                    masked_num = mask_phone_number(acc.get("phone", ""), acc.get("country_code"))

                                    try:
                                        me = await bot.get_me()
                                        bot_username = me.username
                                    except Exception:
                                        bot_username = "N/A"

                                    group_msg = (
                                        f"🎉 **New Purchase Successful (Auto-Detected)**\n\n"
                                        f"👤 **User:** {user_mention}\n"
                                        f"📛 **Username:** {username_str}\n"
                                        f"🆔 **User ID:** `{user_id}`\n"
                                        f"📱 **Account Number:** `{masked_num}`\n"
                                        f"💰 **Price:** ₹{acc_price}\n\n"
                                        f"🤖 **Bot:** @{bot_username}"
                                    )
                                    try:
                                        await bot.send_message(OTP_GROUP_ID, group_msg, link_preview=False)
                                    except Exception as ge:
                                        print(f"Purchase log send err: {ge}")

                                # User ko success message
                                await send_login_success_message(user_id, session_data)

                                # Cleanup
                                if user_id in login_states:
                                    del login_states[user_id]
                                continue

                # ─── 3. OTP EXPIRY & STOCK RETURN ───
                if not session_data.get("login_verified", False):
                    otp_time = session_data.get("otp_received_at", 0)
                    if otp_time > 0:
                        elapsed = datetime.now().timestamp() - otp_time
                        warning_count = session_data.get("warning_count", 0)

                        # First Warning (5 min)
                        if elapsed > 300 and warning_count == 0:
                            try:
                                await bot.send_message(
                                    user_id,
                                    "⏰ **OTP Expire Ho Gaya!**\n\n"
                                    "Aapne 5 minute tak login nahi kiya. **Code 5 minute mein expire ho jayega.**\n\n"
                                    "Kripya apne Telegram app me **'Send Code'** par click karein, "
                                    "aur phir yahan **'🔄 Check New Messages'** par click karein.\n\n"
                                    f"📱 *Phone: `{session_data['account']['phone']}`*",
                                    buttons=[
                                        [styled_inline("🔄 Check New Messages", b"fetch_otp", "success")]
                                    ]
                                )
                                session_data["warning_count"] = 1
                            except Exception as e:
                                print(f"First warning err: {e}")

                        # Second Warning (10 min) — Refund
                        elif elapsed > 600 and warning_count == 1:
                            try:
                                acc = session_data.get("account", {})
                                phone = acc.get("phone")
                                price = float(acc.get("price", 0.0))

                                if phone:
                                    accounts_col.update_one(
                                        {"phone": phone},
                                        {"$set": {"sold": False}, "$unset": {"buyer_id": ""}}
                                    )
                                    _busy_phones.discard(phone)

                                if user_id not in OWNER_IDS and price > 0:
                                    users_col.update_one(
                                        {"user_id": int(user_id)},
                                        {"$inc": {"wallet": price, "total_spent": -price}}
                                    )

                                await bot.send_message(
                                    user_id,
                                    f"❌ **Account Stock Me Wapas Chala Gaya!**\n\n"
                                    f"Aapne 10 minute tak login nahi kiya, isliye account wapas stock me add kar diya gaya hai.\n\n"
                                    f"💰 **Refund:** ₹{price:.2f} aapke wallet me wapas aa gaye hain.\n\n"
                                    f"📱 *Phone: `{phone}`*\n\n"
                                    f"👉 *Naya account lene ke liye 'Buy Account' par click karein.*"
                                )

                                user_client_bg = session_data.get("client")
                                if user_client_bg:
                                    await safe_disconnect(user_client_bg)

                                if user_id in login_states:
                                    del login_states[user_id]

                            except Exception as e:
                                print(f"Second warning err: {e}")

            except Exception as e:
                print(f"Auto-checker error for {user_id}: {e}")            


async def typewriter_effect(event, final_text, buttons=None):
    # HTML tags hata kar plain text banate hain
    plain_text = re.sub(r'<[^>]+>', '', final_text)
    msg = None
    
    try:
        # 1. Turant message bhejo buttons ke saath taaki grid icon load ho jaye
        msg = await event.respond(" ▌", buttons=buttons)
    except Exception:
        pass

    if msg:
        typed_text = ""
        chunk_size = 5
        
        for i in range(0, len(plain_text), chunk_size):
            typed_text += plain_text[i:i+chunk_size]
            try:
                await msg.edit(typed_text + " ▌")
            except Exception:
                # Agar message delete ho jaye ya rate limit lag jaye, toh typing rok do
                break 
            await asyncio.sleep(0.1) 
            
        try:
            # Aakhri me pura stylish message edit karo
            await msg.edit(final_text, buttons=buttons, parse_mode='html', link_preview=False)
            return # Edit success, function yahin se wapas jayega
        except Exception:
            # Agar final edit fail ho jaye (message invalid ho gaya), toh purane adhure msg ko delete karo
            try:
                await msg.delete()
            except Exception:
                pass

    # 2. Fallback: Agar edit nahi hua, toh bina error diye naya pura message bhej do
    try:
        await event.respond(final_text, buttons=buttons, parse_mode='html', link_preview=False)
    except Exception:
        pass

@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_cmd(event: events.NewMessage.Event):
    user_id = event.sender_id

    # ✅ Bot off check (non-owner)
    if user_id not in OWNER_IDS:
        is_off, off_msg = get_bot_status()
        if is_off:
            await event.reply(
                f"⚠️ <b>Bot Under Maintenance</b>\n\n"
                f"<blockquote>{off_msg}</blockquote>",
                parse_mode='html'
            )
            return

    sender = await event.get_sender()
    first_name = sender.first_name
    is_premium = bool(getattr(sender, "premium", False))   # ✅ Premium check

    referrer_id = None
    parts = event.text.split()
    if len(parts) > 1:
        try:
            raw_ref = parts[1]
            clean_ref = raw_ref.replace("REF", "").replace("4F886", "").strip()
            possible_ref = int(clean_ref)
            if possible_ref != user_id:
                referrer_id = possible_ref
        except ValueError:
            pass

    get_user(user_id, referrer_id)

    if not await is_user_joined(bot, user_id):
        keyboard = await build_force_join_keyboard()
        await event.reply(
            f"👋 **Welcome {first_name}!**\n\n"
            "⚠️ **Bot access karne ke liye pehle hamare Channels/Groups ko join karein:**",
            buttons=keyboard
        )
        return

    me = await bot.get_me()
    bot_name = me.first_name.upper()
    ref_link = f"https://t.me/{me.username}?start=REF{user_id}4F886"

        # ✅ Typewriter Animation Logic
    custom_config = settings_col.find_one({"type": "custom_start_message"})

    if custom_config and "message" in custom_config:
        stylish_welcome_text = custom_config["message"].format(
            first_name=first_name, bot_name=bot_name, ref_link=ref_link
        )
        stylish_welcome_text = apply_premium_emojis_to_text(stylish_welcome_text, is_premium)
        await typewriter_effect(event, stylish_welcome_text, buttons=main_keyboard())
    else:
        stylish_welcome_text = (
            f"<b>╔══════════════════════╗</b>\n"
            f"<b>║   {pe('💎', is_premium)} WELCOME TO {pe('💎', is_premium)}   ║</b>\n"
            f"<b>║   {bot_name}</b>\n"
            f"<b>╚══════════════════════╝</b>\n\n"
            f"{pe('👋', is_premium)} Hey <b>{first_name}</b>\n\n"
            f"<blockquote>"
            f"{pe('🎁', is_premium)} <b>REFER & EARN</b>\n"
            f"{pe('🟢', is_premium)} <b>Bonus Rate:</b> <code>2% Instant Cashback</code>\n"
            f"{pe('🔵', is_premium)} <b>Kaise:</b> Jab dost wallet deposit karega, 2% bonus seedhe aapko milega\n"
            f"{pe('🟡', is_premium)} <b>Unlimited:</b> Jitna refer karoge, utna zyada profit"
            f"</blockquote>\n\n"
            f"{pe('🔗', is_premium)} <b>Your Referral Link:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{pe('⚡', is_premium)} <b>Choose an option below:</b> {pe('⚡', is_premium)}"
        )
        await typewriter_effect(event, stylish_welcome_text, buttons=main_keyboard())

@bot.on(events.NewMessage(pattern=r"^/setstartmsg"))
async def set_start_msg_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return

    replied = await event.get_reply_message()
    input_text = event.text.split(maxsplit=1)

    new_msg = None
    source_label = ""

    # ✅ Priority 1: Reply-based (kisi message ko reply karke /setstartmsg)
    if replied and replied.text:
        new_msg = replied.text
        source_label = "📎 Quoted message (reply)"
    # ✅ Priority 2: Direct command text (/setstartmsg <text>)
    elif len(input_text) >= 2:
        new_msg = input_text[1].strip()
        source_label = "✏️ Direct command text"
    else:
        # Usage guide
        await event.reply(
            "⚠️ <b>Format:</b>\n\n"
            "1️⃣ <b>Direct:</b>\n"
            "<code>/setstartmsg Aapka message yahan...</code>\n\n"
            "2️⃣ <b>Reply method (recommended):</b>\n"
            "Kisi bhi message ko <b>reply</b> karke <code>/setstartmsg</code> bhejein\n"
            "→ Woh message hi custom start message ban jayega\n\n"
            "🔤 <b>Variables:</b>\n"
            "• <code>{first_name}</code> — User ka naam\n"
            "• <code>{bot_name}</code> — Bot ka naam\n"
            "• <code>{ref_link}</code> — Referral link",
            parse_mode='html',
            reply_to=event.message
        )
        return

    # Save to MongoDB
    settings_col.update_one(
        {"type": "custom_start_message"},
        {"$set": {"message": new_msg}},
        upsert=True
    )

    # ✅ QUOTED confirmation (Telegram me original command ke reply me dikhega)
    await event.reply(
        f"✅ <b>Custom Start Message Successfully Set!</b>\n\n"
        f"📌 <b>Source:</b> {source_label}\n\n"
        f"<blockquote>{new_msg}</blockquote>\n\n"
        f"💡 <i>Ab users ko /start karne par yahi message dikhega.</i>\n"
        f"🔄 <i>Reset karne ke liye: <code>/resetstartmsg</code></i>",
        parse_mode='html',
        reply_to=event.message   # 👈 QUOTE (jaisa /botoff me hai)
    )

@bot.on(events.NewMessage(pattern=r"^/resetstartmsg"))
async def reset_start_msg_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    settings_col.delete_one({"type": "custom_start_message"})
    await event.reply("🔄 **Start Message Reset Ho Kar Default Wala Ho Gaya Hai!**")

@bot.on(events.NewMessage(pattern=r"^/testai$"))
async def test_ai_cmd(event):
    if event.sender_id not in OWNER_IDS:
        return
    reply = await event.get_reply_message()
    if not reply or not reply.photo:
        await event.reply("❌ Kisi payment screenshot ko reply karke /testai bhejo")
        return
    await event.reply("⏳ Analyzing...")
    buf = BytesIO()
    await bot.download_media(reply.photo, file=buf)
    raw_bytes = buf.getvalue()
    mime = _detect_image_mime(raw_bytes)
    data = await extract_ss_details(raw_bytes)
    await event.reply(
        f"📊 **DEBUG INFO**\n\n"
        f"📦 Bytes: `{len(raw_bytes)}`\n"
        f"🖼️ MIME: `{mime}`\n"
        f"🤖 AI Status: `{data.get('_ai_status', '?')}`\n\n"
        f"```json\n{json.dumps(data, indent=2)}\n```",
        parse_mode='markdown'
    )
    
@bot.on(events.NewMessage(pattern=r"^/add$"))
async def add_single_acc_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    keyboard = [
        [styled_inline("🔥 Spam Account", b"add_type_spam", "danger"),
         styled_inline("🌱 Fresh Account", b"add_type_fresh", "success")],
        [styled_inline("🆕 New Registered", b"add_type_registered", "primary")]
    ]
    await event.reply("🌍 **[Owner]** Select Account Type:", buttons=keyboard)

@bot.on(events.ChatAction())
async def on_join_request(event):
    """Telethon join request tracking via ChatAction / Raw events"""
    try:
        if event.user_joined or event.user_added:
            user_id = event.user_id
            chat_id = event.chat_id
            db["join_requests"].update_one(
                {"user_id": user_id, "chat_id": chat_id},
                {"$set": {"user_id": user_id, "chat_id": chat_id, "timestamp": datetime.now()}},
                upsert=True
            )
    except Exception:
        pass

@bot.on(events.NewMessage(pattern=r"^/addsupport"))
async def add_support_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS: return
    args = event.text.split(maxsplit=1)
    if len(args) < 2:
        await event.reply("⚠️ **Format:** `/addsupport @username`")
        return
    
    username = args[1].strip().replace("@", "").replace("https://t.me/", "")
    if not username:
        await event.reply("❌ Invalid username!")
        return
        
    current_list = get_support_usernames()
    if username in current_list:
        await event.reply(f"⚠️ `@{username}` pehle se hi support list me hai!")
        return
        
    current_list.append(username)
    settings_col.update_one({"type": "support_settings"}, {"$set": {"usernames": current_list}}, upsert=True)
    
    await event.reply(f"✅ `@{username}` Support List me add ho gaya!\n\n📋 **Total Support IDs:** `{len(current_list)}`\n\n💡 Check karne ke liye `/listsupport` bhejein.")

@bot.on(events.NewMessage(pattern=r"^/removesupport"))
async def remove_support_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS: return
    args = event.text.split(maxsplit=1)
    if len(args) < 2:
        await event.reply("⚠️ **Format:** `/removesupport @username`")
        return
        
    username = args[1].strip().replace("@", "").replace("https://t.me/", "")
    current_list = get_support_usernames()
    
    if username not in current_list:
        await event.reply(f"❌ `@{username}` support list me nahi hai!")
        return
        
    current_list.remove(username)
    settings_col.update_one({"type": "support_settings"}, {"$set": {"usernames": current_list}}, upsert=True)
    
    await event.reply(f"🗑️ `@{username}` Support List se hata diya gaya!\n\n📋 **Baaki Support IDs:** `{len(current_list)}`")

@bot.on(events.NewMessage(pattern=r"^/listsupport"))
async def list_support_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS: return
    current_list = get_support_usernames()
    if not current_list:
        await event.reply("❌ Support list khaali hai!")
        return
        
    text = "👑 **CURRENT SUPPORT USERNAMES:**\n\n"
    for idx, u in enumerate(current_list, 1):
        text += f"{idx}. `@{u}`\n"
    await event.reply(text)
    
# ================= CREDIT & DEBIT =================
@bot.on(events.NewMessage(pattern=r"^/credit"))
async def credit_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    args = event.text.split(maxsplit=2)
    if len(args) < 3:
        await event.reply(
            "⚠️ **Format:**\n`/credit <user_id_or_username> <amount>`\n\n"
            "**Example:**\n`/credit 123456789 500`\n`/credit @username 200`"
        )
        return
    target_identifier = args[1].strip()
    try:
        amount = float(args[2].strip())
        if amount <= 0:
            await event.reply("❌ Amount 0 se zyada honi chahiye!")
            return
    except ValueError:
        await event.reply("❌ Invalid amount! Kripya sahi number dalein.")
        return

    query = {}
    if target_identifier.startswith("@"):
        user_doc = users_col.find_one({"username": target_identifier.replace("@", "").lower()})
        try:
            resolved_user = await bot.get_entity(target_identifier)
            query = {"user_id": resolved_user.id}
            target_user_id = resolved_user.id
            target_name = resolved_user.first_name
        except Exception:
            await event.reply("❌ Yeh username Telegram par nahi mila ya user ne bot start nahi kiya hai.")
            return
    else:
        try:
            target_user_id = int(target_identifier)
            query = {"user_id": target_user_id}
            resolved_user = await bot.get_entity(target_user_id)
            target_name = resolved_user.first_name
        except ValueError:
            await event.reply("❌ Invalid User ID!")
            return
        except Exception:
            target_name = "User"

    get_user(target_user_id)
    users_col.update_one(query, {"$inc": {"wallet": amount}})
    updated_user = users_col.find_one(query)
    new_balance = updated_user.get("wallet", 0.0)

    await event.reply(
        f"✅ **Successfully Credited!**\n\n"
        f"👤 **User:** {target_name} (`{target_user_id}`)\n"
        f"💰 **Added Amount:** ₹{amount}\n"
        f"💳 **New Wallet Balance:** ₹{new_balance:.2f}"
    )
    try:
        await bot.send_message(target_user_id,
            f"🎁 **Your Wallet Has Been Credited!**\n\n"
            f"💰 **Amount Added:** ₹{amount}\n"
            f"💳 **Current Wallet Balance:** ₹{new_balance:.2f}\n"
            f"👑 **Credited By:** Owner")
    except Exception:
        pass


@bot.on(events.NewMessage(pattern=r"^/debit"))
async def debit_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    args = event.text.split(maxsplit=2)
    if len(args) < 3:
        await event.reply(
            "⚠️ **Format:**\n`/debit <user_id_or_username> <amount>`\n\n"
            "**Example:**\n`/debit 123456789 200`\n`/debit @username 100`"
        )
        return
    target_identifier = args[1].strip()
    try:
        amount = float(args[2].strip())
        if amount <= 0:
            await event.reply("❌ Amount 0 se zyada honi chahiye!")
            return
    except ValueError:
        await event.reply("❌ Invalid amount! Kripya sahi number dalein.")
        return

    query = {}
    if target_identifier.startswith("@"):
        try:
            resolved_user = await bot.get_entity(target_identifier)
            query = {"user_id": resolved_user.id}
            target_user_id = resolved_user.id
            target_name = resolved_user.first_name
        except Exception:
            await event.reply("❌ Yeh username Telegram par nahi mila ya user ne bot start nahi kiya hai.")
            return
    else:
        try:
            target_user_id = int(target_identifier)
            query = {"user_id": target_user_id}
            resolved_user = await bot.get_entity(target_user_id)
            target_name = resolved_user.first_name
        except ValueError:
            await event.reply("❌ Invalid User ID!")
            return
        except Exception:
            target_name = "User"

    get_user(target_user_id)
    target_user_data = users_col.find_one(query)
    current_balance = target_user_data.get("wallet", 0.0)
    new_balance = current_balance - amount
    users_col.update_one(query, {"$set": {"wallet": new_balance}})

    await event.reply(
        f"✅ **Successfully Debited!**\n\n"
        f"👤 **User:** {target_name} (`{target_user_id}`)\n"
        f"💸 **Deducted Amount:** ₹{amount}\n"
        f"💳 **New Wallet Balance:** ₹{new_balance:.2f}"
    )
    try:
        await bot.send_message(target_user_id,
            f"⚠️ **Your Wallet Has Been Debited!**\n\n"
            f"💸 **Deducted Amount:** ₹{amount}\n"
            f"💳 **Current Wallet Balance:** ₹{new_balance:.2f}\n"
            f"👑 **Debited By:** Owner")
    except Exception:
        pass


# ================= TRACK CHATS =================
@bot.on(events.ChatAction())
async def on_chat_member_update(event):
    try:
        me = await bot.get_me()
        if event.user_id == me.id and (event.user_added or event.user_joined):
            chat = await event.get_chat()
            chat_title = getattr(chat, "title", str(event.chat_id))
            chats_col.update_one(
                {"chat_id": event.chat_id},
                {"$set": {"chat_id": event.chat_id, "title": chat_title, "type": type(chat).__name__}},
                upsert=True
            )
    except Exception as e:
        print(f"Error tracking chat member update: {e}")


@bot.on(events.NewMessage())
async def track_chats(event: events.NewMessage.Event):
    if event.is_private:
        return
    try:
        chat = await event.get_chat()
        chat_title = getattr(chat, "title", str(event.chat_id))
        chats_col.update_one(
            {"chat_id": event.chat_id},
            {"$set": {"chat_id": event.chat_id, "title": chat_title, "type": type(chat).__name__}},
            upsert=True
        )
    except Exception:
        pass


# ================= BULK ADD =================
@bot.on(events.NewMessage(pattern=r"^/addbulk"))
async def add_bulk_cmd(event: events.NewMessage.Event):
    user_id = event.sender_id
    if user_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2:
        await event.reply(
            "⚠️ **Format:**\n\n"
            "`/addbulk`\n"
            "`Country|Country code|Price|Number with code|password/None|spam/fresh/registered`"
        )

        return
    lines = input_text[1].strip().split("\n")
    queue = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            c_name = parts[0]; c_code = parts[1]
            try:
                price = float(parts[2])
            except ValueError:
                continue
            phone = parts[3]
            password = parts[4] if len(parts) > 4 and parts[4].lower() != "none" else None
            acc_type = parts[5].lower() if len(parts) > 5 and parts[5].lower() in ["spam", "fresh", "registered"] else "fresh"
            queue.append({"c_name": c_name, "c_code": c_code, "price": price,
                          "phone": phone, "password": password, "type": acc_type})
    if not queue:
        await event.reply("❌ Valid numbers nahi mile format me!")
        return
    user_states[user_id] = {"bulk_queue": queue, "completed": 0, "total": len(queue)}
    await process_next_bulk_account(bot, user_id, event)


# ================= OWNER LOGOUT =================
@bot.on(events.NewMessage(pattern=r"^/logout"))
async def logout_acc_cmd(event: events.NewMessage.Event):
    user_id = event.sender_id
    if user_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2:
        await event.reply("⚠️ **Format:** `/logout <phone_number>`\nExample: `/logout +919876543210`")
        return
    target_phone = input_text[1].strip()
    acc = accounts_col.find_one({"phone": target_phone})
    if not acc:
        await event.reply(f"❌ Account `{target_phone}` database me nahi mila!")
        return
    status_msg = await event.reply(f"⏳ Logging out account `{target_phone}`...")
    session_name = f"sessions/{target_phone.replace('+', '').replace(' ', '')}"
    temp_client = TelegramClient(session_name, API_ID, API_HASH)
    try:
        await temp_client.connect()
        try:
            await temp_client.log_out()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        await cleanup_session(session_name, temp_client)
        accounts_col.update_one({"phone": target_phone}, {"$set": {"sold": True, "status": "logged_out"}})
    await status_msg.edit(f"✅ **Account `{target_phone}` logged out and removed from stock!**")


# ================= FORCE JOIN =================
@bot.on(events.NewMessage(pattern=r"^/addfjoin"))
async def add_fjoin_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2 or "|" not in input_text[1]:
        await event.reply(
            "⚠️ **Format:**\n`/addfjoin @username_or_chatid | https://t.me/invite_link | Display Name`\n\n"
            "**Example:**\n`/addfjoin @itz_darkX | https://t.me/itz_darkX | 📢 Backup Channel`"
        )
        return
    parts = [p.strip() for p in input_text[1].split("|")]
    username = parts[0]; link = parts[1]
    title = parts[2] if len(parts) > 2 else "📢 Join Channel"
    config = settings_col.find_one({"type": "force_join"})
    channels = config.get("channels", []) if config else []
    if any(c["username"] == username for c in channels):
        await event.reply("⚠️ Yeh channel/group pehle se Force Join list me hai!")
        return
    channels.append({"username": username, "link": link, "title": title})
    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": channels}}, upsert=True)
    await event.reply(
        f"✅ **Force Join Added Successfully!**\n\n"
        f"📛 **Name/Title:** {title}\n"
        f"📢 **Target:** `{username}`\n"
        f"🔗 **Link:** {link}"
    )


@bot.on(events.NewMessage(pattern=r"^/fjoinlist"))
async def list_fjoin_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    channels = await get_force_join_channels()
    if not channels:
        await event.reply("ℹ️ Abhi koi bhi Force Join channel set nahi hai.")
        return
    text = "📢 **CURRENT FORCE JOIN CHANNELS/GROUPS:**\n\n"
    for idx, ch in enumerate(channels, 1):
        text += f"{idx}. **Target:** `{ch['username']}`\n   🔗 **Link:** {ch['link']}\n\n"
    await event.reply(text)


@bot.on(events.NewMessage(pattern=r"^/clearfjoin"))
async def clear_fjoin_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2:
        await event.reply("⚠️ **Format:**\n`/clearfjoin @username_or_link`\n\n**Example:**\n`/clearfjoin @itz_darkX`")
        return
    target = input_text[1].strip()
    config = settings_col.find_one({"type": "force_join"})
    if not config or "channels" not in config:
        await event.reply("ℹ️ Koi bhi Force Join channel set nahi hai.")
        return
    channels = config.get("channels", [])
    new_channels = [c for c in channels if c["username"] != target and c["link"] != target]
    if len(new_channels) == len(channels):
        await event.reply(f"❌ '{target}' se milta hua koi bhi channel/group list me nahi mila!")
        return
    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": new_channels}}, upsert=True)
    await event.reply(f"✅ **Success!** `{target}` ko Force Join list se hata diya gaya hai.")


@bot.on(events.NewMessage(pattern=r"^/disablefjoin"))
async def disable_fjoin_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    settings_col.update_one({"type": "force_join_status"}, {"$set": {"status": "off"}}, upsert=True)
    await event.reply("🔴 **Force Join System Successfully OFF Kar Diya Gaya Hai!**\n\nAb koi bhi user bina join kiye bot use kar payega.")


@bot.on(events.NewMessage(pattern=r"^/enablefjoin"))
async def enable_fjoin_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    settings_col.update_one({"type": "force_join_status"}, {"$set": {"status": "on"}}, upsert=True)
    await event.reply("🟢 **Force Join System Successfully ON Kar Diya Gaya Hai!**\n\nAb users ko bot use karne ke liye channels join karne honge.")


@bot.on(events.NewMessage(pattern=r"^/resetdb"))
async def reset_database_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    try:
        users_col.delete_many({})
        accounts_col.delete_many({})
        chats_col.delete_many({})
        user_states.clear()
        login_states.clear()
        await event.reply("🧹 **MongoDB Successfully Reset!**\n\nSaare Users, Accounts Stock, aur Chat logs delete ho chuke hain.")
    except Exception as e:
        await event.reply(f"❌ Reset karne me error aaya: `{e}`")


async def process_next_bulk_account(client, user_id, event_or_msg):
    state = user_states.get(user_id)
    if not state or not state.get("bulk_queue"):
        completed = state.get("completed", 0) if state else 0
        total = state.get("total", 0) if state else 0
        text = f"🎉 **Bulk Process Completed!**\nTotal Added: `{completed}/{total}`"
        await event_or_msg.reply(text)
        user_states.pop(user_id, None)
        return

    current_acc = state["bulk_queue"].pop(0)
    phone = current_acc["phone"]
    session_folder = "sessions"
    os.makedirs(session_folder, exist_ok=True)
    session_name = f"{session_folder}/{phone.replace('+', '').replace(' ', '')}"

    old_client = state.get("temp_client")
    if old_client:
        await cleanup_session(session_name, old_client)
    else:
        await cleanup_session(session_name)

    acc_client = TelegramClient(session_name, API_ID, API_HASH)
    existing = accounts_col.find_one({"phone": phone, "status": {"$ne": "logged_out"}})
    if existing:
        await event_or_msg.reply(
            f"⚠️ `{phone}` already DB me hai (Type: {existing.get('type','fresh')}). "
            f"Pehle `/logout {phone}` karo."
        )
        state["state"] = None
        await cleanup_session(session_name, acc_client)
        await process_next_bulk_account(client, user_id, event_or_msg)
        return

    try:
        await acc_client.connect()
        sent_code = await acc_client.send_code_request(phone)
        state["state"] = "awaiting_bulk_otp"
        state["current_acc"] = current_acc
        state["temp_client"] = acc_client
        state["temp_code_hash"] = sent_code.phone_code_hash

        status_text = (
            f"🔄 **Processing Account [{state['completed'] + 1}/{state['total']}]**\n\n"
            f"📱 **Phone:** `{phone}`\n"
            f"🌍 **Country:** {current_acc['c_name']} ({current_acc['c_code']})\n"
            f"💰 **Price:** ₹{current_acc['price']}\n\n"
            f"📩 **Enter OTP for `{phone}`:**"
        )
        await event_or_msg.reply(status_text)
    except Exception as e:
        await cleanup_session(session_name, acc_client)
        await event_or_msg.reply(f"❌ Error for `{phone}`: {str(e)}\nSkipping to next...")
        await process_next_bulk_account(client, user_id, event_or_msg)


# ================= STOCK CHECK =================
@bot.on(events.NewMessage(pattern=r"^/stock"))
async def stock_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    pipeline = [
        {"$match": {"sold": False}},
        {"$group": {
            "_id": {"country": "$country_name", "code": "$country_code", "type": {"$ifNull": ["$type", "fresh"]}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id.country": 1}}
    ]
    stock_data = list(accounts_col.aggregate(pipeline))
    total_unsold = accounts_col.count_documents({"sold": False})
    total_sold = accounts_col.count_documents({"sold": True})
    if not stock_data:
        await event.reply("📊 **STOCK STATUS**\n\n❌ Database me abhi koi unsold account nahi hai!")
        return
    formatted_stock = {}
    for item in stock_data:
        c_name = item["_id"]["country"]; c_code = item["_id"]["code"]
        acc_type = str(item["_id"]["type"]).upper(); count = item["count"]
        key = f"{c_name} ({c_code})"
        if key not in formatted_stock:
            formatted_stock[key] = {"FRESH": 0, "SPAM": 0, "REGISTERED": 0}
        formatted_stock[key][acc_type] = count
    text = "📊 **OWNER STOCK SUMMARY**\n\n"
    text += f"🟢 **Total Unsold Stock:** `{total_unsold}`\n"
    text += f"🔴 **Total Sold Out:** `{total_sold}`\n"
    text += "────────────────────\n\n"
    for country_info, types in formatted_stock.items():
        text += f"🌍 **{country_info}**\n"
        text += f"   ├── 🌱 Fresh: `{types.get('FRESH', 0)}`\n"
        text += f"   ├── 🔥 Spam: `{types.get('SPAM', 0)}`\n"
        text += f"   └── 🆕 Registered: `{types.get('REGISTERED', 0)}`\n\n"
    await event.reply(text)


# ================= BROADCAST =================
@bot.on(events.NewMessage(pattern=r"^/(broadcast|gcast)"))
async def broadcast_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    reply_msg = await event.get_reply_message()
    if not reply_msg and len(event.text.split(maxsplit=1)) < 2:
        await event.reply(
            "⚠️ **Broadcast Command Usage:**\n\n"
            "1. Kisi bhi message (Photo, Video, Text, Sticker) ko **reply** karke likhein: `/broadcast`\n"
            "2. Direct message ke liye likhein: `/broadcast Yahan apna text likhein`"
        )
        return
    status_msg = await event.reply("📢 **Broadcast Process Started...**")
    all_users = list(users_col.find({}, {"user_id": 1}))
    all_chats = list(chats_col.find({}, {"chat_id": 1}))
    targets = list(set([u["user_id"] for u in all_users if "user_id" in u] +
                       [c["chat_id"] for c in all_chats if "chat_id" in c]))
    success = 0; failed = 0
    for chat_id in targets:
        
        try:
            if reply_msg:
                # Agar reply me sirf text hai → quote ke saath bhejo
                if reply_msg.text and not reply_msg.media:
                    quoted_text = f"<blockquote>{reply_msg.text}</blockquote>"
                    await bot.send_message(chat_id, quoted_text, parse_mode='html', link_preview=True)
                else:
                    # Media reply (photo/video/sticker) → as-is forward karo
                    await reply_msg.forward_to(chat_id)
            else:
                # Direct text → quote ke saath bhejo
                broadcast_text = event.text.split(maxsplit=1)[1]
                quoted_text = f"<blockquote>{broadcast_text}</blockquote>"
                await bot.send_message(chat_id, quoted_text, parse_mode='html', link_preview=True)
            success += 1
            await asyncio.sleep(0.05)
        
        except (UserDeactivatedBanError, UserIsBlockedError, PeerIdInvalidError):
            users_col.delete_one({"user_id": chat_id})
            chats_col.delete_one({"chat_id": chat_id})
            failed += 1
        except Exception:
            failed += 1
    
    await status_msg.edit(
        f"✅ **Broadcast Completed Successfully!**\n\n"
        f"🟢 **Delivered:** `{success}`\n"
        f"🔴 **Failed/Blocked:** `{failed}`\n"
        f"📊 **Total Attempted:** `{len(targets)}`"
    )

async def log_auto_verify_failure(user_id, sender, amount, reason, upi_txn_id="N/A"):
    """Auto-verify fail hone pe OTP_GROUP me notification bhejo."""
    try:
        username_str = f"@{sender.username}" if sender.username else "N/A"
        now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        attempt_date = now.strftime("%d-%m-%Y")
        attempt_time = now.strftime("%I:%M:%S %p")
        failed_msg = (
            f"⚠️ **Auto-Verify Failed!**\n\n"
            f"👤 **User:** {sender.first_name or 'User'}\n"
            f"📛 **Username:** {username_str}\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Amount:** ₹{amount}\n"
            f"❌ **Reason:** {reason}\n"
            f"🔢 **Txn ID:** `{upi_txn_id or 'N/A'}`\n"
            f"🕐 **Attempted At:** {attempt_date} {attempt_time}\n\n"
            f"🤖 **Bot :** @DarktgOtp_bot\n"
        )
        await bot.send_message(OTP_GROUP_ID, failed_msg)
    except Exception as e:
        print(f"Auto-verify fail log err: {e}")

# ================= CALLBACK HANDLER =================
@bot.on(events.CallbackQuery())
async def callback_handler(event: events.CallbackQuery.Event):
    user_id = event.sender_id
    if user_id not in OWNER_IDS:
        is_off, off_msg = get_bot_status()
        if is_off:
            await event.answer(f"⚠️ {off_msg}", alert=True)
            return

    try:
        data = event.data.decode()
    except Exception:
        return

    # Skip force-join check for the verify callback
    if data != "check_force_join":
        if not await is_user_joined(bot, user_id):
            keyboard = await build_force_join_keyboard()
            try:
                await event.edit(
                    "⚠️ **Aapne hamare required Channels/Groups ko leave kar diya hai!**\n\n"
                    "Bot use karne ke liye pehle unhe join karein:",
                    buttons=keyboard
                )
            except Exception:
                await event.respond("⚠️ **Aapne channels/groups leave kar diye hain!** Pehle join karein:", buttons=keyboard)
            await event.answer("❌ Pehle channels/groups join karein!", alert=True)
            return

    state = get_state(user_id)
    is_owner = (user_id in OWNER_IDS)
    user = get_user(user_id)
    sender = await event.get_sender()

    if data == "check_force_join":
        if await is_user_joined(bot, user_id):
            await event.answer("✅ Verification successful! Welcome to the bot.", alert=True)
            me = await bot.get_me()
            bot_name = me.first_name.upper()
            ref_link = f"https://t.me/{me.username}?start=REF{user_id}4F886"

            is_premium_cb = bool(getattr(sender, "premium", False))
            custom_config = settings_col.find_one({"type": "custom_start_message"})
            
            if custom_config and "message" in custom_config:
                stylish_welcome_text = custom_config["message"].format(
                    first_name=sender.first_name,
                    bot_name=bot_name,
                    ref_link=ref_link
                )
                stylish_welcome_text = apply_premium_emojis_to_text(stylish_welcome_text, is_premium_cb)
            else:
                stylish_welcome_text = (
                    f"<b>╔══════════════════════╗</b>\n"
                    f"<b>║   {pe('💎', is_premium_cb)} WELCOME TO {pe('💎', is_premium_cb)}   ║</b>\n"
                    f"<b>║   {bot_name}</b>\n"
                    f"<b>╚══════════════════════╝</b>\n\n"
                    f"{pe('👋', is_premium_cb)} Hey <b>{sender.first_name}</b>, welcome aboard!\n\n"
                    f"<blockquote>"
                    f"{pe('🎁', is_premium_cb)} <b>REFER & EARN</b>\n"
                    f"{pe('🟢', is_premium_cb)} <b>Bonus Rate:</b> <code>2% Instant Cashback</code>\n"
                    f"{pe('🔵', is_premium_cb)} <b>Kaise:</b> Jab dost wallet deposit karega, 2% bonus seedhe aapko milega\n"
                    f"{pe('🟡', is_premium_cb)} <b>Unlimited:</b> Jitna refer karoge, utna zyada profit"
                    f"</blockquote>\n\n"
                    f"{pe('🔗', is_premium_cb)} <b>Your Referral Link:</b>\n"
                    f"<code>{ref_link}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{pe('⚡', is_premium_cb)} <b>Choose an option below:</b> {pe('⚡', is_premium_cb)}"
                )

            # Purana Force Join message delete karo
            try:
                await event.delete()
            except Exception:
                pass

            # 👇 Seedha Typewriter Effect start karo
            await typewriter_effect(event, stylish_welcome_text, buttons=main_keyboard())
            return
            
        else:
            # Agar join nahi kiya hai toh...
            keyboard = await build_force_join_keyboard()
            await event.answer("❌ Aapne abhi tak saare channels/groups join nahi kiye hain!", alert=True)
            try:
                await event.edit(
                    f"👋 <b>Welcome, {sender.first_name}!</b>\n\n"
                    "⚠️ <b>Bot access karne ke liye pehle hamare Channels/Groups ko join karein:</b>",
                    buttons=keyboard,
                    parse_mode='html'
                )
            except Exception:
                pass
            return

    elif data == "profile":
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=REF{user_id}4F886"
        username_str = f"@{sender.username}" if sender.username else "N/A"
        profile_text = (
            f"👤 **USER PROFILE**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📛 **Username:** {username_str}\n"
            f"💰 **Wallet Balance:** ₹{user.get('wallet', 0.0):.2f}\n"
            f"📅 **Start Date:** `{user.get('start_date', 'N/A')}`\n\n"
            f"🔗 **Your Referral Link:**\n`{ref_link}`\n\n"
            f"🎁 *Har referral deposit ka 2% aapke wallet me auto-credit hoga!*"
        )
        await event.edit(profile_text, buttons=[[styled_inline("🔙 Back", b"back_main", "primary")]])

    elif data == "stats":
        bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})
        stats_text = (
            f"📊 **YOUR ACCOUNT STATS**\n\n"
            f"🛒 **Total Accounts Bought:** {bought_count}\n"
            f"💸 **Total Amount Spent:** ₹{user.get('total_spent', 0.0):.2f}\n"
            f"💵 **Total Amount Deposited:** ₹{user.get('total_deposited', 0.0):.2f}\n"
            f"🎁 **Referral Earnings (2%):** ₹{user.get('referral_profit', 0.0):.2f}"
        )
        stats_buttons = [
            [styled_inline("👥 Referral Log", b"ref_log", "primary"),
             styled_inline("🛒 Purchase Log", b"purchase_log", "success")],
            [styled_inline("🔙 Back", b"back_main", "primary")]
        ]
        await event.edit(stats_text, buttons=stats_buttons)

    elif data == "ref_log":
        referred_users = list(users_col.find({"referred_by": user_id}))
        text = f"👥 **REFERRAL LOG** (Total: {len(referred_users)})\n\n"
        if referred_users:
            for idx, ref in enumerate(referred_users, 1):
                text += f"{idx}. User ID: `{ref['user_id']}` | Joined: `{ref.get('start_date', 'N/A')}`\n"
        else:
            text += "Aapne abhi tak kisi ko refer nahi kiya hai."
        await event.edit(text, buttons=[[styled_inline("🔙 Back to Stats", b"stats", "primary")]])

    elif data == "purchase_log":
        purchased_accs = list(accounts_col.find({"buyer_id": user_id, "sold": True}))
        text = f"🛒 **PURCHASE LOG** (Total: {len(purchased_accs)})\n\n"
        if purchased_accs:
            for idx, acc in enumerate(purchased_accs, 1):
                masked_num = mask_phone_number(acc['phone'], acc.get('country_code'))
                text += f"{idx}. Phone: `{masked_num}` | Price: ₹{acc['price']}\n"
        else:
            text += "Aapne abhi tak koi account buy nahi kiya hai."
        await event.edit(text, buttons=[[styled_inline("🔙 Back to Stats", b"stats", "primary")]])

    
    elif data == "support":
        usernames = get_support_usernames()
        
        # Agar list khaali hai toh sirf "Back" button dikhao
        if not usernames:
            support_keyboard = [[styled_inline("🔙 Back", b"back_main", "primary")]]
            await event.edit(
                "🛠️ **Customer Support**\n\n❌ **Abhi koi Support available nahi hai.**\nKripya thodi der baad try karein.", 
                buttons=support_keyboard
            )
            return
            
        support_keyboard = []
        for u in usernames:
            support_keyboard.append([styled_url(f"💬 Contact @{u}", f"https://t.me/{u}", "danger")])
            
        support_keyboard.append([styled_inline("🔙 Back", b"back_main", "primary")])
        
        await event.edit(
            "🛠️ **Customer Support**\n\nFor any issues, contact Owner by clicking the button below:", 
            buttons=support_keyboard
        )

    elif data == "back_main":
        state["state"] = None
        await event.edit("Select an option from below:", buttons=main_keyboard())

    elif data == "add_acc":
        if not is_owner:
            await event.answer("❌ Only the Owner can add accounts!", alert=True)
            return
        keyboard = [
            [styled_inline("🔥 Spam Account", b"add_type_spam", "danger"),
             styled_inline("🌱 Fresh Account", b"add_type_fresh", "success")],
            [styled_inline("🆕 Registered Account", b"add_type_registered", "primary")]
        ]
        await event.respond("🌍 **[Owner]** Select Account Type:", buttons=keyboard)

    elif data in ["add_type_spam", "add_type_fresh", "add_type_registered"]:
        if not is_owner:
            return
        if "spam" in data: acc_type = "spam"
        elif "registered" in data: acc_type = "registered"
        else: acc_type = "fresh"
        state["temp_acc_type"] = acc_type
        state["state"] = "awaiting_country_name"
        await event.edit(f"🌍 **[Owner - {acc_type.upper()}]** Enter Country Name (e.g. India, USA):")

    elif data == "buy_acc":
        # User ne Back dabaya — account SABKE liye free karo
        if user_id in login_states:
            sd = login_states[user_id]
            phone = sd.get("account", {}).get("phone")

            # OTP aane se PEHLE hi Back dabaya hai (is_logged_in == False)
            if not sd.get("is_logged_in", False):
                # DB me bhi confirm karo ki sold=False hai
                if phone:
                    accounts_col.update_one(
                        {"phone": phone},
                        {"$set": {"sold": False}, "$unset": {"buyer_id": ""}}
                    )
                # ✅ SAFAI: login_states + _busy_phones + client — sab clear
                await cleanup_user_login(user_id)
            else:
                # OTP aa chuka tha — sirf busy se hataao, session mat todo
                if phone:
                    _busy_phones.discard(phone)

        keyboard = [
            [styled_inline("🔥 Spam Accounts", b"buy_type_spam", "danger"),
             styled_inline("🌱 Fresh Accounts", b"buy_type_fresh", "success")],
            [styled_inline("🆕 New Registered", b"buy_type_registered", "primary")]
        ]
        await event.edit("🛒 **Select Account Category:**", buttons=keyboard)

    elif data in ["buy_type_spam", "buy_type_fresh", "buy_type_registered"]:
        if "spam" in data: selected_type = "spam"
        elif "registered" in data: selected_type = "registered"
        else: selected_type = "fresh"
        countries = get_available_countries(selected_type)
        if not countries:
            await event.answer(f"❌ No {selected_type.upper()} accounts in stock right now!", alert=True)
            return
        btn_list = []
        for c in countries:
            c_name, c_count, c_code = c["_id"], c["count"], c["code"]
            flag = get_flag_emoji(c_name)
            btn_list.append([styled_inline(
                f"{flag} {c_name}",
                f"select_country_{selected_type}_{c_name}".encode(),
                "primary"
            )])
        btn_list.append([styled_inline("🔙 Back", b"buy_acc", "primary")])
        await event.edit(f"🛒 **Select Country ({selected_type.upper()}):**", buttons=btn_list)

    elif data.startswith("select_country_"):
        parts = data.split("_")
        selected_type = parts[2]
        selected_country = "_".join(parts[3:])
        type_filter = [
            {"type": selected_type},
            {"type": {"$exists": False}} if selected_type == "fresh" else {"_id": None}
        ]
        available_accs = list(accounts_col.find({
            "country_name": selected_country, "sold": False, "$or": type_filter
        }))
        if not available_accs:
            await event.answer("❌ No accounts left in this category!", alert=True)
            return
        btn_list = []
        for acc in available_accs:
            phone = acc["phone"]; price = acc["price"]
            masked_num = mask_phone_number(phone, acc.get('country_code'))
            btn_list.append([styled_inline(
                f"📱 {masked_num} - ₹{price}",
                f"buy_acc_{phone}".encode(),
                "success"
            )])
        btn_list.append([styled_inline("🔙 Back", f"buy_type_{selected_type}".encode(), "primary")])
        await event.edit(f"🛒 **Available {selected_type.upper()} Accounts for {selected_country}:**", buttons=btn_list)

    elif data == "deposit_home":
        await event.answer("💳 Deposit Home")
        sender = await event.get_sender()
        user_mention_html = f'<a href="tg://user?id={user_id}">{sender.first_name or "User"}</a>'

        # Fresh balance fetch (kyunki history ke baad update hui ho sakti hai)
        fresh_user = get_user(user_id)

        balance_text = (
            f"<b>💳 DEPOSIT MONEY</b>\n\n"
            f"<blockquote>"
            f"👤 <b>User:</b> {user_mention_html}\n"
            f"💰 <b>Current Balance:</b> <code>₹{fresh_user.get('wallet', 0.0):.2f}</code>\n"
            f"📊 <b>Total Deposited:</b> <code>₹{fresh_user.get('total_deposited', 0.0):.2f}</code>"
            f"</blockquote>\n\n"
            f"👇 <b>Choose an option:</b>"
        )
        keyboard = [
            [styled_inline("💳 Deposit via UPI", b"deposit_upi", "success"),
             styled_inline("📜 History", b"deposit_history", "primary")]
        ]
        await event.edit(balance_text, buttons=keyboard, parse_mode='html')
        return
    
    elif data == "deposit_upi":
        await event.answer("✅ Deposit via UPI")
        state["state"] = "awaiting_deposit_amount"
        await event.edit(
            f"<b>💵 Enter Deposit Amount</b>\n\n"
            f"<blockquote>"
            f"💰 <b>Minimum:</b> ₹11\n"
            f"💡 <b>Example:</b> <code>100</code>, <code>500</code>, <code>1000</code>\n\n"
            f"⚠️ <i>Sirf numbers type karein</i>"
            f"</blockquote>",
            buttons=[[styled_inline("❌ Cancel", b"back_main", "danger")]],
            parse_mode='html'
        )
        return

    elif data == "deposit_history":
        await event.answer("📜 Loading approved deposits...")
        deposits = list(db["deposits"].find(
            {"user_id": user_id, "status": "approved"}
        ).sort("_id", -1).limit(15))

        if not deposits:
            hist_text = (
                f"<b>📜 APPROVED DEPOSIT HISTORY</b>\n\n"
                f"<blockquote>"
                f"❌ <b>Koi approved deposit nahi mila</b>\n\n"
                f"👉 Pehli deposit start karein!"
                f"</blockquote>"
            )
        else:
            total_approved_amt = sum(float(d.get("amount", 0)) for d in deposits)
            entries_lines = []
            for idx, d in enumerate(deposits, 1):
                amt = d.get("amount", 0)
                dt = d.get("date") or "N/A"
                tm = d.get("time") or "N/A"
                txn = d.get("upi_txn_id") or "N/A"
                entries_lines.append(
                    f"<b>{idx}. 🟢 ₹{amt}</b>\n"
                    f"📅 <code>{dt}</code> • ⏰ <code>{tm}</code>\n"
                    f"🔢 <code>{txn}</code>"
                )
            entries_text = "\n\n".join(entries_lines)

            hist_text = (
                f"<b>📜 APPROVED DEPOSITS</b> — Last {len(deposits)}\n\n"
                f"<blockquote>"
                f"💰 <b>Total:</b> <code>₹{total_approved_amt:.2f}</code>\n\n"
                f"{entries_text}"
                f"</blockquote>"
            )

        back_kb = [
            [styled_inline("💳 Deposit via UPI", b"deposit_upi", "success")],
            [styled_inline("🔙 Back to Deposit", b"deposit_home", "primary")]
        ]
        await event.edit(hist_text, buttons=back_kb, parse_mode='html')
        return
    
    elif data == "deposit_cancel":
        state["state"] = None
        state["deposit_amount"] = 0.0
        try:
            await event.delete()
        except Exception:
            pass
        await event.answer("❌ Deposit cancelled successfully.", alert=True)
        return

    elif data == "deposit_yes_paid":
        amount = state.get("deposit_amount", 0.0)
        state["state"] = "awaiting_deposit_ss"
        try:
            await event.delete()
        except Exception:
            pass
        instruction_text = (
            f"📸 **Send Payment Screenshot (SS)**\n\n"
            f"💰 **Amount:** ₹{amount}\n"
            f"📌 **UPI ID:** `{UPI_ID}`\n\n"
            f"Kripya apne payment ka successful screenshot yahin chat me send karein."
        )
        cancel_keyboard = [[styled_inline("❌ Cancel", b"deposit_cancel", "danger")]]
        await event.respond(instruction_text, buttons=cancel_keyboard)
        await event.answer()
        return

    elif data.startswith("do_auto_verify_"):
        try:
            parts = data.split("_")
            amount = float(parts[3])
            state = get_state(user_id)
            photo_file_id = state.get("pending_photo_id")
            qr_gen_time = state.get("qr_generated_at")

            if not photo_file_id:
                await event.answer("❌ Session expired ya photo nahi mili!", alert=True)
                return

            msg_wait = await event.edit("🔍 Bot screenshot verify kar raha hai, kripya intezaar karein...")

            from io import BytesIO
            _buf = BytesIO()
            await bot.download_media(photo_file_id, file=_buf)
            photo_bytes = _buf.getvalue()
            print(f"📸 Downloaded {len(photo_bytes)} bytes")

            # ─────────────────────────────────────────────
            #  AI EXTRACTION
            # ─────────────────────────────────────────────
            details = await extract_ss_details(photo_bytes)
            ai_status    = details.get("_ai_status", "unknown")
            upi_txn_id   = details.get("upi_txn_id") or "N/A"
            customer_upi = details.get("customer_upi") or "N/A"
            ss_date      = details.get("date") or "N/A"
            ss_time      = details.get("time") or "N/A"
            ss_amount    = details.get("amount")
            ss_receiver  = details.get("receiver") or "N/A"

            print(f"📋 [AUTO] ai_status={ai_status} | txn={upi_txn_id} | "
                  f"date={ss_date} | time={ss_time} | amt={ss_amount}")

            # ─────────────────────────────────────────────
            #  AI FAIL CHECK
            # ─────────────────────────────────────────────
            if ai_status != "success" and upi_txn_id == "N/A" and ss_date == "N/A":
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "⚠️ **Screenshot abhi auto-verify nahi ho paya.**\n\n"
                    "System issue ke wajah se screenshot padh nahi paya. "
                    "Kripya **Manual Verify** button try karein — admin khud check karega.\n\n"
                    "👉 **Screenshot dobara send karke Manual Verify pe click karein.**"
                )
                await log_auto_verify_failure(user_id, sender, amount, "AI could not read screenshot", upi_txn_id)
                return

            # ─────────────────────────────────────────────
            #  MISSING FIELD CHECK
            # ─────────────────────────────────────────────
            missing = []
            if upi_txn_id == "N/A":
                missing.append("Transaction ID / UTR")
            if ss_date == "N/A":
                missing.append("Date")
            if ss_time == "N/A":
                missing.append("Time")

            if missing:
                state["state"] = "awaiting_deposit_ss"
                missing_list = "\n".join("• " + m for m in missing)
                await msg_wait.edit(
                    f"❌ **Screenshot me yeh cheezein clear nahi dikh rahi:**\n\n"
                    f"{missing_list}\n\n"
                    f"👉 **Full payment receipt bhejein** jisme UTR, Date, Time saaf dikh rahe hon.\n"
                    f"📸 Screenshot **crop mat karein** — pura receipt bhejein."
                )
                await log_auto_verify_failure(
                    user_id, sender, amount,
                    f"Missing fields: {', '.join(missing)}",
                    upi_txn_id
                )
                return
            # ─────────────────────────────────────────────
            #  (Iske baad ka code — amount check, time check,
            #   receiver check, duplicate check, insert,
            #   success message, group log — SAME rehne do)
            # ─────────────────────────────────────────────

            if ss_amount is not None and float(ss_amount) != float(amount):
                state["state"] = "awaiting_deposit_ss"
                # ...
                await msg_wait.edit(
                    f"❌ **Amount Mismatch!** Aapne ₹{amount} ki request dali thi par SS me ₹{ss_amount} hai.\n\n"
                    "👉 **Kripya sahi amount ka real screenshot dobara send karein:**"
                )
                await log_auto_verify_failure(
                    user_id, sender, amount,
                    f"Amount mismatch (expected ₹{amount}, got ₹{ss_amount})",
                    upi_txn_id
                ) 
                return

            if qr_gen_time and ss_date and ss_time:
                ss_dt = parse_ss_datetime(ss_date, ss_time)
                if not ss_dt or ss_dt < qr_gen_time:
                    state["state"] = "awaiting_deposit_ss"
                    await msg_wait.edit(
                        "❌ **Invalid Screenshot Time!** Yeh screenshot scanner aane se pehle ka hai ya time match nahi hua (Purana SS).\n\n"
                        "👉 **Kripya naya payment karke uska fresh screenshot bhejien:**"
                    )
                    await log_auto_verify_failure(
                        user_id, sender, amount,
                        "Old screenshot (before QR generation)",
                        upi_txn_id
                    ) 
                    return
            else:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "❌ **Time Verification Failed!** Screenshot se payment ka time read nahi ho paya.\n\n"
                    "👉 **Kripya saaf screenshot dobara send karein:**"
                )
                await log_auto_verify_failure(
                    user_id, sender, amount,
                    "Time verification failed (could not parse)",
                    upi_txn_id
                )
                return

            if ss_receiver and UPI_ID:
                clean_ss_recv = re.sub(r'[\s@._-]', '', str(ss_receiver)).lower()
                clean_bot_upi = re.sub(r'[\s@._-]', '', str(UPI_ID)).lower()
                bot_handle = clean_bot_upi.split()[0] if clean_bot_upi else ""
                if clean_bot_upi not in clean_ss_recv and clean_ss_recv not in clean_bot_upi and bot_handle not in clean_ss_recv:
                    state["state"] = "awaiting_deposit_ss"
                    await msg_wait.edit(
                        f"❌ **Wrong Receiver UPI!** Yeh payment kisi aur ke UPI (`{ss_receiver}`) par bheja gaya hai, hamare UPI par nahi.\n\n"
                        "👉 **Kripya sahi UPI par payment karke uska naya screenshot send karein:**"
                    )
                    await log_auto_verify_failure(
                        user_id, sender, amount,
                        f"Wrong receiver UPI (`{ss_receiver}`)",
                        upi_txn_id
                    )
                    return

            existing_txn = db["deposits"].find_one({"upi_txn_id": upi_txn_id if upi_txn_id else "NONE"})
            if existing_txn:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "❌ **Duplicate Transaction ID!** Yeh UTR pehle hi use ho chuki hai.\n\n"
                    "👉 **Kripya naya screenshot dobara send karein:**"
                )
                await log_auto_verify_failure(
                    user_id, sender, amount,
                    "Duplicate UTR (already used)",
                    upi_txn_id
                )
                return

            db["deposits"].insert_one({
                "user_id": user_id, "upi_txn_id": upi_txn_id, "customer_upi": customer_upi,
                "amount": amount, "date": ss_date, "time": ss_time,
                "status": "approved", "approved_by": "Automatic Bot Click"
            })
            users_col.update_one({"user_id": user_id}, {"$inc": {"wallet": amount, "total_deposited": amount}})
            target_user = users_col.find_one({"user_id": user_id})
            referrer_id = target_user.get("referred_by") if target_user else None
            if referrer_id:
                commission = (amount * 2) / 100
                users_col.update_one({"user_id": int(referrer_id)},
                                     {"$inc": {"wallet": commission, "referral_profit": commission}})
                try:
                    await bot.send_message(referrer_id,
                        f"🎉 Referral commission received! Referred user deposited ₹{amount}, you got ₹{commission:.2f} bonus.")
                except Exception:
                    pass

            success_text = (
                f"🎉 **Deposit Auto-Approved Successfully!**\n\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **Txn ID:** `{upi_txn_id or 'N/A'}`\n"
                f"✨ Aapka payment successfully verify karke wallet me add kar diya gaya hai!"
            )
            await msg_wait.edit(success_text)

            masked_sender_upi = mask_upi_id(customer_upi)
            now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            auto_date_str = now.strftime("%d-%m-%Y")
            auto_time_str = now.strftime("%I:%M:%S %p")
            approved_info = f"Automatic Bot at {auto_date_str} {auto_time_str}"
            username_str_auto = f"@{sender.username}" if sender.username else "N/A"
            group_msg = (
                f"🎉 **Payment Approved Successful (Auto)!**\n\n"
                f"👤 **User:** {sender.first_name or 'User'}\n"
                f"📛 **Username:** {username_str_auto}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{upi_txn_id or 'N/A'}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"👑 **Approved By:** {approved_info}\n\n"
                f"🤖 **Bot :** @DarktgOtp_bot\n"
            )
            try:
                await bot.send_message(OTP_GROUP_ID, group_msg)
            except Exception as e:
                print(f"OTP group log err: {e}")

        except Exception as e:
            print(f"🚨 AUTO VERIFY CRASH: {e}")
            import traceback
            traceback.print_exc()
            try:
                await event.answer(f"❌ Verification failed: {str(e)[:100]}", alert=True)
            except Exception:
                pass
            try:
                await event.edit(
                    "❌ **Verification me error aa gaya!**\n\n"
                    "👉 Kripya **Manual Verify** option try karein ya thodi der baad dobara koshish karein."
                )
            except Exception:
                pass

    elif data.startswith("do_manual_verify_"):
        try:
            parts = data.split("_")
            amount = float(parts[3])
            state = get_state(user_id)
            photo_file_id = state.get("pending_photo_id")

            if not photo_file_id:
                await event.answer("❌ Session expired! Kripya dobara try karein.", alert=True)
                return

            msg_wait = await event.edit("⏳ Processing manual verification... (10-20 seconds)")

            from io import BytesIO
            _buf = BytesIO()
            await bot.download_media(photo_file_id, file=_buf)
            photo_bytes = _buf.getvalue()
            print(f"📸 [MANUAL] Downloaded {len(photo_bytes)} bytes")

        # ✅ Retry AI once if it fully fails
            details = await extract_ss_details(photo_bytes)
            if (
                details.get("is_valid_ss") is False
                and details.get("upi_txn_id") == "N/A"
                and details.get("date") == "N/A"
            ):
                print("🔁 [MANUAL] AI returned all N/A — retrying once...")
                await asyncio.sleep(1.5)
                details = await extract_ss_details(photo_bytes)

            upi_txn_id   = details.get("upi_txn_id") or "N/A"
            customer_upi = details.get("customer_upi") or "N/A"
            ss_date      = details.get("date") or "N/A"
            ss_time      = details.get("time") or "N/A"
            ss_amount    = details.get("amount")

            print(f"📋 [MANUAL] Extracted → txn={upi_txn_id}, upi={customer_upi}, "
                  f"date={ss_date}, time={ss_time}, amt={ss_amount}")

        # ✅ Insert with strings, never None
            db["deposits"].update_one(
                {"user_id": user_id, "amount": amount, "status": "pending"},
                {"$set": {
                    "user_id": user_id,
                    "upi_txn_id": upi_txn_id,     # "N/A" string bhi fine hai
                    "customer_upi": customer_upi,
                    "amount": amount,
                    "date": ss_date,
                    "time": ss_time,
                    "status": "pending"
                }},
                upsert=True
            )

            await safe_edit(msg_wait,
                "⏳ **Manual Request Submitted!**\n\n"
                "Aapka screenshot admin ke paas bhej diya gaya hai. Kripya intezaar karein."
            )

        # ✅ Warn admin if AI couldn't read
            ai_failed_note = ""
            if upi_txn_id == "N/A" and ss_date == "N/A" and ss_time == "N/A":
                ai_failed_note = "\n\n⚠️ **AI extraction failed** — Admin please verify manually from screenshot."

            username = f"@{sender.username}" if sender.username else "N/A"
            admin_caption = (
                f"💵 **New Manual Deposit Request**\n\n"
                f"👤 **User:** {sender.first_name or 'User'}\n"
                f"📛 **Username:** {username}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"💰 **Amount:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{upi_txn_id}`\n"
                f"💳 **Customer UPI:** `{customer_upi}`\n"
                f"📅 **Txn Date:** `{ss_date}`\n"
                f"⏰ **Txn Time:** `{ss_time}`"
                f"{ai_failed_note}"
            )
            try:
                await bot.send_file(
                    PAYMENT_LOG_GROUP, photo_file_id,
                    caption=admin_caption,
                    buttons=[[
                        styled_inline("✅ Approve", f"approve_dep_{user_id}_{amount}".encode(), "success"),
                        styled_inline("❌ Reject", f"reject_dep_{user_id}_{amount}".encode(), "danger")
                    ]]
                )
            except Exception as e:
                print(f"Admin group send err: {e}")
    
        except Exception as e:
            print(f"🚨 MANUAL VERIFY CRASH: {e}")
            import traceback
            traceback.print_exc()
            try:
                await event.answer(f"❌ Manual verification failed: {str(e)[:100]}", alert=True)
            except Exception:
                pass
            try:
                await event.edit(
                    "❌ **Manual verification me error aa gaya!**\n\n"
                    "👉 Kripya thodi der baad dobara try karein ya Support se contact karein."
                )
            except Exception:
                pass
                
    elif data.startswith("buy_acc_"):
        target_phone = data.replace("buy_acc_", "")

        # ✅ NAYA: Agar user ka koi session already hai toh purana cleanup karo (re-select ke liye)
        if user_id in login_states:
            old_sd = login_states[user_id]
            old_phone = old_sd.get("account", {}).get("phone")
            # Agar wahi number dobara select kar raha hai, toh purana disconnect karke naya start karo
            if old_phone:
                _busy_phones.discard(old_phone)
            await cleanup_user_login(user_id)

        already_busy = any(s.get("account", {}).get("phone") == target_phone for s in login_states.values())
        if already_busy or target_phone in _busy_phones:
            await event.answer("⚠️ Yeh account abhi kisi aur user ne select kiya hai. Kuch der baad try karein.", alert=True)
            return
        
        acc = accounts_col.find_one({"phone": target_phone, "sold": False})
        if not acc:
            await event.answer("❌ Account no longer available!", alert=True)
            return
        if not is_owner and float(user.get("wallet", 0)) < float(acc["price"]):
            await event.answer("❌ Insufficient Wallet Balance! Please Deposit money first.", alert=True)
            return
        _busy_phones.add(target_phone)
        session_name = f"sessions/{target_phone.replace('+', '').replace(' ', '')}"
        lock = _lock_for(session_name)
        if lock.locked():
            _busy_phones.discard(target_phone)
            await event.answer("⏳ Session busy hai, thodi der me try karein.", alert=True)
            return
        async with lock:
            user_client = TelegramClient(session_name, API_ID, API_HASH)
            try:
                if user_client.is_connected():
                    await user_client.disconnect()
                await user_client.connect()
            except Exception as e:
                _busy_phones.discard(target_phone)
                await event.answer(f"❌ Session connection error: {e}", alert=True)
                return
        login_states[user_id] = {
            "account": acc, "client": user_client,
            "purchase_time": datetime.now().timestamp(), "is_logged_in": False,
            "last_msg_id": 0, "session_terminated": False
        }

        # ✅ NAYA: Baseline capture karo (yaad rakho account select karte waqt kaun-kaun logged in hai)
        try:
            initial_sessions = await get_active_sessions_hash(user_client)
            if initial_sessions is not None:
                login_states[user_id]["baseline_sessions"] = initial_sessions
                print(f"📸 [SESSION] Baseline captured at selection: {len(initial_sessions)} devices")
        except Exception as e:
            print(f"⚠️ [SESSION] Baseline capture failed: {e}")
        
        owner_tag = " [👑 OWNER FREE ACCESS]" if is_owner else ""
        
        # 👇 Yahan hum message ka ID save kar rahe hain
        sent_msg = await event.edit(
            f"✅ **Account Selected!**{owner_tag}\n\n"
            f"📞 **Phone Number:** `{acc['phone']}`\n\n"
            f"👉 Login now in your Telegram app using this phone number.\n"
            f"⏳ *OTP aapko yahan automatically mil jayega (15 second ke andar). Kripya intezaar karein.*\n\n"
            f"⚠️ *Agar account cancel karna hai, toh 'Back' dabayein. Aapka account delete nahi hoga, bas aap wapas list me chale jayenge.*",
            buttons=[
                [styled_inline("🔙 Back", b"buy_acc", "primary")]
            ]
        )
        
        login_states[user_id]["msg_id"] = sent_msg.id

    elif data == "fetch_otp":
        if user_id not in login_states:
            await event.answer("❌ Active session nahi mila. Kripya firse account select karein.", alert=True)
            return
            
        session_data = login_states[user_id]
        user_client = session_data.get("client")
        
        await event.answer("🔍 Checking messages...", alert=False)
        
        try:
            if not user_client.is_connected():
                await user_client.connect()
                
            last_msg_id = session_data.get("last_msg_id", 0)
            # 👈 limit=5 karo taaki ek saath aaye toh sab process ho
            messages = await user_client.get_messages(777000, limit=5, min_id=last_msg_id)
            
            if messages:
                processed_any = False
                for msg in reversed(messages):
                    msg_ts = msg.date.timestamp() if msg.date else 0
                    if msg_ts >= session_data.get("purchase_time", 0):
                        session_data["last_msg_id"] = msg.id
                        await process_telegram_message(user_id, msg, session_data)
                        processed_any = True
                
                if not processed_any:
                    await event.answer("❌ Naya OTP nahi mila! Kripya Telegram app me 'Send Code' par click karein.", alert=True)
            else:
                await event.answer("❌ Naya OTP nahi mila! Kripya Telegram app me 'Send Code' par click karein.", alert=True)
        except Exception as e:
            print(f"Fetch OTP Error: {e}")
            await event.answer("Error fetching OTP.", alert=True)

    elif data == "customer_logout":
        if user_id in login_states:
            session_data = login_states[user_id]
            acc = session_data["account"]
            user_client = session_data["client"]
            masked_num = mask_phone_number(acc['phone'], acc.get('country_code'))
            user_mention = sender.first_name or "User"
            username_str = f"@{sender.username}" if sender.username else "N/A"
            acc_price = acc.get("price", 0.0)
            
            # ✅ Sirf tabhi group me purchase log jaye jab user actually login kar chuka ho
            if session_data.get("is_logged_in", False):
                me = await bot.get_me()
                bot_username = me.username
                group_msg = (
                    f"🎉 **New Purchase Successful**\n\n"
                    f"👤 **User:** {user_mention}\n"
                    f"📛 **Username:** {username_str}\n"
                    f"🆔 **User ID:** `{user_id}`\n"
                    f"📱 **Account Number:** `{masked_num}`\n"
                    f"💰 **Price:** ₹{acc_price}\n\n"
                    f"✨ **Thanks for purchase!**\n\n"
                    f"🤖 **Bot:** @{bot_username}"
                )
                try:
                    await bot.send_message(OTP_GROUP_ID, group_msg, link_preview=False)
                except Exception as ge:
                    print(f"Failed to send to OTP Group: {ge}")
                    
                for owner_id in OWNER_IDS:
                    try:
                        await bot.send_message(owner_id,
                            f"🔔 **Session Ended / Logout Alert**\n\n"
                            f"👤 **User:** {user_mention}\n"
                            f"📛 **Username:** {username_str}\n"
                            f"🆔 **Customer ID:** `{user_id}`\n"
                            f"📱 **Phone:** `{acc['phone']}`\n"
                            f"Customer clicked logout from bot.")
                    except Exception as oe:
                        print(f"Failed to send alert to owner {owner_id}: {oe}")
            
            # Client ko disconnect karo (chahe login kiya ho ya nahi)
            try:
                if user_client.is_connected():
                    await user_client.log_out()
            except Exception:
                pass
                
            del login_states[user_id]
            await event.edit("✅ You have logged out successfully from the session.")
        else:
            await event.answer("No active session to logout.", alert=True)

    elif data.startswith("approve_dep_") or data.startswith("reject_dep_"):
        parts = data.split("_")
        action = parts[0]
        target_user_id = int(parts[2]); amount = float(parts[3])
        target_user = users_col.find_one({"user_id": target_user_id})
        deposit_record = db["deposits"].find_one({"user_id": target_user_id, "amount": amount, "status": "pending"})
        txn_id = "N/A"
        if deposit_record:
            raw_txn = deposit_record.get("upi_txn_id") or deposit_record.get("google_txn_id")
            # "N/A", "none", "null", ya empty string ko ignore karo
            if raw_txn and str(raw_txn).strip().lower() not in ["n/a", "none", "null", ""]:
                txn_id = raw_txn
        try:
            tu = await bot.get_entity(target_user_id)
            user_mention = tu.first_name or "User"
            username_str = f"@{tu.username}" if tu.username else "N/A"
        except Exception:
            user_mention = f"User (`{target_user_id}`)"
            username_str = "N/A"
        if action == "approve":
            db["deposits"].update_one(
                {"user_id": target_user_id, "amount": amount, "status": "pending"},
                {"$set": {"status": "approved"}})
            users_col.update_one({"user_id": target_user_id},
                                 {"$inc": {"wallet": amount, "total_deposited": amount}})
            referrer_id = target_user.get("referred_by") if target_user else None
            if referrer_id:
                commission = (amount * 2) / 100
                users_col.update_one({"user_id": int(referrer_id)},
                                     {"$inc": {"wallet": commission, "referral_profit": commission}})
                try:
                    await bot.send_message(referrer_id,
                        f"🎉 **Referral Commission Received!**\n\n"
                        f"Your referred user ({user_mention}) has deposited ₹{amount}.\n"
                        f"💰 **You have received a 2% (₹{commission:.2f}) bonus in your wallet!**")
                except Exception:
                    pass
            now_ap = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            approval_date_str_ap = now_ap.strftime("%d-%m-%Y")
            approval_time_str_ap = now_ap.strftime("%I:%M:%S %p")
            approved_by_info_ap = f"{sender.first_name or 'User'} at {approval_date_str_ap} {approval_time_str_ap}"
            masked_sender_upi_ap = mask_upi_id(deposit_record.get("customer_upi")) if deposit_record else "N/A"
            log_group_msg = (
                f"✅ **Deposit Approved Successfully**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Approved Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi_ap}`\n"
                f"👑 **Approved By:** {approved_by_info_ap}"
            )
            await event.edit(log_group_msg)
            user_msg = (
                f"🎉 **Your Deposit Has Been Approved!**\n\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"✨ Your deposit has been approved and the amount has been added to your wallet!"
            )
            try:
                await bot.send_message(target_user_id, user_msg)
            except Exception:
                pass
            masked_sender_upi = mask_upi_id(deposit_record.get("customer_upi")) if deposit_record else "N/A"
            now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            approval_date_str = now.strftime("%d-%m-%Y")
            approval_time_str = now.strftime("%I:%M:%S %p")
            approved_by_info = f"{sender.first_name or 'User'} at {approval_date_str} {approval_time_str}"
            group_approve_msg = (
                f"🎉 **Payment Approved Successful!**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{txn_id}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"👑 **Approved By:** {approved_by_info}\n\n"
                f"🤖 **Bot :** @DarktgOtp_bot\n"
            )
            try:
                await bot.send_message(OTP_GROUP_ID, group_approve_msg)
            except Exception as e:
                print(f"Failed to send approval to OTP group: {e}")
        else:
            db["deposits"].update_one(
                {"user_id": target_user_id, "amount": amount, "status": "pending"},
                {"$set": {"status": "rejected"}})
            masked_sender_upi = mask_upi_id(deposit_record.get("customer_upi")) if deposit_record else "N/A"
            now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            rejection_date_str = now.strftime("%d-%m-%Y")
            rejection_time_str = now.strftime("%I:%M:%S %p")
            rejected_by_info = f"{sender.first_name or 'User'} at {rejection_date_str} {rejection_time_str}"
            log_group_msg = (
                f"❌ **Deposit Rejected**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Rejected Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"👑 **Rejected By:** {rejected_by_info}"
            )
            await event.edit(log_group_msg)
            target_user_state = get_state(target_user_id)
            target_user_state["state"] = "awaiting_deposit_ss"
            target_user_state["deposit_amount"] = amount
            user_msg = (
                f"❌ **Deposit Request Rejected**\n\n"
                f"💰 **Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"❌ Aapka payment screenshot admin dwara reject kar diya gaya hai.\n\n"
                f"👉 **Kripya apna real aur sahi payment screenshot yahin chat me dobara send karein:**"
            )
            try:
                await bot.send_message(target_user_id, user_msg)
            except Exception:
                pass
            group_reject_msg = (
                f"❌ **Payment Rejected!**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Rejected Amount:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{txn_id}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"👑 **Rejected By:** {rejected_by_info}\n\n"
                f"🤖 **Bot :** @DarktgOtp_bot\n"
            )
            try:
                await bot.send_message(OTP_GROUP_ID, group_reject_msg)
            except Exception as e:
                print(f"Failed to send rejection to OTP group: {e}")


# ================= MESSAGE HANDLER =================
@bot.on(events.NewMessage())
async def handle_text(event: events.NewMessage.Event):
    if not event.is_private:
        return
    text = event.text
    if not text or text.startswith("/"):
        return

    user_id = event.sender_id

    if user_id not in OWNER_IDS:
        is_off, off_msg = get_bot_status()
        if is_off:
            await event.reply(
                f"⚠️ <b>Bot Under Maintenance</b>\n\n"
                f"<blockquote>{off_msg}</blockquote>",
                parse_mode='html'
            )
            return
        
    if not await is_user_joined(bot, user_id):
        keyboard = await build_force_join_keyboard()
        await event.reply(
            "⚠️ **Aapne hamare required Channels/Groups ko leave kar diya hai!**\n\n"
            "Bot ko dobara use karne ke liye pehle sabhi channels/groups ko join karein:",
            buttons=keyboard
        )
        return

    state = get_state(user_id)
    user = get_user(user_id)
    is_owner = (user_id in OWNER_IDS)

    if text == "💳 Deposit Money":
        sender = await event.get_sender()
        user_mention_html = f'<a href="tg://user?id={user_id}">{sender.first_name or "User"}</a>'

        balance_text = (
            f"<b>💳 DEPOSIT MONEY</b>\n\n"
            f"<blockquote>"
            f"👤 <b>User:</b> {user_mention_html}\n"
            f"💰 <b>Current Balance:</b> <code>₹{user.get('wallet', 0.0):.2f}</code>\n"
            f"📊 <b>Total Deposited:</b> <code>₹{user.get('total_deposited', 0.0):.2f}</code>"
            f"</blockquote>\n\n"
            f"👇 <b>Choose an option:</b>"
        )
        keyboard = [
            [styled_inline("💳 Deposit via UPI", b"deposit_upi", "success"),
             styled_inline("📜 History", b"deposit_history", "primary")]
        ]
        await event.reply(balance_text, buttons=keyboard, parse_mode='html')
        return

    elif text == "🛒 Buy Account":
        keyboard = [
        [styled_inline("🔥 Spam Accounts", b"buy_type_spam", "danger"),
         styled_inline("🌱 Fresh Accounts", b"buy_type_fresh", "success")],
        [styled_inline("🆕 New Registered", b"buy_type_registered", "primary")]
    ]
        await event.reply("🛒 **Select Account Category:**", buttons=keyboard)
        return

    elif text == "👤 Profile":
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=REF{user_id}4F886"
        sender = await event.get_sender()
        username_str = f"@{sender.username}" if sender.username else "N/A"
        user_mention = sender.first_name or "User"
        profile_text = (
            f"👤 **USER PROFILE**\n\n"
            f"👤 **Name:** {user_mention}\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📛 **Username:** {username_str}\n"
            f"💰 **Wallet Balance:** ₹{user.get('wallet', 0.0):.2f}\n"
            f"📅 **Start Date:** `{user.get('start_date', 'N/A')}`\n\n"
            f"🔗 **Your Referral Link:**\n`{ref_link}`\n\n"
            f"🎁 *Har referral deposit ka 2% aapke wallet me auto-credit hoga!*"
        )
        await event.reply(profile_text)
        return

    elif text == "📊 Stats":
        bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})
        stats_text = (
            f"📊 **YOUR ACCOUNT STATS**\n\n"
            f"🛒 **Total Accounts Bought:** {bought_count}\n"
            f"💸 **Total Amount Spent:** ₹{user.get('total_spent', 0.0):.2f}\n"
            f"💵 **Total Amount Deposited:** ₹{user.get('total_deposited', 0.0):.2f}\n"
            f"🎁 **Referral Earnings (2%):** ₹{user.get('referral_profit', 0.0):.2f}"
        )
        stats_buttons = [[styled_inline("👥 Referral Log", b"ref_log", "primary"),
                          styled_inline("🛒 Purchase Log", b"purchase_log", "success")]]
        await event.reply(stats_text, buttons=stats_buttons)
        return

    elif text == "🛠️ Support":
        usernames = get_support_usernames()
        
        # Agar list khaali hai toh bina button ke message bhejo
        if not usernames:
            await event.reply(
                "🛠️ **Customer Support**\n\n❌ **Abhi koi Support available nahi hai.**\nKripya thodi der baad try karein."
            )
            return
            
        support_keyboard = []
        for u in usernames:
            support_keyboard.append([styled_url(f"💬 Contact @{u}", f"https://t.me/{u}", "danger")])
            
        await event.reply(
            "🛠️ **Customer Support**\n\nFor any issues, contact Owner by clicking the button below:",
            buttons=support_keyboard
        )
        return

    # BULK OTP
    if state and state.get("state") == "awaiting_bulk_otp":
        otp = text.strip().replace(" ", "")
        acc_client = state["temp_client"]
        curr = state["current_acc"]
        phone = curr["phone"]
        try:
            if not acc_client.is_connected():
                await acc_client.connect()
            await acc_client.sign_in(phone, code=otp, phone_code_hash=state["temp_code_hash"])
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {"phone": phone, "price": curr["price"],
                          "country_name": curr["c_name"], "country_code": curr["c_code"],
                          "type": curr.get("type", "fresh"), "sold": False,
                          "2fa_password": curr["password"]}},
                upsert=True)
            await acc_client.disconnect()
            await event.reply(f"✅ `{phone}` Logged in & Added Successfully!")
            state["completed"] = state.get("completed", 0) + 1
            state["state"] = None
            await process_next_bulk_account(bot, user_id, event)
        except SessionPasswordNeededError:
            if curr["password"]:
                try:
                    await acc_client.sign_in(password=curr["password"])
                    accounts_col.update_one(
                        {"phone": phone},
                        {"$set": {"phone": phone, "price": curr["price"],
                                  "country_name": curr["c_name"], "country_code": curr["c_code"],
                                  "type": curr.get("type", "fresh"), "sold": False,
                                  "2fa_password": curr["password"]}},
                        upsert=True)
                    await acc_client.disconnect()
                    await event.reply(f"✅ `{phone}` (with 2FA) Logged in & Saved!")
                    state["completed"] = state.get("completed", 0) + 1
                    state["state"] = None
                    await process_next_bulk_account(bot, user_id, event)
                except Exception as p_err:
                    await acc_client.disconnect()
                    await event.reply(f"❌ Incorrect 2FA Password for `{phone}`: {str(p_err)}")
                    state["state"] = None
                    await process_next_bulk_account(bot, user_id, event)
            else:
                await acc_client.disconnect()
                await event.reply(f"❌ 2FA Password Required for `{phone}` but none provided!")
                state["state"] = None
                await process_next_bulk_account(bot, user_id, event)
        except Exception as e:
            await acc_client.disconnect()
            await event.reply(f"❌ Failed to sign in `{phone}`: {str(e)}")
            state["state"] = None
            await process_next_bulk_account(bot, user_id, event)
        return

    if state["state"] == "awaiting_country_name" and is_owner:
        state["temp_country_name"] = text.strip()
        state["state"] = "awaiting_country_code"
        await event.reply("📞 Enter Country Code (e.g. +91, +1, +62):")
        return

    elif state["state"] == "awaiting_country_code" and is_owner:
        state["temp_country_code"] = text.strip()
        state["state"] = "awaiting_acc_phone"
        await event.reply(f"📱 Enter full phone number with country code (e.g. {state['temp_country_code']}9876543210):")
        return

    if state["state"] == "awaiting_acc_phone" and is_owner:
        phone = text.strip()
        state["temp_phone"] = phone
        state["state"] = "awaiting_acc_price"
        await event.reply(f"Phone `{phone}` saved. Now enter selling PRICE for this account (e.g. 50):")

    elif state["state"] == "awaiting_acc_price" and is_owner:
        try:
            price = float(text.strip())
        except ValueError:
            await event.reply("❌ Invalid price! Enter numbers only:")
            return
        phone = state["temp_phone"]
        existing = accounts_col.find_one({"phone": phone, "status": {"$ne": "logged_out"}})
        if existing:
            await event.reply(f"⚠️ `{phone}` already DB me hai (Type: {existing.get('type','fresh')}).\n"
                              f"Pehle `/logout {phone}` karo.")
            state["state"] = None
            return
        session_folder = "sessions"
        os.makedirs(session_folder, exist_ok=True)
        session_name = f"{session_folder}/{phone.replace('+', '').replace(' ', '')}"
        old_client = state.get("temp_client")
        if old_client:
            await cleanup_session(session_name, old_client)
        else:
            await cleanup_session(session_name)
        acc_client = TelegramClient(session_name, API_ID, API_HASH)
        try:
            await acc_client.connect()
            sent_code = await acc_client.send_code_request(phone)
            state["temp_client"] = acc_client
            state["temp_code_hash"] = sent_code.phone_code_hash
            state["temp_price"] = price
            state["state"] = "awaiting_acc_otp"
            await event.reply(f"📲 OTP sent to `{phone}`. Enter the OTP code received on that account:")
        except Exception as e:
            await cleanup_session(session_name, acc_client)
            state["state"] = None
            await event.reply(f"❌ Connection Error: `{e}`\n\nPlease try again.")

    elif state["state"] == "awaiting_acc_otp" and is_owner:
        otp = text.strip().replace(" ", "")
        acc_client = state["temp_client"]
        phone = state["temp_phone"]
        try:
            await acc_client.sign_in(phone, code=otp, phone_code_hash=state["temp_code_hash"])
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {"phone": phone, "price": state["temp_price"],
                          "country_name": state.get("temp_country_name", "Unknown"),
                          "country_code": state.get("temp_country_code", "+"),
                          "type": state.get("temp_acc_type", "fresh"),
                          "sold": False, "2fa_password": None}},
                upsert=True)
            await acc_client.disconnect()
            state["state"] = None
            await event.reply(f"✅ Account `{phone}` logged in & saved to MongoDB stock successfully!")
        except SessionPasswordNeededError:
            state["state"] = "awaiting_acc_2fa"
            await event.reply("🔑 Account is protected with 2FA Password. Enter the 2FA Password:")
        except Exception as e:
            await event.reply(f"❌ Login failed: `{e}`\nTry entering OTP again:")

    elif state["state"] == "awaiting_acc_2fa" and is_owner:
        password = text.strip()
        acc_client = state["temp_client"]
        phone = state["temp_phone"]
        try:
            await acc_client.sign_in(password=password)
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {"phone": phone, "price": state["temp_price"],
                          "country_name": state.get("temp_country_name", "Unknown"),
                          "country_code": state.get("temp_country_code", "+"),
                          "type": state.get("temp_acc_type", "fresh"),
                          "sold": False, "2fa_password": password}},
                upsert=True)
            await acc_client.disconnect()
            state["state"] = None
            await event.reply(f"✅ Account `{phone}` with 2FA added to MongoDB stock successfully!")
        except PasswordHashInvalidError:
            await event.reply("❌ Wrong 2FA Password! Try again:")

    elif state["state"] == "awaiting_deposit_amount":
        try:
            amount = float(text.strip())
            if amount <= 0:
                await event.reply("❌ Amount must be greater than 10!")
                return
            if amount <= 10:
                await event.reply("❌ Minimum deposit must be greater than **₹10**!\n")
                return
            state["deposit_amount"] = amount
            state["qr_generated_at"] = datetime.now()
            state["state"] = "awaiting_deposit_ss"
            upi_link = f"upi://pay?pa={UPI_ID}&pn=Store&am={amount}&cu=INR"
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=25,      # ✅ QR bada (default 10)
                border=1,         # ✅ White border kam (default 4)
            )
            qr.add_data(upi_link)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            bio.name = 'qr.png'
            qr_img.save(bio, 'PNG')
            bio.seek(0)
            caption_msg = (
                f"<b>💳 Payment Details</b>\n\n"
                f"<blockquote>"
                f"💰 <b>Amount:</b> <code>₹{amount}</code>\n"
                f"📌 <b>UPI ID:</b> <code>{UPI_ID}</code>"
                f"</blockquote>\n\n"
                f"<blockquote>"
                f"<b>📖 How to Pay:</b>\n"
                f"1️⃣ 📷 Scan QR Code\n"
                f"2️⃣ 💸 Pay ₹{amount}\n"
                f"3️⃣ ✅ Click Yes Paid\n"
                f"4️⃣ 📸 Send Screenshot"
                f"</blockquote>"
            )
            keyboard = [[styled_inline("✅ Yes Paid", b"deposit_yes_paid", "success"),
                         styled_inline("❌ Cancel", b"deposit_cancel", "danger")]]
            await event.reply(
                file=bio,
                message=caption_msg,
                buttons=keyboard,
                parse_mode='html'
            )
        except ValueError:
            await event.reply("❌ Invalid amount. Enter a valid number (e.g., 100):")


# Photo handler (Telethon routes photos through NewMessage as well)
@bot.on(events.NewMessage())
async def handle_photo(event: events.NewMessage.Event):
    if not event.photo:
        return
    user_id = event.sender_id

    if user_id not in OWNER_IDS:
        is_off, off_msg = get_bot_status()
        if is_off:
            await event.reply(
                f"⚠️ <b>Bot Under Maintenance</b>\n\n"
                f"<blockquote>{off_msg}</blockquote>",
                parse_mode='html'
            )
            return
        
    state = get_state(user_id)
    if state.get("state") == "awaiting_deposit_ss":
        amount = state["deposit_amount"]
        state["state"] = None
        state["pending_photo_id"] = event.photo
        state["pending_amount"] = amount
        customer_text = (
            f"📸 **Payment Screenshot Received!**\n\n"
            f"💰 **Amount:** ₹{amount}\n\n"
            f"👉 Aap apna deposit kaise verify karwana chahte hain? Niche diye gaye option me se select karein:"
        )
        keyboard = [
            [styled_inline("🤖 Automatic Verify & Approve", f"do_auto_verify_{amount}".encode(), "success")],
            [styled_inline("👤 Manual Verify (Admin Check)", f"do_manual_verify_{amount}".encode(), "primary")]
        ]
        await event.reply(customer_text, buttons=keyboard)


# ================= WEB SERVER =================
from aiohttp import web

async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")


async def main():
    print("Bot started with MongoDB persistence...")
    print(f"MONGO_URI: {'SET' if MONGO_URI else 'MISSING'}")
    print("Connecting to Telegram...")
    await bot.start(bot_token=BOT_TOKEN)

    asyncio.create_task(web_server())              # 👈 YE LINE ADD KARO
    asyncio.create_task(background_message_checker())

    print("✅ Connected to Telegram!")
    me = await bot.get_me()
    print(f"✅ Bot username: @{me.username}")
    print("Bot is running...")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
