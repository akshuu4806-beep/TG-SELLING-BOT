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

bot = Client("StoreBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# Gemini API Keys (Environment variable se fetch hongi)
raw_api_keys = os.getenv("API_KEYS", "")
API_KEYS = [k.strip() for k in raw_api_keys.split(",") if k.strip()]

if not API_KEYS:
    raise ValueError("❌ Error: API_KEYS environment variable is missing!")

current_key_index = 0
ai_client = genai.Client(api_key=API_KEYS[current_key_index])

# ================= SESSION LOCK MANAGER =================
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
    
    models_to_try = ['gemini-3.8-flash', 'gemini-1.5-flash']

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
                
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "NOT_FOUND" in err_msg:
                    current_key_index = (current_key_index + 1) % len(API_KEYS)
                    ai_client = genai.Client(api_key=API_KEYS[current_key_index])
                    print(f"🔄 Switching to next Gemini API Key (Index: {current_key_index})")
                    break  
                
                if "503" in err_msg or "UNAVAILABLE" in err_msg:
                    await asyncio.sleep(1.5)
                    continue
                break

    return {"is_valid_ss": False, "upi_txn_id": None, "google_txn_id": None, "date": None, "time": None, "amount": None, "receiver": None}

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
    """Pending login session ko clear karta hai aur phone number ko unbusy karta hai."""
    if user_id in login_states:
        session_data = login_states.pop(user_id, None)
        if session_data:
            acc = session_data.get("account", {})
            target_phone = acc.get("phone")
            if target_phone:
                _busy_phones.discard(target_phone)  
            
            client_obj = session_data.get("client")
            if client_obj:
                try:
                    if client_obj.is_connected:
                        await client_obj.disconnect()
                except Exception:
                    pass

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
    config = settings_col.find_one({"type": "force_join"})
    if config and "channels" in config:
        return config["channels"]
    
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
            clean_ref = raw_ref.replace("REF", "").replace("4F886", "").strip()
            possible_ref = int(clean_ref)
            if possible_ref != user_id:
                referrer_id = possible_ref
        except ValueError:
            pass

    get_user(user_id, referrer_id)

    if not await is_user_joined(client, user_id):
        keyboard = await build_force_join_keyboard()
        await message.reply_text(
            f"👋 **Welcome {first_name}!**\n\n"
            "⚠️ **Bot access karne ke liye pehle hamare Channels/Groups ko join karein:**",
            reply_markup=keyboard
        )
        return

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

    session_str = acc.get("session_string")
    if session_str:
        temp_client = Client(name="logout_client", api_id=API_ID, api_hash=API_HASH, session_string=session_str, in_memory=True)
        try:
            await temp_client.connect()
            await temp_client.log_out()
        except Exception:
            pass
        finally:
            try:
                if temp_client.is_connected:
                    await temp_client.disconnect()
            except Exception:
                pass

    accounts_col.update_one(
        {"phone": target_phone}, 
        {"$set": {"sold": True, "status": "logged_out"}}
    )

    await status_msg.edit_text(f"✅ **Account `{target_phone}` logged out and removed from stock!**")

# Force Join Commands
@bot.on_message(filters.command("addfjoin") & filters.private)
async def add_fjoin_cmd(client: Client, message: Message):
    if int(message.from_user.id) not in OWNER_IDS:
        return

    input_text = message.text.split(maxsplit=1)
    if len(input_text) < 2 or "|" not in input_text[1]:
        await message.reply_text(
            "⚠️ **Format:**\n`/addfjoin @username_or_chatid | https://t.me/invite_link | Display Name`"
        )
        return

    parts = [p.strip() for p in input_text[1].split("|")]
    username = parts[0]
    link = parts[1]
    title = parts[2] if len(parts) > 2 else f"📢 Join Channel"

    config = settings_col.find_one({"type": "force_join"})
    channels = config.get("channels", []) if config else [
        {"username": CHANNEL_USERNAME, "link": CHANNEL_LINK, "title": "📢 Join Channel 1"},
        {"username": GROUP_USERNAME, "link": GROUP_LINK, "title": "💬 Join OTP Group"}
    ]

    if any(c["username"] == username for c in channels):
        await message.reply_text("⚠️ Yeh channel/group pehle se Force Join list me hai!")
        return

    channels.append({"username": username, "link": link, "title": title})
    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": channels}}, upsert=True)
    await message.reply_text(f"✅ **Force Join Added Successfully!**")

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

@bot.on_message(filters.command("clearfjoin") & filters.private)
async def clear_fjoin_cmd(client: Client, message: Message):
    if int(message.from_user.id) not in OWNER_IDS:
        return

    settings_col.update_one({"type": "force_join"}, {"$set": {"channels": []}}, upsert=True)
    await message.reply_text("🧹 **Saare Force Join Channels/Groups clear kar diye gaye hain!**")

@bot.on_message(filters.command("resetdb") & filters.private)
async def reset_database_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

    try:
        users_col.delete_many({})
        accounts_col.delete_many({})
        chats_col.delete_many({})
        user_states.clear()
        login_states.clear()
        await message.reply_text("🧹 **MongoDB Successfully Reset!**")
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

    old_client = state.get("temp_client")
    if old_client:
        try:
            if old_client.is_connected:
                await old_client.disconnect()
        except Exception:
            pass

    acc_client = Client(name=f"bulk_{phone}", api_id=API_ID, api_hash=API_HASH, in_memory=True)

    existing = accounts_col.find_one({"phone": phone, "status": {"$ne": "logged_out"}})
    if existing:
        await message_or_cb.reply_text(f"⚠️ `{phone}` already DB me hai. Pehle `/logout {phone}` karo.")
        state["state"] = None
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
            f"📩 **Enter OTP for `{phone}`:**"
        )
        if hasattr(message_or_cb, "reply_text"):
            await message_or_cb.reply_text(status_text)
        else:
            await message_or_cb.message.reply_text(status_text)

    except Exception as e:
        try:
            if acc_client.is_connected:
                await acc_client.disconnect()
        except Exception:
            pass
        err_text = f"❌ Error for `{phone}`: {str(e)}\nSkipping to next..."
        if hasattr(message_or_cb, "reply_text"):
            await message_or_cb.reply_text(err_text)
        else:
            await message_or_cb.message.reply_text(err_text)
        await process_next_bulk_account(client, user_id, message_or_cb)

