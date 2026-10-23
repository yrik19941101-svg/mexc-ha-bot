import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== СЕТКА ПАРАМЕТРОВ ДЛЯ ПОИСКА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45           # Глубина истории для анализа
FIXED_TRADE_AMOUNT = 1.0        # Фиксированная маржа ($1)
ASSUMED_LEVERAGE = 20           # Плечо

# Что именно будет перебирать оптимизатор:
TP_GRID = [1.0, 1.5, 2.0]       # Тейк-профит (% от маржи)
SL_GRID = [0.7, 0.9, 1.1]       # Стоп-лосс (% от маржи)
EMA_GRID = [20, 50, 100]        # Периоды EMA для тренда
BODY_GRID = [0.001, 0.003]      # Минимальный размер тела свечи (фильтр флета)


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
            "real_close": c, "body_pct": body_pct, "color": color
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


async def run_optimizer():
    print("🚀 Запуск оптимизатора стратегии на истории BingX...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]

    # Возьмем топ-40 пар для быстрого и качественного теста
    active_swaps = active_swaps[:40]

    print(f"📊 Скачиваем историю для {len(active_swaps)} пар за {DAYS_TO_BACKTEST} дней...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    cached_market_data = {}
    for sym in active_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1200)
            # ИСПРАВЛЕНА ОШИБКА СКОБОК ЗДЕСЬ:
            if ohlcv and len(ohlcv) > 100:
                ha_candles = calculate_heikin_ashi(ohlcv)
                closes = [c["real_close"] for c in ha_candles]
                cached_market_data[sym] = {
                    "ha_candles": ha_candles,
                    "closes": closes
                }
        except Exception as e:
            print(f"⚠️ Ошибка загрузки {sym}: {e}", flush=True)

    await exchange.close()
    print(f"💾 Успешно загружено и закешировано пар: {len(cached_market_data)}. Начинаем перебор комбинаций...", flush=True)

    if len(cached_market_data) == 0:
        print("❌ Не удалось загрузить ни одной пары. Проверьте интернет-соединение.", flush=True)
        return

    combinations = list(product(TP_GRID, SL_GRID, EMA_GRID, BODY_GRID))
    print(f"🔬 Всего комбинаций для проверки: {len(combinations)}\n", flush=True)

    best_results = []

    for tp_pct, sl_pct, ema_p, min_body in combinations:
        total_signals = 0
        wins = 0
        losses = 0
        total_pnl = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            closes = data["closes"]
            ema_values = calculate_ema(closes, ema_p)

            for idx in range(ema_p, len(ha_candles) - 24):
                prev_c = ha_candles[idx - 1]
                curr_c = ha_candles[idx]
                
                if curr_c["body_pct"] < min_body:
                    continue

                target_dir = None
                current_price = curr_c["real_close"]
                ema_val = ema_values[idx]

                if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                    if current_price < ema_val:
                        target_dir = "SHORT"
                elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                    if current_price > ema_val:
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

        if marked > 15:
            best_results.append({
                "tp": tp_pct, "sl": sl_pct, "ema": ema_p, "body": min_body,
                "signals": marked, "winrate": winrate, "pnl": total_pnl
            })

    best_results.sort(key=lambda x: x["pnl"], reverse=True)

    print("\n" + "="*60, flush=True)
    print("🏆 ТОП-5 ЛУЧШИХ КОНФИГУРАЦИЙ СТРАТЕГИИ:", flush=True)
    print("="*60, flush=True)
    for i, res in enumerate(best_results[:5], 1):
        print(f"#{i} | TP: +{res['tp']*100:.0f}% | SL: -{res['sl']*100:.0f}% | EMA: {res['ema']} | Фильтр тела: {res['body']} | Сделок: {res['signals']} | Винрейт: {res['winrate']:.1f}% | PnL: ${res['pnl']:.2f}", flush=True)
    print("="*60, flush=True)

if __name__ == "__main__":
    asyncio.run(run_optimizer())
