import asyncio
import csv
import datetime
import os
import aiohttp
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ВАШ_CHAT_ID")

TIMEFRAME = "1h"
HISTORY_LIMIT = 150            # Глубина истории для точного расчета Heikin Ashi
LIVE_POLL_INTERVAL = 3         # Опрос кандидатов каждые 3 сек
MIN_REMAINING_MINUTES = 30.0   # Вход разрешен только если до конца часа >= 30 мин
MAX_REMAINING_MINUTES = 59.8   # Пропускаем первые 12 секунд часа для закрытия старых свечей
MIN_BODY_PCT = 0.001           # Минимальный порог тела свечи (0.001% — защита только от 100% равенства цен)
CONFIRM_CHECKS = 1             # Сигнал сразу при появлении нужного цвета

LOG_FILE = "signals_log.csv"

RAW_TICKERS = [
    # Скрин 1
    "BTWUSDT", "ZECUSDT", "TAOUSDT", "LINKUSDT", "ONDOUSDT", "AKEUSDT", "ADAUSDT",
    # Скрин 2
    "INJUSDT", "OPUSDT", "LTCUSDT", "AAVEUSDT", "FILUSDT", "HBARUSDT", "APTUSDT",
    # Скрин 3
    "ICPUSDT", "XLMUSDT", "XPLUSDT", "JTOUSDT", "RIVERUSDT", "ATOMUSDT", "SHIBUSDT",
    # Скрин 4
    "ASTERUSDT", "PONSUSDT", "龙虾USDT", "LABUSDT", "DASHUSDT", "VVVUSDT", "PENGUUSDT",
    # Скрин 5
    "LDOUSDT", "VIRTUALUSDT", "BRUSDT", "GALAUSDT", "CHIPUSDT", "MEGAUSDT", "TRUMPUSDT",
    # Скрин 6
    "POLUSDT", "FLOKIUSDT", "ONEUSDT", "JASMYUSDT", "1000BONKUSDT", "COAIUSDT", "APEUSDT",
    # Скрин 7
    "LSKUSDT", "VELVETUSDT", "ORDIUSDT", "WLFIUSDT", "ACEUSDT", "LYNUSDT", "COPPER(XCU)USDT",
    # Скрин 8
    "SPXUSDT", "KAITOUSDT", "MYXUSDT", "SYNUSDT", "ESPORTSUSDT", "BILLUSDT", "BLESSUSDT",
    # Скрин 9
    "UAIUSDT", "DEXEUSDT", "PIPPINUSDT", "CYSUSDT", "POWERUSDT", "TUTUSDT", "XAIUSDT",
    # Скрин 10
    "LIGHTUSDT", "GAS(NG)USDT", "ALLOUSDT", "HNTUSDT", "SKYAIUSDT", "PLATINUM(XPT)USDT", "EVAAUSDT",
    # Скрин 11
    "PALLADIUM(XPD)USDT", "BICOUSDT", "BTRUSDT", "HEIUSDT", "REUSDT", "FOLKSUSDT", "STOUSDT",
    # Скрин 12
    "NICKEL(XNI)USDT", "LAYERUSDT", "ALUMINIUM(XAL)USDT", "LEAD(XPB)USDT", "ZINC(XZN)USDT", "ACNUSDT", "AKAMUSDT",
]

active_symbols = {}
current_hour_ts = 0
hourly_candidates = {}
color_confirm_counter = {}
sent_signals = set()


# ==================== РАБОТА С БАЗОЙ СТАТИСТИКИ (CSV) ====================
CSV_HEADERS = [
    "signal_id", "signal_time_utc", "ticker", "direction", "entry_price",
    "remaining_minutes", "streak_candles", "reversal_body_pct", "live_body_pct",
    "status", "updated_time_utc"
]

def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()

