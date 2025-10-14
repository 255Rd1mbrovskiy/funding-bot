# minimal funding bot: /start, /rate SYMBOL
import re
import os
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
KYIV_TZ = ZoneInfo("Europe/Kyiv")

BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"

async def fetch_binance(symbol: str):
    symbol = symbol.upper()
    async with aiohttp.ClientSession() as s:
        async with s.get(BINANCE_PREMIUM, params={"symbol": symbol}, timeout=10) as r:
            r.raise_for_status()
            data = await r.json()
    rate = float(data.get("lastFundingRate") or data.get("fundingRate") or 0.0)
    next_ms = int(data.get("nextFundingTime") or 0)
    next_dt = datetime.fromtimestamp(next_ms/1000, tz=timezone.utc).astimezone(KYIV_TZ)
    return symbol, rate, next_dt

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Команда:\n/rate SYMBOL — показати funding rate (напр. /rate BTCUSDT)"
    )

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /rate BTCUSDT")
        return
    symbol = context.args[0]
    try:
        sym, rate, next_dt = await fetch_binance(symbol)
        await update.message.reply_text(
            f"📌 {sym}\nFunding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S %Z}"
        )
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

def parse_interval(text: str) -> int | None:
    """
    '30m' -> 1800, '1h' -> 3600, '45s' -> 45
    """
    m = re.fullmatch(r"\s*(\d+)\s*([smhSMH])\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return None

async def alarm_tick(context):
    """JobQueue callback: надсилає поточний funding для symbol."""
    chat_id = context.job.chat_id
    symbol = context.job.data["symbol"]
    try:
        sym, rate, next_dt = await fetch_binance(symbol)
        await context.bot.send_message(
            chat_id,
            f"⏰ Funding alert\n📌 {sym}\nFunding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S %Z}"
        )
    except Exception as e:
        await context.bot.send_message(chat_id, f"Помилка отримання funding для {symbol}: {e}")

async def alarm_cmd(update, context):
    """
    /alarm SYMBOL INTERVAL  (приклад: /alarm ENSUSDT 30m)
    INTERVAL: 10s / 30m / 1h
    """
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /alarm SYMBOL INTERVAL (напр. /alarm BTCUSDT 30m)")
        return
    symbol = context.args[0].upper()
    seconds = parse_interval(context.args[1])
    if not seconds:
        await update.message.reply_text("Невірний інтервал. Формати: 10s, 30m, 1h")
        return

    jq = context.application.job_queue
    name = f"{update.effective_chat.id}:{symbol}"
    # при повторному /alarm замінюємо попередній джоб
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    jq.run_repeating(
        alarm_tick,
        interval=seconds,
        first=0,  # відправити одразу
        chat_id=update.effective_chat.id,
        name=name,
        data={"symbol": symbol},
    )
    await update.message.reply_text(f"✅ Запущено аларм для {symbol} кожні {context.args[1]}."
                                    f"\nЗупинка: /stopalarm {symbol}")

async def stopalarm_cmd(update, context):
    """ /stopalarm SYMBOL — зупинити повторні сповіщення для символу """
    if not context.args:
        await update.message.reply_text("Використання: /stopalarm SYMBOL")
        return
    symbol = context.args[0].upper()
    name = f"{update.effective_chat.id}:{symbol}"
    jq = context.application.job_queue
    jobs = jq.get_jobs_by_name(name)
    for j in jobs:
        j.schedule_removal()
    if jobs:
        await update.message.reply_text(f"🛑 Зупинено аларм для {symbol}.")
    else:
        await update.message.reply_text(f"Не знайдено активного аларму для {symbol}.")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN не задано в .env")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("stopalarm", stopalarm_cmd))
    # У v21 run_polling — синхронний блокуючий метод
    app.run_polling()

if __name__ == "__main__":
    main()