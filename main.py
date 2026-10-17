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
HISTORY_LIMIT = 150            # Глубина истории для точного расчета Heikin Ashi
LIVE_POLL_INTERVAL = 3         # Опрос кандидатов каждые 3 сек
MIN_REMAINING_MINUTES = 30.0   # Вход разрешен только если до конца часа >= 30 мин
SCAN_MAX_REMAINING_MIN = 59.75 # Сканируем кандидатов через 15 секунд после начала часа
ENTRY_MAX_REMAINING_MIN = 58.5 # Входы разрешены через 1.5 минуты после начала часа
MIN_BODY_PCT = 0.005           # Минимальный порог тела свечи HA
CONFIRM_CHECKS = 2             # Подтверждение условия 2 проверки подряд (~6 сек защиты от прокола)

# --- НАСТРОЙКИ НАСТОЯЩЕГО ОТКАТА ОТ ОТКРЫТИЯ ЧАСА ---
PULLBACK_RETRACE_RATIO = 0.40  # Откат от открытия часа должен составить >= 40% от тела прошлой свечи
MIN_PULLBACK_PCT = 0.35        # Обязательный минимальный откат от цены открытия часа в % (защита от входа на месте)
DIRECT_PULLBACK_PCT = 0.65     # Сильный откат в % от открытия часа (даже если прошлая свеча была огромной)

LOG_FILE = "signals_log.csv"
CHAT_ID_FILE = "chat_id.txt"

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
trigger_confirm_counter = {}
sent_signals = set()


# ==================== АВТО-СОХРАНЕНИЕ CHAT_ID ====================
def get_target_chat_id() -> str:
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != "ВАШ_CHAT_ID" and TELEGRAM_CHAT_ID.strip():
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
    "trigger_reason", "status", "updated_time_utc"
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

def update_signal_status(signal_id: str, new_status: str) -> dict | None:
    init_csv()
    rows = []
    updated_row = None
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "trigger_reason" not in row or not row["trigger_reason"]:
                row["trigger_reason"] = "Смена цвета HA"
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
    return (
        f"📊 <b>Статистика сигналов:</b>\n\n"
        f"Всего сигналов
