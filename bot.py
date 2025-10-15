import os
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import difflib

import aiohttp
from aiohttp import ClientResponseError
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

# ----------------- LOGGING & ENV -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for name in ["telegram", "telegram.ext", "httpx", "aiohttp", "urllib3", "asyncio"]:
    logging.getLogger(name).setLevel(logging.WARNING)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")   # напр. https://your-app.onrender.com
ADMIN_ID       = os.getenv("ADMIN_ID")  # опційно

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN is required")
if not PUBLIC_URL:
    raise SystemExit("PUBLIC_URL is required (e.g. https://your-app.onrender.com)")

PORT = int(os.getenv("PORT", "8000"))  # Render дає PORT

# ----------------- BINANCE ENDPOINTS -----------------
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUTURES_24H     = "https://fapi.binance.com/fapi/v1/ticker/24hr"
EXCHANGE_INFO   = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# ----------------- CACHED FUTURES SYMBOLS -----------------
_futures_symbols: set[str] = set()  # тільки PERPETUAL & TRADING

async def load_futures_symbols():
    """Завантажити список валідних ф’ючерсних символів у кеш."""
    global _futures_symbols
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(EXCHANGE_INFO, timeout=15) as r:
                r.raise_for_status()
                info = await r.json()
        syms = set()
        for sdef in info.get("symbols", []):
            if sdef.get("contractType") == "PERPETUAL" and sdef.get("status") == "TRADING":
                syms.add(sdef["symbol"].upper())
        _futures_symbols = syms
        logging.info(f"Loaded futures symbols: {len(_futures_symbols)}")
    except Exception as e:
        logging.warning(f"load_futures_symbols error: {e}")

async def validate_symbol(sym: str) -> tuple[bool, str | None]:
    """True/None якщо символ валідний на Futures; False/підказка інакше."""
    if not _futures_symbols:
        await load_futures_symbols()
    u = sym.upper()
    if u in _futures_symbols:
        return True, None
    hint = difflib.get_close_matches(u, list(_futures_symbols), n=1, cutoff=0.6)
    return False, (hint[0] if hint else None)

# ----------------- BINANCE HELPERS -----------------
def parse_interval(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*([smhSMH])\s*", text)
    if not m:
        return None
    n = int(m.group(1)); unit = m.group(2).lower()
    return n if unit == "s" else n*60 if unit == "m" else n*3600

async def fetch_binance(symbol: str):
    """Return (symbol, funding_rate_decimal, next_dt_kyiv, mark_price)."""
    params = {"symbol": symbol.upper()}
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(BINANCE_PREMIUM, params=params, timeout=12) as r:
                r.raise_for_status()
                data = await r.json()
        except ClientResponseError as e:
            if e.status == 400:
                raise ValueError("Binance 400 (Bad Request) — ймовірно, символ не підтримується на Futures.") from e
            raise
    rate = float(data.get("lastFundingRate") or data.get("fundingRate") or 0.0)
    next_dt = datetime.fromtimestamp(int(data["nextFundingTime"])/1000, tz=timezone.utc) + timedelta(hours=3)  # Київ ~ UTC+3
    mark_price = float(data.get("markPrice") or 0.0)
    return symbol.upper(), rate, next_dt, mark_price

async def fetch_futures_24h(symbol: str):
    """Return (last_price, change_pct)."""
    params = {"symbol": symbol.upper()}
    async with aiohttp.ClientSession() as s:
        async with s.get(FUTURES_24H, params=params, timeout=12) as r:
            r.raise_for_status()
            data = await r.json()
    last = float(data.get("lastPrice") or 0.0)
    chg  = float(data.get("priceChangePercent") or 0.0)
    return last, chg

# ----------------- COMMANDS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привіт!\n\n"
        "Команди:\n"
        "• /rate SYMBOL — funding з Binance (напр. /rate BTCUSDT, /rate 0GUSDT)\n"
        "• /alarm SYMBOL INTERVAL — регулярні оновлення (напр. /alarm BTCUSDT 30m)\n"
        "• /stopalarm [SYMBOL] — зупинити всі або конкретний аларм\n"
        "• /ping — діагностика\n\n"
        "Формати: 10s, 30m, 1h\n"
        "⚠️ Працюємо з Binance Futures (PERPETUAL). Якщо монета лише на Spot — фандінгу немає."
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"pong | symbols={len(_futures_symbols)}")

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /rate BTCUSDT")
        return
    symbol = context.args[0].upper()

    ok, hint = await validate_symbol(symbol)
    if not ok:
        msg = (f"❌ На Binance Futures (PERPETUAL) символ **{symbol}** не знайдено.\n"
               f"Можливо, монета лише на Spot (фандінгу немає).")
        if hint:
            msg += f"\nМожливо: `{hint}`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    try:
        sym, rate, next_dt, mark = await fetch_binance(symbol)
        last, chg = await fetch_futures_24h(sym)
        price = last or mark
        arrow = "🔺" if chg >= 0 else "🔻"
        await update.message.reply_text(
            f"📌 {sym}\n"
            f"Funding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}\n"
            f"Price: ${price:,.4f}\n"
            f"24h: {arrow} {chg:.2f}%"
        )
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {e}\n(Символ '{symbol}' є у списку PERPETUAL? "
            f"Спробуй близький: {hint or 'BTCUSDT'})"
        )
    except Exception as e:
        logging.exception("rate_cmd error")
        await update.message.reply_text(f"Помилка: {e}")

