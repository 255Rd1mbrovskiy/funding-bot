import os
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import difflib

import aiohttp
from aiohttp import web, ClientResponseError
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

# ---------- LOGGING & ENV ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# приглушимо балакучі логи сторонніх бібліотек
for name in ["telegram", "telegram.ext", "httpx", "aiohttp", "urllib3", "asyncio"]:
    logging.getLogger(name).setLevel(logging.WARNING)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # опц.: твій Telegram user id для нотифікацій

# ---------- ENDPOINTS ----------
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUTURES_24H     = "https://fapi.binance.com/fapi/v1/ticker/24hr"
EXCHANGE_INFO   = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# ---------- STATE (антизасинання + валідні символи) ----------
_bot_task: asyncio.Task | None = None
_bot_started: bool = False
_futures_symbols: set[str] = set()  # PERPETUAL & TRADING

# ---------- ВАЛІДАЦІЯ СИМВОЛІВ (PERPETUAL) ----------
async def load_futures_symbols():
    """Качає список валідних ф’ючерсних символів у кеш."""
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
    """True, None якщо символ валідний; інакше False і підказка."""
    if not _futures_symbols:
        await load_futures_symbols()
    u = sym.upper()
    if u in _futures_symbols:
        return True, None
    hint = difflib.get_close_matches(u, list(_futures_symbols), n=1, cutoff=0.6)
    return False, (hint[0] if hint else None)

# ---------- УТИЛІТИ ----------
def parse_interval(text: str) -> int | None:
    """'30s'->30, '30m'->1800, '1h'->3600"""
    m = re.fullmatch(r"\s*(\d+)\s*([smhSMH])\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n if unit == "s" else n * 60 if unit == "m" else n * 3600

async def fetch_binance(symbol: str):
    """
    Повертає: (SYMBOL, funding_rate_decimal, next_dt_kyiv, mark_price)
    Ловить 400/429/5xx і повертає читабельну помилку.
    """
    params = {"symbol": symbol.upper()}
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(BINANCE_PREMIUM, params=params, timeout=12) as r:
                r.raise_for_status()
                data = await r.json()
        except ClientResponseError as e:
            if e.status == 400:
                raise ValueError("Binance відповів 400 (Bad Request) — ймовірно, символ не підтримується на Futures.") from e
            raise
    rate = float(data.get("lastFundingRate") or data.get("fundingRate") or 0.0)
    # Київ = UTC+3 (спрощено без zoneinfo)
    next_dt = datetime.fromtimestamp(int(data["nextFundingTime"]) / 1000, tz=timezone.utc) + timedelta(hours=3)
    mark_price = float(data.get("markPrice") or 0.0)
    return symbol.upper(), rate, next_dt, mark_price

async def fetch_futures_24h(symbol: str):
    """Повертає: (last_price, change_pct) для USDT-маржин ф’ючерсів."""
    params = {"symbol": symbol.upper()}
    async with aiohttp.ClientSession() as s:
        async with s.get(FUTURES_24H, params=params, timeout=12) as r:
            r.raise_for_status()
            data = await r.json()
    last = float(data.get("lastPrice") or 0.0)
    chg = float(data.get("priceChangePercent") or 0.0)  # у відсотках
    return last, chg

async def notify_admin_startup(app):
    if not ADMIN_ID:
        return
    try:
        await app.bot.send_message(int(ADMIN_ID), "✅ Bot started (Render wake/redeploy).")
    except Exception:
        pass

async def ensure_bot_running():
    """Стартує polling, якщо він ще не піднятий (або впав)."""
    global _bot_task, _bot_started
    if _bot_task and not _bot_task.done():
        return "running"
    _bot_started = False
    _bot_task = asyncio.create_task(run_telegram_app())
    return "starting"

# ---------- ГЛОБАЛЬНИЙ ERROR-HANDLER ----------
async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logging.exception("Unhandled error in handler", exc_info=context.error)
        if update and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Сталася технічна помилка під час обробки команди. Спробуй ще раз."
            )
    except TelegramError:
        pass

# ---------- WATCHDOG ----------
async def watchdog():
    # раз на хвилину перевіряємо, що polling живий; якщо ні — піднімаємо
    while True:
        try:
            if not _bot_started or (_bot_task and _bot_task.done()):
                logging.warning("Watchdog: polling not running -> restarting...")
                await ensure_bot_running()
        except Exception as e:
            logging.warning(f"Watchdog error: {e}")
        await asyncio.sleep(60)