def log_new_signal(data: dict):
    init_csv()
    with open(LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(data)

def update_signal_status(signal_id: str, new_status: str) -> dict | None:
    init_csv()
    rows = []
    updated_row = None
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["signal_id"] == signal_id:
                row["status"] = new_status
                row["updated_time_utc"] = now_str
                updated_row = row
            rows.append(row)

    if updated_row:
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
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
            st = row["status"]
            d = row["direction"]
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
    return (
        f"📊 <b>Статистика сигналов:</b>\n\n"
        f"Всего сигналов: <b>{total}</b>\n"
        f"✅ Зашло: <b>{win}</b>\n"
        f"❌ Не зашло: <b>{loss}</b>\n"
        f"⏳ Не отмечено: <b>{pending}</b>\n"
        f"🎯 <b>Винрейт (по отмеченным): {winrate:.1f}%</b>\n\n"
        f"🟢 LONG: {long_win} зашло / {long_loss} не зашло\n"
        f"🔴 SHORT: {short_win} зашло / {short_loss} не зашло\n\n"
        f"📥 Для выгрузки файла анализа отправьте /export"
    )


# ==================== РАСЧЕТ HEIKIN ASHI ====================
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
            "real_close": c,
            "body_pct": body_pct,
            "color": color
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
    status = data["status"]

    mexc_pair = ticker[:-4] + "_USDT"
    chart_url = f"https://futures.mexc.com/exchange/{mexc_pair}"

    if direction == "SHORT":
        header = "🔴 <b>СИГНАЛ: SHORT (ВНИЗ)</b>"
        chain = f"{'🟢' * min(streak, 6)} ({streak} закр.) ➔ 🔴 (1 закр.) ➔ 🟢 (живая)"
    else:
        header = "🟢 <b>СИГНАЛ: LONG (ВВЕРХ)</b>"
        chain = f"{'🔴' * min(streak, 6)} ({streak} закр.) ➔ 🟢 (1 закр.) ➔ 🔴 (живая)"

    if status == "ЗАШЕЛ":
        status_line = "✅ <b>Результат: СИГНАЛ ЗАШЕЛ</b>"
    elif status == "НЕ ЗАШЕЛ":
        status_line = "❌ <b>Результат: НЕ ЗАШЕЛ</b>"
    else:
        status_line = "⏳ <b>Результат: Ожидает отметки (нажми кнопку ниже)</b>"

    return (
        f"{header}\n\n"
        f"📌 <b>Пара:</b> <code>{ticker}</code> (<a href='{chart_url}'>Открыть график</a>)\n"
        f"💰 <b>Цена входа:</b> <code>{price}</code>\n"
        f"🕒 <b>Время (MEXC):</b> {dt_str} UTC\n"
        f"⏱ <b>До закрытия 1H свечи:</b> {rem_min:.1f} мин.\n\n"
        f"📊 <b>Цепочка Heikin Ashi (1H):</b>\n{chain}\n"
        f"▪️ Тело разворотной свечи: {rev_pct:.3f}%\n"
        f"▪️ Тело живой свечи: {live_pct:.3f}%\n\n"
        f"{status_line}"
    )

def build_inline_keyboard(signal_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Зашел", "callback_data": f"WIN|{signal_id}"},
                {"text": "❌ Не зашел", "callback_data": f"LOSS|{signal_id}"}
            ]
        ]
    }

async def send_telegram_signal(session: aiohttp.ClientSession, data: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": build_signal_message(data),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_inline_keyboard(data["signal_id"])
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                print(f"[Ошибка TG sendMessage]: {await resp.text()}", flush=True)
    except Exception as e:
        print(f"[Ошибка связи с Telegram]: {e}", flush=True)

async def send_telegram_file(session: aiohttp.ClientSession, chat_id: str):
    init_csv()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(LOG_FILE, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field(
        "caption",
        "📁 Файл статистики сигналов.\n"
        "Отправь его для анализа или перешли обратно боту для восстановления базы."
    )
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
                                    "text": "✅ База статистики <code>signals_log.csv</code> успешно восстановлена!",
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
                        cand_list = ", ".join(
                            [f"{k} ({v['target_dir']})" for k, v in hourly_candidates.items()]
                        ) or "нет кандидатов в этом часе"
                        status_msg = (
                            f"🤖 <b>Бот Heikin Ashi (MEXC Futures 1H) активен</b>\n"
                            f"Отслеживается пар: <b>{len(active_symbols)} из {len(RAW_TICKERS)}</b>\n"
                            f"Кандидаты текущего часа: <b>{len(hourly_candidates)}</b>\n"
                            f"<code>{cand_list}</code>\n\n"
                            f"<b>Доступные команды:</b>\n"
                            f"/stats — сводка результатов и винрейт\n"
                            f"/export — скачать CSV-файл для анализа\n"
                            f"/status — проверить текущих кандидатов часа"
                        )
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id, "text": status_msg, "parse_mode": "HTML"
                        })

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Ошибка поллинга TG]: {e}", flush=True)
            await asyncio.sleep(5)


# ==================== СКАНИРОВАНИЕ MEXC FUTURES ====================
async def resolve_mexc_symbols(exchange):
    markets = await exchange.load_markets()
    resolved = {}

    id_map = {}
    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT":
            raw_id = m.get("id", "").replace("_", "")
            id_map[raw_id] = sym

    for ticker in RAW_TICKERS:
        if ticker in id_map:
            resolved[ticker] = id_map[ticker]

    print(f"[Инициализация] Найдено пар на MEXC Futures: {len(resolved)} из {len(RAW_TICKERS)}", flush=True)
    return resolved


