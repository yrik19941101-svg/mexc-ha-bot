import asyncio
import csv
import datetime
import os
import aiohttp
import ccxt.async_support as ccxt

# ==================== ОСНОВНЫЕ НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAME = "1h"
HISTORY_LIMIT = 120
LIVE_POLL_INTERVAL = 3
MIN_REMAINING_MINUTES = 25.0    # Вход разрешен в первые 35 минут часа (остаток >= 25 мин)
SCAN_MAX_REMAINING_MIN = 59.75  # Сканируем рынок через 15 секунд после начала часа
ENTRY_MAX_REMAINING_MIN = 59.0  # Мониторинг точки входа через 1 минуту после начала часа
MIN_BODY_PCT = 0.005
CONFIRM_CHECKS = 2              # Подтверждение отбоя 2 проверки подряд (~6 сек)

# Ликвидность и скорость сканирования BingX
MIN_24H_VOLUME_USDT = 1_500_000 # Берем только ликвидные пары с оборотом >= 1.5 млн USDT за 24ч
SCAN_CONCURRENCY = 10
LIVE_CONCURRENCY = 8

# --- ЭТАП 3: СИЛА РАЗВОРОТНОЙ СВЕЧИ НА 1H ---
REV_MIN_STRENGTH_RATIO = 0.35   # Тело разворотной свечи >= 35% от среднего тела серии
REV_MIN_ABS_BODY_PCT = 0.08     # Минимальное тело разворотной свечи HA (0.08%)

# --- ЭТАП 4: ДВУХФАЗНЫЙ ВХОД (ФАЗА 1: ОТКАТ -> ФАЗА 2: ОТБОЙ ОТ ПИКА) ---
PULLBACK_RETRACE_RATIO = 0.45   # Фаза 1: откат >= 45% от размаха предыдущей свечи
MIN_PULLBACK_PCT = 0.50         # Фаза 1: минимальный откат от цены открытия часа (0.50%)
DIRECT_PULLBACK_PCT = 0.85      # Фаза 1: безусловный глубокий откат от открытия часа (0.85%)

MIN_BOUNCE_PCT = 0.18           # Фаза 2: минимальный отскок цены от пика отката в нашу сторону (0.18%)
MIN_BOUNCE_RATIO = 0.22         # Фаза 2: цена должна отбить минимум 22% от длины волны отката

# --- НАСТРОЙКИ RSI (ДЛЯ РАЗВОРОТОВ НА ЭКСТРЕМУМАХ) ---
RSI_PERIOD = 14
RSI_SHORT_OVERBOUGHT = 68.0     # Перекупленность для SHORT, если 1D еще зеленый
RSI_LONG_OVERSOLD = 32.0        # Перепроданность для LONG, если 1D еще красный

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
    "rsi_value", "mtf_mode", "trigger_reason", "status", "updated_time_utc"
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
                row["trigger_reason"] = "Откат + Отбой"
            if "rsi_value" not in row or not row["rsi_value"]:
                row["rsi_value"] = "50.0"
            if "mtf_mode" not in row or not row["mtf_mode"]:
                row["mtf_mode"] = "1D + 6H + 1H"
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
        "📊 <b>Статистика сигналов (BingX Futures):</b>",
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


# ==================== РАСЧЕТ HEIKIN ASHI, 6H И RSI ====================
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


