import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== РАСШИРЕННАЯ СЕТКА ПАРАМЕТРОВ ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45
FIXED_TRADE_AMOUNT = 1.0
ASSUMED_LEVERAGE = 20

# Новые сетки для перебора
TP_GRID = [1.0, 1.5]
SL_GRID = [0.9, 1.1]
EMA_GRID = [20, 50]
RSI_FILTER_ENABLED = [True, False]
VOLUME_MULT_GRID = [1.0, 1.3]


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
        
        ha_candles.append({
            "timestamp": ts, "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
            "ha_close": ha_close, "real_open": o, "real_high": h, "real_low": l,
            "real_close": c, "body_pct": body_pct, "color": color, "volume": v
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


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    rsi = [50.0] * period
    for i in range(period, len(deltas)):
        gain = gains[i]
        loss = losses[i]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100.0 - (100.0 / (1.0 + rs)))
            
    return [50.0] + rsi


async def run_advanced_optimizer():
    print("🚀 Запуск расширенного оптимизатора (RSI + Объемы + EMA)...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    # ИСПРАВЛЕНА СКОБКА ЗДЕСЬ 👇
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]
    active_swaps = active_swaps[:35]

    print(f"📊 Скачиваем историю для {len(active_swaps)} пар...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    cached_market_data = {}
    for sym in active_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1200)
            if ohlcv and len(ohlcv) > 100:
                ha_candles = calculate_heikin_ashi(ohlcv)
                closes = [c["real_close"] for c in ha_candles]
                volumes = [c["volume"] for c in ha_candles]
                rsi_values = calculate_rsi(closes, 14)
                
                cached_market_data[sym] = {
                    "ha_candles": ha_candles,
                    "closes": closes,
                    "volumes": volumes,
                    "rsi": rsi_values
                }
        except Exception as e:
            print(f"⚠️ Ошибка загрузки {sym}: {e}", flush=True)

    await exchange.close()
    print(f"💾 Закешировано пар: {len(cached_market_data)}. Перебираем комбинации...", flush=True)

    if len(cached_market_data) == 0:
        print("❌ Нет данных для анализа.", flush=True)
        return

    combinations = list(product(TP_GRID, SL_GRID, EMA_GRID, RSI_FILTER_ENABLED, VOLUME_MULT_GRID))
    print(f"🔬 Всего комбинаций: {len(combinations)}\n", flush=True)

    best_results = []

    for tp_pct, sl_pct, ema_p, use_rsi, vol_mult in combinations:
        total_signals = 0
        wins = 0
        losses = 0
        total_pnl = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            closes = data["closes"]
            volumes = data["volumes"]
            rsi_vals = data["rsi"]
            ema_values = calculate_ema(closes, ema_p)

            for idx in range(max(ema_p, 20), len(ha_candles) - 24):
                prev_c = ha_candles[idx - 1]
                curr_c = ha_candles[idx]
                
                avg_vol = sum(volumes[idx-20:idx]) / 20 if idx >= 20 else volumes[idx]
                if curr_c["volume"] < avg_vol * vol_mult:
                    continue

                target_dir = None
                current_price = curr_c["real_close"]
                ema_val = ema_values[idx]
                rsi_val = rsi_vals[idx]

                if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                    if current_price < ema_val:
                        if not use_rsi or (35 < rsi_val < 70):
                            target_dir = "SHORT"
                elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                    if current_price > ema_val:
                        if not use_rsi or (30 < rsi_val < 65):
                            target_dir = "LONG"

                if not target_dir:
                    continue

                total_signals += 1
                entry_p = current_price
                
                pos_size = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
                sl_delta = (FIXED_TRADE_AMOUNT * sl_pct / pos_size) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * tp_pct / pos_size) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta

                trade_result = None
                for future_c in ha_candles[idx + 1 : idx + 25]:
                    f_high = future_c["real_high"]
                    f_low = future_c["real_low"]

                    if target_dir == "LONG":
                        hit_tp = f_high >= tp_price
                        hit_sl = f_low <= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "LOSS"
                            break
                    else:
                        hit_tp = f_low <= tp_price
                        hit_sl = f_high >= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "LOSS"
                            break

                if trade_result == "WIN":
                    wins += 1
                    total_pnl += FIXED_TRADE_AMOUNT * tp_pct
                elif trade_result == "LOSS":
                    losses += 1
                    total_pnl -= FIXED_TRADE_AMOUNT * sl_pct

        marked = wins + losses
        winrate = (wins / marked * 100.0) if marked > 0 else 0.0

        if marked > 10:
            best_results.append({
                "tp": tp_pct, "sl": sl_pct, "ema": ema_p, "rsi": use_rsi, "vol": vol_mult,
                "signals": marked, "winrate": winrate, "pnl": total_pnl
            })

    best_results.sort(key=lambda x: x["winrate"], reverse=True)

    print("\n" + "="*70, flush=True)
    print("🏆 ТОП-5 КОНФИГУРАЦИЙ ПО ВИНРЕЙТУ:", flush=True)
    print("="*70, flush=True)
    for i, res in enumerate(best_results[:5], 1):
        print(f"#{i} | Винрейт: {res['winrate']:.1f}% | TP: +{res['tp']*100:.0f}% | SL: -{res['sl']*100:.0f}% | EMA: {res['ema']} | RSI: {res['rsi']} | Объем x{res['vol']} | Сделок: {res['signals']} | PnL: ${res['pnl']:.2f}", flush=True)
    print("="*70, flush=True)

if __name__ == "__main__":
    asyncio.run(run_advanced_optimizer())
