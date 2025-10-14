import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------- базові налаштування ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"

# ---------- утиліти ----------
def parse_interval(text: str) -> int | None:
    """'30s'->30, '30m'->1800, '1h'->3600"""
    m = re.fullmatch(r"\s*(\d+)\s*([smhSMH])\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n if unit == "s" else n * 60 if unit == "m" else n * 3600

async def fetch_binance(symbol: str):
    url = BINANCE_PREMIUM
    params = {"symbol": symbol.upper()}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, timeout=12) as r:
            r.raise_for_status()
            data = await r.json()
    rate = float(data.get("lastFundingRate") or data.get("fundingRate") or 0.0)
    # Kyiv = UTC+3 у прикладі (спростимо без zoneinfo)
    next_dt = datetime.fromtimestamp(int(data["nextFundingTime"]) / 1000, tz=timezone.utc) + timedelta(hours=3)
    return symbol.upper(), rate, next_dt

# ---------- команди ----------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привіт!\n\n"
        "Команди:\n"
        "• /rate SYMBOL — поточний funding з Binance (напр. /rate BTCUSDT)\n"
        "• /alarm SYMBOL INTERVAL — періодичні сповіщення (напр. /alarm ENSUSDT 30m)\n"
        "• /stopalarm [SYMBOL] — зупинити всі або конкретний аларм\n"
        "\nІнтервали: 10s, 30m, 1h\n"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /rate BTCUSDT")
        return
    symbol = context.args[0]
    try:
        sym, rate, next_dt = await fetch_binance(symbol)
        await update.message.reply_text(
            f"📌 {sym}\nFunding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}"
        )
    except Exception as e:
        logging.exception("rate_cmd error")
        await update.message.reply_text(f"Помилка: {e}")

# ----- alarm -----
async def alarm_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    symbol = context.job.data["symbol"]
    try:
        sym, rate, next_dt = await fetch_binance(symbol)
        await context.bot.send_message(
            chat_id,
            f"⏰ Funding update\n📌 {sym}\nFunding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}"
        )
    except Exception as e:
        logging.exception("alarm_tick error")
        await context.bot.send_message(chat_id, f"Помилка отримання funding для {symbol}: {e}")

async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /alarm SYMBOL INTERVAL (напр. /alarm BTCUSDT 30m)")
        return
    symbol = context.args[0].upper()
    seconds = parse_interval(context.args[1])
    if not seconds:
        await update.message.reply_text("Невірний інтервал. Доступно: 10s, 30m, 1h")
        return

    jq = context.application.job_queue
    name = f"{update.effective_chat.id}:{symbol}"
    # прибираємо попередній, якщо був
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    jq.run_repeating(
        alarm_tick,
        interval=seconds,
        first=0,  # одразу перше повідомлення
        chat_id=update.effective_chat.id,
        name=name,
        data={"symbol": symbol},
    )
    await update.message.reply_text(f"✅ Запущено аларм для {symbol} кожні {context.args[1]}.\nЗупинка: /stopalarm {symbol}")

async def stopalarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jq = context.application.job_queue
    if context.args:
        symbol = context.args[0].upper()
        name = f"{update.effective_chat.id}:{symbol}"
        jobs = jq.get_jobs_by_name(name)
        for j in jobs:
            j.schedule_removal()
        msg = f"🛑 Зупинено аларм для {symbol}." if jobs else f"Не знайдено активного аларму для {symbol}."
    else:
        # зупинити всі для цього чату
        cnt = 0
        for j in list(jq.jobs()):
            if j.name and str(update.effective_chat.id) in j.name:
                j.schedule_removal()
                cnt += 1
        msg = f"🛑 Зупинено {cnt} аларм(и)." if cnt else "Активних алармів не знайдено."
    await update.message.reply_text(msg)

# ---------- Telegram runtime (без run_polling) ----------
async def run_telegram_app():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN не задано в .env/Env Vars")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("stopalarm", stopalarm_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Telegram bot started (polling).")
    await asyncio.Future()  # ніколи не завершується

# ---------- Простий веб-сервер для Render ----------
async def run_web_server():
    async def ok(_):
        return web.Response(text="OK")
    webapp = web.Application()
    webapp.router.add_get("/", ok)
    webapp.router.add_get("/healthz", ok)

    # PORT має бути заданий Render'ом; не ставимо дефолт
    port = int(os.environ["PORT"])
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"✅ Web server is listening on 0.0.0.0:{port}")
    await asyncio.Future()  # ніколи не завершується

# ---------- Головний запуск ----------
async def main():
    # 1) Спочатку піднімаємо веб-сервер, щоб Render побачив порт
    web_task = asyncio.create_task(run_web_server())
    await asyncio.sleep(1)
    # 2) Потім запускаємо Telegram-бота
    await run_telegram_app()
    await web_task

if __name__ == "__main__":
    asyncio.run(main())
