import asyncio
import csv
import datetime
import os
import aiohttp
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAME = "1h"
HISTORY_LIMIT = 120             # Достаточно для точного EMA(50), RSI(14) и Heikin Ashi
LIVE_POLL_INTERVAL = 4          # Опрос кандидатов каждые 4 сек
MIN_REMAINING_MINUTES = 30.0    # Вход разрешен только если до конца часа >= 30 мин
SCAN_MAX_REMAINING_MIN = 59.75  # Сканируем рынок через 15 секунд после начала часа
ENTRY_MAX_REMAINING_MIN = 57.5  # Ждем первые 2.5 минуты часа для формирования глубокого отката
MIN_BODY_PCT = 0.005
CONFIRM_CHECKS = 3              # Удержание уровня отката минимум 3 проверки подряд

# Параллельность запросов к MEXC (15 одновременных запросов позволяют проверить ~600 пар за ~25 сек)
SCAN_CONCURRENCY = 15
LIVE_CONCURRENCY = 10

# --- НАСТРОЙКИ ГЛУБОКОГО ОТКАТА ОТ ОТКРЫТИЯ ЧАСА ---
PULLBACK_RETRACE_RATIO = 0.70   # Откат минимум 70% от размаха предыдущей свечи (или среднего размаха)
MIN_PULLBACK_PCT = 0.85         # Обязательный минимальный откат от открытия часа не менее 0.85%
DIRECT_PULLBACK_PCT = 1.40      # Безусловный глубокий откат от открытия часа (1.40%+)

# --- НАСТРОЙКИ ФИЛЬТРА ТРЕНДА (EMA) И ПЕРЕКУПЛЕННОСТИ (RSI) ---
EMA_PERIOD = 50
RSI_PERIOD = 14

# Пороги для SHORT:
RSI_SHORT_TREND = 55.0          # Если идем ПО ТРЕНДУ вниз (цена ниже EMA 50), достаточно RSI >= 55
RSI_SHORT_OVERBOUGHT = 68.0     # Если идем ПРОТИВ тренда, нужна сильная перекупленность RSI >= 68

# Пороги для LONG:
RSI_LONG_TREND = 45.0           # Если идем ПО ТРЕНДУ вверх (цена выше EMA 50), достаточно RSI <= 45
RSI_LONG_OVERSOLD = 32.0        # Если идем ПРОТИВ тренда, нужна сильная перепроданность RSI <= 32

LOG_FILE = "signals_log.csv"
CHAT_ID_FILE = "chat_id.txt"

active_symbols = {}
current_hour_ts = 0
hourly_candidates = {}
trigger_confirm_counter = {}
sent_signals = set()
is_scanning = False


# ==================== АВТО-СОХРАНЕНИЕ CHAT_ID ====================
def get_target_chat_id() -> str:
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID not in ("ВАШ_ТОКЕН_БОТА", "ВАШ_CHAT_ID") and TELEGRAM_CHAT_ID.strip():
        return TELEGRAM_CHAT_ID.strip()
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
            saved = f.read().strip()
            if saved:
                return saved
    return ""


def save_target_chat_id(chat_id):
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(chat_id))


# ==================== РАБОТА С БАЗОЙ СТАТИСТИКИ (CSV) ====================
CSV_HEADERS = [
    "signal_id", "signal_time_utc", "ticker", "direction", "entry_price",
    "remaining_minutes", "streak_candles", "reversal_body_pct", "live_body_pct",
    "rsi_value", "trend_mode", "trigger_reason", "status", "updated_time_utc"
]


def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()


