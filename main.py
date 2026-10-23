import asyncio
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ БЭКТЕСТА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 60           # Глубина истории (60 дней)
FIXED_TRADE_AMOUNT = 1.0        # Фиксированная маржа ($1), без умножения
SL_PERCENT_OF_MARGIN = 0.90     # Стоп-лосс: -90% от маржи
TP_PERCENT_OF_MARGIN = 2.00     # Тейк-профит: +200% от маржи
ASSUMED_LEVERAGE = 20           # Плечо для расчета ценового расстояния
MIN_REMAINING_MINUTES = 30.0    # До закрытия свечи должно оставаться не менее 30 мин


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


async def run_backtest():
    print("🚀 Запуск бэктеста по всем фьючерсным парам BingX (TP 200%, без догона)...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]

    print(f"📊 Найдено пар на BingX: {len(active_swaps)}. Скачиваем историю за {DAYS_TO_BACKTEST} дней...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    total_signals = 0
    wins = 0
    losses = 0
    total_pnl_usd = 0.0

    for i, sym in enumerate(active_swaps):
        try:
            ohlcv_1h = await exchange.fetch_ohlcv(sym, timeframe="1h", since=since_timestamp, limit=1500)
            if not ohlcv_1h or len(ohlcv_1h) < 50: 
                continue

            for idx in range(1, len(ohlcv_1h) - 25):
                prev_candle = ohlcv_1h[idx - 1]
                curr_candle = ohlcv_1h[idx]
                
                p_open, p_close = prev_candle[1], prev_candle[4]
                prev_color = "GREEN" if p_close > p_open else "RED"

                c_open, c_high, c_low, c_close = curr_candle[1], curr_candle[2], curr_candle[3], curr_candle[4]
                
                target_dir = None
                if prev_color == "RED" and c_close > c_open:
                    target_dir = "LONG"
                elif prev_color == "GREEN" and c_close < c_open:
                    target_dir = "SHORT"

                if not target_dir: 
                    continue

                total_signals += 1
                entry_p = c_close
                
                pos_size = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
                sl_delta = (FIXED_TRADE_AMOUNT * SL_PERCENT_OF_MARGIN / pos_size) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * TP_PERCENT_OF_MARGIN / pos_size) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta

                trade_result = None
                for future_c in ohlcv_1h[idx : idx + 24]:
                    f_high = future_c[2]
                    f_low = future_c[3]

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
                    total_pnl_usd += FIXED_TRADE_AMOUNT * TP_PERCENT_OF_MARGIN
                elif trade_result == "LOSS":
                    losses += 1
                    total_pnl_usd -= FIXED_TRADE_AMOUNT * SL_PERCENT_OF_MARGIN

        except Exception as e:
            pass

    await exchange.close()

    print("\n" + "="*45, flush=True)
    print("📈 РЕЗУЛЬТАТЫ БЭКТЕСТА (TP 200%, Фикс. маржа):", flush=True)
    print(f"Всего сделок: {total_signals}", flush=True)
    print(f"✅ Успешных (TP +200%): {wins}", flush=True)
    print(f"❌ Убыточных (SL -90%): {losses}", flush=True)
    marked = wins + losses
    winrate = (wins / marked * 100.0) if marked > 0 else 0.0
    print(f"🎯 Итоговый Винрейт: {winrate:.1f}%", flush=True)
    print(f"💰 Итоговый PnL: ${total_pnl_usd:.2f}", flush=True)
    print("="*45, flush=True)

if __name__ == "__main__":
    asyncio.run(run_backtest())
