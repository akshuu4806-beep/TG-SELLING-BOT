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
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message
)
from pyrogram.errors import (
    SessionPasswordNeeded, 
    PhoneCodeInvalid, 
    PasswordHashInvalid, 
    UserNotParticipant,
    InputUserDeactivated,
    UserIsBlocked,
    PeerIdInvalid,
    ChatAdminRequired,
    ChannelPrivate
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
users_col = db["users"]        # {user_id, wallet}
accounts_col = db["accounts"]  # {phone, price, session_string, 2fa_password, sold, buyer_id}
chats_col = db["chats"]        # Groups aur Channels store karne ke liye

settings_col = db["settings"]  # Persistent storage for dynamic configs


# Temporary runtime states
user_states = {}  
login_states = {} 

bot = Client("StoreBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
# Gemini API Keys (Environment variable se fetch hongi)
raw_api_keys = os.getenv("API_KEYS", "")
API_KEYS = [k.strip() for k in raw_api_keys.split(",") if k.strip()]

# Agar Render par API_KEYS set nahi ki hongi toh bot error dega taaki aapko pata chale
if not API_KEYS:
    raise ValueError("❌ Error: API_KEYS environment variable is missing!")

current_key_index = 0
ai_client = genai.Client(api_key=API_KEYS[current_key_index])
# Helper functions

# ================= SESSION LOCK MANAGER =================
_session_locks: dict = {}       # {session_name: asyncio.Lock}
_busy_phones: set = set()       # Currently in-use phones

def get_formatted_time():
    now = datetime.now()
    date_str = now.strftime("%d-%m-%Y")
    time_str = now.strftime("%I:%M:%S %p")
    return date_str, time_str

async def extract_ss_details(photo_bytes: bytes) -> dict:
    global current_key_index, ai_client
    
    prompt = (
        "Analyze this payment screenshot. Extract details in JSON format:\n"
        "1. 'upi_txn_id': UPI transaction ID / UTR / Reference No (null if missing).\n"
        "2. 'google_txn_id': Google transaction ID if present (null if missing).\n"
        "3. 'date': Payment date as shown on SS in 'YYYY-MM-DD' format. Return null if missing.\n"
        "4. 'time': Payment time as shown on SS in 'HH:MM:SS AM/PM' or 'HH:MM' format. Return null if missing.\n"
        "5. 'amount': Numeric payment amount shown on SS (null if missing).\n"
        "6. 'receiver': Receiver UPI ID or Name shown on SS (null if missing).\n"
        "7. 'is_valid_ss': true if it's a valid successful payment receipt with transaction IDs, date and time, else false.\n\n"
        "Respond ONLY with valid JSON with keys: upi_txn_id, google_txn_id, date, time, amount, receiver, is_valid_ss."
    )  
    
    # Stable aur supported models list
    models_to_try = ['gemini-3.8-flash', 'gemini-1.5-flash']

    # Saari keys ke liye loop chalega
    for _ in range(len(API_KEYS)):
        for model_name in models_to_try:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=[
                        prompt,
                        genai.types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg")
                    ]
                )
                cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
                data = json.loads(cleaned_text)
                return data
            except Exception as e:
                err_msg = str(e)
                print(f"Gemini OCR Error with Key index {current_key_index} ({model_name}): {err_msg}")
                
                # Agar Quota exhaust hua hai ya koi limit error hai, toh agli key par switch karo
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "NOT_FOUND" in err_msg:
                    current_key_index = (current_key_index + 1) % len(API_KEYS)
                    ai_client = genai.Client(api_key=API_KEYS[current_key_index])
                    print(f"🔄 Switching to next Gemini API Key (Index: {current_key_index})")
                    break  # Agli key ke sath loop dobara try karega
                
                if "503" in err_msg or "UNAVAILABLE" in err_msg:
                    await asyncio.sleep(1.5)
                    continue
                break

    return {"is_valid_ss": False, "upi_txn_id": None, "google_txn_id": None, "date": None, "time": None, "amount": None, "receiver": None}

def parse_ss_datetime(date_str: str, time_str: str):
    """
    Date aur Time strings ko datetime object me convert karta hai.
    """
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
    """Pending login session ko clear karta hai aur phone number ko unbusy karta hai."""
    if user_id in login_states:
        session_data = login_states.pop(user_id, None)
        if session_data:
            acc = session_data.get("account", {})
            target_phone = acc.get("phone")
            if target_phone:
                _busy_phones.discard(target_phone)  # Phone release ho gaya
            
            client_obj = session_data.get("client")
            if client_obj:
                await safe_disconnect(client_obj)

def _lock_for(name: str) -> asyncio.Lock:
    if name not in _session_locks:
        _session_locks[name] = asyncio.Lock()
    return _session_locks[name]

async def safe_disconnect(client):
    if not client:
        return
    try:
        if client.is_connected:
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
                    # Agar purana account hai jisme 'type' missing tha, usko default 'fresh' maano
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

    # DB ka country_code use karo (jo add karte time save hua tha)
    cc = country_code.strip() if country_code else ""
    if not cc.startswith('+'):
        cc = '+' + cc
    cc = cc.strip()

    # Agar country_code empty ya invalid ho, phone se guess karo (fallback)
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
    # Custom mapping common short forms ke liye
    custom_flags = {
        "usa": "🇺🇸",
        "us": "🇺🇸",
        "uk": "🇬🇧",
        "uae": "🇦🇪",
        "india": "🇮🇳"
    }
    
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
        await asyncio.sleep(0.2)  # OS ko handle release karne ka time do

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
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💳 Deposit Money"), KeyboardButton("🛒 Buy Account")],
            [KeyboardButton("👤 Profile"), KeyboardButton("📊 Stats")],
            [KeyboardButton("🛠️ Support")]
        ],
        resize_keyboard=True
    )

def logout_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Logout Bot Session", callback_data="customer_logout")]
    ])

def force_join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK)],
        [InlineKeyboardButton("💬 Otp Group", url=GROUP_LINK)],
        [InlineKeyboardButton("✅ Verify / Check Join", callback_data="check_force_join")]
    ])

async def get_force_join_channels():
    """Database se configured force join channels fetch karta hai."""
    config = settings_col.find_one({"type": "force_join"})
    if config and "channels" in config:
        return config["channels"]  # List of dicts: [{"username": "@channel", "link": "https://..."}]
    
    # Fallback to default hardcoded config if DB is empty
    return [
        {"username": CHANNEL_USERNAME, "link": CHANNEL_LINK},
        {"username": GROUP_USERNAME, "link": GROUP_LINK}
    ]

async def is_user_joined(client: Client, user_id: int) -> bool:
    channels = await get_force_join_channels()
    for ch in channels:
        try:
            await client.get_chat_member(ch["username"], user_id)
        except UserNotParticipant:
            return False
        except Exception as e:
            print(f"Force Join Check Error for {ch['username']}: {e}")
            return False
    return True

async def build_force_join_keyboard():
    channels = await get_force_join_channels()
    buttons = []
    for idx, ch in enumerate(channels, 1):
        # Agar custom title set hai toh wo use karo, warna default 'Join Channel X'
        title = ch.get("title") or f"📢 Join Channel {idx}"
        buttons.append([InlineKeyboardButton(title, url=ch["link"])])
    
    buttons.append([InlineKeyboardButton("✅ Verify / Check Join", callback_data="check_force_join")])
    return InlineKeyboardMarkup(buttons)

