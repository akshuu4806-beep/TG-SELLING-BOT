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

from telethon import TelegramClient, events, Button, functions, types
from telethon.tl.functions.channels import GetParticipantRequest, GetFullChannelRequest
from telethon.tl.functions.messages import GetHistoryRequest, GetFullChatRequest, CheckChatInviteRequest
from telethon.tl.types import ChannelParticipantsSearch, InputPeerChannel, InputPeerChat
from telethon import Button
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


async def extract_ss_details(photo_bytes: bytes) -> dict:
    global current_key_index, ai_client

    prompt = (
        "Analyze this payment screenshot. Extract details in JSON format:\n"
        "1. 'upi_txn_id': UPI transaction ID / UTR / Reference No (null if missing).\n"
        "2. 'customer_upi': Sender or Customer UPI ID shown on SS (null if missing).\n"
        "3. 'date': Payment date as shown on SS in 'YYYY-MM-DD' format. Return null if missing.\n"
        "4. 'time': Payment time as shown on SS in 'HH:MM:SS AM/PM' or 'HH:MM' format. Return null if missing.\n"
        "5. 'amount': Numeric payment amount shown on SS (null if missing).\n"
        "6. 'receiver': Receiver UPI ID or Name shown on SS (null if missing).\n"
        "7. 'is_valid_ss': true if it's a valid successful payment receipt with transaction IDs, date and time, else false.\n\n"
        "Respond ONLY with valid JSON with keys: upi_txn_id, customer_upi, date, time, amount, receiver, is_valid_ss."
    )

    # ✅ FIXED: sahi model names (gemini-2.0-flash-exp + gemini-1.5-flash)
    models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash']

    for attempt in range(len(API_KEYS) * len(models_to_try)):
        model_name = models_to_try[attempt % len(models_to_try)]
        try:
            # ✅ FIXED: 25 sec timeout
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=model_name,
                    contents=[
                        prompt,
                        genai.types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg")
                    ]
                ),
                timeout=25.0
            )
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned_text)
            print(f"✅ AI success with {model_name} (key {current_key_index})")
            return data

        except asyncio.TimeoutError:
            print(f"⏰ AI TIMEOUT with {model_name} (key {current_key_index}) — trying next")
        except Exception as e:
            err_msg = str(e)
            print(f"❌ AI error [{model_name}] key {current_key_index}: {err_msg[:200]}")

            # Sirf quota/rate limit pe key switch karo
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                current_key_index = (current_key_index + 1) % len(API_KEYS)
                ai_client = genai.Client(api_key=API_KEYS[current_key_index])
                print(f"🔄 Switched to API key index {current_key_index}")

        # Chhota delay to avoid hammering
        await asyncio.sleep(0.3)

    # Sab fail ho gaya
    print("🚨 AI FAILED COMPLETELY — returning default")
    return {"is_valid_ss": False, "upi_txn_id": None, "customer_upi": None,
            "date": None, "time": None, "amount": None, "receiver": None}


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
                        type=types.ButtonTypeDefault(),
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
        single_use=True,
        selective=False,
        persistent=False
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
        except UserNotParticipantError:
            is_verified = False
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
    settings_col.update_one(
        {"type": "bot_status"},
        {"$set": {"status": "off", "message": off_msg, "off_at": datetime.now()}},
        upsert=True
    )
    await event.reply(
        f"🔴 <b>Bot OFF Ho Gaya!</b>\n\n"
        f"📝 <b>Non-owner users ko yeh message dikhega:</b>\n"
        f"<blockquote>{off_msg}</blockquote>\n\n"
        f"🟢 Wapas ON karne ke liye: <code>/boton</code>",
        parse_mode='html'
    )

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

