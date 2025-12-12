import os

import json

import asyncio

import logging

from datetime import datetime, timedelta, timezone

from typing import List, Optional



# Third-party imports

from fastapi import FastAPI, Request, HTTPException

from fastapi.responses import JSONResponse

import uvicorn

import stripe

from dotenv import load_dotenv

from supabase import create_client, Client

from groq import AsyncGroq

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

import httpx



# Load Environment Variables

load_dotenv()



# --- CONFIGURATION ---

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")

SUPABASE_KEY = os.getenv("SUPABASE_KEY")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")

RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")



# Limits

FREE_MSG_LIMIT = 10

FREE_IMG_LIMIT = 3



# Setup Clients

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

stripe.api_key = STRIPE_KEY

app = FastAPI()



# Logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

logger = logging.getLogger(__name__)



# --- DATABASE HELPERS ---



async def get_or_create_user(user_id, username, first_name):

data = supabase.table("users").select("*").eq("telegram_id", user_id).execute()

if not data.data:

new_user = {

"telegram_id": user_id,

"username": username,

"first_name": first_name,

"daily_msg_count": 0,

"daily_img_count": 0

}

supabase.table("users").insert(new_user).execute()

return new_user


# Check Daily Reset

user = data.data[0]

last_reset = datetime.fromisoformat(user['last_reset_time'].replace('Z', '+00:00'))

if datetime.now(timezone.utc) - last_reset > timedelta(hours=24):

supabase.table("users").update({

"daily_msg_count": 0,

"daily_img_count": 0,

"last_reset_time": datetime.now(timezone.utc).isoformat()

}).eq("telegram_id", user_id).execute()

user['daily_msg_count'] = 0

user['daily_img_count'] = 0

return user



async def get_active_session(user_id):

data = supabase.table("active_sessions").select("*, characters(*)").eq("user_id", user_id).execute()

return data.data[0] if data.data else None



async def update_chat_history(user_id, role, content):

session = await get_active_session(user_id)

if not session: return


history = session['chat_history']

history.append({"role": role, "content": content})


# Keep history manageable (last 20 messages)

if len(history) > 20: history = history[-20:]


supabase.table("active_sessions").update({"chat_history": history}).eq("user_id", user_id).execute()

return history



# --- AI & IMAGE LOGIC ---



async def generate_response(history, system_prompt, style):

messages = [{"role": "system", "content": f"{system_prompt}. Style: {style}"}] + history

try:

chat_completion = await groq_client.chat.completions.create(

messages=messages,

model="llama-3.3-70b-versatile", # High quality, fast

temperature=0.7,

max_tokens=300,

)

return chat_completion.choices[0].message.content

except Exception as e:

logger.error(f"Groq Error: {e}")

return "I'm having a little trouble thinking right now, darling..."



async def generate_image(prompt, style, lora_key):

# This sends a request to RunPod ComfyUI

if not RUNPOD_ENDPOINT_ID or not RUNPOD_API_KEY:

return None # Skip if not configured


url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync"


# Simplified ComfyUI Payload

payload = {

"input": {

"prompt": f"{style} style, {prompt}, masterpiece, best quality",

"lora": lora_key

}

}


headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}


async with httpx.AsyncClient() as client:

try:

resp = await client.post(url, json=payload, headers=headers, timeout=60)

data = resp.json()

# Assuming RunPod returns an image URL in output['output']['images'][0]

if 'output' in data and 'images' in data['output']:

return data['output']['images'][0]

except Exception as e:

logger.error(f"Image Gen Error: {e}")

return None



# --- TELEGRAM HANDLERS ---



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

user = update.effective_user

await get_or_create_user(user.id, user.username, user.first_name)


keyboard = [

[InlineKeyboardButton("Choose Character 👩", callback_data="menu_chars")],

[InlineKeyboardButton("My Checkpoints 💾", callback_data="menu_checkpoints")],

[InlineKeyboardButton("Upgrade to Premium 💎", callback_data="menu_premium")]

]

await update.message.reply_text(

f"Hi {user.first_name}! I'm your AI companion. Choose a character to start!",

reply_markup=InlineKeyboardMarkup(keyboard)

)



