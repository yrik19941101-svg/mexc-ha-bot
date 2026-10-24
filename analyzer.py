import ccxt
import pandas as pd
import numpy as np

# Список из 20 ликвидных пар на Bybit
SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT',
    'AVAX/USDT', 'DOGE/USDT', 'LINK/USDT', 'DOT/USDT', 'NEAR/USDT',
    'APT/USDT', 'ARB/USDT', 'OP/USDT', 'SUI/USDT', 'INJ/USDT',
    'FIL/USDT', 'RENDER/USDT', 'ATOM/USDT', 'LTC/USDT', 'TIA/USDT'
]

def fetch_historical_data(exchange, symbol, timeframe='4h', limit=400):
    """Загрузка исторических данных с Bybit"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"Ошибка загрузки {symbol}: {e}")
        return None

def backtest_strategy(df, symbol):
    """Логика бэктеста стратегии (структура + зоны спроса) для конкретной пары"""
    trades = []
    initial_capital = 1000.0
    capital = initial_capital
    risk_per_trade = 0.02 # Риск 2% на сделку
    
    in_position = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    position_type = None
    
    for i in range(10, len(df)):
        current_close = df.loc[i, 'close']
        current_low = df.loc[i, 'low']
        current_high = df.loc[i, 'high']
        
        if not in_position:
            # Ищем зону спроса (минимальные цены за последние 5 баров)
            recent_low = df.loc[i-5:i-1, 'low'].min()
            recent_high = df.loc[i-5:i-1, 'high'].max()
            
            # Условие входа в Лонг при откате к зоне спроса
            if current_low <= recent_low * 1.002:
                in_position = True
                position_type = 'LONG'
                entry_price = current_close
                stop_loss = recent_low * 0.993 # Стоп за зону с запасом
                risk = entry_price - stop_loss
                if risk <= 0:
                    in_position = False
                    continue
                take_profit = entry_price + (risk * 2.5) # R:R 1:2.5
                
        elif in_position:
            if position_type == 'LONG':
                if current_low <= stop_loss:
                    capital -= capital * risk_per_trade
                    in_position = False
                    trades.append({'result': 'LOSS'})
                elif current_high >= take_profit:
                    capital += capital * risk_per_trade * 2.5
                    in_position = False
                    trades.append({'result': 'WIN'})
                    
    wins = len([t for t in trades if t['result'] == 'WIN'])
    losses = len([t for t in trades if t['result'] == 'LOSS'])
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0
    net_profit_pct = ((capital - initial_capital) / initial_capital) * 100
    
    return {
        'symbol': symbol,
        'trades': total,
        'wins': wins,
        'losses': losses,
        'winrate': winrate,
        'profit_pct': net_profit_pct
    }

def main():
    exchange = ccxt.bybit()
    print(f"Запуск бэктеста для {len(SYMBOLS)} пар за ~60 дней (таймфрейм 4h)...\n")
    
    results = []
    
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='4h', limit=400)
        if df is not None and not df.empty:
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Доходность: {res['profit_pct']:+.2f}%")
        else:
            print(f"[{symbol}] Пропущено (нет данных)")
            
    # Общая сводка
    if results:
        df_res = pd.DataFrame(results)
        print("\n" + "="*45)
        print(" ИТОГОВАЯ СВОДКА ПО ВСЕМ ПАРАМ ")
        print("="*45)
        print(f"Всего протестировано пар: {len(df_res)}")
        print(f"Средний винрейт по портфелю: {df_res['winrate'].mean():.2f}%")
        print(f"Средняя доходность пассивного портфеля: {df_res['profit_pct'].mean():+.2f}%")
        best_pair = df_res.loc[df_res['profit_pct'].idxmax()]
        worst_pair = df_res.loc[df_res['profit_pct'].idxmin()]
        print(f"Лучшая пара: {best_pair['symbol']} ({best_pair['profit_pct']:+.2f}%)")
        print(f"Худшая пара: {worst_pair['symbol']} ({worst_pair['profit_pct']:+.2f}%)")
        print("="*45)

if __name__ == "__main__":
    main()
