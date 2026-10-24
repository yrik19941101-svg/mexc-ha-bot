import ccxt
import pandas as pd
import numpy as np

def calculate_indicators(df):
    """Рассчитывает технические индикаторы на основе DataFrame со свечами."""
    # 1. Экспоненциальная средняя (EMA20)
    df['ema20'] = df['close'].ewm(span=20).mean()
    
    # 2. Полосы Боллинджера (20, 2)
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['upper_band'] = df['sma20'] + (df['std20'] * 2)
    df['lower_band'] = df['sma20'] - (df['std20'] * 2)
    
    # 3. RSI (14 периодов)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # 4. Анализ объемов (сравнение с 20-периодной средней)
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    df['high_volume'] = df['volume'] > (df['vol_ma20'] * 1.5) # Объем выше среднего в 1.5 раза
    
    return df

def analyze_market_with_indicators():
    """Выгружает топ-30 монет и анализирует рынок за последние 30 дней."""
    print("Подключение к бирже и поиск ликвидных фьючерсов...")
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    exchange.load_markets()
    
    # Получаем топ-30 по суточному объему (quoteVolume)
    tickers = exchange.fetch_tickers()
    futures_tickers = {s: d for s, d in tickers.items() if 'USDT' in s and d.get('quoteVolume')}
    sorted_coins = sorted(futures_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0), reverse=True)
    top_30 = [item[0] for item in sorted_coins[:30]]
    
    print(f"Топ-30 ликвидных монет получено. Начинаем глубокий анализ за 30 дней...")
    strategy_signals = {}
    
    for symbol in top_30:
        try:
            # 720 часовых свечей = ровно 30 дней истории (24 часа * 30 дней)
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=720)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_indicators(df)
            
            last = df.iloc[-1]
            
            # Условия для стратегии:
            is_oversold = last['close'] <= last['lower_band'] and last['rsi'] < 30
            is_overbought = last['close'] >= last['upper_band'] and last['rsi'] > 70
            
            if is_oversold and last['high_volume']:
                signal = 'STRONG_BUY_DIP'
            elif is_overbought:
                signal = 'STRONG_SELL_TOP'
            else:
                signal = 'NEUTRAL'
                
            strategy_signals[symbol] = {
                'close': float(last['close']),
                'rsi': round(float(last['rsi']), 2) if not pd.isna(last['rsi']) else 0.0,
                'signal': signal
            }
        except Exception as e:
            print(f"Ошибка при обработке монеты {symbol}: {e}")
            
    return strategy_signals

if __name__ == "__main__":
    signals = analyze_market_with_indicators()
    print("\n--- Результаты анализа стратегии за 30 дней ---")
    for coin, data in signals.items():
        print(f"{coin} -> Цена: {data['close']}, RSI: {data['rsi']}, Сигнал: {data['signal']}")
