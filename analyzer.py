import ccxt
import pandas as pd
import numpy as np
import time

SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT',
    'AVAX/USDT', 'DOGE/USDT', 'LINK/USDT', 'DOT/USDT', 'NEAR/USDT',
    'APT/USDT', 'ARB/USDT', 'OP/USDT', 'SUI/USDT', 'INJ/USDT',
    'FIL/USDT', 'RENDER/USDT', 'ATOM/USDT', 'LTC/USDT', 'TIA/USDT'
]

def fetch_historical_data(exchange, symbol, timeframe='1h', days=60):
    """Загрузка данных за 60 дней (1440 часовых свечей)"""
    try:
        now = exchange.milliseconds()
        since = now - days * 24 * 60 * 60 * 1000
        all_ohlcv = []
        
        while since < now:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000:
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.drop_duplicates(subset=['timestamp']).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Ошибка загрузки {symbol}: {e}")
        return None

def prepare_indicators(df):
    """Расчет EMA, наклона тренда, ATR и RSI(14)"""
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
    
    # ATR (14)
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # RSI (14) для фильтрации перекупленности/перепроданности при тесте зоны
    delta = df['close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(50)
    
    df['body'] = (df['close'] - df['open']).abs()
    return df

def backtest_strategy(df, symbol):
    initial_capital = 150.0
    capital = initial_capital
    risk_per_trade = 0.02  # Фиксированный риск 2% от текущего баланса
    
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    ema20 = df['ema20'].values
    ema50 = df['ema50'].values
    ema100 = df['ema100'].values
    atrs = df['atr'].values
    rsis = df['rsi'].values
    bodies = df['body'].values
    
    demand_zones = []
    supply_zones = []
    
    in_position = False
    pos_type = None
    entry_price = 0.0
    stop_loss = 0.0
    risk_dist = 0.0
    be_trigger_price = 0.0  # 0.90R -> стоп в +0.20R (без закрытия объема)
    tp1_price = 0.0         # 1.40R -> закрываем 75%, стоп остатка в +0.80R
    tp2_price = 0.0         # 2.60R -> закрываем оставшиеся 25% (итог +1.70R)
    be_activated = False
    tp1_hit = False
    risk_usd = 0.0
    
    consec_losses = 0
    cooldown_until = 0
    
    trades = []
    
    for i in range(105, len(df)):
        if capital <= 10:
            break
            
        curr_open = opens[i]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_close = closes[i]
        curr_atr = atrs[i]
        
        if np.isnan(curr_atr) or curr_atr == 0:
            continue

        # -------------------------------------------------------------------------
        # 1. ПОИСК ЗОН СПРОСА И ПРЕДЛОЖЕНИЯ С РЕАЛЬНЫМ ПРОБОЕМ СТРУКТУРЫ (BOS)
        # -------------------------------------------------------------------------
        p = i - 3
        if p >= 12:
            # Зона Спроса: Swing Low + импульс вверх + пробой предыдущего локального максимума (BOS)
            is_swing_low = (lows[p] <= np.min(lows[p-5:p])) and (lows[p] <= np.min(lows[p+1:i+1]))
            impulse_up = (np.max(highs[p+1:i+1]) - lows[p]) >= (atrs[p] * 1.35)
            prev_local_high = np.max(highs[p-5:p])
            bos_up = np.max(closes[p+1:i+1]) > prev_local_high
            
            if is_swing_low and impulse_up and bos_up:
                demand_zones.append({
                    'bottom': lows[p],
                    'top': lows[p] + atrs[p] * 0.65,
                    'bar': p
                })
                
            # Зона Предложения: Swing High + импульс вниз + пробой предыдущего локального минимума (BOS)
            is_swing_high = (highs[p] >= np.max(highs[p-5:p])) and (highs[p] >= np.max(highs[p+1:i+1]))
            impulse_down = (highs[p] - np.min(lows[p+1:i+1])) >= (atrs[p] * 1.35)
            prev_local_low = np.min(lows[p-5:p])
            bos_down = np.min(closes[p+1:i+1]) < prev_local_low
            
            if is_swing_high and impulse_down and bos_down:
                supply_zones.append({
                    'bottom': highs[p] - atrs[p] * 0.65,
                    'top': highs[p],
                    'bar': p
                })

        # Очистка пробитых и старых зон (>100 часов)
        demand_zones = [
            z for z in demand_zones 
            if (curr_close >= z['bottom']) and (i - z['bar'] <= 100)
        ]
        supply_zones = [
            z for z in supply_zones 
            if (curr_close <= z['top']) and (i - z['bar'] <= 100)
        ]

        # -------------------------------------------------------------------------
        # 2. УПРАВЛЕНИЕ ОТКРЫТОЙ ПОЗИЦИЕЙ (ДВУХЭТАПНЫЙ ТРЕЙЛИНГ)
        # -------------------------------------------------------------------------
        if in_position:
            if pos_type == 'LONG':
                # Проверка касания стоп-лосса (или трейлинг-стопа)
                if curr_low <= stop_loss:
                    if tp1_hit:
                        # 75% закрыто на 1.40R (+1.05R) + 25% по трейлингу на +0.80R (+0.20R) = +1.25R
                        capital += risk_usd * 1.25
                        trades.append('WIN')
                        consec_losses = 0
                    elif be_activated:
                        # Сработал защитный щит в +0.20R (сделка спасена от разворота!)
                        capital += risk_usd * 0.20
                        trades.append('WIN')
                        consec_losses = 0
                    else:
                        # Полный стоп -1.0R
                        capital -= risk_usd
                        trades.append('LOSS')
                        consec_losses += 1
                        if consec_losses >= 2:
                            cooldown_until = i + 36  # Пауза 36 часов при распиле на паре
                            consec_losses = 0
                    in_position = False
                    continue
                
                # Проверка полного тейка TP2 (2.60R)
                if curr_high >= tp2_price:
                    # 75% на 1.40R (+1.05R) + 25% на 2.60R (+0.65R) = +1.70R
                    capital += risk_usd * 1.70
                    trades.append('WIN')
                    consec_losses = 0
                    in_position = False
                    continue
                    
                # Проверка TP1 (1.40R) -> фиксируем 75%, стоп остатка в +0.80R
                if not tp1_hit and curr_high >= tp1_price:
                    tp1_hit = True
                    be_activated = True
                    stop_loss = entry_price + (risk_dist * 0.80)
                    
                # Проверка защитного щита (0.90R) -> перенос стопа в +0.20R без потери объема
                elif not be_activated and curr_high >= be_trigger_price:
                    be_activated = True
                    stop_loss = entry_price + (risk_dist * 0.20)
                    
            elif pos_type == 'SHORT':
                if curr_high >= stop_loss:
                    if tp1_hit:
                        capital += risk_usd * 1.25
                        trades.append('WIN')
                        consec_losses = 0
                    elif be_activated:
                        capital += risk_usd * 0.20
                        trades.append('WIN')
                        consec_losses = 0
                    else:
                        capital -= risk_usd
                        trades.append('LOSS')
                        consec_losses += 1
                        if consec_losses >= 2:
                            cooldown_until = i + 36
                            consec_losses = 0
                    in_position = False
                    continue
                    
                if curr_low <= tp2_price:
                    capital += risk_usd * 1.70
                    trades.append('WIN')
                    consec_losses = 0
                    in_position = False
                    continue
                    
                if not tp1_hit and curr_low <= tp1_price:
                    tp1_hit = True
                    be_activated = True
                    stop_loss = entry_price - (risk_dist * 0.80)
                    
                elif not be_activated and curr_low <= be_trigger_price:
                    be_activated = True
                    stop_loss = entry_price - (risk_dist * 0.20)
                    
            continue

        # Если пара в кулдауне после серии стопов в боковике — пропускаем входы
        if i < cooldown_until:
            continue

        # -------------------------------------------------------------------------
        # 3. ПОИСК ВХОДА (ФИЛЬТР НАКЛОНА ТРЕНДА + RSI + РЕАКЦИЯ ОТ ЗОНЫ)
        # -------------------------------------------------------------------------
        # Тренд должен иметь реальный наклон EMA100, а не лежать в плоском флэте
        uptrend = (curr_close > ema100[i]) and (ema20[i] >= ema50[i] * 0.998) and (ema100[i] > ema100[i-5])
        downtrend = (curr_close < ema100[i]) and (ema20[i] <= ema50[i] * 1.002) and (ema100[i] < ema100[i-5])
        
        candle_range = curr_high - curr_low
        if candle_range == 0:
            continue

        # --- ВХОД В LONG ---
        if uptrend and demand_zones:
            matched_zone = None
            for z in reversed(demand_zones):
                if (i - z['bar'] >= 4) and (curr_low <= z['top'] or lows[i-1] <= z['top']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                # RSI фильтр: входим только после реальной разгрузки/отката
                rsi_ok = (rsis[i-1] <= 56) or (rsis[i] <= 56)
                bull_break = (curr_close > curr_open) and (curr_close > highs[i-1]) and (bodies[i] > curr_atr * 0.20)
                bull_pinbar = (curr_close > curr_open) and ((curr_close - curr_low) / candle_range >= 0.62) and (curr_close > closes[i-1])
                
                if rsi_ok and (bull_break or bull_pinbar):
                    stop_price = min(matched_zone['bottom'], curr_low, lows[i-1]) - (curr_atr * 0.22)
                    r_dist = curr_close - stop_price
                    
                    if (curr_atr * 0.35) <= r_dist <= (curr_atr * 1.45):
                        in_position = True
                        pos_type = 'LONG'
                        entry_price = curr_close
                        stop_loss = stop_price
                        risk_dist = r_dist
                        be_trigger_price = entry_price + (r_dist * 0.90)
                        tp1_price = entry_price + (r_dist * 1.40)
                        tp2_price = entry_price + (r_dist * 2.60)
                        be_activated = False
                        tp1_hit = False
                        risk_usd = capital * risk_per_trade
                        demand_zones.remove(matched_zone)
                        continue

        # --- ВХОД В SHORT ---
        if downtrend and supply_zones:
            matched_zone = None
            for z in reversed(supply_zones):
                if (i - z['bar'] >= 4) and (curr_high >= z['bottom'] or highs[i-1] >= z['bottom']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                rsi_ok = (rsis[i-1] >= 44) or (rsis[i] >= 44)
                bear_break = (curr_close < curr_open) and (curr_close < lows[i-1]) and (bodies[i] > curr_atr * 0.20)
                bear_pinbar = (curr_close < curr_open) and ((curr_high - curr_close) / candle_range >= 0.62) and (curr_close < closes[i-1])
                
                if rsi_ok and (bear_break or bear_pinbar):
                    stop_price = max(matched_zone['top'], curr_high, highs[i-1]) + (curr_atr * 0.22)
                    r_dist = stop_price - curr_close
                    
                    if (curr_atr * 0.35) <= r_dist <= (curr_atr * 1.45):
                        in_position = True
                        pos_type = 'SHORT'
                        entry_price = curr_close
                        stop_loss = stop_price
                        risk_dist = r_dist
                        be_trigger_price = entry_price - (r_dist * 0.90)
                        tp1_price = entry_price - (r_dist * 1.40)
                        tp2_price = entry_price - (r_dist * 2.60)
                        be_activated = False
                        tp1_hit = False
                        risk_usd = capital * risk_per_trade
                        supply_zones.remove(matched_zone)
                        continue

    wins = trades.count('WIN')
    losses = trades.count('LOSS')
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0.0
    net_profit = capital - initial_capital
    
    return {
        'symbol': symbol,
        'trades': total,
        'winrate': winrate,
        'final_capital': capital,
        'profit_usd': net_profit
    }

def main():
    exchange = ccxt.bybit()
    print(f"Запуск бэктеста v6.0 (BOS + Защитный щит 0.9R + 1.4R/2.6R + Анти-распил)...\n")
    
    results = []
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='1h', days=60)
        if df is not None and len(df) > 200:
            df = prepare_indicators(df)
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Баланс: ${res['final_capital']:.2f} ({res['profit_usd']:+.2f}$)")
        else:
            print(f"[{symbol}] Пропущено (мало данных)")
        time.sleep(0.1)
            
    if results:
        df_res = pd.DataFrame(results)
        profitable_pairs = len(df_res[df_res['profit_usd'] > 0])
        print("\n" + "="*60)
        print(" ИТОГОВАЯ СВОДКА v6.0 (BOS + ЗАЩИТА 0.9R + АНТИ-РАСПИЛ) ")
        print("="*60)
        print(f"Всего сделок по портфелю: {df_res['trades'].sum()}")
        print(f"Плюсовых пар из {len(df_res)}: {profitable_pairs}")
        print(f"Средний винрейт по портфелю: {df_res['winrate'].mean():.2f}%")
        print(f"Средний конечный баланс: ${df_res['final_capital'].mean():.2f}")
        print(f"Общий финансовый результат по портфелю: {df_res['profit_usd'].sum():+.2f}$")
        best = df_res.loc[df_res['profit_usd'].idxmax()]
        worst = df_res.loc[df_res['profit_usd'].idxmin()]
        print(f"Лучшая пара: {best['symbol']} ({best['profit_usd']:+.2f}$)")
        print(f"Худшая пара: {worst['symbol']} ({worst['profit_usd']:+.2f}$)")
        print("="*60)

if __name__ == "__main__":
    main()
