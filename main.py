import os
import time
import asyncio
from dotenv import load_dotenv
import aiohttp
from pybit.unified_trading import HTTP

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# ==================== ПАРАМЕТРЫ СТРАТЕГИИ V2.0 ====================
START_BALANCE = 10.0          # Стартовый депозит ($10)
LEVERAGE = 12                 # Снижено с 20x до 12x для защиты от вымывания комиссиями
MARGIN_USAGE = 0.85           # Эффективное плечо = 10.2x
TAKER_FEE = 0.00055           # Комиссия Bybit Taker (0.055%)
MAKER_FEE = 0.00020           # Комиссия Bybit Maker для лимитного TP (0.020%)
SLIPPAGE = 0.00035            # Проскальзывание при входе IOC-лимиткой (0.035%)

LOOKBACK_WINDOW = 60          # Ищем сильные уровни за последние 60 минут
MIN_LEVEL_TOUCHES = 2         # Минимум 2 четких касания уровня до пробоя (как в видео)
TOUCH_TOLERANCE_PCT = 0.0018  # Коридор касания уровня (0.18%)
BREAKOUT_BUFFER_PCT = 0.0008  # Фильтр от ложного закола: вход после продавливания на 0.08%
MIN_NATR_15M = 0.0032         # Фильтр "Монета в игре": мин. волатильность 1-мин свечей (0.32%)
RVOL_THRESHOLD = 3.2          # Всплеск объема на пробое в 3.2 раза выше среднего
COOLDOWN_BARS = 25            # Пауза 25 минут после сделки по монете (защита от пилы)

HARD_SL_PCT = 0.0045          # Серверный стоп-лосс (0.45%)
TAKE_PROFIT_PCT = 0.0140      # Лимитный Take-Profit (1.40% чистого движения)
TRAIL_ACTIVATE_PCT = 0.0055   # Активация трейлинга (0.55%)
TRAIL_OFFSET_PCT = 0.0022     # Шаг отката трейлинга (0.22%)
TIME_STOP_MIN_IMPULSE = 0.0030 # Мин. импульс (0.30%) для удержания позиции
# ==================================================================


def fetch_historical_klines(http: HTTP, symbol: str, days: int = 5) -> list:
    """Скачивает 1-минутные свечи с Bybit V5 за последние N дней"""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    current_end = end_ms
    while current_end > start_ms:
        res = http.get_kline(
            category="linear",
            symbol=symbol,
            interval="1",
            end=current_end,
            limit=1000
        )
        rows = res.get("result", {}).get("list", [])
        if not rows:
            break
        all_candles.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts >= current_end:
            break
        current_end = oldest_ts - 60_000
        time.sleep(0.08)

    all_candles.reverse()
    return [
        {
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "vol": float(c[5])
        }
        for c in all_candles
    ]


def count_distinct_touches(candles: list, level: float, is_resistance: bool) -> int:
    """Считает количество изолированных касаний уровня (минимум через 3 свечи друг от друга)"""
    touches = 0
    last_touch_idx = -10
    for idx, c in enumerate(candles):
        price = c["high"] if is_resistance else c["low"]
        dist_pct = abs(price - level) / level
        if dist_pct <= TOUCH_TOLERANCE_PCT and (idx - last_touch_idx) >= 4:
            touches += 1
            last_touch_idx = idx
    return touches


