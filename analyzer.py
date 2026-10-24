import ccxt
import pandas as pd

def get_top_liquid_futures():
    exchange = ccxt.binance({
        'options': {'defaultType': 'future'}
    })
    exchange.load_markets()
    
    # Получаем данные тикеров
    tickers = exchange.fetch_tickers()
    
    # Фильтруем USDT-фьючерсы и сортируем по суточному объему (quoteVolume)
    futures_tickers = {
        symbol: data for symbol, data in tickers.items() 
        if '/USDT:USDT' in symbol or ('USDT' in symbol and data.get('quoteVolume'))
    }
    
    sorted_coins = sorted(
        futures_tickers.items(), 
        key=lambda x: x[1].get('quoteVolume', 0) if x[1] else 0, 
        reverse=True
    )
    
    # Берем топ-30
    top_30 = [item[0] for item in sorted_coins[:30]]
    return exchange, top_30

def analyze_market():
    exchange, symbols = get_top_liquid_futures()
    print(f"Топ-30 ликвидных монет получено: {len(symbols)}")
    
    strategy_signals = {}
    
    for symbol in symbols:
        try:
            # Загружаем свечи (например, 1h)
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Простейший поиск закономерности: расчет RSI или отклонения от EMA
            df['ema20'] = df['close'].ewm(span=20).mean()
            last_close = df['close'].iloc[-1]
            last_ema = df['ema20'].iloc[-1]
            
            # Закономерность: если цена сильно ниже EMA20, возможен отскок (Mean Reversion)
            deviation = (last_close - last_ema) / last_ema
            strategy_signals[symbol] = {
                'close': last_close,
                'deviation_ema20': deviation,
                'signal': 'BUY_DIP' if deviation < -0.03 else ('SELL_TOP' if deviation > 0.03 else 'NEUTRAL')
            }
        except Exception as e:
            print(f"Ошибка по монете {symbol}: {e}")
            
    return strategy_signals

if __name__ == "__main__":
    signals = analyze_market()
    print("Результаты анализа стратегии:", signals)