# ================= START COMMAND =================
@bot.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    referrer_id = None
    if len(message.command) > 1:
        try:
            raw_ref = message.command[1]
            # Extra text ko remove karke clean user_id extract karna
            clean_ref = raw_ref.replace("REF", "").replace("4F886", "").strip()
            
            possible_ref = int(clean_ref)
            if possible_ref != user_id:
                referrer_id = possible_ref
        except ValueError:
            pass

    get_user(user_id, referrer_id)

    # Dynamic Force Join Check
    if not await is_user_joined(client, user_id):
        keyboard = await build_force_join_keyboard()
        await message.reply_text(
            f"👋 **Welcome {first_name}!**\n\n"
            "⚠️ **Bot access karne ke liye pehle hamare Channels/Groups ko join karein:**",
            reply_markup=keyboard
        )
        return

    # Referral link generate karna
    bot_info = await client.get_me()
    bot_name = bot_info.first_name.upper()
    ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"

    stylish_welcome_text = (
        f"╔══════════════════════════╗\n"
        f"║   💎 **WELCOME TO** 💎   ║\n"
        f"║   **{bot_name}**          \n"
        f"╚══════════════════════════╝\n\n"
        f"👋 **Welcome, {first_name}!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 **REFER & EARN PROGRAM** 💎\n"
        f"├ 🎁 **Bonus Rate:** `2% Instant Cashback`\n"
        f"├ 💸 **Kaise Kamayein:** Jab bhi aapka dost wallet me deposit karega, 2% bonus seedhe aapke wallet me add ho jayega!\n"
        f"└ 📊 **Unlimited Earnings:** Jitna refer karoge, utna zyada profit!\n\n"
        f"🔗 **Your Referral Link:**\n"
        f"`{ref_link}`\n\n"
        f"⚡ Choose an option below: ⚡\n"
        f"───────────────"
    )

    await message.reply_text(
        text=stylish_welcome_text,
        reply_markup=main_keyboard(),
        disable_web_page_preview=True
    )

@bot.on_message(filters.command("add") & filters.private)
async def add_single_acc_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Spam Account", callback_data="add_type_spam")],
        [InlineKeyboardButton("🌱 Fresh Account", callback_data="add_type_fresh")]
    ])
    await message.reply_text("🌍 **[Owner]** Select Account Type:", reply_markup=keyboard)

# ================= TRACK CHATS & GROUPS =================
@bot.on_chat_member_updated()
async def on_chat_member_update(client: Client, chat_member):
    try:
        bot_id = (await client.get_me()).id
        if chat_member.new_chat_member and chat_member.new_chat_member.user.id == bot_id:
            chat = chat_member.chat
            chat_title = chat.title or str(chat.id)
            chats_col.update_one(
                {"chat_id": chat.id},
                {"$set": {
                    "chat_id": chat.id,
                    "title": chat_title,
                    "type": str(chat.type)
                }},
                upsert=True
            )
    except Exception as e:
        print(f"Error tracking chat member update: {e}")

@bot.on_message((filters.group | filters.channel) & ~filters.private)
async def track_chats(client: Client, message: Message):
    chat_title = message.chat.title or str(message.chat.id)
    chats_col.update_one(
        {"chat_id": message.chat.id},
        {"$set": {
            "chat_id": message.chat.id, 
            "title": chat_title, 
            "type": str(message.chat.type)
        }},
        upsert=True
    )