def run_backtest_on_candles(candles: list, symbol: str, balance: float) -> tuple:
    trades = []
    eff_leverage = LEVERAGE * MARGIN_USAGE
    cooldown_until = 0

    for i in range(LOOKBACK_WINDOW, len(candles) - 5):
        if i < cooldown_until:
            continue

        window = candles[i - LOOKBACK_WINDOW:i]
        recent_15 = candles[i - 15:i]
        curr = candles[i]

        # 1. Фильтр "Монета в игре прямо сейчас": проверяем текущую волатильность (NATR)
        avg_range_pct = sum((c["high"] - c["low"]) / c["low"] for c in recent_15) / 15.0
        if avg_range_pct < MIN_NATR_15M:
            continue

        # 2. Фильтр всплеска объема на пробойной свече (RVOL)
        avg_vol = sum(c["vol"] for c in window) / len(window)
        if avg_vol <= 0 or (curr["vol"] / avg_vol) < RVOL_THRESHOLD:
            continue

        # 3. Находим горизонтальные уровни за 60 минут
        res_level = max(c["high"] for c in window)
        sup_level = min(c["low"] for c in window)

        # 4. Фильтр поджатия перед пробоем (последние 4 свечи не должны быть гигантским памп-сквизом)
        pre_4 = candles[i - 4:i]
        pre_high = max(c["high"] for c in pre_4)
        pre_low = min(c["low"] for c in pre_4)
        if (pre_high - pre_low) / pre_low > avg_range_pct * 3.2:
            continue

        side = None
        entry_price = 0.0

        # Проверка пробоя сопротивления (LONG) с проверкой числа касаний
        if curr["high"] >= res_level * (1 + BREAKOUT_BUFFER_PCT) and curr["close"] > res_level:
            touches = count_distinct_touches(window, res_level, is_resistance=True)
            if touches >= MIN_LEVEL_TOUCHES:
                side = "LONG"
                entry_price = res_level * (1 + BREAKOUT_BUFFER_PCT)

        # Проверка пробоя поддержки (SHORT) с проверкой числа касаний
        elif curr["low"] <= sup_level * (1 - BREAKOUT_BUFFER_PCT) and curr["close"] < sup_level:
            touches = count_distinct_touches(window, sup_level, is_resistance=False)
            if touches >= MIN_LEVEL_TOUCHES:
                side = "SHORT"
                entry_price = sup_level * (1 - BREAKOUT_BUFFER_PCT)

        if not side:
            continue

        # Оцениваем развитие импульса на окне из пробойной + следующих 3 минутных свечей
        impulse_bars = candles[i:i + 4]
        if side == "LONG":
            max_fav_price = max(b["high"] for b in impulse_bars)
            max_adv_price = min(b["low"] for b in impulse_bars[:2])
            first_bar_mfe = (curr["high"] - entry_price) / entry_price
            mfe_pct = (max_fav_price - entry_price) / entry_price
            mae_pct = (entry_price - max_adv_price) / entry_price
        else:
            max_fav_price = min(b["low"] for b in impulse_bars)
            max_adv_price = max(b["high"] for b in impulse_bars[:2])
            first_bar_mfe = (entry_price - curr["low"]) / entry_price
            mfe_pct = (entry_price - max_fav_price) / entry_price
            mae_pct = (max_adv_price - entry_price) / entry_price

        # Логика выходов:
        # 1. Если пробойная свеча сразу вкатила обратно под уровень — срабатывает быстрый Time-Stop
        if first_bar_mfe < TIME_STOP_MIN_IMPULSE and (
            (side == "LONG" and curr["close"] <= entry_price) or
            (side == "SHORT" and curr["close"] >= entry_price)
        ):
            raw_pnl_pct = -0.0005
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TIME_STOP"

        # 2. Если импульс сразу пошел и забрал лимитный Take-Profit (Maker)
        elif mfe_pct >= TAKE_PROFIT_PCT and mae_pct < HARD_SL_PCT:
            raw_pnl_pct = TAKE_PROFIT_PCT
            fee_pct = TAKER_FEE + MAKER_FEE + SLIPPAGE
            outcome = "TAKE_PROFIT"

        # 3. Если импульс дал от +0.55%, но не дошел до полного тейка — закрытие по Trailing-Stop
        elif mfe_pct >= TRAIL_ACTIVATE_PCT and mae_pct < HARD_SL_PCT:
            raw_pnl_pct = mfe_pct - TRAIL_OFFSET_PCT
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TRAILING"

        # 4. Если после входа цена резко развернулась в стоп-лосс
        elif mae_pct >= HARD_SL_PCT:
            raw_pnl_pct = -HARD_SL_PCT
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "HARD_SL"

        # 5. Вялое движение без развития за первые 2 минуты — закрытие по Time-Stop около нуля
        else:
            raw_pnl_pct = -0.0003
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TIME_STOP"

        net_move_pct = raw_pnl_pct - fee_pct
        pnl_usd = balance * (net_move_pct * eff_leverage)
        balance = max(0.0, balance + pnl_usd)
        cooldown_until = i + COOLDOWN_BARS

        trades.append({
            "symbol": symbol,
            "side": side,
            "outcome": outcome,
            "pnl_usd": pnl_usd,
            "balance": balance
        })

        if balance < 1.0:
            break

    return balance, trades


async def send_tg_report(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"})


def main():
    http = HTTP(testnet=False)
    tickers = http.get_tickers(category="linear")["result"]["list"]
    # Берем топ-5 волатильных монет с высоким оборотом
    volatile = [
        t["symbol"] for t in sorted(tickers, key=lambda x: abs(float(x.get("price24hPcnt", 0))), reverse=True)
        if t["symbol"].endswith("USDT")
        and t["symbol"] not in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        and float(t.get("turnover24h", 0)) > 35_000_000
    ][:5]

    print(f"📊 Запуск снайперского бэктеста V2.0 (5 дней) по монетам: {volatile}...")
    balance = START_BALANCE
    peak_balance = START_BALANCE
    max_dd_pct = 0.0
    all_trades = []

    for sym in volatile:
        candles = fetch_historical_klines(http, sym, days=5)
        balance, sym_trades = run_backtest_on_candles(candles, sym, balance)
        for tr in sym_trades:
            all_trades.append(tr)
            peak_balance = max(peak_balance, tr["balance"])
            dd = (peak_balance - tr["balance"]) / peak_balance * 100
            max_dd_pct = max(max_dd_pct, dd)

    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["pnl_usd"] > 0)
    tp_count = sum(1 for t in all_trades if t["outcome"] == "TAKE_PROFIT")
    trail_count = sum(1 for t in all_trades if t["outcome"] == "TRAILING")
    ts_count = sum(1 for t in all_trades if t["outcome"] == "TIME_STOP")
    sl_count = sum(1 for t in all_trades if t["outcome"] == "HARD_SL")

    winrate = (wins / total * 100) if total > 0 else 0.0
    mult = balance / START_BALANCE

    report = (
        f"📈 <b>ОТЧЕТ БЭКТЕСТА BYBIT V2.0 (СНАЙПЕРСКИЙ ФИЛЬТР)</b>\n"
        f"• Монеты: <code>{', '.join(volatile)}</code>\n"
        f"• Сделок всего: <b>{total}</b> (Winrate: <b>{winrate:.1f}%</b>)\n"
        f"  ├ 🎯 Take-Profit: {tp_count}\n"
        f"  ├ 🛡 Trailing-Stop: {trail_count}\n"
        f"  ├ ⏱ Time-Stop: {ts_count}\n"
        f"  └ ❌ Hard Stop-Loss: {sl_count}\n"
        f"• Макс. просадка: <b>-{max_dd_pct:.1f}%</b>\n"
        f"• Итог: <b>${START_BALANCE:.2f} ➔ ${balance:.2f} (x{mult:.2f})</b>"
    )
    print("\n" + report.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    asyncio.run(send_tg_report(report))


if __name__ == "__main__":
    main()