def log_new_signal(data: dict):
    init_csv()
    with open(LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writerow(data)


def update_signal_status(signal_id: str, new_status: str):
    init_csv()
    rows = []
    updated_row = None
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "trigger_reason" not in row or not row["trigger_reason"]:
                row["trigger_reason"] = "Глубокий откат"
            if "rsi_value" not in row or not row["rsi_value"]:
                row["rsi_value"] = "50.0"
            if "trend_mode" not in row or not row["trend_mode"]:
                row["trend_mode"] = "Н/Д"
            if row["signal_id"] == signal_id:
                row["status"] = new_status
                row["updated_time_utc"] = now_str
                updated_row = row
            rows.append(row)

    if updated_row:
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    return updated_row


def get_stats_summary() -> str:
    init_csv()
    total = 0
    win = 0
    loss = 0
    pending = 0
    long_win, long_loss = 0, 0
    short_win, short_loss = 0, 0

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            st = row.get("status", "")
            d = row.get("direction", "")
            if st == "ЗАШЕЛ":
                win += 1
                if d == "LONG":
                    long_win += 1
                else:
                    short_win += 1
            elif st == "НЕ ЗАШЕЛ":
                loss += 1
                if d == "LONG":
                    long_loss += 1
                else:
                    short_loss += 1
            else:
                pending += 1

    marked = win + loss
    winrate = (win / marked * 100.0) if marked > 0 else 0.0
    lines = [
        "📊 <b>Статистика сигналов:</b>",
        "",
        f"Всего сигналов: <b>{total}</b>",
        f"✅ Зашло: <b>{win}</b>",
        f"❌ Не зашло: <b>{loss}</b>",
        f"⏳ Не отмечено: <b>{pending}</b>",
        f"🎯 <b>Винрейт (по отмеченным): {winrate:.1f}%</b>",
        "",
        f"🟢 LONG: {long_win} зашло / {long_loss} не зашло",
        f"🔴 SHORT: {short_win} зашло / {short_loss} не зашло",
        "",
        "📥 Для выгрузки файла анализа отправьте /export"
    ]
    return "\n".join(lines)


# ==================== РАСЧЕТ HEIKIN ASHI, RSI И EMA ====================
def calculate_ema_series(closes: list, period: int = 50) -> list:
    if not closes:
        return []
    ema = [closes[0]]
    k = 2.0 / (period + 1.0)
    for price in closes[1:]:
        ema.append(price * k + ema[-1] * (1.0 - k))
    return ema


def calculate_rsi_series(closes: list, period: int = 14) -> list:
    n = len(closes)
    if n < period + 1:
        return [50.0] * n

    rsi = [50.0] * n
    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def calculate_heikin_ashi(ohlcv):
    closes = [c[4] for c in ohlcv]
    ema_series = calculate_ema_series(closes, EMA_PERIOD)
    rsi_series = calculate_rsi_series(closes, RSI_PERIOD)

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

        if body_pct < MIN_BODY_PCT:
            color = "DOJI"
        elif ha_close > ha_open:
            color = "GREEN"
        else:
            color = "RED"

        ha_candles.append({
            "timestamp": ts,
            "ha_open": ha_open,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close,
            "real_open": o,
            "real_high": h,
            "real_low": l,
            "real_close": c,
            "body_pct": body_pct,
            "color": color,
            "ema": ema_series[i],
            "rsi": rsi_series[i]
        })
    return ha_candles


def count_consecutive_color(ha_candles, start_index, target_color):
    count = 0
    for i in range(start_index, -1, -1):
        if ha_candles[i]["color"] == target_color:
            count += 1
        else:
            break
    return count


# ==================== TELEGRAM: УВЕДОМЛЕНИЯ И КНОПКИ ====================
def build_signal_message(data: dict) -> str:
    ticker = data["ticker"]
    direction = data["direction"]
    price = data["entry_price"]
    dt_str = data["signal_time_utc"]
    rem_min = float(data["remaining_minutes"])
    streak = int(data["streak_candles"])
    rev_pct = float(data["reversal_body_pct"])
    live_pct = float(data["live_body_pct"])
    rsi_val = float(data.get("rsi_value", 50.0))
    trend_mode = data.get("trend_mode", "По тренду")
    trigger_reason = data.get("trigger_reason", "Глубокий откат")
    status = data["status"]

    mexc_pair = ticker[:-4] + "_USDT" if ticker.endswith("USDT") else ticker
    chart_url = f"https://futures.mexc.com/exchange/{mexc_pair}"

    if direction == "SHORT":
        header = "🔴 <b>СИГНАЛ: SHORT (ВНИЗ)</b>"
        chain = f"{'🟢' * min(streak, 6)} ({streak} закр.) ➔ 🔴 (1 закр.) ➔ ⚡ {trigger_reason}"
    else:
        header = "🟢 <b>СИГНАЛ: LONG (ВВЕРХ)</b>"
        chain = f"{'🔴' * min(streak, 6)} ({streak} закр.) ➔ 🟢 (1 закр.) ➔ ⚡ {trigger_reason}"

    if status == "ЗАШЕЛ":
        status_line = "✅ <b>Результат: СИГНАЛ ЗАШЕЛ</b>"
    elif status == "НЕ ЗАШЕЛ":
        status_line = "❌ <b>Результат: НЕ ЗАШЕЛ</b>"
    else:
        status_line = "⏳ <b>Результат: Ожидает отметки (нажми кнопку ниже)</b>"

    lines = [
        header,
        "",
        f"📌 <b>Пара:</b> <code>{ticker}</code> (<a href='{chart_url}'>Открыть график</a>)",
        f"💰 <b>Цена входа:</b> <code>{price}</code>",
        f"🧭 <b>Режим фильтра:</b> {trend_mode}",
        f"📈 <b>RSI ({RSI_PERIOD}):</b> {rsi_val:.1f}",
        f"🎯 <b>Триггер:</b> {trigger_reason}",
        f"🕒 <b>Время (MEXC):</b> {dt_str} UTC",
        f"⏱ <b>До закрытия 1H свечи:</b> {rem_min:.1f} мин.",
        "",
        "📊 <b>Цепочка Heikin Ashi (1H):</b>",
        chain,
        f"▪️ Тело разворотной свечи: {rev_pct:.3f}%",
        f"▪️ Тело живой свечи HA: {live_pct:.3f}%",
        "",
        status_line
    ]
    return "\n".join(lines)


def build_inline_keyboard(signal_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Зашел", "callback_data": f"WIN|{signal_id}"},
                {"text": "❌ Не зашел", "callback_data": f"LOSS|{signal_id}"}
            ]
        ]
    }