# ================= START COMMAND =================
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

    first_name = (await event.get_sender()).first_name

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

    custom_config = settings_col.find_one({"type": "custom_start_message"})
    if custom_config and "message" in custom_config:
        stylish_welcome_text = custom_config["message"].format(
            first_name=first_name, bot_name=bot_name, ref_link=ref_link
        )
    else:
        stylish_welcome_text = (
            f"<b>╔══════════════════════╗</b>\n"
            f"<b>║   💎 WELCOME TO 💎   ║</b>\n"
            f"<b>║   {bot_name}</b>\n"
            f"<b>╚══════════════════════╝</b>\n\n"
            f"👋 Hey <b>{first_name}</b>, welcome aboard!\n\n"
            f"<blockquote>"
            f"🎁 <b>REFER & EARN</b>\n"
            f"🟢 <b>Bonus Rate:</b> <code>2% Instant Cashback</code>\n"
            f"🔵 <b>Kaise:</b> Jab dost wallet deposit karega, 2% bonus seedhe aapko milega\n"
            f"🟡 <b>Unlimited:</b> Jitna refer karoge, utna zyada profit"
            f"</blockquote>\n\n"
            f"🔗 <b>Your Referral Link:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Choose an option below:</b> ⚡"
        )

    await event.reply(stylish_welcome_text, buttons=main_keyboard(), link_preview=False, parse_mode='html')

@bot.on(events.NewMessage(pattern=r"^/setstartmsg"))
async def set_start_msg_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    input_text = event.text.split(maxsplit=1)
    if len(input_text) < 2:
        await event.reply(
            "⚠️ **Format:**\n"
            "`/setstartmsg Aapka naya welcome message yahan likhein...`\n\n"
            "Aap isme variables use kar sakte hain:\n"
            "- `{first_name}`: User ka naam\n"
            "- `{bot_name}`: Bot ka naam\n"
            "- `{ref_link}`: Referral link"
        )
        return
    new_msg = input_text[1].strip()
    settings_col.update_one({"type": "custom_start_message"}, {"$set": {"message": new_msg}}, upsert=True)
    await event.reply("✅ **Custom Start Message Successfully Set Ho Gaya Hai!**")


