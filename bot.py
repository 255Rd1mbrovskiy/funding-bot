import asyncio
import difflib
import logging
import os
import re
from datetime import datetime, timezone, timedelta

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ---------- env ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # опційно: твій user_id для нотифів

if not TOKEN:
    raise SystemExit("Set TELEGRAM_TOKEN in environment or .env")

# ---------- Binance endpoints ----------
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_24H     = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_INFO    = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# ---------- shared HTTP session ----------
http_session: aiohttp.ClientSession | None = None
async def http() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return http_session

# ---------- helpers ----------
_futures_symbols: set[str] = set()

async def load_futures_symbols():
    """Кешуємо всі PERPETUAL TRADING символи (для валідації та підказок)."""
    try:
        s = await http()
        async with s.get(BINANCE_INFO) as r:
            r.raise_for_status()
            data = await r.json()
        syms = set()
        for it in data.get("symbols", []):
            if it.get("contractType") == "PERPETUAL" and it.get("status") == "TRADING":
                syms.add(it["symbol"].upper())
        _futures_symbols.clear()
        _futures_symbols.update(syms)
        logging.info("Loaded futures symbols: %d", len(_futures_symbols))
    except Exception as e:
        logging.warning("load_futures_symbols failed: %s", e)

def parse_interval(txt: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*([smhSMH])\s*", txt)
    if not m:
        return None
    n = int(m.group(1)); u = m.group(2).lower()
    return n if u == "s" else n*60 if u == "m" else n*3600

async def validate_symbol(sym: str) -> tuple[bool, str | None]:
    if not _futures_symbols:
        await load_futures_symbols()
    u = sym.upper()
    if u in _futures_symbols:
        return True, None
    hint = difflib.get_close_matches(u, list(_futures_symbols), n=1, cutoff=0.6)
    return False, (hint[0] if hint else None)

async def fetch_funding(sym: str):
    """Повертає (sym, rate_decimal, next_dt_kyiv, mark_price, last_price, 24h_change_pct)."""
    s = await http()

    # funding + next funding + mark price
    async with s.get(BINANCE_PREMIUM, params={"symbol": sym}) as r:
        if r.status == 418:
            # Binance іноді відповідає 418 — дроселінг/захист. Кидаємо м’якше повідомлення.
            raise RuntimeError("Binance відхилив запит (418). Спробуй пізніше.")
        r.raise_for_status()
        prem = await r.json()

    rate = float(prem.get("lastFundingRate") or prem.get("fundingRate") or 0.0)
    next_dt = datetime.fromtimestamp(int(prem["nextFundingTime"])/1000, tz=timezone.utc) + timedelta(hours=3)
    mark = float(prem.get("markPrice") or 0.0)

    # 24h ticker (last + %)
    async with s.get(BINANCE_24H, params={"symbol": sym}) as r2:
        if r2.status == 418:
            raise RuntimeError("Binance відхилив запит (418). Спробуй пізніше.")
        r2.raise_for_status()
        t24 = await r2.json()
    last = float(t24.get("lastPrice") or 0.0)
    chg  = float(t24.get("priceChangePercent") or 0.0)

    return sym, rate, next_dt, mark, last, chg

# ---------- commands ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Привіт!\n\n"
        "Команди:\n"
        "• /rate SYMBOL — funding з Binance (напр. /rate BTCUSDT, /rate 0GUSDT)\n"
        "• /alarm SYMBOL INTERVAL — регулярні оновлення (напр. /alarm BTCUSDT 30m)\n"
        "• /stopalarm [SYMBOL] — зупинити конкретний або всі аларми\n"
        "• /ping — діагностика\n\n"
        "Інтервали: 10s, 30m, 1h\n"
        "Працюємо з Binance Futures (PERPETUAL)."
    )
    await update.message.reply_text(msg)

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"pong | symbols={len(_futures_symbols)}")

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /rate BTCUSDT")
        return
    sym = context.args[0].upper()

    ok, hint = await validate_symbol(sym)
    if not ok:
        text = f"❌ На Binance Futures символ **{sym}** не знайдено."
        if hint:
            text += f"\nМожливо: `{hint}`"
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    try:
        s, rate, next_dt, mark, last, chg = await fetch_funding(sym)
        price = last or mark
        arrow = "🔺" if chg >= 0 else "🔻"
        await update.message.reply_text(
            f"📌 {s}\n"
            f"Funding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}\n"
            f"Price: ${price:,.4f}\n"
            f"24h: {arrow} {chg:.2f}%"
        )
    except Exception as e:
        logging.exception("rate failed")
        await update.message.reply_text(f"Помилка: {e}")