async def send_telegram_signal(session: aiohttp.ClientSession, data: dict) -> bool:
    target_chat = get_target_chat_id()
    if not target_chat:
        print("[ОШИБКА TG] Chat ID не задан! Напишите боту /start в Telegram.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": build_signal_message(data),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_inline_keyboard(data["signal_id"])
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                print(f"[Ошибка TG sendMessage]: {await resp.text()}", flush=True)
                return False
            return True
    except Exception as e:
        print(f"[Ошибка связи с Telegram]: {e}", flush=True)
        return False


async def send_telegram_file(session: aiohttp.ClientSession, chat_id: str):
    init_csv()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(LOG_FILE, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", "📁 Файл статистики сигналов.")
    form.add_field("document", file_bytes, filename="signals_log.csv", content_type="text/csv")
    try:
        async with session.post(url, data=form, timeout=20) as resp:
            if resp.status != 200:
                print(f"[Ошибка отправки файла TG]: {await resp.text()}", flush=True)
    except Exception as e:
        print(f"[Ошибка отправки файла]: {e}", flush=True)


async def telegram_polling_worker(session: aiohttp.ClientSession):
    offset = 0
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    while True:
        try:
            async with session.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 20}, timeout=25) as resp:
                if resp.status != 200:
                    await asyncio.sleep(3)
                    continue
                result = await resp.json()

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    message_id = msg.get("message_id")

                    if chat_id:
                        save_target_chat_id(chat_id)

                    if "|" in cb_data:
                        action, sig_id = cb_data.split("|", 1)
                        new_status = "ЗАШЕЛ" if action == "WIN" else "НЕ ЗАШЕЛ"
                        updated_row = update_signal_status(sig_id, new_status)

                        if updated_row and chat_id and message_id:
                            edit_payload = {
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "text": build_signal_message(updated_row),
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True,
                                "reply_markup": build_inline_keyboard(sig_id)
                            }
                            await session.post(f"{base_url}/editMessageText", json=edit_payload)

                        await session.post(f"{base_url}/answerCallbackQuery", json={
                            "callback_query_id": cb_id,
                            "text": f"Отмечено: {new_status}"
                        })

                elif "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "").strip()
                    chat_id = msg.get("chat", {}).get("id")

                    if chat_id:
                        save_target_chat_id(chat_id)

                    if "document" in msg:
                        doc = msg["document"]
                        if doc.get("file_name", "").endswith(".csv"):
                            file_id = doc["file_id"]
                            async with session.get(f"{base_url}/getFile", params={"file_id": file_id}) as f_resp:
                                f_info = await f_resp.json()
                            file_path = f_info.get("result", {}).get("file_path")
                            if file_path:
                                dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                                async with session.get(dl_url) as dl_resp:
                                    content = await dl_resp.read()
                                with open(LOG_FILE, "wb") as f:
                                    f.write(content)
                                await session.post(f"{base_url}/sendMessage", json={
                                    "chat_id": chat_id,
                                    "text": "✅ База статистики успешно восстановлена!",
                                    "parse_mode": "HTML"
                                })
                        continue

                    if text.startswith("/stats"):
                        summary = get_stats_summary()
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id, "text": summary, "parse_mode": "HTML"
                        })
                    elif text.startswith("/export"):
                        await send_telegram_file(session, chat_id)
                    elif text.startswith("/status") or text.startswith("/start"):
                        items = [
                            f"{k} ({v['target_dir']}, RSI:{v['peak_rsi']:.0f})"
                            for k, v in hourly_candidates.items()
                        ]
                        if len(items) > 40:
                            cand_list = ", ".join(items[:40]) + f" ... и еще {len(items) - 40}"
                        else:
                            cand_list = ", ".join(items) or "нет кандидатов в этом часе"

                        scan_state = "⏳ Идет сканирование рынка..." if is_scanning else "✅ Готово"
                        saved_cid = get_target_chat_id()
                        status_lines = [
                            "🤖 <b>Бот ВСЕ ФЬЮЧЕРСЫ MEXC (HA + Pullback + RSI/EMA)</b>",
                            f"📩 <b>Chat ID:</b> <code>{saved_cid}</code> (подключен ✅)",
                            f"📊 <b>Всего фьючерсов в работе:</b> {len(active_symbols)} пар (USDT-M)",
                            f"🔄 <b>Статус сканера часа:</b> {scan_state}",
                            f"⚙️ <b>Откат:</b> от {MIN_PULLBACK_PCT}% | <b>EMA:</b> {EMA_PERIOD} | <b>RSI:</b> {RSI_PERIOD}",
                            f"🎯 <b>Кандидаты текущего часа ({len(hourly_candidates)}):</b>",
                            f"<code>{cand_list}</code>"
                        ]
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id,
                            "text": "\n".join(status_lines),
                            "parse_mode": "HTML"
                        })

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Ошибка поллинга TG]: {e}", flush=True)
            await asyncio.sleep(5)