# ---------- КОМАНДИ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привіт!\n\n"
        "Команди:\n"
        "• /rate SYMBOL — показати поточний funding з Binance\n"
        "   приклади: /rate BTCUSDT, /rate ENSUSDT, /rate 0GUSDT\n"
        "• /alarm SYMBOL INTERVAL — надсилати funding регулярно\n"
        "   приклади: /alarm ENSUSDT 30m, /alarm BTCUSDT 1h, /alarm ETHUSDT 45s\n"
        "• /stopalarm [SYMBOL] — зупинити всі або конкретний аларм\n"
        "• /ping — діагностика (стан бота)\n"
        "\nФормати інтервалів: 10s, 30m, 1h\n"
        "⚠️ Перевіряємо саме Binance Futures (PERPETUAL). Якщо монета лише на Spot — фандінгу немає."
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"pong | polling={'on' if _bot_started else 'off'} | symbols={len(_futures_symbols)}"
    )

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /rate BTCUSDT")
        return
    symbol = context.args[0].upper()

    ok, hint = await validate_symbol(symbol)
    if not ok:
        msg = (f"❌ На Binance Futures (PERPETUAL) символ **{symbol}** не знайдено.\n"
               f"Можливо, монета доступна лише на Spot (фандінгу немає).")
        if hint:
            msg += f"\nМожливо, ти мав на увазі: `{hint}`"
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
            f"Спробуй близький: {hint or 'наприклад BTCUSDT'})"
        )
    except Exception as e:
        logging.exception("rate_cmd error")
        await update.message.reply_text(f"Помилка: {e}")

# ----- аларми -----
async def alarm_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    symbol = context.job.data["symbol"]
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
            pass  # навіть якщо відправка не вдалася — продовжимо працювати

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

    jq = context.application.job_queue
    name = f"{update.effective_chat.id}:{symbol}"
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
    logging.info(f"Alarm started: chat={update.effective_chat.id}, {symbol}, every {context.args[1]}")
    await update.message.reply_text(
        f"✅ Запущено аларм для {symbol} кожні {context.args[1]}.\nЗупинка: /stopalarm {symbol}"
    )

async def stopalarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jq = context.application.job_queue
    if context.args:
        symbol = context.args[0].upper()
        name = f"{update.effective_chat.id}:{symbol}"
        jobs = jq.get_jobs_by_name(name)
        for j in jobs:
            j.schedule_removal()
        msg = f"🛑 Зупинено аларм для {symbol}." if jobs else f"Не знайдено активного аларму для {symbol}."
        logging.info(f"Alarm stopped: chat={update.effective_chat.id}, {symbol}")
    else:
        cnt = 0
        for j in list(jq.jobs()):
            if j.name and str(update.effective_chat.id) in j.name:
                j.schedule_removal()
                cnt += 1
        msg = f"🛑 Зупинено {cnt} аларм(и)." if cnt else "Активних алармів не знайдено."
        logging.info(f"All alarms stopped: chat={update.effective_chat.id}, count={cnt}")
    await update.message.reply_text(msg)

# ---------- TELEGRAM RUNTIME ----------
async def run_telegram_app():
    global _bot_started
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN не задано в .env/Env Vars")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # error handler
    app.add_error_handler(on_error)

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("stopalarm", stopalarm_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Telegram bot started (polling).")

    # валідні символи та оновлення раз на 6 год
    await load_futures_symbols()
    app.job_queue.run_repeating(
        lambda c: asyncio.create_task(load_futures_symbols()),
        interval=6 * 60 * 60,
        first=6 * 60 * 60,
        name="refresh_symbols"
    )

    _bot_started = True
    await notify_admin_startup(app)

    await asyncio.Future()  # не завершується

# ---------- WEB SERVER (Render) ----------
async def run_web_server():
    async def ok(_):
        # Будимо бота, якщо він не запущений
        state = await ensure_bot_running()
        return web.Response(text=f"OK ({state})")

    async def health(_):
        state = "started" if _bot_started else "starting"
        return web.json_response({"status": state})

    webapp = web.Application()
    webapp.router.add_get("/", ok)
    webapp.router.add_get("/healthz", health)

    port = int(os.environ["PORT"])  # Render задає PORT
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"✅ Web server is listening on 0.0.0.0:{port}")
    await asyncio.Future()  # не завершується

# ---------- MAIN ----------
async def main():
    # 1) Спочатку підіймаємо веб-сервер (щоб Render одразу побачив порт)
    web_task = asyncio.create_task(run_web_server())
    await asyncio.sleep(0.5)
    # 2) Одразу гарантуємо підняття бота
    await ensure_bot_running()
    # 3) Сторож, який контролить polling
    asyncio.create_task(watchdog())
    await web_task

if __name__ == "__main__":
    asyncio.run(main())
