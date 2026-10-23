import os
import csv
import time
import asyncio
import aiohttp
import ccxt.async_support as ccxt
from datetime import datetime, timezone

# ==================== НАСТРОЙКИ СИГНАЛЬНОГО БОТА ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ВСТАВЬ_СВОЙ_CHAT_ID")

# Параметры стратегии (те же снайперские настройки честного теста v3.0)
TIMEFRAME = "1h"
EMA_PERIOD = 20
TRADE_MARGIN_USD = 10.0         # Для расчета рекомендуемого размера ордера
LEVERAGE = 15
TP_PCT_OF_MARGIN = 0.80         # Тейк-профит: +80% (+5.33% цены)
SL_PCT_OF_MARGIN = 0.60         # Стоп-лосс: -60% (-4.00% цены)

MIN_HA_BODY_PCT = 0.50          # Минимальное тело свечи Heikin Ashi (0.50%)
MAX_OPPOSITE_WICK_PCT = 0.08    # Максимальная обратная тень (0.08%)
MIN_MINUTES_LEFT_IN_HOUR = 30   # Правило первой половины свечи (минимум 30 мин до закрытия часа)
TOP_PAIRS_LIMIT = 50            # Сколько ликвидных пар мониторить

CSV_SIGNALS_FILE = "signals_history.csv"
SCAN_INTERVAL_SEC = 45          # Интервал проверки рынка (секунд)
# ====================================================================

# Словарь для защиты от повторной отправки сигнала по одной и той же часовой свече
sent_signals_cache = {}  # symbol -> timestamp свечи


def init_csv():
    if not os.path.exists(CSV_SIGNALS_FILE):
        with open(CSV_SIGNALS_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "symbol", "direction", "entry_price",
                "tp_price", "sl_price", "ha_body_pct"
            ])


def log_signal_to_csv(signal):
    with open(CSV_SIGNALS_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            signal["time"],
            signal["symbol"],
            signal["direction"],
            f"{signal['entry_price']:.6f}",
            f"{signal['tp_price']:.6f}",
            f"{signal['sl_price']:.6f}",
            f"{signal['ha_body_pct']:.2f}"
        ])