# ==================== АВТОЗАГРУЗКА ВСЕХ ФЬЮЧЕРСОВ MEXC ====================
async def load_all_mexc_usdt_swaps(exchange):
    """
    Загружает абсолютно все активные бессрочные USDT-фьючерсы на бирже MEXC.
    """
    markets = await exchange.load_markets(True)
    resolved = {}

    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
            raw_id = m.get("id", "").replace("_", "")
            if not raw_id:
                raw_id = sym.split(":")[0].replace("/", "")
            resolved[raw_id] = sym

    print(f"[Инициализация] Загружено ВСЕХ активных USDT-фьючерсов MEXC: {len(resolved)}", flush=True)
    return resolved


async def scan_hourly_candidates(exchange, hour_open_ts):
    global is_scanning
    is_scanning = True
    candidates = {}
    hour_ms = 3600 * 1000
    prev_closed_ts = hour_open_ts - hour_ms
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def check_one(ticker, ccxt_sym):
        async with sem:
            try:
                ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
            except Exception:
                return

            if not ohlcv or len(ohlcv) < 55:
                return

            closed_candles = [c for c in ohlcv if c[0] < hour_open_ts]

            if len(closed_candles) < 55 or closed_candles[-1][0] != prev_closed_ts:
                return

            ha_closed = calculate_heikin_ashi(closed_candles)
            reversal_candle = ha_closed[-1]
            idx_before_rev = len(ha_closed) - 2

            recent_ranges = [abs(c["real_high"] - c["real_low"]) for c in ha_closed[-5:]]
            avg_recent_range = sum(recent_ranges) / len(recent_ranges)
            rev_real_range = max(
                abs(reversal_candle["real_open"] - reversal_candle["real_close"]),
                abs(reversal_candle["ha_open"] - reversal_candle["ha_close"]),
                avg_recent_range * 0.6
            )

            recent_rsi_list = [c["rsi"] for c in ha_closed[-3:]]

            if reversal_candle["color"] == "RED":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "GREEN")
                if streak >= 3:
                    max_rsi = max(recent_rsi_list)
                    is_downtrend = reversal_candle["real_close"] < reversal_candle["ema"]
                    if (is_downtrend and max_rsi >= RSI_SHORT_TREND - 3.0) or (max_rsi >= RSI_SHORT_OVERBOUGHT - 3.0):
                        candidates[ticker] = {
                            "target_dir": "SHORT",
                            "target_live_color": "GREEN",
                            "streak": streak,
                            "rev_body_pct": reversal_candle["body_pct"],
                            "rev_close_price": reversal_candle["real_close"],
                            "rev_range": rev_real_range,
                            "peak_rsi": max_rsi
                        }

            elif reversal_candle["color"] == "GREEN":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "RED")
                if streak >= 3:
                    min_rsi = min(recent_rsi_list)
                    is_uptrend = reversal_candle["real_close"] > reversal_candle["ema"]
                    if (is_uptrend and min_rsi <= RSI_LONG_TREND + 3.0) or (min_rsi <= RSI_LONG_OVERSOLD + 3.0):
                        candidates[ticker] = {
                            "target_dir": "LONG",
                            "target_live_color": "RED",
                            "streak": streak,
                            "rev_body_pct": reversal_candle["body_pct"],
                            "rev_close_price": reversal_candle["real_close"],
                            "rev_range": rev_real_range,
                            "peak_rsi": min_rsi
                        }

    try:
        await asyncio.gather(*(check_one(t, s) for t, s in active_symbols.items()))
    finally:
        is_scanning = False

    return candidates


