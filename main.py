from flask import Flask
import threading
import os
import re
import time
import requests
import asyncio
from dotenv import load_dotenv
load_dotenv()

from google import genai
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
GROUP_ID = -1004453681781

# --- True Trade API - از Environment رندر خونده میشه، اینجا چیزی ننویس ---
TRUE_TRADE_KEY = os.environ.get("TRUE_TRADE_API_KEY", "")
TRUE_TRADE_SECRET = os.environ.get("TRUE_TRADE_SECRET", "")
TRUE_TRADE_BASE = os.environ.get("TRUE_TRADE_BASE_URL", "https://thetruetrade.io")

client = genai.Client(api_key=GEMINI_KEY)

pending_signal = {}
waiting_edit = set()
waiting_group_edit = set()
last_group_msg_id = None

# --- ذخیره سیگنال های فعال برای چک قیمت ---
active_signals = {}

FOOTER = """

---
⚠️ WARNING / هشدار / ПРЕДУПРЕЖДЕНИЕ

🇮🇷 حد ضرر تمام معاملات باز شما نباید بیشتر از 3% باشد لطفا در انتخاب اهرم دقت فرمایید

🇬🇧 The stop-loss of all your open trades should not be more than 3%. Please be careful when choosing leverage.

🇷🇺 Стоп-лосс всех ваших открытых сделок не должен быть больше 3%. Пожалуйста, будьте осторожны при выборе кредитного плеча.
"""

PROMPT = """You are a trading signal assistant. Convert this input to a clean signal in ENGLISH ONLY.

Format EXACTLY like this - no emojis, no extra lines:

DOGE LONG

Entry : 0.18
TP1 = 0.182
TP2 = 0.183
TP3 = 0.184
SL = 0.17

Rules:
- First line: PAIR + SPACE + LONG or SHORT. Example: DOGE LONG, BTC SHORT
- Second line empty, third line: Entry : NUMBER or Entry : By Market
- Then TP1 = ..., TP2 = ... - EXACTLY same number of TPs as source. Never invent TPs.
- Then SL = ...
- If source says By Market / market / لحظه ای, write Entry : By Market
- Output MUST be 100% ENGLISH. Translate: فروش = SHORT, خرید / لانگ / لنگ = LONG, ورود = Entry, حد سود = TP, حد ضرر = SL
- Keep numbers exactly as in the source.
- Do not add emojis or Timeframe.
"""