async def scan_hourly_candidates(exchange, hour_open_ts):
    """
    ЭТАП 1: Строго фильтрует только закрытые свечи (timestamp < hour_open_ts)
    и проверяет, что последняя закрытая свеча относится ровно к предыдущему часу (hour_open_ts - 1h).
    """
    candidates = {}
    hour_ms = 3600 * 1000
    prev_closed_ts = hour_open_ts - hour_ms
    sem = asyncio.Semaphore(8)

    async def check_one(ticker, ccxt_sym):
        async with sem:
            try:
                ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
            except Exception:
                return

            if not ohlcv or len(ohlcv) < 10:
                return

            # Берем строго свечи, которые открылись ДО начала текущего часа
            closed_candles = [c for c in ohlcv if c[0] < hour_open_ts]

            # Проверяем, что последняя закрытая свеча — это именно предыдущий час (торги не стояли)
            if len(closed_candles) < 10 or closed_candles[-1][0] != prev_closed_ts:
                return

            ha_closed = calculate_heikin_ashi(closed_candles)
            reversal_candle = ha_closed[-1]
            idx_before_rev = len(ha_closed) - 2

            # Кандидат в SHORT: минимум 3 зеленые -> 1 закрытая красная
            if reversal_candle["color"] == "RED":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "GREEN")
                if streak >= 3:
                    candidates[ticker] = {
                        "target_dir": "SHORT",
                        "target_live_color": "GREEN",
                        "streak": streak,
                        "rev_body_pct": reversal_candle["body_pct"]
                    }

            # Кандидат в LONG: минимум 3 красные -> 1 закрытая зеленая
            elif reversal_candle["color"] == "GREEN":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "RED")
                if streak >= 3:
                    candidates[ticker] = {
                        "target_dir": "LONG",
                        "target_live_color": "RED",
                        "streak": streak,
                        "rev_body_pct": reversal_candle["body_pct"]
                    }

    await asyncio.gather(*(check_one(t, s) for t, s in active_symbols.items()))
    return candidates


async def monitor_candidate_live(exchange, session, ticker, info, server_time_ms, hour_open_ts, rem_min):
    """
    ЭТАП 2: Быстрый мониторинг живой свечи для отобранных кандидатов часа.
    """
    if (ticker, hour_open_ts) in sent_signals:
        return

    ccxt_sym = active_symbols[ticker]
    try:
        ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
    except Exception:
        return

    if not ohlcv:
        return

    # Если биржа еще не сформировала свечу нового часа, берем текущую рыночную цену
    # и строим живую свечу Heikin Ashi относительно последней закрытой
    ha_candles = calculate_heikin_ashi(ohlcv)
    live_candle = ha_candles[-1]

    if live_candle["timestamp"] != hour_open_ts:
        return

    if live_candle["color"] == info["target_live_color"]:
        color_confirm_counter[ticker] = color_confirm_counter.get(ticker, 0) + 1
    else:
        color_confirm_counter[ticker] = 0
        return

    if color_confirm_counter[ticker] >= CONFIRM_CHECKS:
        sent_signals.add((ticker, hour_open_ts))
        dt_str = datetime.datetime.fromtimestamp(
            server_time_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")

        signal_id = f"{ticker}_{hour_open_ts // 1000}"
        signal_data = {
            "signal_id": signal_id,
            "signal_time_utc": dt_str,
            "ticker": ticker,
            "direction": info["target_dir"],
            "entry_price": live_candle["real_close"],
            "remaining_minutes": f"{rem_min:.1f}",
            "streak_candles": info["streak"],
            "reversal_body_pct": f"{info['rev_body_pct']:.4f}",
            "live_body_pct": f"{live_candle['body_pct']:.4f}",
            "status": "ОЖИДАЕТ",
            "updated_time_utc": dt_str
        }

        log_new_signal(signal_data)
        await send_telegram_signal(session, signal_data)
        print(f"[СИГНАЛ] {ticker} -> {info['target_dir']} | Цена: {live_candle['real_close']} | Осталось: {rem_min:.1f} мин.", flush=True)


# ==================== ТОЧКА ВХОДА ====================
async def main():
    global active_symbols, current_hour_ts, hourly_candidates, color_confirm_counter

    init_csv()
    exchange = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"}
    })

    async with aiohttp.ClientSession() as session:
        active_symbols = await resolve_mexc_symbols(exchange)
        tg_task = asyncio.create_task(telegram_polling_worker(session))

        print("Бот успешно запущен на хостинге и отслеживает рынок...", flush=True)
        try:
            while True:
                try:
                    server_time_ms = await exchange.fetch_time()
                    hour_ms = 3600 * 1000
                    hour_open_ts = (server_time_ms // hour_ms) * hour_ms
                    hour_close_ts = hour_open_ts + hour_ms
                    remaining_minutes = (hour_close_ts - server_time_ms) / 60000.0

                    # Сканируем кандидатов при смене часа (когда прошло хотя бы 12 сек нового часа)
                    if hour_open_ts != current_hour_ts and remaining_minutes <= MAX_REMAINING_MINUTES:
                        current_hour_ts = hour_open_ts
                        color_confirm_counter.clear()
                        print(f"\n[Новый час] Сканирование всех {len(active_symbols)} пар...", flush=True)
                        hourly_candidates = await scan_hourly_candidates(exchange, hour_open_ts)
                        print(f"[Кандидаты часа] Найдено сетапов: {len(hourly_candidates)} -> {list(hourly_candidates.keys())}", flush=True)

                    if MIN_REMAINING_MINUTES <= remaining_minutes <= MAX_REMAINING_MINUTES and hourly_candidates:
                        tasks = [
                            monitor_candidate_live(
                                exchange, session, ticker, info,
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
