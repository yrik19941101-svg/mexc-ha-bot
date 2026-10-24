import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== НАСТРОЙКИ АСИММЕТРИЧНОЙ ЛАБОРАТОРИИ ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 30           # Глубина истории: 30 дней
INITIAL_BALANCE_USD = 100.00    # Стартовый депозит: $100.00
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit (0.11% за круг)
MAX_HOLD_HOURS = 36             # Максимальное удержание позиции
PAIRS_LIMIT = 30                # Топ-30 пар Bybit
# ============================================================================


def calculate_asymmetric_indicators(ohlcv):
    closes = [c[4] for c in ohlcv]
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    
    def ema(series, period):
        if len(series) < period:
            return [sum(series) / len(series)] * len(series)
        res = [sum(series[:period]) / period]
        mult = 2.0 / (period + 1)
        for val in series[period:]:
            res.append((val - res[-1]) * mult + res[-1])
        return [res[0]] * (period - 1) + res

    ema200 = ema(closes, 200)

    # RSI (14)
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

    # Bollinger Bands (20, 2)
    bb_upper, bb_lower = [], []
    for i in range(len(closes)):
        if i < 20:
            bb_upper.append(closes[i] * 1.02)
            bb_lower.append(closes[i] * 0.98)
        else:
            win = closes[i - 19:i + 1]
            mean = sum(win) / 20
            stdev = (sum((x - mean) ** 2 for x in win) / 20) ** 0.5
            bb_upper.append(mean + (2 * stdev))
            bb_lower.append(mean - (2 * stdev))

    indicators = []
    for i in range(len(closes)):
        indicators.append({
            "timestamp": ohlcv[i][0],
            "close": closes[i],
            "high": highs[i],
            "low": lows[i],
            "ema200": ema200[i],
            "rsi": rsi[i],
            "bb_upper": bb_upper[i],
            "bb_lower": bb_lower[i]
        })
    return indicators


def evaluate_asymmetric_strategy(strategy, market_data):
    sim_balance = INITIAL_BALANCE_USD
    wins, losses = 0, 0
    peak = INITIAL_BALANCE_USD
    max_dd = 0.0

    entry_rule = strategy["entry"]
    tp_mult = strategy["tp"]  # Теперь асимметрично выше (например, 3.0x)
    sl_mult = strategy["sl"]  # Короткий стоп (например, 1.0x)
    leverage = strategy["leverage"]

    for sym, bars in market_data.items():
        pair_busy = -1
        for i in range(200, len(bars) - MAX_HOLD_HOURS):
            if i <= pair_busy:
                continue

            bar = bars[i]
            prev = bars[i - 1]
            price = bar["close"]
            direction = None

            # Фильтр тренда по EMA 200
            is_bullish_trend = price > bar["ema200"]
            is_bearish_trend = price < bar["ema200"]

            if entry_rule == "rsi_trend_pullback":
                if is_bullish_trend and bar["rsi"] < 40 and prev["rsi"] >= 40:
                    direction = "LONG"
                elif is_bearish_trend and bar["rsi"] > 60 and prev["rsi"] <= 60:
                    direction = "SHORT"
            elif entry_rule == "bb_trend_breakout":
                if is_bullish_trend and bar["close"] > bar["bb_upper"] and prev["close"] <= prev["bb_upper"]:
                    direction = "LONG"
                elif is_bearish_trend and bar["close"] < bar["bb_lower"] and prev["close"] >= prev["bb_lower"]:
                    direction = "SHORT"

            if not direction:
                continue

            # Сложный процент: маржа 1% от текущего баланса
            margin = max(0.50, round(sim_balance * 0.01, 2))
            if sim_balance < margin:
                break

            pos_size = margin * leverage
            fee = pos_size * BYBIT_FEE_RATE

            atr_proxy = bar["high"] - bar["low"]
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
                hit_tp = (fb["high"] >= tp_p) if direction == "LONG" else (fb["low"] <= tp_p)
                hit_sl = (fb["low"] <= sl_p) if direction == "LONG" else (fb["high"] >= sl_p)

                if hit_sl:
                    gross_pnl = - (sl_dist / entry_p) * pos_size
                    exit_off = offset
                    break
                elif hit_tp:
                    gross_pnl = (tp_dist / entry_p) * pos_size
                    exit_off = offset
                    break

            if gross_pnl is None:
                last_c = bars[i + MAX_HOLD_HOURS]["close"]
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
    if total_trades < 8:
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


async def run_asymmetric_lab():
    print(f"🧪 Запуск Асимметричной Лаборатории (TP 3x / SL 1x + Тренд EMA200)...", flush=True)
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ][:PAIRS_LIMIT]

    print(f"📊 Загружаем историю по {len(all_swaps)} парам Bybit...", flush=True)
    since_ts = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    market_data = {}

    for sym in all_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_ts, limit=1000)
            if ohlcv and len(ohlcv) > 250:
                market_data[sym] = calculate_asymmetric_indicators(ohlcv)
        except Exception:
            pass

    await exchange.close()
    print(f"💾 Закешировано пар: {len(market_data)}. Тестируем асимметричные стратегии...\n", flush=True)

    rules = ["rsi_trend_pullback", "bb_trend_breakout"]
    tp_grid = [2.5, 3.0, 4.0]  # Асимметричный высокий тейк
    sl_grid = [1.0]            # Короткий стоп
    leverage_grid = [5, 10, 15]

    strategies = [{"entry": r, "tp": tp, "sl": sl, "leverage": lev} for r, tp, sl, lev in product(rules, tp_grid, sl_grid, leverage_grid)]
    print(f"🧬 Всего асимметричных вариантов для проверки: {len(strategies)}\n", flush=True)

    results = []
    for idx, strat in enumerate(strategies, 1):
        res = evaluate_asymmetric_strategy(strat, market_data)
        if res:
            results.append(res)
            print(f"[{idx}/{len(strategies)}] ✅ {strat['entry']} (Плечо:{strat['leverage']}x, TP:{strat['tp']}x) -> Баланс: ${res['balance']:.2f} | Прибыль: ${res['profit']:+.2f}", flush=True)
        else:
            print(f"[{idx}/{len(strategies)}] ❌ Мало сделок / слив", flush=True)

    results.sort(key=lambda x: x["profit"], reverse=True)

    print("\n" + "="*115, flush=True)
    print("🏆 ТОП-3 ЛУЧШИЕ АСИММЕТРИЧНЫЕ СТРАТЕГИИ:", flush=True)
    print("="*115, flush=True)
    for i, r in enumerate(results[:3], 1):
        s = r["strategy"]
        print(
            f"#{i} | Логика: {s['entry']:<20} | Плечо: {s['leverage']:>2}x | TP: {s['tp']}x | SL: {s['sl']}x | "
            f"Итог: <b>${r['balance']:>6.2f}</b> (Прибыль: <b>${r['profit']:>+6.2f}</b>) | "
            f"Винрейт: {r['winrate']:>5.1f}% | Сделок: {r['trades']:>3} | Просадка: {r['dd']:>5.1f}%",
            flush=True
        )
    print("="*115, flush=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_asymmetric_lab())