async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

user_id = update.effective_user.id

text = update.message.text


user_data = await get_or_create_user(user_id, update.effective_user.username, update.effective_user.first_name)

session = await get_active_session(user_id)


# Check if session exists

if not session:

await update.message.reply_text("Please select a character first with /start")

return



# Check Limits (if not premium)

if not user_data['is_premium']:

if user_data['daily_msg_count'] >= FREE_MSG_LIMIT:

# FIX: Added Upgrade Button here

keyboard = [[InlineKeyboardButton("💎 Upgrade Now", callback_data="menu_premium")]]

await update.message.reply_text(

"Daily limit reached! 💎 You need more energy to continue.",

reply_markup=InlineKeyboardMarkup(keyboard)

)

return



# Save User Msg

await update_chat_history(user_id, "user", text)


# Generate AI Response

char = session['characters']

history = session['chat_history']


await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")


# FIX: Force short, seductive responses with emojis

system_instruction = (

f"{char['system_prompt']}. "

f"Current Style: {session['current_style']}. "

"IMPORTANT: Keep your reply SHORT (under 2 sentences). "

"Be seductive, flirty, and use emojis like 😘, 😉, 🔥."

)


response_text = await generate_response(history, system_instruction, session['current_style'])


# Save Assistant Msg

await update_chat_history(user_id, "assistant", response_text)


# FIX: Send Text Response FIRST (so it feels instant)

await update.message.reply_text(response_text)



# Increment Counters

new_msg_count = user_data['daily_msg_count'] + 1

session_counter = session['msg_counter'] + 1


supabase.table("users").update({"daily_msg_count": new_msg_count}).eq("telegram_id", user_id).execute()


# Image Generation Logic (Runs in background now)

if session_counter >= 3:

session_counter = 0 # Reset

if user_data['is_premium'] or user_data['daily_img_count'] < FREE_IMG_LIMIT:

await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")

# This might take a few seconds, but the user already has their text reply!

img_url = await generate_image(response_text, session['current_style'], char['image_lora_key'])

if img_url:

await update.message.reply_photo(img_url)

supabase.table("users").update({"daily_img_count": user_data['daily_img_count'] + 1}).eq("telegram_id", user_id).execute()


supabase.table("active_sessions").update({"msg_counter": session_counter}).eq("user_id", user_id).execute()



async def create_checkpoint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

user_id = update.effective_user.id

session = await get_active_session(user_id)

if not session:

await update.message.reply_text("No active chat to save.")

return


name = f"Save {datetime.now().strftime('%Y-%m-%d %H:%M')}"


data = {

"user_id": user_id,

"character_id": session['character_id'],

"checkpoint_name": name,

"chat_history": session['chat_history'],

"current_style": session['current_style']

}

supabase.table("checkpoints").insert(data).execute()

await update.message.reply_text(f"✅ Game Saved: {name}")



# --- CALLBACK QUERY HANDLER (Menus) ---



async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

query = update.callback_query

await query.answer() # This stops the button from "loading" forever

data = query.data

user_id = query.from_user.id


# --- 1. CHARACTER MENU ---

if data == "menu_chars":

chars = supabase.table("characters").select("*").execute().data

keyboard = []

for c in chars:

# Check if user is premium or if char is free

is_premium_user = supabase.table("users").select("is_premium").eq("telegram_id", user_id).execute().data[0]['is_premium']

lock = "🔒" if (not c['is_free'] and not is_premium_user) else "✨"

keyboard.append([InlineKeyboardButton(f"{c['name']} {lock}", callback_data=f"select_char_{c['id']}")])


await query.edit_message_text("Pick your date for tonight: 😘", reply_markup=InlineKeyboardMarkup(keyboard))


# --- 2. SELECT CHARACTER ---

elif data.startswith("select_char_"):

char_id = data.split("_")[-1]


# Check if locked

char_data = supabase.table("characters").select("*").eq("id", char_id).execute().data[0]

user_data = supabase.table("users").select("is_premium").eq("telegram_id", user_id).execute().data[0]


