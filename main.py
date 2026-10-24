import asyncio
import ccxt.async_support as ccxt

# ==================== ЧИСТЫЕ НАСТРОЙКИ HEIKIN ASHI ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 60           # Глубина истории: 60 дней
INITIAL_BALANCE_USD = 100.00    # Стартовый депозит: $100.00
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit (0.11% за круг)
MAX_HOLD_HOURS = 24             # Максимальное удержание позиции
PAIRS_LIMIT = 30                # Топ-30 пар Bybit
LEVERAGE = 10                   # Плечо 10x
TP_MULT = 2.0                   # Тейк-профит 2.0x ATR
SL_MULT = 1.0                   # Стоп-лосс 1.0x ATR
MIN_BODY_PCT = 0.35             # Минимальное тело свечи Heikin Ashi (%)
MAX_WICK_PCT = 0.15             # Максимальная противоположная тень (%)
# ======================================================================


def calculate_pure_ha(ohlcv):
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
            "timestamp": ts,
            "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close,
            "real_open": o, "real_high": h, "real_low": l, "real_close": c,
            "body_pct": body_pct, "color": color,
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct
        })
    return ha_candles


async def run_pure_ha_backtest():
    print(f"🕯️ Запуск бэктеста ЧИСТОЙ стратегии Heikin Ashi на 60 дней...", flush=True)
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ][:PAIRS_LIMIT]

    print(f"📊 Загружаем историю за 60 дней по {len(all_swaps)} парам Bybit...", flush=True)
    since_ts = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    market_data = {}

    for sym in all_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_ts, limit=1500)
            if ohlcv and len(ohlcv) > 500:
                market_data[sym] = calculate_pure_ha(ohlcv)
        except Exception:
            pass

    await exchange.close()
    print(f"💾 Закешировано пар: {len(market_data)}. Запуск симуляции...\n", flush=True)

    sim_balance = INITIAL_BALANCE_USD
    wins, losses = 0, 0
    peak = INITIAL_BALANCE_USD
    max_dd = 0.0
    pair_busy = {}

    for sym, ha_bars in market_data.items():
        for i in range(50, len(ha_bars) - MAX_HOLD_HOURS):
            if i <= pair_busy.get(sym, -1):
                continue

            prev_c = ha_bars[i - 1]
            curr_c = ha_bars[i]

            # Классическое условие смены цвета Heikin Ashi
            if prev_c["color"] == curr_c["color"]:
                continue
            if curr_c["body_pct"] < MIN_BODY_PCT:
                continue

            direction = None
            if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                if curr_c["lower_wick_pct"] <= MAX_WICK_PCT:
                    direction = "LONG"
            elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                if curr_c["upper_wick_pct"] <= MAX_WICK_PCT:
                    direction = "SHORT"

            if not direction:
                continue

            # Сложный процент: маржа ровно 1% от текущего баланса
            margin = max(0.50, round(sim_balance * 0.01, 2))
            if sim_balance < margin:
                break

            pos_size = margin * LEVERAGE
            fee = pos_size * BYBIT_FEE_RATE

            price = curr_c["real_close"]
            atr_proxy = curr_c["real_high"] - curr_c["real_low"]
            if atr_proxy <= 0:
                continue

            sl_dist = atr_proxy * SL_MULT
            tp_dist = atr_proxy * TP_MULT

            if direction == "LONG":
                entry_p = price
                sl_p = entry_p - sl_dist
                tp_p = entry_p + tp_dist
            else:
                entry_p = price
                sl_p = entry_p + sl_dist
                tp_p = entry_p - tp_dist

            gross_pnl = None
            exit_off = MAX_HOLD_HOURS

            for offset in range(1, MAX_HOLD_HOURS + 1):
                if i + offset >= len(ha_bars):
                    break
                fb = ha_bars[i + offset]
                hit_tp = (fb["real_high"] >= tp_p) if direction == "LONG" else (fb["real_low"] <= tp_p)
                hit_sl = (fb["real_low"] <= sl_p) if direction == "LONG" else (fb["real_high"] >= sl_p)

                if hit_sl:
                    gross_pnl = - (sl_dist / entry_p) * pos_size
                    exit_off = offset
                    break
                elif hit_tp:
                    gross_pnl = (tp_dist / entry_p) * pos_size
                    exit_off = offset
                    break

            if gross_pnl is None:
                last_c = ha_bars[i + MAX_HOLD_HOURS]["real_close"]
                chg = (last_c - entry_p) / entry_p if direction == "LONG" else (entry_p - last_c) / entry_p
                gross_pnl = chg * pos_size

            pair_busy[sym] = i + exit_off
            net_pnl = gross_pnl - fee
            sim_balance += net_pnl

            if sim_balance <= 0:
                sim_balance = 0.01
                break

            if sim_balance > peak:
                peak = sim_balance
            dd = (peak - sim_balance) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

            if net_pnl > 0:
                wins += 1
            else:
                losses += 1

    total_trades = wins + losses
    winrate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    profit = sim_balance - INITIAL_BALANCE_USD

    print("="*105, flush=True)
    print("🏆 РЕЗУЛЬТАТЫ БЭКТЕСТА ЧИСТОЙ СТРАТЕГИИ HEIKIN ASHI НА 60 ДНЕЙ:", flush=True)
    print("="*105, flush=True)
    print(f"• Итоговый баланс: <b>${sim_balance:.2f}</b> (Чистая прибыль: <b>${profit:+.2f}</b>)")
    print(f"• Винрейт: {winrate:.1f}% (Побед: {wins} / Поражений: {losses} из {total_trades} сделок)")
    print(f"• Максимальная просадка: <b>{max_dd:.1f}%</b>")
    print("="*105, flush=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_pure_ha_backtest())
