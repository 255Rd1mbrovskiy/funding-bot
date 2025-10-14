import os
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

# === Завантажуємо токен ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# === Команди ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привіт! Надішли команду /rate BTCUSDT або /alarm BTCUSDT 30m")

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Вкажи монету, наприклад: /rate BTCUSDT")
        return
    symbol = context.args[0].upper()
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await update.message.reply_text("⚠️ Не вдалося отримати дані з Binance.")
                    return
                data = await resp.json()
                rate = float(data["lastFundingRate"]) * 100
                next_time = datetime.fromtimestamp(data["nextFundingTime"]/1000, tz=timezone.utc) + timedelta(hours=3)
                await update.message.reply_text(
                    f"📊 *{symbol}*\nFunding: `{rate:.6f}%`\nNext funding: `{next_time.strftime('%Y-%m-%d %H:%M:%S')}`",
                    parse_mode="Markdown"
                )
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

# === Команда /alarm ===
async def alarm_task(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    symbol = context.job.data
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            rate = float(data["lastFundingRate"]) * 100
            next_time = datetime.fromtimestamp(data["nextFundingTime"]/1000, tz=timezone.utc) + timedelta(hours=3)
            await context.bot.send_message(
                chat_id,
                text=f"⏰ Funding update for *{symbol}*\nRate: `{rate:.6f}%`\nNext: `{next_time.strftime('%Y-%m-%d %H:%M:%S')}`",
                parse_mode="Markdown"
            )

async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❗ Формат: /alarm BTCUSDT 30m (або 1h)")
        return

    symbol = context.args[0].upper()
    interval = context.args[1]

    unit = interval[-1]
    try:
        val = int(interval[:-1])
    except ValueError:
        await update.message.reply_text("❗ Неправильний формат часу (30m / 1h)")
        return

    minutes = val * 60 if unit == "h" else val
    job_name = f"{update.effective_chat.id}_{symbol}"
    jq = context.job_queue
    jq.run_repeating(alarm_task, interval=minutes*60, first=5, chat_id=update.effective_chat.id, name=job_name, data=symbol)
    await update.message.reply_text(f"✅ Сповіщення кожні {interval} для {symbol}")

async def stopalarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jq = context.job_queue
    jobs = jq.get_jobs_by_name(f"{update.effective_chat.id}_")
    for j in jobs:
        j.schedule_removal()
    await update.message.reply_text("🛑 Усі сповіщення зупинено.")

# === Telegram запуск ===
async def run_telegram_app():
    if not TELEGRAM_TOKEN:
        raise SystemExit("❌ TELEGRAM_TOKEN не задано в .env або в Environment Variables.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("stopalarm", stopalarm_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Future()  # тримає вічно

# === Web-сервер для Render ===
async def run_web_server():
    async def ok(_):
        return web.Response(text="OK")
    webapp = web.Application()
    webapp.router.add_get("/", ok)
    webapp.router.add_get("/healthz", ok)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await asyncio.Future()

# === Головна подія ===
async def main():
    await asyncio.gather(run_telegram_app(), run_web_server())

if __name__ == "__main__":
    asyncio.run(main())