if not char_data['is_free'] and not user_data['is_premium']:

await query.message.reply_text("🔒 That character is for Premium users only! Upgrade to chat with her.")

return



# Start Session

supabase.table("active_sessions").delete().eq("user_id", user_id).execute()

supabase.table("active_sessions").insert({

"user_id": user_id,

"character_id": char_id,

"current_style": "Realistic"

}).execute()


await query.edit_message_text(f"I'm ready for you... say hello to {char_data['name']}. 😉")



# --- 3. CHECKPOINT MENU ---

elif data == "menu_checkpoints":

# Fetch saves from DB

saves = supabase.table("checkpoints").select("*").eq("user_id", user_id).order("created_at", desc=True).execute().data


if not saves:

await query.edit_message_text("🚫 No saved games found.\nUse /checkpoint while chatting to save!")

return


keyboard = []

for save in saves:

# Create a button for each save file

btn_text = f"📂 {save['checkpoint_name']} ({save['current_style']})"

keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"restore_{save['id']}")])


await query.edit_message_text("Select a save file to load:", reply_markup=InlineKeyboardMarkup(keyboard))



# --- 4. RESTORE CHECKPOINT ---

elif data.startswith("restore_"):

checkpoint_id = data.split("_")[1]

save_data = supabase.table("checkpoints").select("*").eq("id", checkpoint_id).execute().data[0]


# Restore into Active Session

supabase.table("active_sessions").delete().eq("user_id", user_id).execute()

supabase.table("active_sessions").insert({

"user_id": user_id,

"character_id": save_data['character_id'],

"current_style": save_data['current_style'],

"chat_history": save_data['chat_history']

}).execute()


await query.edit_message_text(f"✅ Memory Loaded: {save_data['checkpoint_name']}\nContinue where you left off!")



# --- 5. PREMIUM MENU ---

elif data == "menu_premium":

# This sends the Stripe Payment Link

# IMPORTANT: Replace the url below with your real Stripe Payment Link

keyboard = [[InlineKeyboardButton("💳 Click to Pay $9.99", url="https://buy.stripe.com/test_12345")]]


await query.message.reply_text(

"💎 **Premium Access**\n\n"

"🔥 Unlimited Messages\n"

"📸 Unlimited Photos\n"

"🔓 Unlock Raven (Goth Girl)\n\n"

"Click below to upgrade:",

reply_markup=InlineKeyboardMarkup(keyboard),

parse_mode="Markdown"

)



# --- FASTAPI SERVER (Webhooks) ---



@app.post("/stripe_webhook")

async def stripe_webhook(request: Request):

payload = await request.body()

sig_header = request.headers.get('STRIPE_SIGNATURE')


try:

event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

except Exception as e:

raise HTTPException(status_code=400, detail=str(e))


if event['type'] == 'checkout.session.completed':

session = event['data']['object']

# Metadata should contain telegram_id

tg_id = session.get('metadata', {}).get('telegram_id')

if tg_id:

supabase.table("users").update({"is_premium": True}).eq("telegram_id", tg_id).execute()


return {"status": "success"}



@app.get("/")

def health_check():

return {"status": "online", "service": "Girlfriend Bot"}



# --- MAIN ENTRY POINT ---



async def run_bot():

"""Runs the Telegram Bot"""

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

app.add_handler(CommandHandler("checkpoint", create_checkpoint_command))

app.add_handler(CallbackQueryHandler(button_handler))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


await app.initialize()

await app.start()

await app.updater.start_polling() # Polling is easiest for non-server setups


# Keep running until cancelled

try:

while True:

await asyncio.sleep(3600)

except asyncio.CancelledError:

await app.updater.stop()

await app.stop()



# Combine FastAPI and Bot

if __name__ == "__main__":

# Create event loop

loop = asyncio.new_event_loop()

asyncio.set_event_loop(loop)


# Start Bot in background

loop.create_task(run_bot())


# Start FastAPI Server

config = uvicorn.Config(app=app, host="0.0.0.0", port=8000, loop=loop)

server = uvicorn.Server(config)

loop.run_until_complete(server.serve())