@bot.on_message(filters.command("stock") & filters.private)
async def stock_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if int(user_id) not in OWNER_IDS:
        return

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

    formatted_stock = {}
    for item in stock_data:
        c_name = item["_id"]["country"]
        c_code = item["_id"]["code"]
        acc_type = str(item["_id"]["type"]).upper()
        count = item["count"]

        key = f"{c_name} ({c_code})"
        if key not in formatted_stock:
            formatted_stock[key] = {"FRESH": 0, "SPAM": 0}
        formatted_stock[key][acc_type] = count

    text = "📊 **OWNER STOCK SUMMARY**\n\n"
    text += f"🟢 **Total Unsold Stock:** `{total_unsold}`\n"
    text += f"🔴 **Total Sold Out:** `{total_sold}`\n────────────────────\n\n"

    for country_info, types in formatted_stock.items():
        fresh_cnt = types.get("FRESH", 0)
        spam_cnt = types.get("SPAM", 0)
        text += f"🌍 **{country_info}**\n   ├── 🌱 Fresh: `{fresh_cnt}`\n   └── 🔥 Spam: `{spam_cnt}`\n\n"

    await message.reply_text(text)

@bot.on_message(filters.command(["broadcast", "gcast"]) & filters.user(OWNER_IDS))
async def broadcast_cmd(client: Client, message: Message):
    target_msg = message.reply_to_message if message.reply_to_message else message

    if target_msg == message and len(message.command) < 2:
        await message.reply_text("⚠️ **Broadcast Usage:** Reply to a message or provide text.")
        return

    status_msg = await message.reply_text("📢 **Broadcast Process Started...**")

    all_users = list(users_col.find({}, {"user_id": 1}))
    all_chats = list(chats_col.find({}, {"chat_id": 1}))
    targets = list(set([u["user_id"] for u in all_users if "user_id" in u] + [c["chat_id"] for c in all_chats if "chat_id" in c]))

    success, failed = 0, 0
    for chat_id in targets:
        try:
            if message.reply_to_message:
                await target_msg.copy(chat_id)
            else:
                await client.send_message(chat_id, message.text.split(maxsplit=1)[1])
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(f"✅ **Broadcast Completed!**\n🟢 Delivered: `{success}`\n🔴 Failed: `{failed}`")

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
            await callback.answer("✅ Verification successful!", show_alert=True)
            bot_info = await client.get_me()
            ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"
            await callback.message.edit_text(f"👋 **Welcome!**\n\n🔗 Your Referral Link:\n`{ref_link}`", disable_web_page_preview=True)
            await callback.message.reply_text("Select an option:", reply_markup=main_keyboard())
        else:
            keyboard = await build_force_join_keyboard()
            await callback.answer("❌ Please join all channels/groups first!", show_alert=True)
            await callback.message.edit_text("⚠️ **Join channels first:**", reply_markup=keyboard)
        return

    elif data == "profile":
        bot_info = await client.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}4F886"
        username_str = f"@{callback.from_user.username}" if callback.from_user.username else "N/A"
        profile_text = (
            f"👤 **USER PROFILE**\n\n🆔 ID: `{user_id}`\n📛 Username: {username_str}\n"
            f"💰 Wallet: ₹{user.get('wallet', 0.0):.2f}\n🔗 Ref Link:\n`{ref_link}`"
        )
        await callback.message.edit_text(profile_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))

    elif data == "stats":
        bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})
        stats_text = (
            f"📊 **STATS**\n🛒 Bought: {bought_count}\n💸 Spent: ₹{user.get('total_spent', 0.0):.2f}\n"
            f"💵 Deposited: ₹{user.get('total_deposited', 0.0):.2f}\n🎁 Ref Earnings: ₹{user.get('referral_profit', 0.0):.2f}"
        )
        await callback.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Referral Log", callback_data="ref_log"), InlineKeyboardButton("🛒 Purchase Log", callback_data="purchase_log")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]))

    elif data == "ref_log":
        referred_users = list(users_col.find({"referred_by": user_id}))
        text = f"👥 **REFERRAL LOG** (Total: {len(referred_users)})\n\n"
        for idx, ref in enumerate(referred_users, 1):
            text += f"{idx}. ID: `{ref['user_id']}`\n"
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="stats")]]))

    elif data == "purchase_log":
        purchased_accs = list(accounts_col.find({"buyer_id": user_id, "sold": True}))
        text = f"🛒 **PURCHASE LOG** (Total: {len(purchased_accs)})\n\n"
        for idx, acc in enumerate(purchased_accs, 1):
            text += f"{idx}. Phone: `{mask_phone_number(acc['phone'], acc.get('country_code'))}` | ₹{acc['price']}\n"
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="stats")]]))

    elif data == "support":
        await callback.message.edit_text("🛠️ **Support**\nContact Owner:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Support", url="https://t.me/Innocent_vaibhav")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]))

    elif data == "back_main":
        await cleanup_user_login(user_id)
        state["state"] = None
        await callback.message.edit_text("Select an option:", reply_markup=main_keyboard())

    elif data == "buy_acc":
        await cleanup_user_login(user_id)
        await callback.message.edit_text("🛒 **Select Account Category:**", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 Spam Accounts", callback_data="buy_type_spam"), InlineKeyboardButton("🌱 Fresh Accounts", callback_data="buy_type_fresh")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]))

    elif data in ["buy_type_spam", "buy_type_fresh"]:
        await cleanup_user_login(user_id)
        selected_type = "spam" if "spam" in data else "fresh"
        countries = get_available_countries(selected_type)
        if not countries:
            await callback.answer(f"❌ No {selected_type.upper()} accounts available!", show_alert=True)
            return

        btn_list = [[InlineKeyboardButton(f"{get_flag_emoji(c['_id'])} {c['_id']} ({c['code']}) | Stock: {c['count']}", callback_data=f"select_country_{selected_type}_{c['_id']}")] for c in countries]
        btn_list.append([InlineKeyboardButton("🔙 Back", callback_data="buy_acc")])
        await callback.message.edit_text(f"🛒 **Select Country ({selected_type.upper()}):**", reply_markup=InlineKeyboardMarkup(btn_list))

    elif data.startswith("select_country_"):
        await cleanup_user_login(user_id)
        parts = data.split("_")
        selected_type = parts[2]
        selected_country = "_".join(parts[3:])

        type_filter = [{"type": selected_type}, {"type": {"$exists": False}} if selected_type == "fresh" else {"_id": None}]
        available_accs = list(accounts_col.find({"country_name": selected_country, "sold": False, "$or": type_filter}))

        if not available_accs:
            await callback.answer("❌ No accounts left!", show_alert=True)
            return

        btn_list = [[InlineKeyboardButton(f"📱 {mask_phone_number(acc['phone'], acc.get('country_code'))} - ₹{acc['price']}", callback_data=f"buy_acc_{acc['phone']}")] for acc in available_accs]
        btn_list.append([InlineKeyboardButton("🔙 Back", callback_data="buy_acc")])
        await callback.message.edit_text(f"🛒 **Available Accounts:**", reply_markup=InlineKeyboardMarkup(btn_list))

    elif data.startswith("buy_acc_"):
        target_phone = data.replace("buy_acc_", "")

        already_busy = any(s.get("account", {}).get("phone") == target_phone for s in login_states.values())
        if already_busy or target_phone in _busy_phones:
            await callback.answer("⚠️ Account already selected by someone else!", show_alert=True)
            return
        
        await cleanup_user_login(user_id)
        acc = accounts_col.find_one({"phone": target_phone, "sold": False})

        if not acc:
            await callback.answer("❌ Account no longer available!", show_alert=True)
            return

        if not is_owner and float(user.get("wallet", 0)) < float(acc["price"]):
            await callback.answer("❌ Insufficient Wallet Balance!", show_alert=True)
            return

        _busy_phones.add(target_phone)
        session_string = acc.get("session_string")

        if not session_string:
            _busy_phones.discard(target_phone)
            await callback.answer("❌ Session string missing for this account!", show_alert=True)
            return

        user_client = Client(name=f"client_{user_id}", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True)
        try:
            await user_client.connect()
        except Exception as e:
            _busy_phones.discard(target_phone)
            await callback.answer(f"❌ Connection error: {e}", show_alert=True)
            return

        login_states[user_id] = {
            "account": acc,
            "client": user_client,
            "purchase_time": datetime.now().timestamp(),
            "is_logged_in": False
        }

        await callback.message.edit_text(
            f"✅ **Account Selected!**\n\n📞 **Phone:** `{acc['phone']}`\n\n👉 Login in your Telegram app, then click below to fetch OTP.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Request OTP / 2FA Code", callback_data="fetch_otp")],
                [InlineKeyboardButton("🔙 Back", callback_data="buy_acc")]
            ])
        )

    elif data == "fetch_otp":
        if user_id not in login_states:
            await callback.answer("❌ Active session not found.", show_alert=True)
            return

        session_data = login_states[user_id]
        if datetime.now().timestamp() - session_data.get("purchase_time", 0) > 300:
            await cleanup_user_login(user_id)
            await callback.answer("⏰ Time expired (5 mins)!", show_alert=True)
            await callback.message.edit_text("⏰ **Session Expired!**")
            return

        acc = session_data["account"]
        user_client: Client = session_data["client"]

        try:
            if not user_client.is_connected:
                await user_client.connect()

            otp_found = False
            async for msg in user_client.get_chat_history(777000, limit=5):
                if not msg.text or (msg.date.timestamp() if msg.date else 0) < session_data.get("purchase_time", 0):
                    continue

                otp_code = extract_otp(msg.text)
                if otp_code != "NOT-FOUND":
                    otp_found = True
                    if not session_data.get("is_logged_in", False):
                        if not is_owner:
                            new_wallet = float(user.get("wallet", 0)) - float(acc["price"])
                            users_col.update_one({"user_id": int(user_id)}, {"$set": {"wallet": new_wallet}, "$inc": {"total_spent": float(acc["price"])}})

                        accounts_col.update_one({"phone": acc["phone"]}, {"$set": {"sold": True, "buyer_id": int(user_id)}})
                        session_data["is_logged_in"] = True

                    await callback.message.reply_text(
                        f"📩 **OTP:** `{otp_code}`\n🔐 **2FA:** `{acc.get('2fa_password') or 'None'}`",
                        reply_markup=logout_keyboard()
                    )
                    break

            if not otp_found:
                await callback.answer("❌ OTP not received yet!", show_alert=True)
        except Exception as e:
            await callback.answer(f"Error fetching OTP: {e}", show_alert=True)

    elif data == "customer_logout":
        if user_id in login_states:
            session_data = login_states[user_id]
            if not session_data.get("is_logged_in", False):
                await callback.answer("❌ Verify login first!", show_alert=True)
                return

            acc = session_data["account"]
            user_client = session_data["client"]
            try:
                if user_client.is_connected:
                    await user_client.log_out()
            except Exception:
                pass

            del login_states[user_id]
            await callback.message.edit_text("✅ Logged out successfully!")
        else:
            await callback.answer("No active session.", show_alert=True)

    elif data.startswith("approve_dep_") or data.startswith("reject_dep_"):
        parts = data.split("_")
        action = parts[0]
        target_user_id = int(parts[2])
        amount = float(parts[3])

        if action == "approve":
            db["deposits"].update_one({"user_id": target_user_id, "amount": amount, "status": "pending"}, {"$set": {"status": "approved"}})
            users_col.update_one({"user_id": target_user_id}, {"$inc": {"wallet": amount, "total_deposited": amount}})
            await callback.message.edit_text(f"✅ Approved ₹{amount} for user `{target_user_id}`")
            try:
                await bot.send_message(target_user_id, f"🎉 Your deposit of ₹{amount} has been approved!")
            except Exception:
                pass
        else:
            db["deposits"].update_one({"user_id": target_user_id, "amount": amount, "status": "pending"}, {"$set": {"status": "rejected"}})
            await callback.message.edit_text(f"❌ Rejected ₹{amount} for user `{target_user_id}`")

    elif data == "deposit_yes_paid":
        amount = state.get("deposit_amount", 0.0)
        state["state"] = "awaiting_deposit_ss"
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.reply_text("📸 Send payment screenshot here:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="deposit_cancel")]]))
        await callback.answer()

    elif data == "deposit_cancel":
        state["state"] = None
        state["deposit_amount"] = 0.0
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("❌ Cancelled.", show_alert=True)

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
        await message.reply_text("💵 Enter deposit amount (e.g., 100):")
        return

    elif text == "🛒 Buy Account":
        await message.reply_text("🛒 Select Account Category:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 Spam Accounts", callback_data="buy_type_spam"), InlineKeyboardButton("🌱 Fresh Accounts", callback_data="buy_type_fresh")]
        ]))
        return
    
    elif text == "👤 Profile":
        bot_info = await client.get_me()
        await message.reply_text(f"👤 ID: `{user_id}`\n💰 Wallet: ₹{user.get('wallet', 0.0):.2f}")
        return

    elif text == "📊 Stats":
        bought_count = accounts_col.count_documents({"buyer_id": user_id, "sold": True})
        await message.reply_text(f"📊 Bought: {bought_count}\nSpent: ₹{user.get('total_spent', 0.0):.2f}")
        return

    elif text == "🛠️ Support":
        await message.reply_text("🛠️ Contact Support:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support", url="https://t.me/Innocent_vaibhav")]]))
        return

    # Bulk OTP Handler
    if state and state.get("state") == "awaiting_bulk_otp":
        otp = text.strip().replace(" ", "")
        acc_client = state["temp_client"]
        curr = state["current_acc"]
        phone = curr["phone"]

        try:
            if not acc_client.is_connected:
                await acc_client.connect()

            await acc_client.sign_in(phone, state["temp_code_hash"], otp)
            session_str = await acc_client.export_session_string()

            accounts_col.update_one(
                {"phone": phone},
                {"$set": {
                    "phone": phone, "price": curr["price"],
                    "country_name": curr["c_name"], "country_code": curr["c_code"],
                    "type": curr.get("type", "fresh"),
                    "sold": False, "2fa_password": curr["password"],
                    "session_string": session_str
                }},
                upsert=True
            )
            await acc_client.disconnect()

            await message.reply_text(f"✅ `{phone}` Logged in & Added Successfully!")
            state["completed"] = state.get("completed", 0) + 1
            state["state"] = None
            await process_next_bulk_account(client, user_id, message)
        except Exception as e:
            await acc_client.disconnect()
            await message.reply_text(f"❌ Failed: {str(e)}")
            state["state"] = None
            await process_next_bulk_account(client, user_id, message)
        return

    if state["state"] == "awaiting_deposit_amount":
        try:
            amount = float(text.strip())
            if amount <= 10:
                await message.reply_text("❌ Minimum deposit must be greater than ₹10!")
                return
            state["deposit_amount"] = amount
            state["state"] = "awaiting_deposit_ss"

            upi_link = f"upi://pay?pa={UPI_ID}&pn=Store&am={amount}&cu=INR"
            qr_img = qrcode.make(upi_link)
            bio = BytesIO()
            bio.name = 'qr.png'
            qr_img.save(bio, 'PNG')
            bio.seek(0)

            await message.reply_photo(photo=bio, caption=f"Scan & Pay ₹{amount}\nUPI: `{UPI_ID}`", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes Paid", callback_data="deposit_yes_paid"), InlineKeyboardButton("❌ Cancel", callback_data="deposit_cancel")]
            ]))
        except ValueError:
            await message.reply_text("❌ Enter valid amount:")

@bot.on_message(filters.photo)
async def handle_photo(client: Client, message: Message):
    user_id = message.from_user.id
    state = get_state(user_id)

    if state.get("state") == "awaiting_deposit_ss":
        amount = state["deposit_amount"]
        state["state"] = None

        photo_bytes = await client.download_media(message.photo.file_id, in_memory=True)
        details = await extract_ss_details(photo_bytes.getvalue())

        upi_txn_id = details.get("upi_txn_id") or "N/A"
        db["deposits"].insert_one({"user_id": user_id, "amount": amount, "upi_txn_id": upi_txn_id, "status": "pending"})

        await bot.send_photo(
            chat_id=PAYMENT_LOG_GROUP,
            photo=message.photo.file_id,
            caption=f"💵 **Deposit Request**\nUser: {message.from_user.mention}\nAmount: ₹{amount}\nTxn ID: `{upi_txn_id}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"approve_dep_{user_id}_{amount}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_dep_{user_id}_{amount}")]
            ])
        )
        await message.reply_text("⏳ Screenshot sent to admin for verification.")

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
    print("Bot started with MongoDB Session String persistence...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    bot.run()