@bot.on(events.NewMessage(pattern=r"^/resetstartmsg"))
async def reset_start_msg_cmd(event: events.NewMessage.Event):
    if event.sender_id not in OWNER_IDS:
        return
    settings_col.delete_one({"type": "custom_start_message"})
    await event.reply("🔄 **Start Message Reset Ho Kar Default Wala Ho Gaya Hai!**")


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
            "`India|+91|50|+919876543210|pass1|spam`\n"
            "`India|+91|50|+919876543211|none|fresh`"
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

            # Same welcome as /start (HTML version)
            custom_config = settings_col.find_one({"type": "custom_start_message"})
            if custom_config and "message" in custom_config:
                stylish_welcome_text = custom_config["message"].format(
                    first_name=sender.first_name,
                    bot_name=bot_name,
                    ref_link=ref_link
                )
            else:
                stylish_welcome_text = (
                    f"<b>╔══════════════════════╗</b>\n"
                    f"<b>║   💎 WELCOME TO 💎   ║</b>\n"
                    f"<b>║   {bot_name}</b>\n"
                    f"<b>╚══════════════════════╝</b>\n\n"
                    f"👋 Hey <b>{sender.first_name}</b>, welcome aboard!\n\n"
                    f"<blockquote>"
                    f"🎁 <b>REFER & EARN</b>\n"
                    f"🟢 <b>Bonus Rate:</b> <code>2% Instant Cashback</code>\n"
                    f"🔵 <b>Kaise:</b> Jab dost wallet deposit karega, 2% bonus seedhe aapko milega\n"
                    f"🟡 <b>Unlimited:</b> Jitna refer karoge, utna zyada profit"
                    f"</blockquote>\n\n"
                    f"🔗 <b>Your Referral Link:</b>\n"
                    f"<code>{ref_link}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ <b>Choose an option below:</b> ⚡"
                )

            # Delete old force-join message first
            try:
                await event.delete()
            except Exception:
                pass

            # Send ONE new message — full welcome + keyboard (same as /start)
            await event.respond(
                stylish_welcome_text,
                buttons=main_keyboard(),
                link_preview=False,
                parse_mode='html'
            )
            return
        else:
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
        support_keyboard = [
            [styled_url("💬 Contact Support", "https://t.me/ll_its_rudransh_ll", "danger")],
            [styled_inline("🔙 Back", b"back_main", "primary")]
        ]
        await event.edit("🛠️ **Customer Support**\n\n For any issues, contact Owner by clicking the button below:", buttons=support_keyboard)

    elif data == "back_main":
        await cleanup_user_login(user_id)
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
        await cleanup_user_login(user_id)
        keyboard = [
            [styled_inline("🔥 Spam Accounts", b"buy_type_spam", "danger"),
             styled_inline("🌱 Fresh Accounts", b"buy_type_fresh", "success")],
            [styled_inline("🆕 New Registered", b"buy_type_registered", "primary")]
        ]
        await event.edit("🛒 **Select Account Category:**", buttons=keyboard)

    elif data in ["buy_type_spam", "buy_type_fresh", "buy_type_registered"]:
        await cleanup_user_login(user_id)
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
                f"{flag} {c_name} ({c_code}) | Stock: {c_count}",
                f"select_country_{selected_type}_{c_name}".encode(),
                "primary"
            )])
        btn_list.append([styled_inline("🔙 Back to Category", b"buy_acc", "primary")])
        await event.edit(f"🛒 **Select Country ({selected_type.upper()}):**", buttons=btn_list)

    elif data.startswith("select_country_"):
        await cleanup_user_login(user_id)
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
        btn_list.append([styled_inline("🔙 Back to Categories", b"buy_acc", "primary")])
        await event.edit(f"🛒 **Available {selected_type.upper()} Accounts for {selected_country}:**", buttons=btn_list)

    elif data == "deposit":
        state["state"] = "awaiting_deposit_amount"
        await event.respond("💵 Enter the amount you want to deposit (e.g., 100):")

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

            details = await extract_ss_details(photo_bytes)
            upi_txn_id = details.get("upi_txn_id")
            customer_upi = details.get("customer_upi") or "N/A"
            ss_date = details.get("date")
            ss_time = details.get("time")
            ss_amount = details.get("amount")
            ss_receiver = details.get("receiver")
            is_valid = details.get("is_valid_ss")

            if not is_valid or not upi_txn_id or not ss_date or not ss_time:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "❌ **Galt ya Adhoora Screenshot!**\n\n"
                    "Transaction ID, Date ya Time saaf nahi dikh raha hai.\n\n"
                    "👉 **Kripya apna real aur saaf payment screenshot yahin dobara send karein:**"
                )
                return

            if ss_amount is not None and float(ss_amount) != float(amount):
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    f"❌ **Amount Mismatch!** Aapne ₹{amount} ki request dali thi par SS me ₹{ss_amount} hai.\n\n"
                    "👉 **Kripya sahi amount ka real screenshot dobara send karein:**"
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
                    return
            else:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "❌ **Time Verification Failed!** Screenshot se payment ka time read nahi ho paya.\n\n"
                    "👉 **Kripya saaf screenshot dobara send karein:**"
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
                    return

            existing_txn = db["deposits"].find_one({"upi_txn_id": upi_txn_id if upi_txn_id else "NONE"})
            if existing_txn:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit(
                    "❌ **Duplicate Transaction ID!** Yeh UTR pehle hi use ho chuki hai.\n\n"
                    "👉 **Kripya naya screenshot dobara send karein:**"
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
            group_msg = (
                f"🎉 **Payment Approved Successful (Auto)!**\n\n"
                f"👤 **User:** {sender.first_name or 'User'}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{upi_txn_id or 'N/A'}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"🤖 **Approved By:** {approved_info}\n\n"
                f"🤖 **Bot :** @DarktgOtp_bot\n"
            )
            try:
                await bot.send_message(PAYMENT_LOG_GROUP, group_msg)
            except Exception as e:
                print(f"Payment group log err: {e}")
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

            msg_wait = await event.edit("⏳ Processing manual verification request... It takes 1 to 2 mints")

            from io import BytesIO
            _buf = BytesIO()
            await bot.download_media(photo_file_id, file=_buf)
            photo_bytes = _buf.getvalue()
            print(f"📸 [MANUAL] Downloaded {len(photo_bytes)} bytes")

            details = await extract_ss_details(photo_bytes)
            upi_txn_id = details.get("upi_txn_id") or "N/A"
            customer_upi = details.get("customer_upi")
            ss_date = details.get("date") or "N/A"
            ss_time = details.get("time") or "N/A"

            db["deposits"].update_one(
                {"user_id": user_id, "amount": amount, "status": "pending"},
                {"$set": {
                    "user_id": user_id,
                    "upi_txn_id": upi_txn_id if upi_txn_id != "N/A" else None,
                    "customer_upi": customer_upi if customer_upi else None,
                    "amount": amount, "date": ss_date, "time": ss_time, "status": "pending"
                }},
                upsert=True
            )
            try:
                await msg_wait.edit(
                    "⏳ **Manual Request Submitted!**\n\n"
                    "Aapka screenshot saari details ke sath admin ke paas bhej diya gaya hai. Kripya thoda intezaar karein."
                )
            except Exception:
                pass

            username = f"@{sender.username}" if sender.username else "N/A"
            admin_caption = (
                f"💵 **New Manual Deposit Request**\n\n"
                f"👤 **User:** {sender.first_name or 'User'}\n"
                f"📛 **Username:** {username}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"💰 **Amount:** ₹{amount}\n"
                f"🔢 **UPI Txn ID:** `{upi_txn_id}`\n"
                f"💳 **Customer UPI:** `{customer_upi or 'N/A'}`\n"
                f"📅 **Txn Date:** `{ss_date}`\n"
                f"⏰ **Txn Time:** `{ss_time}`"
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
        already_busy = any(s.get("account", {}).get("phone") == target_phone for s in login_states.values())
        if already_busy or target_phone in _busy_phones:
            await event.answer("⚠️ Yeh account abhi kisi aur user ne select kiya hai. Kuch der baad try karein.", alert=True)
            return
        await cleanup_user_login(user_id)
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
            "purchase_time": datetime.now().timestamp(), "is_logged_in": False
        }
        owner_tag = " [👑 OWNER FREE ACCESS]" if is_owner else ""
        await event.edit(
            f"✅ **Account Selected!**{owner_tag}\n\n"
            f"📞 **Phone Number:** `{acc['phone']}`\n\n"
            f"👉 Login now in your Telegram app using this phone number.\n"
            f"When code arrives, press the button below to fetch OTP.\n"
            f"⏳ *Note: Aapke paas login karne ke liye 5 minute ka time hai.*",
            buttons=[
                [styled_inline("📩 Request OTP / 2FA Code", b"fetch_otp", "success")],
                [styled_inline("🔙 Back to Countries", b"buy_acc", "primary")]
            ]
        )

    elif data == "fetch_otp":
        if user_id not in login_states:
            await event.answer("❌ Active session nahi mila. Kripya firse account select karein.", alert=True)
            return
        session_data = login_states[user_id]
        purchase_time = session_data.get("purchase_time", 0)
        if datetime.now().timestamp() - purchase_time > 300:
            await cleanup_user_login(user_id)
            await event.answer("⏰ Time limit expired (5 mins)! Number stock me wapas chala gaya.", alert=True)
            await event.edit("⏰ **Session Expired!** Aapne 5 minute ke andar login nahi kiya, isliye account stock me wapas bhej diya gaya hai.")
            return
        acc = session_data["account"]
        user_client: TelegramClient = session_data["client"]
        try:
            if not user_client.is_connected():
                await user_client.connect()
        except Exception as conn_err:
            await event.answer(f"❌ Session re-connection failed: {conn_err}", alert=True)
            return
        try:
            otp_found = False
            async for msg in user_client.iter_messages(777000, limit=5):
                if not msg.text:
                    continue
                msg_ts = msg.date.timestamp() if msg.date else 0
                if msg_ts < purchase_time:
                    continue
                otp_code = extract_otp(msg.text)
                if otp_code != "NOT-FOUND":
                    otp_found = True
                    if not session_data.get("is_logged_in", False):
                        if not is_owner:
                            new_wallet = float(user.get("wallet", 0)) - float(acc["price"])
                            users_col.update_one(
                                {"user_id": int(user_id)},
                                {"$set": {"wallet": new_wallet}, "$inc": {"total_spent": float(acc["price"])}}
                            )
                        accounts_col.update_one({"phone": acc["phone"]},
                                                {"$set": {"sold": True, "buyer_id": int(user_id)}})
                        session_data["is_logged_in"] = True
                    two_fa = acc.get("2fa_password") or "None"
                    masked_num = mask_phone_number(acc["phone"], acc.get('country_code'))
                    await event.respond(
                        f"📩 **Login Verification Code:**\n\n"
                        f"📱 **Phone:** `{masked_num}`\n"
                        f"🔑 **OTP:** `{otp_code}`\n"
                        f"🔐 **2FA Password:** `{two_fa}`",
                        buttons=logout_keyboard()
                    )
                    break
            if not otp_found:
                await event.answer("❌ Naya OTP nahi mila! Kripya Telegram app me 'Send Code' par click karein.", alert=True)
        except Exception as e:
            print(f"OTP Fetch Error: {e}")
            await event.answer("Error fetching OTP. Make sure you pressed 'Send Code' on Telegram app.", alert=True)

    elif data == "customer_logout":
        if user_id in login_states:
            session_data = login_states[user_id]
            if not session_data.get("is_logged_in", False):
                await event.answer("❌ Aapne abhi tak new login verify nahi kiya hai!", alert=True)
                return
            acc = session_data["account"]
            user_client = session_data["client"]
            masked_num = mask_phone_number(acc['phone'], acc.get('country_code'))
            user_mention = sender.first_name or "User"
            username_str = f"@{sender.username}" if sender.username else "N/A"
            acc_price = acc.get("price", 0.0)
            me = await bot.get_me()
            ref_link = f"https://t.me/{me.username}?start=REF{user_id}4F886"
            group_msg = (
                f"🎉 **New Purchase Successful**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"📱 **Account Number:** `{masked_num}`\n"
                f"💰 **Price:** ₹{acc_price}\n\n"
                f"✨ **Thanks for purchase!**\n"
                f"🔗 **Referral Link:** `{ref_link}`"
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
            txn_id = deposit_record.get("upi_txn_id") or deposit_record.get("google_txn_id") or "N/A"
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
            log_group_msg = (
                f"✅ **Deposit Approved Successfully**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Approved Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"👑 **Approved By:** {sender.first_name or 'User'}"
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
                await bot.send_message(PAYMENT_LOG_GROUP, group_approve_msg)
            except Exception as e:
                print(f"Failed to send approval to payment group: {e}")
            try:
                await bot.send_message(OTP_GROUP_ID, group_approve_msg)
            except Exception as e:
                print(f"Failed to send approval to OTP group: {e}")
        else:
            db["deposits"].update_one(
                {"user_id": target_user_id, "amount": amount, "status": "pending"},
                {"$set": {"status": "rejected"}})
            masked_sender_upi = mask_upi_id(deposit_record.get("customer_upi")) if deposit_record else "N/A"
            log_group_msg = (
                f"❌ **Deposit Rejected**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Rejected Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"💳 **Customer UPI:** `{masked_sender_upi}`\n"
                f"👑 **Rejected By:** {sender.first_name or 'User'}"
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
                f"❌ **Payment Request Rejected!**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"👑 **Rejected By:** {sender.first_name or 'User'}"
            )
            try:
                await bot.send_message(PAYMENT_LOG_GROUP, group_reject_msg)
            except Exception as e:
                print(f"Failed to send rejection to payment group: {e}")
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
        state["state"] = "awaiting_deposit_amount"
        await event.reply("💵 Enter the amount you want to deposit (e.g., 100):")
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
        support_keyboard = [[styled_url("💬 Contact Support", "https://t.me/ll_its_rudransh_ll", "danger")]]
        await event.reply("🛠️ **Customer Support**\n\nFor any issues, contact Owner by clicking the button below:",
                          buttons=support_keyboard)
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
                box_size=30,      # ✅ QR bada (default 10)
                border=0,         # ✅ White border kam (default 4)
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
    print("✅ Connected to Telegram!")
    me = await bot.get_me()
    print(f"✅ Bot username: @{me.username}")
    print("Bot is running...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