# --- فیلتر ضد فارسی ---
def strip_persian(text):
    if not text:
        return text
    text = re.sub(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+', '', text)
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if line == "":
            lines.append(line)
            continue
        lines.append(line)
    cleaned = []
    for l in lines:
        if l == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(l)
    return '\n'.join(cleaned).strip()

# --- تبدیل اعداد فارسی به انگلیسی ---
def to_en_num(s):
    fa = "۰۱۲۳۴۵۶۷۸۹"
    ar = "٠١٢٣٤٥٦٧٨٩"
    for i, ch in enumerate(fa):
        s = s.replace(ch, str(i))
    for i, ch in enumerate(ar):
        s = s.replace(ch, str(i))
    s = s.replace("٫", ".").replace("٬", "").replace("،", "")
    return s

# --- تبدیل سریع سیگنال فارسی به انگلیسی، بدون جمینای ---
def parse_farsi_signal(text):
    try:
        text = to_en_num(text)
        # پیدا کردن ارز - حتی بدون کلمه ارز
        m_pair = re.search(r'(?:ارز|Pair|جفت ارز)\s*:\s*([A-Z0-9/]+)', text, re.I)
        if m_pair:
            pair = m_pair.group(1).strip().upper().replace("/USDT", "").replace("/USD", "")
        else:
            m2 = re.search(r'\b(BTC|ETH|SOL|XRP|DOGE|ADA|AVAX|LINK|TON|NOT|XAU|GOLD|BNB|TRX|DOT)\b', text, re.I)
            if not m2:
                return None
            pair = m2.group(1).upper()

        # جهت - Lang = Long
        direction = "LONG"
        if re.search(r'(SHORT|شورت|فروش|سل|SELL)', text, re.I):
            direction = "SHORT"
        elif re.search(r'(LONG|LANG|لانگ|لنگ|خرید|BUY)', text, re.I):
            direction = "LONG"

        # ورود - عدد یا By Market
        entry = None
        if re.search(r'(By Market|MARKET|مارکت|لحظه|بازار|فعلی)', text, re.I):
            entry = "By Market"
        else:
            m_entry = re.search(r'(?:ورود|Entry|خرید)\s*[:\-–=]?\s*([0-9]+\.[0-9]+|[0-9]+)', text)
            if m_entry:
                entry = m_entry.group(1)

        # تارگت ها - همه فرمت ها
        tps = []
        for m in re.finditer(r'(?:حد\s*سود|تارگت|هدف|TP|Take Profit)\s*(?:اول|دوم|سوم|چهارم|پنجم|ششم|\d+)?\s*[:\-–=]?\s*([0-9]+\.[0-9]+|[0-9]+)', text, re.I):
            v = m.group(1)
            if v not in tps and v != entry:
                tps.append(v)
        # اگه همه TP ها توی یه خط با / بودن
        if len(tps) <= 1:
            m_line = re.search(r'(?:حد\s*سود|تارگت).*?[:\-–=]\s*([0-9.,\s/]+)', text, re.I)
            if m_line and "/" in m_line.group(1):
                tps = [x.strip() for x in m_line.group(1).split("/") if x.strip()]

        m_sl = re.search(r'(?:حد\s*ضرر|استاپ|SL|Stop Loss)\s*[:\-–=]?\s*([0-9]+\.[0-9]+|[0-9]+)', text)
        sl = m_sl.group(1) if m_sl else None

        if not entry or not tps:
            return None

        # خروجی دقیقا به فرمتی که خواستی
        lines = [f"{pair} {direction}", "", f"Entry : {entry}"]
        for i, tp in enumerate(tps, 1):
            lines.append(f"TP{i} = {tp}")
        if sl:
            lines.append(f"SL = {sl}")
        return "\n".join(lines)
    except Exception as e:
        print("farsi parse error:", e)
        return None

# ========== True Trade Price Checker ==========

def parse_signal(text):
    try:
        # فرمت جدید: DOGE LONG
        m_pair = re.search(r'^([A-Z0-9]+)\s+(LONG|SHORT)', text, re.M | re.I)
        if not m_pair:
            m_pair2 = re.search(r"Pair:\s*([A-Z0-9/]+)", text)
            if not m_pair2:
                return None
            pair = m_pair2.group(1).replace("/", "").strip()
            m_dir = re.search(r"Direction:\s*(LONG|SHORT)", text, re.I)
            direction = m_dir.group(1).upper() if m_dir else "LONG"
        else:
            pair = m_pair.group(1).upper()
            direction = m_pair.group(2).upper()

        if pair in ["BTC", "ETH", "SOL", "XRP", "DOGE", "XAU", "GOLD"]:
            pair = pair + "USDT" if pair != "XAU" and pair != "GOLD" else "XAUUSD"

        def get_num(pattern):
            m = re.search(pattern, text, re.I)
            if not m:
                return None
            v = m.group(1).replace(",", "").strip()
            if v.lower() == "by market":
                return None
            try:
                return float(v)
            except:
                return None

        entry = get_num(r"Entry\s*:\s*([0-9.,]+|By Market)")
        sl = get_num(r"SL\s*[:=]\s*([0-9.,]+)")

        tps = []
        for m in re.finditer(r"TP(\d+)\s*[:=]\s*([0-9.,]+)", text, re.I):
            tps.append(float(m.group(2).replace(",", "")))
        tps = sorted(tps) if direction == "LONG" else sorted(tps, reverse=True)

        return {
            "pair": pair,
            "direction": direction,
            "entry": entry,
            "tps": tps,
            "sl": sl,
            "hit": set()
        }
    except Exception as e:
        print("parse error:", e)
        return None

def get_price_from_true_trade(pair):
    urls = [
        f"{TRUE_TRADE_BASE}/api/v1/ticker/price?symbol={pair}",
        f"{TRUE_TRADE_BASE}/api/v1/ticker?symbol={pair}",
        f"{TRUE_TRADE_BASE}/api/ticker/price?symbol={pair}",
        f"{TRUE_TRADE_BASE}/api/v1/market/ticker?symbol={pair}",
        f"{TRUE_TRADE_BASE}/api/v1/public/ticker?symbol={pair}",
    ]
    headers = {}
    if TRUE_TRADE_KEY:
        headers["X-API-KEY"] = TRUE_TRADE_KEY
        headers["Authorization"] = f"Bearer {TRUE_TRADE_KEY}"
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                j = r.json()
                for k in ["price", "last", "lastPrice", "close"]:
                    if isinstance(j, dict) and k in j and j[k]:
                        return float(j[k])
                if isinstance(j, dict) and "data" in j and isinstance(j["data"], dict):
                    for k in ["price", "last", "lastPrice", "close"]:
                        if k in j["data"] and j["data"][k]:
                            return float(j["data"][k])
                if isinstance(j, dict) and "ticker" in j and isinstance(j["ticker"], dict):
                    for k in ["price", "last", "lastPrice"]:
                        if k in j["ticker"]:
                            return float(j["ticker"][k])
        except Exception as e:
            continue
    return None

async def check_signals_loop(app):
    await asyncio.sleep(10)
    print("Price checker started...")
    while True:
        for msg_id, sig in list(active_signals.items()):
            price = get_price_from_true_trade(sig["pair"])
            if not price:
                continue
            print(f"Checking {sig['pair']} price: {price} TPs: {sig['tps']} SL: {sig['sl']}")
            msg = ""
            if sig["direction"] == "LONG":
                for i, tp in enumerate(sig["tps"], 1):
                    if price >= tp and f"TP{i}" not in sig["hit"]:
                        sig["hit"].add(f"TP{i}")
                        msg = f"✅ TP{i} Hit!\n💰 {sig['pair']} | Price: {price}\n🎯 TP{i}: {tp}"
                if sig["sl"] and price <= sig["sl"] and "SL" not in sig["hit"]:
                    sig["hit"].add("SL")
                    msg = f"❌ SL Hit!\n💰 {sig['pair']} | Price: {price}\n🛑 SL: {sig['sl']}"
            else:
                for i, tp in enumerate(sig["tps"], 1):
                    if price <= tp and f"TP{i}" not in sig["hit"]:
                        sig["hit"].add(f"TP{i}")
                        msg = f"✅ TP{i} Hit!\n💰 {sig['pair']} | Price: {price}\n🎯 TP{i}: {tp}"
                if sig["sl"] and price >= sig["sl"] and "SL" not in sig["hit"]:
                    sig["hit"].add("SL")
                    msg = f"❌ SL Hit!\n💰 {sig['pair']} | Price: {price}\n🛑 SL: {sig['sl']}"
            if msg:
                try:
                    await app.bot.send_message(chat_id=GROUP_ID, text=msg)
                except Exception as e:
                    print("Send hit error:", e)
            if "SL" in sig["hit"] or (len(sig["tps"]) > 0 and f"TP{len(sig['tps'])}" in sig["hit"]):
                active_signals.pop(msg_id, None)
        await asyncio.sleep(30)

# ========== Bot Handlers ==========

async def start(update, context):
    await update.message.reply_text("Hi! Send me the signal to forward to the group ✅\n\nCommands:\n/edit - Edit last message sent to group")

async def send_preview(update, signal_text):
    user_id = update.message.chat.id
    signal_text = strip_persian(signal_text)
    if "WARNING" not in signal_text:
        full_text = signal_text + FOOTER
    else:
        full_text = signal_text
    pending_signal[user_id] = full_text
    keyboard = [
        [InlineKeyboardButton("✅ Send to Group", callback_data="send")],
        [InlineKeyboardButton("✏️ Edit", callback_data="edit_preview")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]
    await update.message.reply_text(f"Preview:\n\n{full_text}\n\nCheck it before sending:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text(update, context):
    if update.message.chat.type != "private":
        return
    user_id = update.message.chat.id
    text = update.message.text

    if user_id in waiting_edit:
        waiting_edit.remove(user_id)
        pending_signal[user_id] = text
        await send_preview(update, text)
        return

    if user_id in waiting_group_edit:
        waiting_group_edit.remove(user_id)
        await edit_last_group_message(update, context, text)
        return

    if text.startswith("/edit"):
        new_text = text.replace("/edit", "").strip()
        if new_text:
            await edit_last_group_message(update, context, new_text)
        else:
            waiting_group_edit.add(user_id)
            await update.message.reply_text("Send the corrected text to edit the last group message:")
        return

    # --- اول سعی کن بدون جمینای، فوری تبدیل کن ---
    fast = parse_farsi_signal(text)
    if fast:
        await send_preview(update, fast)
        return

    # اگه متن عجیب بود، برو سراغ جمینای
    await update.message.reply_text("Analyzing... ⏳")
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[PROMPT + "\n\nSignal text:\n" + text]
        )
        clean = strip_persian(response.text)
        await send_preview(update, clean)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_photo(update, context):
    if update.message.chat.type != "private":
        return
    await update.message.reply_text("Reading image... ⏳")
    try:
        photo = await update.message.photo[-1].get_file()
        photo_bytes = await photo.download_as_bytearray()
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[PROMPT, genai.types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg")]
        )
        clean = strip_persian(response.text)
        await send_preview(update, clean)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def button_handler(update, context):
    global last_group_msg_id
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "send":
        signal = pending_signal.get(user_id)
        if not signal:
            await query.edit_message_text("No pending signal.")
            return
        sent = await context.bot.send_message(chat_id=GROUP_ID, text=signal)
        last_group_msg_id = sent.message_id
        parsed = parse_signal(signal)
        if parsed:
            active_signals[sent.message_id] = parsed
            print("Tracking:", parsed)
        pending_signal.pop(user_id, None)
        await query.edit_message_text(f"Sent to group ✅\n\n{signal}\n\nTo edit it later, send /edit + new text")

    elif query.data == "edit_preview":
        waiting_edit.add(user_id)
        await query.edit_message_text("Send the corrected signal text:")

    elif query.data == "cancel":
        pending_signal.pop(user_id, None)
        await query.edit_message_text("Cancelled ❌")

async def edit_last_group_message(update, context, new_text):
    global last_group_msg_id
    if not last_group_msg_id:
        await update.message.reply_text("No message sent to group yet to edit.")
        return
    signal_part = new_text.split("---")[0] if "---" in new_text else new_text
    signal_part = strip_persian(signal_part)
    final_text = signal_part + FOOTER
    try:
        await context.bot.edit_message_text(chat_id=GROUP_ID, message_id=last_group_msg_id, text=final_text)
        await update.message.reply_text("Group message edited ✅")
    except Exception as e:
        await update.message.reply_text(f"Error editing: {e}")

def main():
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30)
    telegram_app = Application.builder().token(BOT_TOKEN).request(request).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot started...")
    async def post_init(app):
        asyncio.create_task(check_signals_loop(app))
    telegram_app.post_init = post_init
    telegram_app.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    main()