# ================= BULK ADD HANDLER =================
@bot.on_message(filters.command("addbulk") & filters.private)
async def add_bulk_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    input_text = message.text.split(maxsplit=1)
    if len(input_text) < 2:
        await message.reply_text(
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
            c_name = parts[0]
            c_code = parts[1]
            try:
                price = float(parts[2])
            except ValueError:
                continue
            phone = parts[3]
            password = parts[4] if len(parts) > 4 and parts[4].lower() != "none" else None
            acc_type = parts[5].lower() if len(parts) > 5 and parts[5].lower() in ["spam", "fresh"] else "fresh"
            
            queue.append({
                "c_name": c_name, "c_code": c_code, 
                "price": price, "phone": phone, "password": password,
                "type": acc_type
            })

    if not queue:
        await message.reply_text("❌ Valid numbers nahi mile format me!")
        return

    user_states[user_id] = {
        "bulk_queue": queue,
        "completed": 0,
        "total": len(queue)
    }

    await process_next_bulk_account(client, user_id, message)

# ================= OWNER LOGOUT COMMAND =================
@bot.on_message(filters.command("logout") & filters.private)
async def logout_acc_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    input_text = message.text.split(maxsplit=1)
    if len(input_text) < 2:
        await message.reply_text("⚠️ **Format:** `/logout <phone_number>`\nExample: `/logout +919876543210`")
        return

    target_phone = input_text[1].strip()
    acc = accounts_col.find_one({"phone": target_phone})

    if not acc:
        await message.reply_text(f"❌ Account `{target_phone}` database me nahi mila!")
        return

    status_msg = await message.reply_text(f"⏳ Logging out account `{target_phone}`...")

    session_name = f"sessions/{target_phone.replace('+', '').replace(' ', '')}"
    temp_client = Client(session_name, api_id=API_ID, api_hash=API_HASH)

    try:
        if not temp_client.is_connected:
            await temp_client.connect()
        try:
            await temp_client.log_out()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        await cleanup_session(session_name, temp_client)
        
        # STOCK REMOVAL LOGIC: sold: True set kar rahe hain taaki stock se hat jaye
        accounts_col.update_one(
            {"phone": target_phone}, 
            {"$set": {"sold": True, "status": "logged_out"}}
        )

    await status_msg.edit_text(f"✅ **Account `{target_phone}` logged out and removed from stock!**")

# 1. Add Force Join Channel/Group
@bot.on_message(filters.command("addfjoin") & filters.private)
async def add_fjoin_cmd(client: Client, message: Message):
    if int(message.from_user.id) not in OWNER_IDS:
        return

    input_text = message.text.split(maxsplit=1)
    if len(input_text) < 2 or "|" not in input_text[1]:
        await message.reply_text(
            "⚠️ **Format:**\n`/addfjoin @username_or_chatid | https://t.me/invite_link | Display Name`\n\n"
            "**Example:**\n"
            "`/addfjoin @itz_darkX | https://t.me/itz_darkX | 📢 Join Main Channel`"
        )
        return

    parts = [p.strip() for p in input_text[1].split("|")]
    username = parts[0]
    link = parts[1]
    title = parts[2] if len(parts) > 2 else f"📢 Join Channel"

    config = settings_col.find_one({"type": "force_join"})
    
    if not config or "channels" not in config:
        channels = [
            {"username": CHANNEL_USERNAME, "link": CHANNEL_LINK, "title": "📢 Join Channel 1"},
            {"username": GROUP_USERNAME, "link": GROUP_LINK, "title": "💬 Join OTP Group"}
        ]
    else:
        channels = config.get("channels", [])

    if any(c["username"] == username for c in channels):
        await message.reply_text("⚠️ Yeh channel/group pehle se Force Join list me hai!")
        return

    channels.append({"username": username, "link": link, "title": title})
    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": channels}}, upsert=True)
    
    await message.reply_text(
        f"✅ **Force Join Added Successfully!**\n\n"
        f"📛 **Button Name:** {title}\n"
        f"📢 **Target:** `{username}`\n"
        f"🔗 **Link:** {link}"
    )

# 2. View Current Force Join List
@bot.on_message(filters.command("fjoinlist") & filters.private)
async def list_fjoin_cmd(client: Client, message: Message):
    if int(message.from_user.id) not in OWNER_IDS:
        return

    channels = await get_force_join_channels()
    if not channels:
        await message.reply_text("ℹ️ Abhi koi bhi Force Join channel set nahi hai.")
        return

    text = "📢 **CURRENT FORCE JOIN CHANNELS/GROUPS:**\n\n"
    for idx, ch in enumerate(channels, 1):
        text += f"{idx}. **Target:** `{ch['username']}`\n   🔗 **Link:** {ch['link']}\n\n"
    
    await message.reply_text(text)

# 3. Reset/Clear All Force Join Limits
@bot.on_message(filters.command("clearfjoin") & filters.private)
async def clear_fjoin_cmd(client: Client, message: Message):
    if int(message.from_user.id) not in OWNER_IDS:
        return

    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": []}}, upsert=True)
    await message.reply_text("🧹 **Saare Force Join Channels/Groups clear kar diye gaye hain!**")

# ================= RESET DATABASE COMMAND (OWNER ONLY) =================
@bot.on_message(filters.command("resetdb") & filters.private)
async def reset_database_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    try:
        # Saare collections ka data delete karna
        users_col.delete_many({})
        accounts_col.delete_many({})
        chats_col.delete_many({})

        # Local runtime memory state clear karna
        user_states.clear()
        login_states.clear()

        await message.reply_text("🧹 **MongoDB Successfully Reset!**\n\nSaare Users, Accounts Stock, aur Chat logs delete ho chuke hain.")
    except Exception as e:
        await message.reply_text(f"❌ Reset karne me error aaya: `{e}`")

async def process_next_bulk_account(client, user_id, message_or_cb):
    state = user_states.get(user_id)
    if not state or not state.get("bulk_queue"):
        completed = state.get("completed", 0) if state else 0
        total = state.get("total", 0) if state else 0
        text = f"🎉 **Bulk Process Completed!**\nTotal Added: `{completed}/{total}`"
        
        if hasattr(message_or_cb, "reply_text"):
            await message_or_cb.reply_text(text)
        else:
            await message_or_cb.message.reply_text(text)
        
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

    acc_client = Client(session_name, api_id=API_ID, api_hash=API_HASH)

    existing = accounts_col.find_one({"phone": phone, "status": {"$ne": "logged_out"}})
    if existing:
        await message_or_cb.reply_text(
            f"⚠️ `{phone}` already DB me hai (Type: {existing.get('type','fresh')}). "
            f"Pehle `/logout {phone}` karo."
        )
        state["state"] = None
        await cleanup_session(session_name, acc_client)
        await process_next_bulk_account(client, user_id, message_or_cb)
        return
    
    try:
        await acc_client.connect()
        sent_code = await acc_client.send_code(phone)
        
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
        if hasattr(message_or_cb, "reply_text"):
            await message_or_cb.reply_text(status_text)
        else:
            await message_or_cb.message.reply_text(status_text)

    except Exception as e:
        await cleanup_session(session_name, acc_client)
        err_text = f"❌ Error for `{phone}`: {str(e)}\nSkipping to next..."
        if hasattr(message_or_cb, "reply_text"):
            await message_or_cb.reply_text(err_text)
        else:
            await message_or_cb.message.reply_text(err_text)
        
        await process_next_bulk_account(client, user_id, message_or_cb)

# ================= STOCK CHECK COMMAND =================
@bot.on_message(filters.command("stock") & filters.private)
async def stock_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    # Aggregate stock details by country and type
    pipeline = [
        {"$match": {"sold": False}},
        {
            "$group": {
                "_id": {
                    "country": "$country_name",
                    "code": "$country_code",
                    "type": {"$ifNull": ["$type", "fresh"]}
                },
                "count": {"$sum": 1}
            }
        },
        {"$sort": {"_id.country": 1}}
    ]
    
    stock_data = list(accounts_col.aggregate(pipeline))
    total_unsold = accounts_col.count_documents({"sold": False})
    total_sold = accounts_col.count_documents({"sold": True})

    if not stock_data:
        await message.reply_text("📊 **STOCK STATUS**\n\n❌ Database me abhi koi unsold account nahi hai!")
        return

    # Country-wise stats gather karna
    formatted_stock = {}
    for item in stock_data:
        c_name = item["_id"]["country"]
        c_code = item["_id"]["code"]
        # Database me type lower/upper kisi me bhi ho, standard UPPERCASE me read karo
        acc_type = str(item["_id"]["type"]).upper()
        count = item["count"]

        key = f"{c_name} ({c_code})"
        if key not in formatted_stock:
            formatted_stock[key] = {"FRESH": 0, "SPAM": 0}
        
        formatted_stock[key][acc_type] = count

    # Response text create karna
    text = "📊 **OWNER STOCK SUMMARY**\n\n"
    text += f"🟢 **Total Unsold Stock:** `{total_unsold}`\n"
    text += f"🔴 **Total Sold Out:** `{total_sold}`\n"
    text += "────────────────────\n\n"

    for country_info, types in formatted_stock.items():
        fresh_cnt = types.get("FRESH", 0)
        spam_cnt = types.get("SPAM", 0)
        text += f"🌍 **{country_info}**\n"
        text += f"   ├── 🌱 Fresh: `{fresh_cnt}`\n"
        text += f"   └── 🔥 Spam: `{spam_cnt}`\n\n"

    await message.reply_text(text)

# ================= BROADCAST HANDLER =================
@bot.on_message(filters.command(["broadcast", "gcast"]) & filters.user(OWNER_IDS))
async def broadcast_cmd(client: Client, message: Message):
    target_msg = message.reply_to_message if message.reply_to_message else message

    if target_msg == message and len(message.command) < 2:
        await message.reply_text(
            "⚠️ **Broadcast Command Usage:**\n\n"
            "1. Kisi bhi message (Photo, Video, Text, Sticker) ko **reply** karke likhein: `/broadcast`\n"
            "2. Direct message ke liye likhein: `/broadcast Yahan apna text likhein`"
        )
        return

    status_msg = await message.reply_text("📢 **Broadcast Process Started...**")

    all_users = list(users_col.find({}, {"user_id": 1}))
    all_chats = list(chats_col.find({}, {"chat_id": 1}))

    targets = list(set([u["user_id"] for u in all_users if "user_id" in u] + [c["chat_id"] for c in all_chats if "chat_id" in c]))

    success = 0
    failed = 0

    for chat_id in targets:
        try:
            if message.reply_to_message:
                await target_msg.copy(chat_id)
            else:
                broadcast_text = message.text.split(maxsplit=1)[1]
                await client.send_message(chat_id, broadcast_text)
            
            success += 1
            await asyncio.sleep(0.05)

        except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
            users_col.delete_one({"user_id": chat_id})
            chats_col.delete_one({"chat_id": chat_id})
            failed += 1

        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ **Broadcast Completed Successfully!**\n\n"
        f"🟢 **Delivered:** `{success}`\n"
        f"🔴 **Failed/Blocked:** `{failed}`\n"
        f"📊 **Total Attempted:** `{len(targets)}`"
    )