# ----- alarms -----
async def alarm_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    symbol  = context.job.data["symbol"]
    try:
        sym, rate, next_dt, mark = await fetch_binance(symbol)
        last, chg = await fetch_futures_24h(sym)
        price = last or mark
        arrow = "🔺" if chg >= 0 else "🔻"
        await context.bot.send_message(
            chat_id,
            f"⏰ Funding update\n"
            f"📌 {sym}\n"
            f"Funding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}\n"
            f"Price: ${price:,.4f}\n"
            f"24h: {arrow} {chg:.2f}%"
        )
    except Exception as e:
        logging.exception("alarm_tick error")
        try:
            await context.bot.send_message(chat_id, f"⚠️ Помилка під час оновлення {symbol}: {e}")
        except Exception:
            pass

async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /alarm SYMBOL INTERVAL (напр. /alarm BTCUSDT 30m)")
        return
    symbol = context.args[0].upper()

    ok, hint = await validate_symbol(symbol)
    if not ok:
        msg = (f"❌ На Binance Futures (PERPETUAL) символ **{symbol}** не знайдено.\n"
               f"Можливо, монета на Spot (фандінгу немає).")
        if hint:
            msg += f"\nМожливо: `{hint}`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    seconds = parse_interval(context.args[1])
    if not seconds:
        await update.message.reply_text("Невірний інтервал. Доступно: 10s, 30m, 1h")
        return

    jq   = context.application.job_queue
    name = f"{update.effective_chat.id}:{symbol}"
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    jq.run_repeating(
        alarm_tick,
        interval=seconds,
        first=0,   # відправити перше одразу
        chat_id=update.effective_chat.id,
        name=name,
        data={"symbol": symbol},
    )
    await update.message.reply_text(
        f"✅ Запущено аларм для {symbol} кожні {context.args[1]}.\nЗупинка: /stopalarm {symbol}"
    )

async def stopalarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jq = context.application.job_queue
    if context.args:
        symbol = context.args[0].upper()
        name = f"{update.effective_chat.id}:{symbol}"
        jobs = jq.get_jobs_by_name(name)
        for j in jobs: j.schedule_removal()
        msg = f"🛑 Зупинено аларм для {symbol}." if jobs else f"Не знайдено активного аларму для {symbol}."
    else:
        cnt = 0
        for j in list(jq.jobs()):
            if j.name and str(update.effective_chat.id) in j.name:
                j.schedule_removal(); cnt += 1
        msg = f"🛑 Зупинено {cnt} аларм(и)." if cnt else "Активних алармів не знайдено."
    await update.message.reply_text(msg)

# ----------------- ERROR HANDLER -----------------
async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logging.exception("Unhandled error in handler", exc_info=context.error)
        if update and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id, "⚠️ Сталася помилка. Спробуй ще раз.")
    except TelegramError:
        pass

# ----------------- WEBHOOK RUNTIME -----------------
async def run_webhook():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("ping",       ping_cmd))
    app.add_handler(CommandHandler("rate",       rate_cmd))
    app.add_handler(CommandHandler("alarm",      alarm_cmd))
    app.add_handler(CommandHandler("stopalarm",  stopalarm_cmd))

    # init + start application
    await app.initialize()
    await app.start()

    # завантажити символи одразу і оновлювати кожні 6 год
    await load_futures_symbols()
    app.job_queue.run_repeating(
        lambda c: asyncio.create_task(load_futures_symbols()),
        interval=6*60*60, first=6*60*60, name="refresh_symbols"
    )

    # налаштовуємо webhook
    path = f"/webhook/{TELEGRAM_TOKEN}"
    webhook_url = f"{PUBLIC_URL}{path}"
    await app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)

    # вбудований сервер PTB (aiohttp) — слухає тільки наш path
    await app.updater.start_webhook(listen="0.0.0.0", port=PORT, url_path=path)

    # нотиф адміну
    if ADMIN_ID:
        try:
            await app.bot.send_message(int(ADMIN_ID), f"✅ Bot webhook started on {webhook_url}")
        except Exception:
            pass

    # блокуючий wait
    await asyncio.Future()

# ----------------- MAIN -----------------
if __name__ == "__main__":
    asyncio.run(run_webhook())