async def alarm_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    symbol = context.job.data["symbol"]
    try:
        s, rate, next_dt, mark, last, chg = await fetch_funding(symbol)
        price = last or mark
        arrow = "🔺" if chg >= 0 else "🔻"
        await context.bot.send_message(
            chat_id,
            f"⏰ Funding update\n"
            f"📌 {s}\n"
            f"Funding: {rate*100:.6f}% ({rate:.10f})\n"
            f"Next (Kyiv): {next_dt:%Y-%m-%d %H:%M:%S}\n"
            f"Price: ${price:,.4f}\n"
            f"24h: {arrow} {chg:.2f}%"
        )
    except Exception as e:
        logging.exception("alarm tick failed")
        await context.bot.send_message(chat_id, f"⚠️ Помилка під час оновлення {symbol}: {e}")

async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /alarm SYMBOL INTERVAL (напр. /alarm BTCUSDT 30m)")
        return
    sym = context.args[0].upper()
    seconds = parse_interval(context.args[1])
    if not seconds:
        await update.message.reply_text("Невірний інтервал. Доступно: 10s, 30m, 1h")
        return

    ok, hint = await validate_symbol(sym)
    if not ok:
        text = f"❌ На Binance Futures символ **{sym}** не знайдено."
        if hint:
            text += f"\nМожливо: `{hint}`"
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    jq = context.application.job_queue
    name = f"{update.effective_chat.id}:{sym}"
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    jq.run_repeating(
        alarm_tick,
        interval=seconds,
        first=0,        # перше повідомлення одразу
        chat_id=update.effective_chat.id,
        name=name,
        data={"symbol": sym},
    )
    await update.message.reply_text(
        f"✅ Запущено аларм для {sym} кожні {context.args[1]}.\nЗупинка: /stopalarm {sym}"
    )

async def stopalarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jq = context.application.job_queue
    if context.args:
        sym = context.args[0].upper()
        name = f"{update.effective_chat.id}:{sym}"
        jobs = jq.get_jobs_by_name(name)
        for j in jobs:
            j.schedule_removal()
        msg = f"🛑 Зупинено аларм для {sym}." if jobs else f"Активного аларму для {sym} не знайдено."
    else:
        cnt = 0
        for j in list(jq.jobs()):
            if j.name and str(update.effective_chat.id) in j.name:
                j.schedule_removal()
                cnt += 1
        msg = f"🛑 Зупинено {cnt} аларм(и)." if cnt else "Активних алармів не знайдено."
    await update.message.reply_text(msg)

# ---------- main ----------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("stopalarm", stopalarm_cmd))

    # список ф’ючерсних символів + автооновлення раз на 6 год
    await load_futures_symbols()
    app.job_queue.run_repeating(
        lambda c: asyncio.create_task(load_futures_symbols()),
        interval=6*60*60, first=6*60*60, name="refresh_symbols"
    )

    if ADMIN_ID:
        try:
            await app.bot.send_message(int(ADMIN_ID), "✅ Bot started.")
        except Exception:
            pass

    await app.initialize()
    await app.start()
    logging.info("Bot is polling…")
    await app.updater.start_polling()
    await asyncio.Future()  # тримаємо процес живим

if __name__ == "__main__":
    asyncio.run(main())