# ================= CALLBACK HANDLER =================
@bot.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id)
    state = get_state(user_id)
    data = callback.data

    is_owner = (int(user_id) in OWNER_IDS)

    if data == "check_force_join":
        if await is_user_joined(client, user_id):
            await callback.answer("✅ Verification successful! Welcome to the bot.", show_alert=True)
            
            # Bot details & Dynamic Name fetch
            bot_info = await client.get_me()
            bot_name = bot_info.first_name.upper()
            ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"

            stylish_welcome_text = (
                f"╔══════════════════════════╗\n"
                f"║   💎 **WELCOME TO** 💎   ║\n"
                f"║   **{bot_name}**          \n"
                f"╚══════════════════════════╝\n\n"
                f"👋 **Welcome, {callback.from_user.first_name}!**\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💎 **REFER & EARN PROGRAM** 💎\n"
                f"├ 🎁 **Bonus Rate:** `2% Instant Cashback`\n"
                f"├ 💸 **Kaise Kamayein:** Jab bhi aapka dost wallet me deposit karega, 2% bonus seedhe aapke wallet me add ho jayega!\n"
                f"└ 📊 **Unlimited Earnings:** Jitna refer karoge, utna zyada profit!\n\n"
                f"🔗 **Aapka Referral Link:**\n"
                f"`{ref_link}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

            await callback.message.edit_text(
                text=stylish_welcome_text,
                disable_web_page_preview=True
            )
            # Main keyboard reply me bhejien
            await callback.message.reply_text(
                "Select an option from below:",
                reply_markup=main_keyboard()
            )
        else:
            # Refresh Force Join Keyboard
            keyboard = await build_force_join_keyboard()
            await callback.answer("❌ Aapne abhi tak saare channels/groups join nahi kiye hain!", show_alert=True)
            await callback.message.edit_text(
                f"👋 **Welcome, {callback.from_user.first_name}!**\n\n"
                "⚠️ **Bot access karne ke liye pehle hamare Channels/Groups ko join karein:**",
                reply_markup=keyboard
            )
        return

    elif data == "profile":
        bot_info = await client.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"
        username_str = f"@{callback.from_user.username}" if callback.from_user.username else "N/A"
    
        profile_text = (
            f"👤 **USER PROFILE**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📛 **Username:** {username_str}\n"
            f"💰 **Wallet Balance:** ₹{user.get('wallet', 0.0):.2f}\n"
            f"📅 **Start Date:** `{user.get('start_date', 'N/A')}`\n\n"
            f"🔗 **Your Referral Link:**\n`{ref_link}`\n\n"
            f"🎁 *Har referral deposit ka 2% aapke wallet me auto-credit hoga!*"
        )
        await callback.message.edit_text(
            profile_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        )

    elif data == "stats":
        bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})
    
        stats_text = (
            f"📊 **YOUR ACCOUNT STATS**\n\n"
            f"🛒 **Total Accounts Bought:** {bought_count}\n"
            f"💸 **Total Amount Spent:** ₹{user.get('total_spent', 0.0):.2f}\n"
            f"💵 **Total Amount Deposited:** ₹{user.get('total_deposited', 0.0):.2f}\n"
            f"🎁 **Referral Earnings (2%):** ₹{user.get('referral_profit', 0.0):.2f}"
        )
    
        stats_buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Referral Log", callback_data="ref_log"), InlineKeyboardButton("🛒 Purchase Log", callback_data="purchase_log")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await callback.message.edit_text(stats_text, reply_markup=stats_buttons)

    elif data == "ref_log":
        referred_users = list(users_col.find({"referred_by": user_id}))
        text = f"👥 **REFERRAL LOG** (Total: {len(referred_users)})\n\n"
        if referred_users:
            for idx, ref in enumerate(referred_users, 1):
                text += f"{idx}. User ID: `{ref['user_id']}` | Joined: `{ref.get('start_date', 'N/A')}`\n"
        else:
            text += "Aapne abhi tak kisi ko refer nahi kiya hai."
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="stats")]])
        )

    elif data == "purchase_log":
        purchased_accs = list(accounts_col.find({"buyer_id": user_id, "sold": True}))
        text = f"🛒 **PURCHASE LOG** (Total: {len(purchased_accs)})\n\n"
        if purchased_accs:
            for idx, acc in enumerate(purchased_accs, 1):
                masked_num = mask_phone_number(acc['phone'], acc.get('country_code'))
                text += f"{idx}. Phone: `{masked_num}` | Price: ₹{acc['price']}\n"
        else:
            text += "Aapne abhi tak koi account buy nahi kiya hai."
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="stats")]])
        )

    elif data == "support":
        support_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/Innocent_vaibhav")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await callback.message.edit_text(
            "🛠️ **Customer Support**\n\n For any issues, contact Owner by clicking the button below:",
            reply_markup=support_keyboard
        )

    elif data == "back_main":
        await cleanup_user_login(user_id)
        state["state"] = None
        await callback.message.edit_text(
            "Select an option from below:",
            reply_markup=main_keyboard()
        )

    elif data == "add_acc":
        if not is_owner:
            await callback.answer("❌ Only the Owner can add accounts!", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔥 Spam Account", callback_data="add_type_spam"),
                InlineKeyboardButton("🌱 Fresh Account", callback_data="add_type_fresh")
            ]
        ])
        await callback.message.reply_text("🌍 **[Owner]** Select Account Type:", reply_markup=keyboard)

    elif data in ["add_type_spam", "add_type_fresh"]:
        if not is_owner:
            return
        acc_type = "spam" if "spam" in data else "fresh"
        state["temp_acc_type"] = acc_type
        state["state"] = "awaiting_country_name"
        await callback.message.edit_text(f"🌍 **[Owner - {acc_type.upper()}]** Enter Country Name (e.g. India, USA):")

    elif data == "buy_acc":
        await cleanup_user_login(user_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔥 Spam Accounts", callback_data="buy_type_spam"),
                InlineKeyboardButton("🌱 Fresh Accounts", callback_data="buy_type_fresh")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await callback.message.edit_text("🛒 **Select Account Category:**", reply_markup=keyboard)

    elif data in ["buy_type_spam", "buy_type_fresh"]:
        await cleanup_user_login(user_id)  # Yeh line yahan add kar dein
        selected_type = "spam" if "spam" in data else "fresh"
        countries = get_available_countries(selected_type)
        if not countries:
            await callback.answer(f"❌ No {selected_type.upper()} accounts in stock right now!", show_alert=True)
            return

        btn_list = []
        for c in countries:
            c_name, c_count, c_code = c["_id"], c["count"], c["code"]
            
            # Flag emoji dynamic fetch logic
            flag = get_flag_emoji(c_name)
            
            btn_list.append([
                InlineKeyboardButton(
                    f"{flag} {c_name} ({c_code}) | Stock: {c_count}", 
                    callback_data=f"select_country_{selected_type}_{c_name}"
                )
            ])
        
        btn_list.append([InlineKeyboardButton("🔙 Back to Category", callback_data="buy_acc")])
        await callback.message.edit_text(f"🛒 **Select Country ({selected_type.upper()}):**", reply_markup=InlineKeyboardMarkup(btn_list))

    elif data.startswith("select_country_"):
        await cleanup_user_login(user_id)  # Yeh line bhi yahan add kar dein
        parts = data.split("_")
        selected_type = parts[2]
        selected_country = "_".join(parts[3:])

        type_filter = [
            {"type": selected_type},
            {"type": {"$exists": False}} if selected_type == "fresh" else {"_id": None}
        ]

        available_accs = list(accounts_col.find({
            "country_name": selected_country, 
            "sold": False,
            "$or": type_filter
        }))

        if not available_accs:
            await callback.answer("❌ No accounts left in this category!", show_alert=True)
            return

        btn_list = []
        for acc in available_accs:
            phone = acc["phone"]
            price = acc["price"]
            masked_num = mask_phone_number(phone, acc.get('country_code'))
            btn_list.append([InlineKeyboardButton(f"📱 {masked_num} - ₹{price}", callback_data=f"buy_acc_{phone}")])
        
        btn_list.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="buy_acc")])
        await callback.message.edit_text(f"🛒 **Available {selected_type.upper()} Accounts for {selected_country}:**", reply_markup=InlineKeyboardMarkup(btn_list))

    elif data == "deposit":
        state["state"] = "awaiting_deposit_amount"
        await callback.message.reply_text("💵 Enter the amount you want to deposit (e.g., 100):")

    elif data == "deposit_cancel":
        state["state"] = None
        state["deposit_amount"] = 0.0
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("❌ Deposit cancelled successfully.", show_alert=True)
        return

    elif data == "deposit_yes_paid":
        amount = state.get("deposit_amount", 0.0)
        state["state"] = "awaiting_deposit_ss" # Ab user SS bhej sakta hai
        
        # Scanner message delete karke naya message bhejna jiske sath Cancel button ho
        try:
            await callback.message.delete()
        except Exception:
            pass

        instruction_text = (
            f"📸 **Send Payment Screenshot (SS)**\n\n"
            f"💰 **Amount:** ₹{amount}\n"
            f"📌 **UPI ID:** `{UPI_ID}`\n\n"
            f"Kripya apne payment ka successful screenshot yahin chat me send karein."
        )

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="deposit_cancel")]
        ])

        await callback.message.reply_text(
            text=instruction_text,
            reply_markup=cancel_keyboard
        )
        await callback.answer()
        return

    elif data.startswith("do_auto_verify_"):
        parts = data.split("_")
        amount = float(parts[3])
        state = get_state(user_id)

        photo_file_id = state.get("pending_photo_id")
        qr_gen_time = state.get("qr_generated_at") # QR generate hone ka time

        if not photo_file_id:
            await callback.answer("❌ Session expired ya photo nahi mili! Kripya dobara deposit try karein.", show_alert=True)
            return

        msg_wait = await callback.message.edit_text("🔍 Bot screenshot verify kar raha hai, kripya intezaar karein...")

        # Photo download karke AI se details extract karna
        photo_bytes_io = await client.download_media(photo_file_id, in_memory=True)
        photo_bytes = photo_bytes_io.getvalue()

        details = await extract_ss_details(photo_bytes)

        upi_txn_id = details.get("upi_txn_id")
        google_txn_id = details.get("google_txn_id")
        ss_date = details.get("date")
        ss_time = details.get("time")
        ss_amount = details.get("amount")
        ss_receiver = details.get("receiver")
        is_valid = details.get("is_valid_ss")

        # 1. Validity Check
        if not is_valid or (not upi_txn_id and not google_txn_id) or not ss_date or not ss_time:
            state["state"] = "awaiting_deposit_ss"
            await msg_wait.edit_text(
                "❌ **Galt ya Adhoora Screenshot!**\n\n"
                "Transaction ID, Date ya Time saaf nahi dikh raha hai.\n\n"
                "👉 **Kripya apna real aur saaf payment screenshot yahin dobara send karein:**"
            )
            return

        # 2. Amount Match Check
        if ss_amount is not None and float(ss_amount) != float(amount):
            state["state"] = "awaiting_deposit_ss"
            await msg_wait.edit_text(
                f"❌ **Amount Mismatch!** Aapne ₹{amount} ki request dali thi par SS me ₹{ss_amount} hai.\n\n"
                "👉 **Kripya sahi amount ka real screenshot dobara send karein:**"
            )
            return

        # 3. Time Validation Check (Scanner generate hone ke baad ka hona chahiye)
        if qr_gen_time and ss_date and ss_time:
            ss_dt = parse_ss_datetime(ss_date, ss_time)
            if ss_dt and ss_dt < qr_gen_time:
                state["state"] = "awaiting_deposit_ss"
                await msg_wait.edit_text(
                    "❌ **Invalid Screenshot Time!** Yeh screenshot scanner aane se pehle ka hai (Purana SS).\n\n"
                    "👉 **Kripya naya payment karke uska fresh screenshot bhejien:**"
                )
                return

        # 4. Receiver UPI ID Match Check (Optional/Strict check agar receiver extract ho)
        if ss_receiver and UPI_ID:
            clean_ss_recv = re.sub(r'[\s@]', '', str(ss_receiver)).lower()
            clean_bot_upi = re.sub(r'[\s@]', '', str(UPI_ID)).lower()
            if clean_bot_upi not in clean_ss_recv and clean_ss_recv not in clean_bot_upi:
                # Agar receiver match nahi hota toh warning ya manual verify par bhej sakte hain
                print(f"Warning: SS Receiver '{ss_receiver}' did not strictly match Bot UPI '{UPI_ID}'")

        # 5. Duplicate Transaction Check
        existing_txn = db["deposits"].find_one({
            "$or": [
                {"upi_txn_id": upi_txn_id} if upi_txn_id else {"upi_txn_id": "NONE"},
                {"google_txn_id": google_txn_id} if google_txn_id else {"google_txn_id": "NONE"}
            ]
        })
        if existing_txn:
            state["state"] = "awaiting_deposit_ss"
            await msg_wait.edit_text(
                "❌ **Duplicate Transaction ID!** Yeh UTR pehle hi use ho chuki hai.\n\n"
                "👉 **Kripya naya screenshot dobara send karein:**"
            )
            return

        # Sabhi checks pass hone ke baad database me save karke balance add kar do
        db["deposits"].insert_one({
            "user_id": user_id,
            "upi_txn_id": upi_txn_id,
            "google_txn_id": google_txn_id,
            "amount": amount,
            "date": ss_date,
            "time": ss_time,
            "status": "approved",
            "approved_by": "Automatic Bot Click"
        })

        users_col.update_one(
            {"user_id": user_id},
            {"$inc": {"wallet": amount, "total_deposited": amount}}
        )

        # Referral Commission (2%)
        target_user = users_col.find_one({"user_id": user_id})
        referrer_id = target_user.get("referred_by") if target_user else None
        if referrer_id:
            commission = (amount * 2) / 100
            users_col.update_one({"user_id": int(referrer_id)}, {"$inc": {"wallet": commission, "referral_profit": commission}})
            try:
                await bot.send_message(referrer_id, f"🎉 Referral commission received! Referred user deposited ₹{amount}, you got ₹{commission:.2f} bonus.")
            except Exception:
                pass

        # Customer ko success message
        success_text = (
            f"🎉 **Deposit Auto-Approved Successfully!**\n\n"
            f"💰 **Amount Added:** ₹{amount}\n"
            f"🔢 **Txn ID:** `{upi_txn_id or google_txn_id}`\n"
            f"✨ Aapka payment successfully verify karke wallet me add kar diya gaya hai!"
        )
        await msg_wait.edit_text(success_text)

        # Log groups par message bhejna
        group_msg = (
            f"🎉 **Payment Approved Successful (Auto)!**\n\n"
            f"👤 **User:** {callback.from_user.mention}\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Amount Added:** ₹{amount}\n"
            f"🔢 **Txn ID / UTR:** `{upi_txn_id or google_txn_id}`\n"
            f"🤖 **Approved By:** Automatic Bot"
        )
        try:
            await bot.send_message(PAYMENT_LOG_GROUP, group_msg)
        except Exception as e:
            print(f"Payment group log err: {e}")

        try:
            await bot.send_message(OTP_GROUP_ID, group_msg)
        except Exception as e:
            print(f"OTP group log err: {e}")
            

    elif data.startswith("do_manual_verify_"):
        parts = data.split("_")
        amount = float(parts[3])
        state = get_state(user_id)
        photo_file_id = state.get("pending_photo_id")

        if not photo_file_id:
            await callback.answer("❌ Session expired! Kripya dobara try karein.", show_alert=True)
            return

        msg_wait = await callback.message.edit_text("⏳ Processing manual verification request...")

        # Photo download karke AI se details extract karna taaki UTR, Date, Time mil sake
        photo_bytes_io = await client.download_media(photo_file_id, in_memory=True)
        photo_bytes = photo_bytes_io.getvalue()

        details = await extract_ss_details(photo_bytes)
        upi_txn_id = details.get("upi_txn_id") or "N/A"
        google_txn_id = details.get("google_txn_id") or "N/A"
        ss_date = details.get("date") or "N/A"
        ss_time = details.get("time") or "N/A"

        # Pending status ke sath database me save karna taaki baad me approve/reject me UTR mil sake
        db["deposits"].update_one(
            {"user_id": user_id, "amount": amount, "status": "pending"},
            {
                "$set": {
                    "user_id": user_id,
                    "upi_txn_id": upi_txn_id if upi_txn_id != "N/A" else None,
                    "google_txn_id": google_txn_id if google_txn_id != "N/A" else None,
                    "amount": amount,
                    "date": ss_date,
                    "time": ss_time,
                    "status": "pending"
                }
            },
            upsert=True
        )

        await msg_wait.edit_text(
            "⏳ **Manual Request Submitted!**\n\n"
            "Aapka screenshot saari details ke sath admin ke paas bhej diya gaya hai. Kripya thoda intezaar karein."
        )

        # Admin group me poori details (UTR, Date, Time) aur Approve/Reject buttons ke sath bhej na
        username = f"@{callback.from_user.username}" if callback.from_user.username else "N/A"
        admin_caption = (
            f"💵 **New Manual Deposit Request**\n\n"
            f"👤 **User:** {callback.from_user.mention}\n"
            f"📛 **Username:** {username}\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"💰 **Amount:** ₹{amount}\n"
            f"🔢 **UPI Txn ID:** `{upi_txn_id}`\n"
            f"🌐 **Google Txn ID:** `{google_txn_id}`\n"
            f"📅 **Txn Date:** `{ss_date}`\n"
            f"⏰ **Txn Time:** `{ss_time}`"
        )

        try:
            await bot.send_photo(
                chat_id=PAYMENT_LOG_GROUP,
                photo=photo_file_id,
                caption=admin_caption,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_dep_{user_id}_{amount}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_dep_{user_id}_{amount}")
                    ]
                ])
            )
        except Exception as e:
            print(f"Admin group send err: {e}")

    elif data.startswith("buy_acc_"):
        target_phone = data.replace("buy_acc_", "")

        # 🔒 RACE CONDITION CHECK: Same phone already kisi aur ne select kiya?
        already_busy = any(
            s.get("account", {}).get("phone") == target_phone
            for s in login_states.values()
        )
        if already_busy or target_phone in _busy_phones:
            await callback.answer("⚠️ Yeh account abhi kisi aur user ne select kiya hai. Kuch der baad try karein.", show_alert=True)
            return
        
        await cleanup_user_login(user_id)

        acc = accounts_col.find_one({"phone": target_phone, "sold": False})

        if not acc:
            await callback.answer("❌ Account no longer available!", show_alert=True)
            return

        if not is_owner and float(user.get("wallet", 0)) < float(acc["price"]):
            await callback.answer("❌ Insufficient Wallet Balance! Please Deposit money first.", show_alert=True)
            return

        # 🔒 Mark as busy immediately (atomic-ish)
        _busy_phones.add(target_phone)
        session_name = f"sessions/{target_phone.replace('+', '').replace(' ', '')}"
        lock = _lock_for(session_name)

        # Agar session file pe koi aur kaam kar raha hai, wait karo
        if lock.locked():
            _busy_phones.discard(target_phone)
            await callback.answer("⏳ Session busy hai, thodi der me try karein.", show_alert=True)
            return

        async with lock:
            user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH)
            try:
                if user_client.is_connected:
                    await user_client.disconnect()
                await user_client.connect()
            except Exception as e:
                _busy_phones.discard(target_phone)
                await callback.answer(f"❌ Session connection error: {e}", show_alert=True)
                return

        login_states[user_id] = {
            "account": acc,
            "client": user_client,
            "purchase_time": datetime.now().timestamp(),
            "is_logged_in": False
        }

        owner_tag = " [👑 OWNER FREE ACCESS]" if is_owner else ""

        await callback.message.edit_text(
            f"✅ **Account Selected!**{owner_tag}\n\n"
            f"📞 **Phone Number:** `{acc['phone']}`\n\n"
            f"👉 Login now in your Telegram app using this phone number.\n"
            f"When code arrives, press the button below to fetch OTP.\n"
            f"⏳ *Note: Aapke paas login karne ke liye 5 minute ka time hai.*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Request OTP / 2FA Code", callback_data="fetch_otp")],
                [InlineKeyboardButton("🔙 Back to Countries", callback_data="buy_acc")]
            ])
        )

    elif data == "fetch_otp":
        if user_id not in login_states:
            await callback.answer("❌ Active session nahi mila. Kripya firse account select karein.", show_alert=True)
            return

        session_data = login_states[user_id]
        purchase_time = session_data.get("purchase_time", 0)
        
        # 1. 5 Minutes (300 seconds) Expiry Logic
        # 1. 5 Minutes (300 seconds) Expiry Logic
        if datetime.now().timestamp() - purchase_time > 300:
            await cleanup_user_login(user_id)
            await callback.answer("⏰ Time limit expired (5 mins)! Number stock me wapas chala gaya.", show_alert=True)
            await callback.message.edit_text("⏰ **Session Expired!** Aapne 5 minute ke andar login nahi kiya, isliye account stock me wapas bhej diya gaya hai.")
            return

        acc = session_data["account"]
        user_client: Client = session_data["client"]

        # Ensure active connection
        try:
            if not user_client.is_connected:
                await user_client.connect()
        except Exception as conn_err:
            await callback.answer(f"❌ Session re-connection failed: {conn_err}", show_alert=True)
            return

        try:
            # 2. Fetch latest messages from official Telegram Service Notifications (777000)
            otp_found = False
            async for msg in user_client.get_chat_history(777000, limit=5):
                if not msg.text:
                    continue
                    
                msg_ts = msg.date.timestamp() if msg.date else 0

                if msg_ts < purchase_time:
                    continue

                otp_code = extract_otp(msg.text)

                if otp_code != "NOT-FOUND":
                    otp_found = True
                    # 3. Mark as SOLD and deduct balance ONLY when OTP is delivered
                    if not session_data.get("is_logged_in", False):
                        if not is_owner:
                            new_wallet = float(user.get("wallet", 0)) - float(acc["price"])
                            users_col.update_one(
                                {"user_id": int(user_id)}, 
                                {
                                    "$set": {"wallet": new_wallet},
                                    "$inc": {"total_spent": float(acc["price"])}
                                }
                            )

                        accounts_col.update_one({"phone": acc["phone"]}, {"$set": {"sold": True, "buyer_id": int(user_id)}})
                        session_data["is_logged_in"] = True

                    two_fa = acc.get("2fa_password") or "None"
                    masked_num = mask_phone_number(acc["phone"], acc.get('country_code'))

                    await callback.message.reply_text(
                        f"📩 **Login Verification Code:**\n\n"
                        f"📱 **Phone:** `{masked_num}`\n"
                        f"🔑 **OTP:** `{otp_code}`\n"
                        f"🔐 **2FA Password:** `{two_fa}`",
                        reply_markup=logout_keyboard()
                    )
                    break  # Stop checking further messages once found

            if not otp_found:
                await callback.answer("❌ Naya OTP nahi mila! Kripya Telegram app me 'Send Code' par click karein.", show_alert=True)

        except Exception as e:
            print(f"OTP Fetch Error: {e}")
            await callback.answer("Error fetching OTP. Make sure you pressed 'Send Code' on Telegram app.", show_alert=True)
            
    elif data == "customer_logout":
        if user_id in login_states:
            session_data = login_states[user_id]
            
            if not session_data.get("is_logged_in", False):
                await callback.answer("❌ Aapne abhi tak new login verify nahi kiya hai!", show_alert=True)
                return

            acc = session_data["account"]
            user_client = session_data["client"]
            masked_num = mask_phone_number(acc['phone'], acc.get('country_code'))
            
            # User ki details aur mention fetch karna
            user_mention = callback.from_user.mention
            username_str = f"@{callback.from_user.username}" if callback.from_user.username else "N/A"

            total_bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})

            # User ka referral link generate karna
            bot_info = await client.get_me()
            ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"

            group_msg = (
                f"🎉 **New Purchase Successful**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"📱 **Account Number:** `{masked_num}`\n"
                f"📊 **Total Accounts Bought:** `{total_bought_count}`\n\n"
                f"✨ **Thanks for purchase!**\n"
                f"🔗 **Referral Link:** `{ref_link}`"
            )

            try:
                await bot.send_message(OTP_GROUP_ID, group_msg, disable_web_page_preview=True)
            except Exception as ge:
                print(f"Failed to send to OTP Group: {ge}")

            # Saare owners ke paas notification bhejne ke liye loop
            for owner_id in OWNER_IDS:
                try:
                    await bot.send_message(
                        owner_id,
                        f"🔔 **Session Ended / Logout Alert**\n\n"
                        f"👤 **User:** {user_mention}\n"
                        f"📛 **Username:** {username_str}\n"
                        f"🆔 **Customer ID:** `{user_id}`\n"
                        f"📱 **Phone:** `{acc['phone']}`\n"
                        f"Customer clicked logout from bot."
                    )
                except Exception as oe:
                    print(f"Failed to send alert to owner {owner_id}: {oe}")

            try:
                if user_client.is_connected:
                    await user_client.log_out()
            except Exception:
                pass

            del login_states[user_id]
            await callback.message.edit_text("✅ You have logged out successfully from the session.")
        else:
            await callback.answer("No active session to logout.", show_alert=True)

    elif data.startswith("approve_dep_") or data.startswith("reject_dep_"):
        parts = data.split("_")
        action = parts[0]
        target_user_id = int(parts[2])
        amount = float(parts[3])

        # User aur deposit details database se nikalna taaki UTR mil sake
        target_user = users_col.find_one({"user_id": target_user_id})
        deposit_record = db["deposits"].find_one({"user_id": target_user_id, "amount": amount, "status": "pending"})
        
        # UTR / Transaction ID fetch karna (UPI ya Google Txn ID me se jo bhi ho)
        txn_id = "N/A"
        if deposit_record:
            txn_id = deposit_record.get("upi_txn_id") or deposit_record.get("google_txn_id") or "N/A"

        # User ka telegram mention ya name banane ke liye
        try:
            target_user_obj = await client.get_users(target_user_id)
            user_mention = target_user_obj.mention
            username_str = f"@{target_user_obj.username}" if target_user_obj.username else "N/A"
        except Exception:
            user_mention = f"User (`{target_user_id}`)"
            username_str = "N/A"

        if action == "approve":
            # Status update deposit record me
            db["deposits"].update_one(
                {"user_id": target_user_id, "amount": amount, "status": "pending"},
                {"$set": {"status": "approved"}}
            )

            users_col.update_one(
                {"user_id": target_user_id},
                {
                    "$inc": {
                        "wallet": amount,
                        "total_deposited": amount
                    }
                }
            )
    
            referrer_id = target_user.get("referred_by") if target_user else None
    
            if referrer_id:
                commission = (amount * 2) / 100
        
                users_col.update_one(
                    {"user_id": int(referrer_id)},
                    {
                        "$inc": {
                            "wallet": commission,
                            "referral_profit": commission
                        }
                    }
                )
        
                try:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 **Referral Commission Received!**\n\n"
                        f"Your referred user ({user_mention}) has deposited ₹{amount}.\n"
                        f"💰 **You have received a 2% (₹{commission:.2f}) bonus in your wallet!**"
                    )
                except Exception:
                    pass

            # Admin Group message update with full details
            log_group_msg = (
                f"✅ **Deposit Approved Successfully**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Approved Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"👑 **Approved By:** {callback.from_user.mention}"
            )
            await callback.message.edit_text(log_group_msg)

            # User ke paas jane wala notification message
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

            group_approve_msg = (
                f"🎉 **Payment Approved Successful!**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Amount Added:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"👑 **Approved By:** {callback.from_user.mention}"
            )
            
            # 1. Payment Verification Group par bhejna
            try:
                await bot.send_message(PAYMENT_LOG_GROUP, group_approve_msg)
            except Exception as e:
                print(f"Failed to send approval to payment group: {e}")

            # 2. Purchase Successful / OTP Group par bhi bhejna
            try:
                await bot.send_message(OTP_GROUP_ID, group_approve_msg)
            except Exception as e:
                print(f"Failed to send approval to OTP group: {e}")

        else:
            # Status update deposit record me (Rejected)
            db["deposits"].update_one(
                {"user_id": target_user_id, "amount": amount, "status": "pending"},
                {"$set": {"status": "rejected"}}
            )

            log_group_msg = (
                f"❌ **Deposit Rejected**\n\n"
                f"👤 **User:** {user_mention}\n"
                f"📛 **Username:** {username_str}\n"
                f"🆔 **User ID:** `{target_user_id}`\n"
                f"💰 **Rejected Amount:** ₹{amount}\n"
                f"🔢 **Txn ID / UTR:** `{txn_id}`\n"
                f"👑 **Rejected By:** {callback.from_user.mention}"
            )
            await callback.message.edit_text(log_group_msg)

            # User ka state wapas active karna taaki wo real SS dobara bhej sake
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
                f"👑 **Rejected By:** {callback.from_user.mention}"
            )
            
            # 1. Payment Verification Group par bhejna
            try:
                await bot.send_message(PAYMENT_LOG_GROUP, group_reject_msg)
            except Exception as e:
                print(f"Failed to send rejection to payment group: {e}")

            # 2. Purchase Successful / OTP Group par bhi bhejna
            try:
                await bot.send_message(OTP_GROUP_ID, group_reject_msg)
            except Exception as e:
                print(f"Failed to send rejection to OTP group: {e}")
                
