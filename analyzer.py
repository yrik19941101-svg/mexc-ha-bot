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
    """Расчет трендовых EMA и волатильности ATR"""
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    df['body'] = (df['close'] - df['open']).abs()
    return df

def backtest_strategy(df, symbol):
    initial_capital = 150.0
    capital = initial_capital
    risk_per_trade = 0.02  # Фиксированный риск 2% от депозита
    
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    ema20 = df['ema20'].values
    ema50 = df['ema50'].values
    ema100 = df['ema100'].values
    atrs = df['atr'].values
    bodies = df['body'].values
    
    # Списки активных нетестированных (свежих) зон
    demand_zones = []  # [{'bottom': float, 'top': float, 'bar': int}]
    supply_zones = []  # [{'bottom': float, 'top': float, 'bar': int}]
    
    in_position = False
    pos_type = None
    entry_price = 0.0
    stop_loss = 0.0
    tp1_price = 0.0
    tp2_price = 0.0
    be_activated = False
    risk_usd = 0.0
    entry_bar = 0
    
    trades = []
    
    for i in range(100, len(df)):
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
        # 1. ПОИСК И РЕГИСТРАЦИЯ НОВЫХ ЗОН СПРОСА И ПРЕДЛОЖЕНИЯ (С ПОДТВЕРЖДЕНИЕМ)
        # -------------------------------------------------------------------------
        # Проверяем свечу p = i - 3 (подтвержденный экстремум с импульсным выходом)
        p = i - 3
        if p >= 10:
            # Проверка формирования Зоны Спроса (Swing Low + импульс вверх)
            is_swing_low = (lows[p] <= np.min(lows[p-5:p])) and (lows[p] <= np.min(lows[p+1:i+1]))
            impulse_up = ( np.max(highs[p+1:i+1]) - lows[p] ) >= (atrs[p] * 1.3)
            if is_swing_low and impulse_up and (closes[i] > highs[p]):
                demand_zones.append({
                    'bottom': lows[p],
                    'top': lows[p] + atrs[p] * 0.65,
                    'bar': p
                })
                
            # Проверка формирования Зоны Предложения (Swing High + импульс вниз)
            is_swing_high = (highs[p] >= np.max(highs[p-5:p])) and (highs[p] >= np.max(highs[p+1:i+1]))
            impulse_down = ( highs[p] - np.min(lows[p+1:i+1]) ) >= (atrs[p] * 1.3)
            if is_swing_high and impulse_down and (closes[i] < lows[p]):
                supply_zones.append({
                    'bottom': highs[p] - atrs[p] * 0.65,
                    'top': highs[p],
                    'bar': p
                })

        # Очистка пробитых (инвалидированных) и устаревших зон (>120 часов)
        demand_zones = [
            z for z in demand_zones 
            if (curr_close >= z['bottom']) and (i - z['bar'] <= 120)
        ]
        supply_zones = [
            z for z in supply_zones 
            if (curr_close <= z['top']) and (i - z['bar'] <= 120)
        ]

        # -------------------------------------------------------------------------
        # 2. СОПРОВОЖДЕНИЕ ОТКРЫТОЙ ПОЗИЦИИ
        # -------------------------------------------------------------------------
        if in_position:
            if pos_type == 'LONG':
                if curr_low <= stop_loss:
                    if be_activated:
                        # 60% закрыто на 1.2R (+0.72R), остаток 40% закрыт в БУ
                        capital += risk_usd * 0.72
                        trades.append('WIN')
                    else:
                        capital -= risk_usd
                        trades.append('LOSS')
                    in_position = False
                    continue
                
                if curr_high >= tp2_price:
                    # 60% на 1.2R (+0.72R) + 40% на 2.4R (+0.96R) = +1.68R
                    capital += risk_usd * 1.68
                    trades.append('WIN')
                    in_position = False
                    continue
                    
                if not be_activated and curr_high >= tp1_price:
                    be_activated = True
                    stop_loss = entry_price  # Перевод остатка в безубыток
                    
            elif pos_type == 'SHORT':
                if curr_high >= stop_loss:
                    if be_activated:
                        capital += risk_usd * 0.72
                        trades.append('WIN')
                    else:
                        capital -= risk_usd
                        trades.append('LOSS')
                    in_position = False
                    continue
                    
                if curr_low <= tp2_price:
                    capital += risk_usd * 1.68
                    trades.append('WIN')
                    in_position = False
                    continue
                    
                if not be_activated and curr_low <= tp1_price:
                    be_activated = True
                    stop_loss = entry_price
                    
            continue

        # -------------------------------------------------------------------------
        # 3. ПОИСК ТОЧКИ ВХОДА ОТ СВЕЖИХ ЗОН С ПОДТВЕРЖДЕНИЕМ
        # -------------------------------------------------------------------------
        uptrend = (curr_close > ema100[i]) and (ema20[i] >= ema50[i] * 0.998)
        downtrend = (curr_close < ema100[i]) and (ema20[i] <= ema50[i] * 1.002)
        
        candle_range = curr_high - curr_low
        if candle_range == 0:
            continue

        # --- ВХОД В LONG ОТ СВЕЖЕЙ ЗОНЫ СПРОСА ---
        if uptrend and demand_zones:
            # Ищем зону, которую цена протестировала на текущем или прошлом баре
            matched_zone = None
            for z in reversed(demand_zones):
                if (i - z['bar'] >= 5) and (curr_low <= z['top'] or lows[i-1] <= z['top']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                # Подтверждение реакции: бычий слом предыдущего бара ИЛИ сильный выкуп (пин-бар) от зоны
                bull_break = (curr_close > curr_open) and (curr_close > highs[i-1]) and (bodies[i] > curr_atr * 0.2)
                bull_pinbar = (curr_close > curr_open) and ((curr_close - curr_low) / candle_range >= 0.65) and (curr_close > closes[i-1])
                
                if bull_break or bull_pinbar:
                    stop_price = min(matched_zone['bottom'], curr_low, lows[i-1]) - (curr_atr * 0.25)
                    risk_dist = curr_close - stop_price
                    
                    # Фильтр Анти-Фомо: не входим, если свеча уже улетела (>1.5 ATR)
                    if (curr_atr * 0.35) <= risk_dist <= (curr_atr * 1.55):
                        in_position = True
                        pos_type = 'LONG'
                        entry_price = curr_close
                        stop_loss = stop_price
                        tp1_price = entry_price + (risk_dist * 1.2)
                        tp2_price = entry_price + (risk_dist * 2.4)
                        be_activated = False
                        risk_usd = capital * risk_per_trade
                        entry_bar = i
                        # Удаляем зону (она отработана!)
                        demand_zones.remove(matched_zone)
                        continue

        # --- ВХОД В SHORT ОТ СВЕЖЕЙ ЗОНЫ ПРЕДЛОЖЕНИЯ ---
        if downtrend and supply_zones:
            matched_zone = None
            for z in reversed(supply_zones):
                if (i - z['bar'] >= 5) and (curr_high >= z['bottom'] or highs[i-1] >= z['bottom']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                bear_break = (curr_close < curr_open) and (curr_close < lows[i-1]) and (bodies[i] > curr_atr * 0.2)
                bear_pinbar = (curr_close < curr_open) and ((curr_high - curr_close) / candle_range >= 0.65) and (curr_close < closes[i-1])
                
                if bear_break or bear_pinbar:
                    stop_price = max(matched_zone['top'], curr_high, highs[i-1]) + (curr_atr * 0.25)
                    risk_dist = stop_price - curr_close
                    
                    if (curr_atr * 0.35) <= risk_dist <= (curr_atr * 1.55):
                        in_position = True
                        pos_type = 'SHORT'
                        entry_price = curr_close
                        stop_loss = stop_price
                        tp1_price = entry_price - (risk_dist * 1.2)
                        tp2_price = entry_price - (risk_dist * 2.4)
                        be_activated = False
                        risk_usd = capital * risk_per_trade
                        entry_bar = i
                        # Удаляем зону (она отработана!)
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
    print(f"Запуск бэктеста v5.0 (Свежие зоны Order Block + Анти-Фомо фильтр + 1.2R/2.4R)...\n")
    
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
        print(" ИТОГОВАЯ СВОДКА v5.0 (СВЕЖИЕ ЗОНЫ + АНТИ-ФОМО) ")
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
