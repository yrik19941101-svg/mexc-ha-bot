import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== РАСШИРЕННЫЕ НАСТРОЙКИ ЛАБОРАТОРИИ ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 60           # Глубина истории: 60 дней стресс-теста
INITIAL_BALANCE_USD = 100.00    # Стартовый депозит: $100.00
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit (0.11% за круг)
MAX_HOLD_HOURS = 48             # Максимальное удержание сделки
PAIRS_LIMIT = 35                # Топ-35 пар Bybit
# ============================================================================


def calculate_deep_indicators(ohlcv):
    ha_candles = []
    closes = [c[4] for c in ohlcv]
    
    def ema(series, period):
        if len(series) < period:
            return [sum(series) / len(series)] * len(series)
        res = [sum(series[:period]) / period]
        mult = 2.0 / (period + 1)
        for val in series[period:]:
            res.append((val - res[-1]) * mult + res[-1])
        return [res[0]] * (period - 1) + res

    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    rsi = [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(chg if chg > 0 else 0)
        losses.append(-chg if chg < 0 else 0)
        if i >= 14:
            ag = sum(gains[-14:]) / 14
            al = sum(losses[-14:]) / 14
            rsi[i] = 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + (ag / al)))

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
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct,
            "rsi": rsi[i],
            "ema50": ema50[i],
            "ema200": ema200[i]
        })
    return ha_candles


def simulate_strategy(strategy, market_data):
    sim_balance = INITIAL_BALANCE_USD
    wins, losses = 0, 0
    peak = INITIAL_BALANCE_USD
    max_dd = 0.0

    entry_logic = strategy["logic"]
    tp_mult = strategy["tp"]
    sl_mult = strategy["sl"]
    leverage = strategy["leverage"]
    min_body = strategy["min_body"]
    max_wick = strategy["max_wick"]

    for sym, bars in market_data.items():
        pair_busy = -1
        for i in range(200, len(bars) - MAX_HOLD_HOURS):
            if i <= pair_busy:
                continue

            prev_c, curr_c = bars[i - 1], bars[i]
            price = curr_c["real_close"]
            direction = None

            if entry_logic == "ha_trend_pullback":
                if prev_c["color"] != curr_c["color"] and curr_c["body_pct"] >= min_body:
                    is_bull = price > curr_c["ema200"]
                    is_bear = price < curr_c["ema200"]
                    if prev_c["color"] == "RED" and curr_c["color"] == "GREEN" and is_bull and curr_c["lower_wick_pct"] <= max_wick:
                        direction = "LONG"
                    elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED" and is_bear and curr_c["upper_wick_pct"] <= max_wick:
                        direction = "SHORT"

            elif entry_logic == "ha_pure_momentum":
                if curr_c["body_pct"] >= min_body and curr_c["upper_wick_pct"] <= max_wick and curr_c["lower_wick_pct"] <= max_wick:
                    if curr_c["color"] == "GREEN" and price > curr_c["ema200"]:
                        direction = "LONG"
                    elif curr_c["color"] == "RED" and price < curr_c["ema200"]:
                        direction = "SHORT"

            if not direction:
                continue

            margin = max(0.50, round(sim_balance * 0.01, 2))
            if sim_balance < margin:
                break

            pos_size = margin * leverage
            fee = pos_size * BYBIT_FEE_RATE

            atr_proxy = curr_c["real_high"] - curr_c["real_low"]
            if atr_proxy <= 0:
                continue

            sl_dist = atr_proxy * sl_mult
            tp_dist = atr_proxy * tp_mult

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
                if i + offset >= len(bars):
                    break
                fb = bars[i + offset]
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
                last_c = bars[i + MAX_HOLD_HOURS]["real_close"]
                chg = (last_c - entry_p) / entry_p if direction == "LONG" else (entry_p - last_c) / entry_p
                gross_pnl = chg * pos_size

            pair_busy = i + exit_off
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
    if total_trades < 10:
        return None

    winrate = (wins / total_trades) * 100.0
    profit = sim_balance - INITIAL_BALANCE_USD

    return {
        "strategy": strategy,
        "balance": sim_balance,
        "profit": profit,
        "winrate": winrate,
        "trades": total_trades,
        "dd": max_dd
    }


async def main():
    print(f"🚀 [МЕГА-ЛАБОРАТОРИЯ]: Запуск расширенного марафона (TP до 5.0x)...", flush=True)
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ][:PAIRS_LIMIT]

    print(f"📊 Загружаем историю за {DAYS_TO_BACKTEST} дней по {len(all_swaps)} парам Bybit...", flush=True)
    since_ts = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    market_data = {}

    for sym in all_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_ts, limit=1600)
            if ohlcv and len(ohlcv) > 500:
                market_data[sym] = calculate_deep_indicators(ohlcv)
        except Exception:
            pass

    await exchange.close()
    print(f"💾 Закешировано пар: {len(market_data)}.", flush=True)

    # Расширенная сетка параметров
    logic_grid = ["ha_trend_pullback", "ha_pure_momentum"]
    tp_grid = [2.5, 3.0, 3.5, 4.0, 4.5, 5.0]       # Тейки до 5.0x
    sl_grid = [1.0, 1.25, 1.5, 2.0]               # Стопы
    leverage_grid = [5, 10, 15, 20]               # Плечи
    min_body_grid = [0.20, 0.35, 0.50]            # Тело HA
    max_wick_grid = [0.10, 0.20, 0.30]            # Тень HA

    all_combinations = list(product(logic_grid, tp_grid, sl_grid, leverage_grid, min_body_grid, max_wick_grid))
    print(f"🧬 Сгенерировано комбинаций для мега-теста: {len(all_combinations)}\n", flush=True)

    results = []
    for idx, (logic, tp, sl, lev, min_b, max_w) in enumerate(all_combinations, 1):
        strat = {
            "logic": logic, "tp": tp, "sl": sl, "leverage": lev, "min_body": min_b, "max_wick": max_w
        }
        res = simulate_strategy(strat, market_data)
        if res:
            results.append(res)
            if res["profit"] > 0:
                print(f"[{idx}/{len(all_combinations)}] ✅ ПЛЮС! {logic} | Плечо:{lev}x | TP:{tp}x/SL:{sl}x | Баланс: ${res['balance']:.2f} (Прибыль: ${res['profit']:+.2f})", flush=True)

    results.sort(key=lambda x: x["profit"], reverse=True)

    print("\n" + "="*125, flush=True)
    print("🏆 ТОП-3 АБСОЛЮТНЫХ ПОБЕДИТЕЛЯ МЕГА-ЛАБОРАТОРИИ:", flush=True)
    print("="*125, flush=True)
    for i, r in enumerate(results[:3], 1):
        s = r["strategy"]
        print(
            f"#{i} | Логика: {s['logic']:<20} | Плечо: {s['leverage']:>2}x | TP: {s['tp']}x | SL: {s['sl']}x | "
            f"Тело HA >= {s['min_body']}% | Тень <= {s['max_wick']}% | "
            f"Итог баланса: <b>${r['balance']:>6.2f}</b> (Чистый профит: <b>${r['profit']:>+6.2f}</b>) | "
            f"Винрейт: {r['winrate']:>5.1f}% | Сделок: {r['trades']:>3} | Просадка: {r['dd']:>5.1f}%",
            flush=True
        )
    print("="*125, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
