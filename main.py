import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== ЧИСТЫЕ НАСТРОЙКИ ЛАБОРАТОРИИ ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 30           # Глубина истории: 30 дней
INITIAL_BALANCE_USD = 100.00    # Стартовый депозит: $100.00
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit (0.11% за круг)
MAX_HOLD_HOURS = 24             # Максимальное удержание позиции
PAIRS_LIMIT = 30                # Топ-30 пар Bybit
# ======================================================================


def calculate_clean_indicators(ohlcv):
    closes = [c[4] for c in ohlcv]
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    
    def sma(period):
        res = []
        for i in range(len(closes)):
            if i < period - 1:
                res.append(closes[i])
            else:
                res.append(sum(closes[i - period + 1:i + 1]) / period)
        return res

    sma20 = sma(20)
    sma50 = sma(50)

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

    # MACD (12, 26, 9)
    def ema(series, period):
        res = [series[0]]
        mult = 2.0 / (period + 1)
        for val in series[1:]:
            res.append((val - res[-1]) * mult + res[-1])
        return res

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = ema(macd_line, 9)

    indicators = []
    for i in range(len(closes)):
        indicators.append({
            "timestamp": ohlcv[i][0],
            "close": closes[i],
            "high": highs[i],
            "low": lows[i],
            "sma20": sma20[i],
            "sma50": sma50[i],
            "rsi": rsi[i],
            "macd": macd_line[i],
            "macd_signal": signal_line[i]
        })
    return indicators


def evaluate_clean_strategy(strategy, market_data):
    sim_balance = INITIAL_BALANCE_USD
    wins, losses = 0, 0
    peak = INITIAL_BALANCE_USD
    max_dd = 0.0

    entry_rule = strategy["entry"]
    tp_mult = strategy["tp"]
    sl_mult = strategy["sl"]
    leverage = strategy["leverage"]

    for sym, bars in market_data.items():
        pair_busy = -1
        for i in range(100, len(bars) - MAX_HOLD_HOURS):
            if i <= pair_busy:
                continue

            bar = bars[i]
            prev = bars[i - 1]
            price = bar["close"]
            direction = None

            if entry_rule == "rsi_bounce":
                if bar["rsi"] < 35 and prev["rsi"] >= 35:
                    direction = "LONG"
                elif bar["rsi"] > 65 and prev["rsi"] <= 65:
                    direction = "SHORT"
            elif entry_rule == "macd_cross":
                if bar["macd"] > bar["macd_signal"] and prev["macd"] <= prev["macd_signal"]:
                    direction = "LONG"
                elif bar["macd"] < bar["macd_signal"] and prev["macd"] >= prev["macd_signal"]:
                    direction = "SHORT"
            elif entry_rule == "sma_cross":
                if bar["sma20"] > bar["sma50"] and prev["sma20"] <= prev["sma50"]:
                    direction = "LONG"
                elif bar["sma20"] < bar["sma50"] and prev["sma20"] >= prev["sma50"]:
                    direction = "SHORT"

            if not direction:
                continue

            # Сложный процент: маржа строго 1% от текущего баланса
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


async def run_clean_lab():
    print(f"🧪 Запуск Чистой Лаборатории (Старт: ${INITIAL_BALANCE_USD}, Сложн. % ставки 1%, плавающее плечо)...", flush=True)
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ][:PAIRS_LIMIT]

    print(f"📊 Загружаем данные по {len(all_swaps)} парам Bybit...", flush=True)
    since_ts = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    market_data = {}

    for sym in all_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_ts, limit=1000)
            if ohlcv and len(ohlcv) > 200:
                market_data[sym] = calculate_clean_indicators(ohlcv)
        except Exception:
            pass

    await exchange.close()
    print(f"💾 Закешировано пар: {len(market_data)}. Перебираем комбинации...\n", flush=True)

    rules = ["rsi_bounce", "macd_cross", "sma_cross"]
    tp_grid = [1.0, 2.0]
    sl_grid = [1.0, 1.5]
    leverage_grid = [3, 5, 10, 15, 20]  # Тестируем разные плечи с нуля

    strategies = [{"entry": r, "tp": tp, "sl": sl, "leverage": lev} for r, tp, sl, lev in product(rules, tp_grid, sl_grid, leverage_grid)]
    print(f"🧬 Всего стратегий для тестирования: {len(strategies)}\n", flush=True)

    results = []
    for idx, strat in enumerate(strategies, 1):
        res = evaluate_clean_strategy(strat, market_data)
        if res:
            results.append(res)
            print(f"[{idx}/{len(strategies)}] ✅ {strat['entry']} (Плечо: {strat['leverage']}x, TP:{strat['tp']}x) -> Баланс: ${res['balance']:.2f} | Прибыль: ${res['profit']:+.2f}", flush=True)
        else:
            print(f"[{idx}/{len(strategies)}] ❌ Мало сделок / слив", flush=True)

    results.sort(key=lambda x: x["profit"], reverse=True)

    print("\n" + "="*110, flush=True)
    print("🏆 ТОП-3 ЛУЧШИЕ СТРАТЕГИИ ПО ИТОГАМ ЧИСТОГО ТЕСТА:", flush=True)
    print("="*110, flush=True)
    for i, r in enumerate(results[:3], 1):
        s = r["strategy"]
        print(
            f"#{i} | Логика: {s['entry']:<15} | Плечо: {s['leverage']:>2}x | TP: {s['tp']}x | SL: {s['sl']}x | "
            f"Итог: <b>${r['balance']:>6.2f}</b> (Прибыль: <b>${r['profit']:>+6.2f}</b>) | "
            f"Винрейт: {r['winrate']:>5.1f}% | Сделок: {r['trades']:>3} | Просадка: {r['dd']:>5.1f}%",
            flush=True
        )
    print("="*110, flush=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_clean_lab())