async def monitor_candidate_live(exchange, session, sem, ticker, info, server_time_ms, hour_open_ts, rem_min):
    if (ticker, hour_open_ts) in sent_signals or ticker not in active_symbols:
        return

    ccxt_sym = active_symbols[ticker]
    async with sem:
        try:
            ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
        except Exception:
            return

    if not ohlcv or len(ohlcv) < 55:
        return

    ha_candles = calculate_heikin_ashi(ohlcv)
    live_candle = ha_candles[-1]

    if live_candle["timestamp"] != hour_open_ts:
        return

    current_price = live_candle["real_close"]
    open_price = live_candle["real_open"]
    ema_val = live_candle["ema"]
    live_rsi = live_candle["rsi"]
    rev_range = info["rev_range"]
    target_dir = info["target_dir"]

    trigger_reason = None
    trend_mode = None
    effective_rsi = live_rsi

    if target_dir == "SHORT" and current_price > open_price:
        pullback_abs = current_price - open_price
        pullback_pct = (pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (pullback_abs / rev_range) if rev_range > 0 else 0.0

        if pullback_pct >= MIN_PULLBACK_PCT and (retrace_ratio >= PULLBACK_RETRACE_RATIO or pullback_pct >= DIRECT_PULLBACK_PCT):
            effective_rsi = max(live_rsi, info["peak_rsi"])
            in_downtrend = current_price < ema_val

            if in_downtrend and effective_rsi >= RSI_SHORT_TREND:
                trend_mode = f"По тренду вниз (&lt; EMA{EMA_PERIOD}) + RSI {effective_rsi:.1f}"
            elif effective_rsi >= RSI_SHORT_OVERBOUGHT:
                trend_mode = f"Перекупленность (RSI {effective_rsi:.1f} &gt;= {RSI_SHORT_OVERBOUGHT:.0f})"

            if trend_mode:
                ha_tag = "Зеленая HA + " if live_candle["color"] == "GREEN" else ""
                trigger_reason = f"{ha_tag}Откат вверх +{pullback_pct:.2f}% ({retrace_ratio*100:.0f}% размаха)"

    elif target_dir == "LONG" and current_price < open_price:
        pullback_abs = open_price - current_price
        pullback_pct = (pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (pullback_abs / rev_range) if rev_range > 0 else 0.0

        if pullback_pct >= MIN_PULLBACK_PCT and (retrace_ratio >= PULLBACK_RETRACE_RATIO or pullback_pct >= DIRECT_PULLBACK_PCT):
            effective_rsi = min(live_rsi, info["peak_rsi"])
            in_uptrend = current_price > ema_val

            if in_uptrend and effective_rsi <= RSI_LONG_TREND:
                trend_mode = f"По тренду вверх (&gt; EMA{EMA_PERIOD}) + RSI {effective_rsi:.1f}"
            elif effective_rsi <= RSI_LONG_OVERSOLD:
                trend_mode = f"Перепроданность (RSI {effective_rsi:.1f} &lt;= {RSI_LONG_OVERSOLD:.0f})"

            if trend_mode:
                ha_tag = "Красная HA + " if live_candle["color"] == "RED" else ""
                trigger_reason = f"{ha_tag}Откат вниз -{pullback_pct:.2f}% ({retrace_ratio*100:.0f}% размаха)"

    if trigger_reason and trend_mode:
        trigger_confirm_counter[ticker] = trigger_confirm_counter.get(ticker, 0) + 1
    else:
        trigger_confirm_counter[ticker] = 0
        return

    if trigger_confirm_counter[ticker] >= CONFIRM_CHECKS:
        dt_str = datetime.datetime.fromtimestamp(
            server_time_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")

        signal_id = f"{ticker}_{hour_open_ts // 1000}"
        clean_trend_csv = trend_mode.replace("&lt;", "<").replace("&gt;", ">")
        signal_data = {
            "signal_id": signal_id,
            "signal_time_utc": dt_str,
            "ticker": ticker,
            "direction": target_dir,
            "entry_price": current_price,
            "remaining_minutes": f"{rem_min:.1f}",
            "streak_candles": info["streak"],
            "reversal_body_pct": f"{info['rev_body_pct']:.4f}",
            "live_body_pct": f"{live_candle['body_pct']:.4f}",
            "rsi_value": f"{effective_rsi:.1f}",
            "trend_mode": clean_trend_csv,
            "trigger_reason": trigger_reason,
            "status": "ОЖИДАЕТ",
            "updated_time_utc": dt_str
        }

        sent_ok = await send_telegram_signal(session, signal_data)
        if sent_ok:
            sent_signals.add((ticker, hour_open_ts))
            log_new_signal(signal_data)
            print(
                f"[СИГНАЛ ОТПРАВЛЕН] {ticker} -> {target_dir} | {clean_trend_csv} | {trigger_reason} | Цена: {current_price}",
                flush=True
            )


# ==================== ТОЧКА ВХОДА ====================
async def main():
    global active_symbols, current_hour_ts, hourly_candidates, trigger_confirm_counter

    init_csv()
    # Отключаем встроенный тормоз enableRateLimit и управляем скоростью через Semaphore(15)
    exchange = ccxt.mexc({
        "enableRateLimit": False,
        "options": {"defaultType": "swap"}
    })

    live_sem = asyncio.Semaphore(LIVE_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        active_symbols = await load_all_mexc_usdt_swaps(exchange)
        tg_task = asyncio.create_task(telegram_polling_worker(session))

        print("Бот успешно запущен и отслеживает ВСЕ фьючерсы MEXC...", flush=True)
        try:
            while True:
                try:
                    server_time_ms = await exchange.fetch_time()
                    hour_ms = 3600 * 1000
                    hour_open_ts = (server_time_ms // hour_ms) * hour_ms
                    hour_close_ts = hour_open_ts + hour_ms
                    remaining_minutes = (hour_close_ts - server_time_ms) / 60000.0

                    if hour_open_ts != current_hour_ts and remaining_minutes <= SCAN_MAX_REMAINING_MIN:
                        current_hour_ts = hour_open_ts
                        trigger_confirm_counter.clear()

                        # Раз в час обновляем список фьючерсов на случай новых листингов
                        try:
                            active_symbols = await load_all_mexc_usdt_swaps(exchange)
                        except Exception:
                            pass

                        print(f"\n[Новый час] Сканирование всех {len(active_symbols)} фьючерсов MEXC...", flush=True)
                        t0 = datetime.datetime.now()
                        hourly_candidates = await scan_hourly_candidates(exchange, hour_open_ts)
                        elapsed = (datetime.datetime.now() - t0).total_seconds()
                        print(
                            f"[Кандидаты часа] Проверено за {elapsed:.1f} сек. Найдено сетапов: {len(hourly_candidates)} -> {list(hourly_candidates.keys())}",
                            flush=True
                        )

                    if MIN_REMAINING_MINUTES <= remaining_minutes <= ENTRY_MAX_REMAINING_MIN and hourly_candidates:
                        tasks = [
                            monitor_candidate_live(
                                exchange, session, live_sem, ticker, info,
                                server_time_ms, hour_open_ts, remaining_minutes
                            )
                            for ticker, info in hourly_candidates.items()
                            if (ticker, hour_open_ts) not in sent_signals
                        ]
                        if tasks:
                            await asyncio.gather(*tasks)

                except Exception as loop_err:
                    print(f"[Ошибка основного цикла]: {loop_err}", flush=True)

                await asyncio.sleep(LIVE_POLL_INTERVAL)
        finally:
            tg_task.cancel()
            await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