# ================= MESSAGE HANDLER =================
@bot.on_message(filters.text & ~filters.command(["start", "add", "addbulk", "broadcast", "gcast"]))
async def handle_text(client: Client, message: Message):
    user_id = message.from_user.id
    text = message.text
    state = get_state(user_id)
    user = get_user(user_id)
    is_owner = (int(user_id) in OWNER_IDS)

    if text == "💳 Deposit Money":
        state["state"] = "awaiting_deposit_amount"
        await message.reply_text("💵 Enter the amount you want to deposit (e.g., 100):")
        return

    elif text == "🛒 Buy Account":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔥 Spam Accounts", callback_data="buy_type_spam"),
                InlineKeyboardButton("🌱 Fresh Accounts", callback_data="buy_type_fresh")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await message.reply_text("🛒 **Select Account Category:**", reply_markup=keyboard)
        return
    
    elif text == "👤 Profile":
        bot_info = await client.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"
        username_str = f"@{message.from_user.username}" if message.from_user.username else "N/A"
        user_mention = message.from_user.mention

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
        await message.reply_text(profile_text)
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
        stats_buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Referral Log", callback_data="ref_log"), InlineKeyboardButton("🛒 Purchase Log", callback_data="purchase_log")]
        ])
        await message.reply_text(stats_text, reply_markup=stats_buttons)
        return

    elif text == "🛠️ Support":
        support_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/Innocent_vaibhav")]
        ])
        await message.reply_text(
            "🛠️ **Customer Support**\n\nFor any issues, contact Owner by clicking the button below:",
            reply_markup=support_keyboard
        )
        return

    # User States
    if state and state.get("state") == "awaiting_bulk_otp":
        otp = text.strip().replace(" ", "")
        acc_client = state["temp_client"]
        curr = state["current_acc"]
        phone = curr["phone"]

        try:
            if not acc_client.is_connected:
                await acc_client.connect()

            await acc_client.sign_in(phone, state["temp_code_hash"], otp)
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {
                    "phone": phone, "price": curr["price"],
                    "country_name": curr["c_name"], "country_code": curr["c_code"],
                    "type": curr.get("type", "fresh"),
                    "sold": False, "2fa_password": curr["password"]
                }},
                upsert=True
            )
            await acc_client.disconnect()

            await message.reply_text(f"✅ `{phone}` Logged in & Added Successfully!")
            state["completed"] = state.get("completed", 0) + 1
            state["state"] = None
            await process_next_bulk_account(client, user_id, message)

        except SessionPasswordNeeded:
            if curr["password"]:
                try:
                    await acc_client.check_password(curr["password"])
                    accounts_col.update_one(
                        {"phone": phone},
                        {"$set": {
                            "phone": phone, "price": curr["price"],
                            "country_name": curr["c_name"], "country_code": curr["c_code"],
                            "type": curr.get("type", "fresh"),
                            "sold": False, "2fa_password": curr["password"]
                        }},
                        upsert=True
                    )
                    await acc_client.disconnect()
                    await message.reply_text(f"✅ `{phone}` (with 2FA) Logged in & Saved!")
                    state["completed"] = state.get("completed", 0) + 1
                    state["state"] = None
                    await process_next_bulk_account(client, user_id, message)
                except Exception as p_err:
                    await acc_client.disconnect()
                    await message.reply_text(f"❌ Incorrect 2FA Password for `{phone}`: {str(p_err)}")
                    state["state"] = None
                    await process_next_bulk_account(client, user_id, message)
            else:
                await acc_client.disconnect()
                await message.reply_text(f"❌ 2FA Password Required for `{phone}` but none provided!")
                state["state"] = None
                await process_next_bulk_account(client, user_id, message)

        except Exception as e:
            await acc_client.disconnect()
            await message.reply_text(f"❌ Failed to sign in `{phone}`: {str(e)}")
            state["state"] = None
            await process_next_bulk_account(client, user_id, message)
        return

    if state["state"] == "awaiting_country_name" and is_owner:
        state["temp_country_name"] = text.strip()
        state["state"] = "awaiting_country_code"
        await message.reply_text("📞 Enter Country Code (e.g. +91, +1, +62):")
        return

    elif state["state"] == "awaiting_country_code" and is_owner:
        state["temp_country_code"] = text.strip()
        state["state"] = "awaiting_acc_phone"
        await message.reply_text(f"📱 Enter full phone number with country code (e.g. {state['temp_country_code']}9876543210):")
        return

    if state["state"] == "awaiting_acc_phone" and is_owner:
        phone = text.strip()
        state["temp_phone"] = phone
        state["state"] = "awaiting_acc_price"
        await message.reply_text(f"Phone `{phone}` saved. Now enter selling PRICE for this account (e.g. 50):")

    elif state["state"] == "awaiting_acc_price" and is_owner:
        try:
            price = float(text.strip())
        except ValueError:
            await message.reply_text("❌ Invalid price! Enter numbers only:")
            return

        phone = state["temp_phone"]

        existing = accounts_col.find_one({"phone": phone, "status": {"$ne": "logged_out"}})
        if existing:
            await message.reply_text(
                f"⚠️ `{phone}` already DB me hai (Type: {existing.get('type','fresh')}).\n"
                f"Pehle `/logout {phone}` karo."
            )
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
        acc_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, phone_number=phone)

        try:
            await acc_client.connect()
            sent_code = await acc_client.send_code(phone)

            state["temp_client"] = acc_client
            state["temp_code_hash"] = sent_code.phone_code_hash
            state["temp_price"] = price
            state["state"] = "awaiting_acc_otp"

            await message.reply_text(f"📲 OTP sent to `{phone}`. Enter the OTP code received on that account:")
        except Exception as e:
            await cleanup_session(session_name, acc_client)
            state["state"] = None
            await message.reply_text(f"❌ Connection Error: `{e}`\n\nPlease try again.")

    elif state["state"] == "awaiting_acc_otp" and is_owner:
        otp = text.strip().replace(" ", "")
        acc_client = state["temp_client"]
        phone = state["temp_phone"]

        try:
            await acc_client.sign_in(phone, state["temp_code_hash"], otp)
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {
                    "phone": phone, "price": state["temp_price"],
                    "country_name": state.get("temp_country_name", "Unknown"),
                    "country_code": state.get("temp_country_code", "+"),
                    "type": state.get("temp_acc_type", "fresh"),
                    "sold": False, "2fa_password": None
                }},
                upsert=True
            )
            await acc_client.disconnect()
            state["state"] = None
            await message.reply_text(f"✅ Account `{phone}` logged in & saved to MongoDB stock successfully!")
            
        except SessionPasswordNeeded:
            state["state"] = "awaiting_acc_2fa"
            await message.reply_text("🔑 Account is protected with 2FA Password. Enter the 2FA Password:")
        except Exception as e:
            await message.reply_text(f"❌ Login failed: `{e}`\nTry entering OTP again:")

    elif state["state"] == "awaiting_acc_2fa" and is_owner:
        password = text.strip()
        acc_client = state["temp_client"]
        phone = state["temp_phone"]

        try:
            await acc_client.check_password(password)
            accounts_col.update_one(
                {"phone": phone},
                {"$set": {
                    "phone": phone, "price": state["temp_price"],
                    "country_name": state.get("temp_country_name", "Unknown"),
                    "country_code": state.get("temp_country_code", "+"),
                    "type": state.get("temp_acc_type", "fresh"),
                    "sold": False, "2fa_password": password
                }},
                upsert=True
            )
            await acc_client.disconnect()
            state["state"] = None
            await message.reply_text(f"✅ Account `{phone}` with 2FA added to MongoDB stock successfully!")
        except PasswordHashInvalid:
            await message.reply_text("❌ Wrong 2FA Password! Try again:")

    elif state["state"] == "awaiting_deposit_amount":
        try:
            amount = float(text.strip())
            if amount <= 0:
                await message.reply_text("❌ Amount must be greater than 10!")
                return

            # Minimum deposit limit (e.g., ₹20 ya jo bhi aap chahein)
            # Agar amount 20 ya usse kam hogi, toh error aayega (20 par bhi scanner nahi banega)
            if amount <= 10:
                await message.reply_text(
                    "❌ Minimum deposit must be greater than **₹10**!\n"
                )
                return

            state["deposit_amount"] = amount
            state["qr_generated_at"] = datetime.now()
            state["state"] = "awaiting_deposit_ss"

            upi_link = f"upi://pay?pa={UPI_ID}&pn=Store&am={amount}&cu=INR"
            qr_img = qrcode.make(upi_link)
            bio = BytesIO()
            bio.name = 'qr.png'
            qr_img.save(bio, 'PNG')
            bio.seek(0)

            # Scanner ke sath details aur Inline Buttons
            caption_msg = (
                f"💳 **Payment Details:**\n\n"
                f"💰 **Amount:** ₹{amount}\n"
                f"🔢 **Ref No / Note:** Deposit of ₹{amount}\n\n"
                f"📌 **UPI ID:** `{UPI_ID}`\n\n"
                f"1. Scan QR Code.\n"
                f"2. Pay ₹{amount} **\n"
                f"3. ✅Click yes paid.\n"
                f"4. Send SS of payment here.\n\n"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes Paid", callback_data="deposit_yes_paid"),
                    InlineKeyboardButton("❌ Cancel", callback_data="deposit_cancel")
                ]
            ])

            await message.reply_photo(
                photo=bio,
                caption=caption_msg,
                reply_markup=keyboard
            )
        except ValueError:
            await message.reply_text("❌ Invalid amount. Enter a valid number (e.g., 100):")

@bot.on_message(filters.photo)
async def handle_photo(client: Client, message: Message):
    user_id = message.from_user.id
    state = get_state(user_id)

    if state.get("state") == "awaiting_deposit_ss":
        amount = state["deposit_amount"]
        
        # State me file_id aur amount temporary save kar lete hain taaki baad me verify ho sake
        state["state"] = None
        state["pending_photo_id"] = message.photo.file_id
        state["pending_amount"] = amount

        customer_text = (
            f"📸 **Payment Screenshot Received!**\n\n"
            f"💰 **Amount:** ₹{amount}\n\n"
            f"👉 Aap apna deposit kaise verify karwana chahte hain? Niche diye gaye option me se select karein:"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🤖 Automatic Verify & Approve", callback_data=f"do_auto_verify_{amount}")
            ],
            [
                InlineKeyboardButton("👤 Manual Verify (Admin Check)", callback_data=f"do_manual_verify_{amount}")
            ]
        ])

        await message.reply_text(customer_text, reply_markup=keyboard)

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

if __name__ == "__main__":
    print("Bot started with MongoDB persistence...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    bot.run()