async def send_tg_message(session: aiohttp.ClientSession, text: str):
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_BOT_TOKEN:
        print(f"[TG SIGNAL LOG]:\n{text}\n", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            await resp.read()
    except Exception as e:
        print(f"⚠️ Ошибка отправки в Telegram: {e}", flush=True)


async def send_tg_document(session: aiohttp.ClientSession, filepath: str):
    if not os.path.exists(filepath):
        await send_tg_message(session, "📂 История сигналов пока пуста.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(TELEGRAM_CHAT_ID))
        data.add_field("document", open(filepath, "rb"), filename=os.path.basename(filepath))
        async with session.post(url, data=data, timeout=20) as resp:
            await resp.read()
    except Exception as e:
        print(f"⚠️ Ошибка отправки CSV истории: {e}", flush=True)


def calculate_heikin_ashi(ohlcv):
    ha_candles = []
    for i, candle in enumerate(ohlcv):
        ts, o, h, l, c, v = candle
        ha_close = (o + h + l + c) / 4.0
        if i == 0:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (ha_candles[i - 1]["ha_open"] + ha_candles[i - 1]["ha_close"]) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)

        body_pct = abs(ha_close - ha_open) / ha_open * 100.0 if ha_open > 0 else 0.0
        color = "GREEN" if ha_close > ha_open else "RED"

        upper_wick_pct = (ha_high - max(ha_open, ha_close)) / ha_open * 100.0 if ha_open > 0 else 0.0
        lower_wick_pct = (min(ha_open, ha_close) - ha_low) / ha_open * 100.0 if ha_open > 0 else 0.0

        ha_candles.append({
            "timestamp": ts, "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
            "ha_close": ha_close, "real_open": o, "real_high": h, "real_low": l,
            "real_close": c, "body_pct": body_pct, "color": color,
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct
        })
    return ha_candles


def calculate_ema(closes, period):
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return [ema[0]] * (period - 1) + ema


async def scanner_loop(exchange, session: aiohttp.ClientSession, valid_symbols: list):
    print("🔍 Сканер сигналов запущен и мониторит рынок...", flush=True)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            minutes_left = 60 - now_utc.minute

            # Правило первой половины свечи (минимум 30 минут до закрытия текущего часа)
            if minutes_left < MIN_MINUTES_LEFT_IN_HOUR:
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue

            for sym in valid_symbols:
                try:
                    ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=60)
                    if not ohlcv or len(ohlcv) < 35:
                        continue

                    ha_candles = calculate_heikin_ashi(ohlcv)
                    closes = [c["real_close"] for c in ha_candles]
                    ema20 = calculate_ema(closes, EMA_PERIOD)

                    prev_c = ha_candles[-2]
                    curr_c = ha_candles[-1]
                    curr_bar_ts = curr_c["timestamp"]

                    # Не отправляем повторный сигнал по одной и той же часовой свече
                    if sent_signals_cache.get(sym) == curr_bar_ts:
                        continue

                    # Фильтр минимального тела свечи Heikin Ashi
                    if curr_c["body_pct"] < MIN_HA_BODY_PCT:
                        continue

                    current_price = curr_c["real_close"]
                    ema_now = ema20[-1]
                    ema_prev = ema20[-4]

                    target_dir = None

                    # Условие LONG (CONTINUATION + отсутствие нижней тени)
                    if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                        if current_price > ema_now and ema_now > ema_prev:
                            if curr_c["lower_wick_pct"] <= MAX_OPPOSITE_WICK_PCT:
                                target_dir = "LONG"

                    # Условие SHORT (CONTINUATION + отсутствие верхней тени)
                    elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                        if current_price < ema_now and ema_now < ema_prev:
                            if curr_c["upper_wick_pct"] <= MAX_OPPOSITE_WICK_PCT:
                                target_dir = "SHORT"

                    if not target_dir:
                        continue

                    sent_signals_cache[sym] = curr_bar_ts

                    # Расчет уровней TP и SL для твоего удобства при ручном открытии
                    pos_size_usd = TRADE_MARGIN_USD * LEVERAGE
                    sl_delta = (TRADE_MARGIN_USD * SL_PCT_OF_MARGIN / pos_size_usd) * current_price
                    tp_delta = (TRADE_MARGIN_USD * TP_PCT_OF_MARGIN / pos_size_usd) * current_price

                    if target_dir == "LONG":
                        sl_price = current_price - sl_delta
                        tp_price = current_price + tp_delta
                    else:
                        sl_price = current_price + sl_delta
                        tp_price = current_price - tp_delta

                    signal_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    signal_data = {
                        "time": signal_time,
                        "symbol": sym,
                        "direction": target_dir,
                        "entry_price": current_price,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "ha_body_pct": curr_c["body_pct"]
                    }

                    log_signal_to_csv(signal_data)

                    # Красивая карточка сигнала в Telegram
                    emoji = "🟢" if target_dir == "LONG" else "🔴"
                    msg = (
                        f"{emoji} <b>СИГНАЛ BINGX: {target_dir}</b>\n\n"
                        f"🪙 Монета: <b>{sym}</b>\n"
                        f"📊 Рекомендуемый вход: <code>{current_price:.6f}</code>\n"
                        f"🎯 Тейк-профит (+{TP_PCT_OF_MARGIN*100:.0f}%): <code>{tp_price:.6f}</code>\n"
                        f"🛑 Стоп-лосс (-{SL_PCT_OF_MARGIN*100:.0f}%): <code>{sl_price:.6f}</code>\n\n"
                        f"⚡️ Плечо: <b>{LEVERAGE}x</b> | Тело HA: <b>{curr_c['body_pct']:.2f}%</b>\n"
                        f"⏰ До закрытия часа: <b>{minutes_left} мин</b>"
                    )
                    await send_tg_message(session, msg)

                except Exception as e:
                    continue

        except Exception as e:
            print(f"⚠️ Ошибка в сканере сигналов: {e}", flush=True)

        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def telegram_commands_loop(session: aiohttp.ClientSession):
    """Слушает команды /history в Telegram для скачивания лога сигналов."""
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    while True:
        try:
            async with session.get(url, params={"offset": offset, "timeout": 20}, timeout=25) as resp:
                data = await resp.json()
                for item in data.get("result", []):
                    offset = item["update_id"] + 1
                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()

                    if text == "/history":
                        await send_tg_document(session, CSV_SIGNALS_FILE)
        except Exception:
            await asyncio.sleep(5)


async def main():
    init_csv()
    print("🚀 Запуск сигнального Telegram-бота BingX...", flush=True)

    exchange = ccxt.bingx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"}
    })

    markets = await exchange.load_markets(True)
    valid_symbols = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)
    ][:TOP_PAIRS_LIMIT]

    async with aiohttp.ClientSession() as session:
        await send_tg_message(
            session,
            f"🤖 <b>Сигнальный бот BingX успешно запущен!</b>\n"
            f"Стратегия: <b>1H Heikin Ashi Sniper (CONTINUATION)</b>\n"
            f"Параметры: TP +80% / SL -60% при плече {LEVERAGE}x\n"
            f"Команды: /history — выгрузить CSV-файл со всеми сигналами."
        )

        await asyncio.gather(
            scanner_loop(exchange, session, valid_symbols),
            telegram_commands_loop(session)
        )


if __name__ == "__main__":
    asyncio.run(main())
