import ccxt
import pandas as pd
import numpy as np

def run_backtest():
    print("Загрузка данных для бэктеста за 60 дней (1440 часов)...")
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    exchange.load_markets()
    
    # Берем топ-15 самых ликвидных монет для ускорения симуляции
    tickers = exchange.fetch_tickers()
    futures_tickers = {s: d for s, d in tickers.items() if 'USDT' in s and d.get('quoteVolume')}
    sorted_coins = sorted(futures_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0), reverse=True)
    top_coins = [item[0] for item in sorted_coins[:15]]
    
    initial_deposit = 100.0
    balance = initial_deposit
    leverage = 20
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    
    print(f"Стартовый депозит: ${balance} | Плечо: {leverage}x | Риск-профиль: TP +6%, SL -3%\n")
    
    history_trades = []

    for symbol in top_coins:
        try:
            # Загружаем 1440 часовых свечей (60 дней)
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=1440)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Индикаторы
            df['ema20'] = df['close'].ewm(span=20).mean()
            df['sma20'] = df['close'].rolling(window=20).mean()
            df['std20'] = df['close'].rolling(window=20).std()
            df['upper_band'] = df['sma20'] + (df['std20'] * 2)
            df['lower_band'] = df['sma20'] - (df['std20'] * 2)
            
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            df['vol_ma20'] = df['volume'].rolling(window=20).mean()
            df['volume_spike'] = df['volume'] > (df['vol_ma20'] * 2.0)
            
            df = df.dropna().reset_index(drop=True)
            
            # Прогон стратегии по истории свеча за свечой
            in_position = False
            entry_price = 0
            tp_price = 0
            sl_price = 0
            margin_amount = 0
            
            for i in range(1, len(df)):
                prev = df.iloc[i-1]
                curr = df.iloc[i]
                
                # Если уже в сделке — проверяем, сработал ли Тейк-профит или Стоп-лосс на текущей свече
                if in_position:
                    # Проверяем максимум и минимум свечи для выхода
                    if curr['high'] >= tp_price:
                        # Сработал Тейк-профит (+6% движения цены * 20 плечо = +120% к марже)
                        profit = margin_amount * 1.20
                        balance += profit
                        winning_trades += 1
                        total_trades += 1
                        in_position = False
                        history_trades.append(('WIN', profit))
                    elif curr['low'] <= sl_price:
                        # Сработал Стоп-лосс (-3% движения цены * 20 плечо = -60% к марже)
                        loss = margin_amount * 0.60
                        balance -= loss
                        losing_trades += 1
                        total_trades += 1
                        in_position = False
                        history_trades.append(('LOSS', -loss))
                
                # Ищем новый сигнал на вход, если не в позиции
                if not in_position and balance > 5: # Если есть деньги на балансе
                    is_breakout = (prev['close'] <= prev['upper_band']) and (curr['close'] > curr['upper_band'])
                    
                    if is_breakout and curr['volume_spike'] and (55 < curr['rsi'] < 75):
                        in_position = True
                        entry_price = curr['close']
                        tp_price = entry_price * 1.06
                        sl_price = entry_price * 0.97
                        
                        # Сложный процент: маржа равна 1% от текущего баланса (минимум 1$)
                        margin_amount = max(1.0, balance * 0.01)
                        
        except Exception as e:
            print(f"Ошибка при бэктесте {symbol}: {e}")
            
    # Итоги бэктеста
    print("\n==============================================")
    print("          ИТОГИ БЭКТЕСТА ЗА 60 ДНЕЙ           ")
    print("==============================================")
    print(f"Начальный депозит:  ${initial_deposit:.2f}")
    print(f"Итоговый баланс:    ${balance:.2f}")
    
    net_profit = balance - initial_deposit
    roi_total = (net_profit / initial_deposit) * 100
    print(f"Чистая прибыль:     ${net_profit:.2f}")
    print(f"Общий ROI стратегии: {roi_total:.2f}%")
    print(f"Всего сделок:       {total_trades}")
    
    if total_trades > 0:
        win_rate = (winning_trades / total_trades) * 100
        print(f"Успешных сделок (WinRate): {win_rate:.1f}% ({winning_trades} побед / {losing_trades} поражений)")
    else:
        print("За выбранный период по жестким критериям фильтра не было открыто сделок (рынок был во флэте/падении).")
    print("==============================================")

if __name__ == "__main__":
    run_backtest()