def build_6h_ohlcv_from_1h(ohlcv_1h: list) -> list:
    six_h_ms = 6 * 3600 * 1000
    buckets = {}
    for c in ohlcv_1h:
        ts, o, h, l, cl, v = c
        b_ts = (ts // six_h_ms) * six_h_ms
        if b_ts not in buckets:
            buckets[b_ts] = [b_ts, o, h, l, cl, v]
        else:
            b = buckets[b_ts]
            if h > b[2]:
                b[2] = h
            if l < b[3]:
                b[3] = l
            b[4] = cl
            b[5] += v

    return [buckets[k] for k in sorted(buckets.keys())]


def calculate_heikin_ashi(ohlcv):
    closes = [c[4] for c in ohlcv]
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


def get_htf_color(ha_candles: list) -> str:
    if not ha_candles:
        return "DOJI"
    last_c = ha_candles[-1]
    if last_c["color"] != "DOJI" and last_c["body_pct"] >= 0.03:
        return last_c["color"]
    if len(ha_candles) >= 2:
        return ha_candles[-2]["color"]
    return last_c["color"]


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
    mtf_mode = data.get("mtf_mode", "1D + 6H + 1H")
    trigger_reason = data.get("trigger_reason", "Откат + Отбой")
    status = data["status"]

    bingx_pair = ticker[:-4] + "-USDT" if ticker.endswith("USDT") and "-" not in ticker else ticker
    chart_url = f"https://bingx.com/ru-ru/perpetual/{bingx_pair}"

    if direction == "SHORT":
        header = "🔴 <b>СИГНАЛ BINGX: SHORT (ВНИЗ)</b>"
        chain = f"{'🟢' * min(streak, 6)} ({streak} закр.) ➔ 🔴 (1 закр.) ➔ 🎯 {trigger_reason}"
    else:
        header = "🟢 <b>СИГНАЛ BINGX: LONG (ВВЕРХ)</b>"
        chain = f"{'🔴' * min(streak, 6)} ({streak} закр.) ➔ 🟢 (1 закр.) ➔ 🎯 {trigger_reason}"

    if status == "ЗАШЕЛ":
        status_line = "✅ <b>Результат: СИГНАЛ ЗАШЕЛ</b>"
    elif status == "НЕ ЗАШЕЛ":
        status_line = "❌ <b>Результат: НЕ ЗАШЕЛ</b>"
    else:
        status_line = "⏳ <b>Результат: Ожидает отметки (нажми кнопку ниже)</b>"

    lines = [
        header,
        "",
        f"📌 <b>Пара:</b> <code>{bingx_pair}</code> (<a href='{chart_url}'>График BingX</a>)",
        f"💰 <b>Цена входа:</b> <code>{price}</code>",
        f"🧭 <b>Старшие ТФ:</b> {mtf_mode}",
        f"📈 <b>RSI (1H):</b> {rsi_val:.1f}",
        f"🎯 <b>Точка входа:</b> {trigger_reason}",
        f"🕒 <b>Время:</b> {dt_str} UTC",
        f"⏱ <b>До закрытия 1H свечи:</b> {rem_min:.1f} мин.",
        "",
        "📊 <b>Цепочка Heikin Ashi (1H):</b>",
        chain,
        f"▪️ Сила разворотной свечи: {rev_pct:.3f}%",
        f"▪️ Текущее тело HA: {live_pct:.3f}%",
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
    form.add_field("caption", "📁 Файл статистики сигналов (BingX Futures).")
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
                    elif text.startswith("/test"):
                        test_data = {
                            "signal_id": "TEST_0",
                            "signal_time_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "ticker": "BTCUSDT",
                            "direction": "SHORT",
                            "entry_price": "68450.0",
                            "remaining_minutes": "42.5",
                            "streak_candles": 4,
                            "reversal_body_pct": "0.4500",
                            "live_body_pct": "0.1900",
                            "rsi_value": "71.2",
                            "mtf_mode": "1D (🔴) + 6H (🔴) По тренду",
                            "trigger_reason": "Пик +0.72% ➔ Отбой вниз -0.24%",
                            "status": "ОЖИДАЕТ",
                            "updated_time_utc": ""
                        }
                        await send_telegram_signal(session, test_data)
                    elif text.startswith("/status") or text.startswith("/start"):
                        items = [
                            f"{k} ({v['target_dir']}, RSI:{v['peak_rsi']:.0f})"
                            for k, v in hourly_candidates.items()
                        ]
                        if len(items) > 40:
                            cand_list = ", ".join(items[:40]) + f" ... и еще {len(items) - 40}"
                        else:
                            cand_list = ", ".join(items) or "нет кандидатов в этом часе"

                        scan_state = "⏳ Идет сканирование BingX..." if is_scanning else "✅ Готово"
                        saved_cid = get_target_chat_id()
                        status_lines = [
                            "🤖 <b>Бот BingX Futures (1D ➔ 6H ➔ 1H + Отбой)</b>",
                            f"📩 <b>Chat ID:</b> <code>{saved_cid}</code> (подключен ✅)",
                            f"💧 <b>Ликвидных пар в работе:</b> {len(active_symbols)} (объем &gt;= $1.5M)",
                            f"🔄 <b>Статус сканера:</b> {scan_state}",
                            f"⚙️ <b>Вход:</b> Откат от {MIN_PULLBACK_PCT}% ➔ Отбой от пика {MIN_BOUNCE_PCT}%",
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


# ==================== ЗАГРУЗКА ЛИКВИДНЫХ ФЬЮЧЕРСОВ BINGX ====================
async def load_liquid_bingx_swaps(exchange):
    markets = await exchange.load_markets(True)
    all_swaps = {}

    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
            raw_id = m.get("id", "").replace("-", "").replace("_", "")
            if not raw_id:
                raw_id = sym.split(":")[0].replace("/", "")
            all_swaps[sym] = raw_id

    liquid_resolved = {}
    try:
        tickers = await exchange.fetch_tickers(list(all_swaps.keys()))
        for sym, t in tickers.items():
            if sym not in all_swaps:
                continue
            qv = t.get("quoteVolume")
            if not qv:
                info = t.get("info", {})
                qv = float(info.get("quoteVolume") or info.get("turnover") or 0.0)
            if not qv and t.get("baseVolume") and t.get("last"):
                qv = float(t["baseVolume"]) * float(t["last"])

            if qv and float(qv) >= MIN_24H_VOLUME_USDT:
                raw_id = all_swaps[sym]
                liquid_resolved[raw_id] = sym
    except Exception as e:
        print(f"[Предупреждение] Не удалось отфильтровать объем через fetch_tickers ({e}), берем все пары.", flush=True)

    if len(liquid_resolved) < 20:
        for sym, raw_id in all_swaps.items():
            liquid_resolved[raw_id] = sym

    print(
        f"[BingX Инициализация] Отобрано ликвидных USDT-фьючерсов: {len(liquid_resolved)} из {len(all_swaps)}",
        flush=True
    )
    return liquid_resolved


# ==================== ЭТАПЫ 1-3: ВОРОНКА 1D -> 6H -> 1H ====================
async def scan_hourly_candidates(exchange, hour_open_ts):
    global is_scanning
    is_scanning = True
    candidates = {}
    hour_ms = 3600 * 1000
    prev_closed_ts = hour_open_ts - hour_ms
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def check_one(ticker, ccxt_sym):
        async with sem:
            await asyncio.sleep(0.04)
            try:
                ohlcv_1h = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1h", limit=HISTORY_LIMIT)
            except Exception:
                return

            if not ohlcv_1h or len(ohlcv_1h) < 55:
                return

            closed_1h = [c for c in ohlcv_1h if c[0] < hour_open_ts]
            if len(closed_1h) < 40 or closed_1h[-1][0] != prev_closed_ts:
                return

            ha_1h = calculate_heikin_ashi(closed_1h)
            reversal_1h = ha_1h[-1]
            idx_before_rev = len(ha_1h) - 2

            # 1. Проверяем серию 3+ одноцветных свечей и разворотную свечу на 1H
            target_dir = None
            streak_1h = 0

            if reversal_1h["color"] == "RED":
                streak_1h = count_consecutive_color(ha_1h, idx_before_rev, "GREEN")
                if streak_1h >= 3:
                    target_dir = "SHORT"
            elif reversal_1h["color"] == "GREEN":
                streak_1h = count_consecutive_color(ha_1h, idx_before_rev, "RED")
                if streak_1h >= 3:
                    target_dir = "LONG"

            if not target_dir:
                return

            # 2. Фильтр силы разворотной свечи (отсекаем слабые микро-свечи)
            streak_slice = ha_1h[idx_before_rev - streak_1h + 1 : idx_before_rev + 1]
            avg_streak_body = sum(c["body_pct"] for c in streak_slice) / len(streak_slice)
            if reversal_1h["body_pct"] < max(REV_MIN_ABS_BODY_PCT, avg_streak_body * REV_MIN_STRENGTH_RATIO):
                return

            # 3. Проверяем 6-часовой таймфрейм (6H HA должен смотреть в сторону сделки)
            ohlcv_6h = build_6h_ohlcv_from_1h(closed_1h)
            ha_6h = calculate_heikin_ashi(ohlcv_6h)
            color_6h = get_htf_color(ha_6h)

            if target_dir == "SHORT" and color_6h != "RED":
                return
            if target_dir == "LONG" and color_6h != "GREEN":
                return

            # 4. Проверяем Дневной таймфрейм (1D HA в цвет сделки ИЛИ экстремальный RSI на 1H)
            try:
                ohlcv_1d = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1d", limit=40)
            except Exception:
                return

            if not ohlcv_1d or len(ohlcv_1d) < 5:
                return

            ha_1d = calculate_heikin_ashi(ohlcv_1d)
            color_1d = get_htf_color(ha_1d)
            recent_rsi = [c["rsi"] for c in ha_1h[-3:]]

            mtf_mode = None
            peak_rsi = reversal_1h["rsi"]

            if target_dir == "SHORT":
                peak_rsi = max(recent_rsi)
                if color_1d == "RED" and peak_rsi >= 48.0:
                    mtf_mode = "1D (🔴) + 6H (🔴) По тренду"
                elif peak_rsi >= RSI_SHORT_OVERBOUGHT:
                    mtf_mode = f"6H (🔴) + Перекупленность RSI {peak_rsi:.0f}"
            else:
                peak_rsi = min(recent_rsi)
                if color_1d == "GREEN" and peak_rsi <= 52.0:
                    mtf_mode = "1D (🟢) + 6H (🟢) По тренду"
                elif peak_rsi <= RSI_LONG_OVERSOLD:
                    mtf_mode = f"6H (🟢) + Перепроданность RSI {peak_rsi:.0f}"

            if not mtf_mode:
                return

            recent_ranges = [abs(c["real_high"] - c["real_low"]) for c in ha_1h[-5:]]
            avg_recent_range = sum(recent_ranges) / len(recent_ranges)
            rev_real_range = max(
                abs(reversal_1h["real_open"] - reversal_1h["real_close"]),
                abs(reversal_1h["ha_open"] - reversal_1h["ha_close"]),
                avg_recent_range * 0.55
            )

            candidates[ticker] = {
                "target_dir": target_dir,
                "streak": streak_1h,
                "rev_body_pct": reversal_1h["body_pct"],
                "rev_range": rev_real_range,
                "peak_rsi": peak_rsi,
                "mtf_mode": mtf_mode
            }

    try:
        await asyncio.gather(*(check_one(t, s) for t, s in active_symbols.items()))
    finally:
        is_scanning = False

    return candidates


# ==================== ЭТАП 4: СНАЙПЕРСКИЙ ВХОД НА ЗАТУХАНИИ ОТКАТА ====================
async def monitor_candidate_live(exchange, session, sem, ticker, info, server_time_ms, hour_open_ts, rem_min):
    if (ticker, hour_open_ts) in sent_signals or ticker not in active_symbols:
        return

    ccxt_sym = active_symbols[ticker]
    async with sem:
        try:
            ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1h", limit=50)
        except Exception:
            return

    if not ohlcv or len(ohlcv) < 20:
        return

    ha_candles = calculate_heikin_ashi(ohlcv)
    live_candle = ha_candles[-1]

    if live_candle["timestamp"] != hour_open_ts:
        return

    open_price = live_candle["real_open"]
    current_price = live_candle["real_close"]
    high_price = live_candle["real_high"]
    low_price = live_candle["real_low"]
    rev_range = info["rev_range"]
    target_dir = info["target_dir"]

    trigger_reason = None

    if target_dir == "SHORT":
        # ФАЗА 1: Был ли достаточный заброс цены ВВЕРХ (откат) от открытия часа
        max_pullback_abs = high_price - open_price
        max_pullback_pct = (max_pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (max_pullback_abs / rev_range) if rev_range > 0 else 0.0

        pullback_ok = (
            max_pullback_pct >= MIN_PULLBACK_PCT
            and (retrace_ratio >= PULLBACK_RETRACE_RATIO or max_pullback_pct >= DIRECT_PULLBACK_PCT)
        )

        # ФАЗА 2: Остановился ли откат и пошла ли цена обратно ВНИЗ от максимума свечи
        if pullback_ok and current_price > open_price:
            bounce_abs = high_price - current_price
            bounce_pct = (bounce_abs / high_price) * 100.0 if high_price > 0 else 0.0
            bounce_ratio = (bounce_abs / max_pullback_abs) if max_pullback_abs > 0 else 0.0

            if bounce_pct >= MIN_BOUNCE_PCT and 0.22 <= bounce_ratio <= 0.85:
                trigger_reason = f"Пик +{max_pullback_pct:.2f}% ➔ Отбой вниз -{bounce_pct:.2f}%"

    elif target_dir == "LONG":
        # ФАЗА 1: Был ли достаточный пролив цены ВНИЗ (откат) от открытия часа
        max_pullback_abs = open_price - low_price
        max_pullback_pct = (max_pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (max_pullback_abs / rev_range) if rev_range > 0 else 0.0

        pullback_ok = (
            max_pullback_pct >= MIN_PULLBACK_PCT
            and (retrace_ratio >= PULLBACK_RETRACE_RATIO or max_pullback_pct >= DIRECT_PULLBACK_PCT)
        )

        # ФАЗА 2: Остановился ли откат и пошла ли цена обратно ВВЕРХ от минимума свечи
        if pullback_ok and current_price < open_price:
            bounce_abs = current_price - low_price
            bounce_pct = (bounce_abs / low_price) * 100.0 if low_price > 0 else 0.0
            bounce_ratio = (bounce_abs / max_pullback_abs) if max_pullback_abs > 0 else 0.0

            if bounce_pct >= MIN_BOUNCE_PCT and 0.22 <= bounce_ratio <= 0.85:
                trigger_reason = f"Дно -{max_pullback_pct:.2f}% ➔ Отбой вверх +{bounce_pct:.2f}%"

    if trigger_reason:
        trigger_confirm_counter[ticker] = trigger_confirm_counter.get(ticker, 0) + 1
    else:
        trigger_confirm_counter[ticker] = 0
        return

    if trigger_confirm_counter[ticker] >= CONFIRM_CHECKS:
        dt_str = datetime.datetime.fromtimestamp(
            server_time_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")

        signal_id = f"{ticker}_{hour_open_ts // 1000}"
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
            "rsi_value": f"{info['peak_rsi']:.1f}",
            "mtf_mode": info["mtf_mode"],
            "trigger_reason": trigger_reason,
            "status": "ОЖИДАЕТ",
            "updated_time_utc": dt_str
        }

        sent_ok = await send_telegram_signal(session, signal_data)
        if sent_ok:
            sent_signals.add((ticker, hour_open_ts))
            log_new_signal(signal_data)
            print(
                f"[СИГНАЛ BINGX] {ticker} -> {target_dir} | {info['mtf_mode']} | {trigger_reason} | Цена: {current_price}",
                flush=True
            )


# ==================== ТОЧКА ВХОДА ====================
async def get_exchange_time_ms(exchange) -> int:
    try:
        t = await exchange.fetch_time()
        if t:
            return int(t)
    except Exception:
        pass
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


async def main():
    global active_symbols, current_hour_ts, hourly_candidates, trigger_confirm_counter

    init_csv()
    exchange = ccxt.bingx({
        "enableRateLimit": False,
        "options": {"defaultType": "swap"}
    })

    live_sem = asyncio.Semaphore(LIVE_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        active_symbols = await load_liquid_bingx_swaps(exchange)
        tg_task = asyncio.create_task(telegram_polling_worker(session))

        print("Бот успешно запущен и отслеживает ликвидные фьючерсы BingX...", flush=True)
        try:
            while True:
                try:
                    server_time_ms = await get_exchange_time_ms(exchange)
                    hour_ms = 3600 * 1000
                    hour_open_ts = (server_time_ms // hour_ms) * hour_ms
                    hour_close_ts = hour_open_ts + hour_ms
                    remaining_minutes = (hour_close_ts - server_time_ms) / 60000.0

                    if hour_open_ts != current_hour_ts and remaining_minutes <= SCAN_MAX_REMAINING_MIN:
                        current_hour_ts = hour_open_ts
                        trigger_confirm_counter.clear()

                        try:
                            active_symbols = await load_liquid_bingx_swaps(exchange)
                        except Exception:
                            pass

                        print(f"\n[Новый час] Сканирование {len(active_symbols)} ликвидных пар BingX...", flush=True)
                        t0 = datetime.datetime.now()
                        hourly_candidates = await scan_hourly_candidates(exchange, hour_open_ts)
                        elapsed = (datetime.datetime.now() - t0).total_seconds()
                        print(
                            f"[Кандидаты BingX] Проверено за {elapsed:.1f} сек. Найдено: {len(hourly_candidates)} -> {list(hourly_candidates.keys())}",
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
