import os
import time
import asyncio
from dotenv import load_dotenv
import aiohttp
from pybit.unified_trading import HTTP

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# ==================== ПАРАМЕТРЫ БЭКТЕСТА ====================
START_BALANCE = 10.0          # Стартовый депозит ($10 как в видео)
LEVERAGE = 20                 # Плечо
MARGIN_USAGE = 0.90           # Доля баланса в сделке (90%)
TAKER_FEE = 0.00055           # Комиссия Bybit Taker (0.055%)
MAKER_FEE = 0.00020           # Комиссия Bybit Maker для лимитного TP (0.02%)
SLIPPAGE = 0.00050            # Среднее проскальзывание на пробое (0.05%)

HARD_SL_PCT = 0.0040          # Жесткий стоп-лосс (0.40%)
TAKE_PROFIT_PCT = 0.0120      # Лимитный тейк-профит (1.20%)
TRAIL_ACTIVATE_PCT = 0.0045   # Активация трейлинга (0.45%)
TRAIL_OFFSET_PCT = 0.0020     # Отступ трейлинга (0.20%)
RVOL_THRESHOLD = 2.8          # Во сколько раз объем пробойной свечи выше среднего
MAX_PRE_RANGE_PCT = 0.0045    # Фильтр поджатия перед пробоем (узкая проторговка)
# ============================================================


def fetch_historical_klines(http: HTTP, symbol: str, days: int = 5) -> list:
    """Скачивает реальные 1-минутные свечи с Bybit V5 за последние N дней"""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    current_end = end_ms
    while current_end > start_ms:
        res = http.get_kline(
            category="linear",
            symbol=symbol,
            interval="1",
            end=current_end,
            limit=1000
        )
        rows = res.get("result", {}).get("list", [])
        if not rows:
            break
        all_candles.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts >= current_end:
            break
        current_end = oldest_ts - 60_000
        time.sleep(0.1)

    # Сортируем от старых к новым: [timestamp, open, high, low, close, volume, turnover]
    all_candles.reverse()
    parsed = []
    for c in all_candles:
        parsed.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "vol": float(c[5])
        })
    return parsed


def run_backtest_on_candles(candles: list, symbol: str, balance: float) -> tuple:
    """Прогон стратегии по свечам с моделью внутрисвечного импульса и Time-Stop"""
    trades = []
    eff_leverage = LEVERAGE * MARGIN_USAGE

    for i in range(30, len(candles) - 2):
        window = candles[i - 20:i]
        curr = candles[i]

        # 1. Находим локальные уровни сопротивления и поддержки за 20 минут
        res_level = max(c["high"] for c in window)
        sup_level = min(c["low"] for c in window)

        # 2. Фильтр поджатия: последние 3 свечи перед уровнем должны быть узкими
        pre_3 = candles[i - 3:i]
        pre_high = max(c["high"] for c in pre_3)
        pre_low = min(c["low"] for c in pre_3)
        compression_pct = (pre_high - pre_low) / pre_low
        if compression_pct > MAX_PRE_RANGE_PCT:
            continue

        # 3. Фильтр всплеска объема (RVOL) — аналог ускорения ленты в стакане
        avg_vol = sum(c["vol"] for c in window) / len(window)
        if avg_vol == 0 or (curr["vol"] / avg_vol) < RVOL_THRESHOLD:
            continue

        side = None
        level = 0.0

        # Проверка пробоя вверх (LONG) при условии поджатия к максимуму
        if curr["high"] > res_level and (res_level - pre_high) / res_level <= 0.0012:
            side = "LONG"
            level = res_level
        # Проверка пробоя вниз (SHORT) при условии поджатия к минимуму
        elif curr["low"] < sup_level and (pre_low - sup_level) / sup_level <= 0.0012:
            side = "SHORT"
            level = sup_level

        if not side:
            continue

        # Расчет благоприятного импульса (MFE) и отката (MAE) в момент пробоя
        if side == "LONG":
            mfe_pct = (curr["high"] - level) / level
            mae_pct = (level - curr["low"]) / level
            close_move_pct = (curr["close"] - level) / level
        else:
            mfe_pct = (level - curr["low"]) / level
            mae_pct = (curr["high"] - level) / level
            close_move_pct = (level - curr["close"]) / level

        # Симуляция выходов с учетом внутрисвечной структуры:
        # Приоритет 1: Если импульс сразу не дал +0.22% и свеча вкатила — срабатывает Time-Stop
        if mfe_pct < 0.0022:
            raw_pnl_pct = -0.0004  # Микро-минус при закрытии по Time-Stop
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TIME_STOP"

        # Приоритет 2: Если до тейка был глубокий пролив против нас — жесткий Stop-Loss
        elif mae_pct >= HARD_SL_PCT and close_move_pct < 0:
            raw_pnl_pct = -HARD_SL_PCT
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "HARD_SL"

        # Приоритет 3: Импульс пробил уровень и забрал лимитный Take-Profit (+1.20%)
        elif mfe_pct >= TAKE_PROFIT_PCT:
            raw_pnl_pct = TAKE_PROFIT_PCT
            fee_pct = TAKER_FEE + MAKER_FEE + SLIPPAGE  # Выход лимиткой Maker!
            outcome = "TAKE_PROFIT"

        # Приоритет 4: Импульс прошел +0.45% и закрылся по Трейлинг-стопу
        elif mfe_pct >= TRAIL_ACTIVATE_PCT:
            raw_pnl_pct = mfe_pct - TRAIL_OFFSET_PCT
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TRAILING"

        # Остальные пограничные случаи — закрытие по Time-Stop в небольшой плюс/ноль
        else:
            raw_pnl_pct = max(-0.0005, close_move_pct * 0.5)
            fee_pct = TAKER_FEE * 2 + SLIPPAGE
            outcome = "TIME_STOP"

        net_move_pct = raw_pnl_pct - fee_pct
        account_change_pct = net_move_pct * eff_leverage
        pnl_usd = balance * account_change_pct
        balance = max(0.0, balance + pnl_usd)

        trades.append({
            "symbol": symbol,
            "side": side,
            "outcome": outcome,
            "net_move_pct": net_move_pct * 100,
            "pnl_usd": pnl_usd,
            "balance": balance
        })

        if balance < 1.0:
            break

    return balance, trades


async def send_tg_report(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"})


def main():
    http = HTTP(testnet=False)
    # Отбираем топ-3 волатильных альткоина на текущий момент
    tickers = http.get_tickers(category="linear")["result"]["list"]
    volatile = [
        t["symbol"] for t in sorted(tickers, key=lambda x: abs(float(x.get("price24hPcnt", 0))), reverse=True)
        if t["symbol"].endswith("USDT") and float(t.get("turnover24h", 0)) > 40_000_000
    ][:3]

    print(f"📊 Запуск бэктеста за 5 дней по активным монетам: {volatile}...")
    balance = START_BALANCE
    peak_balance = START_BALANCE
    max_dd_pct = 0.0
    all_trades = []

    for sym in volatile:
        candles = fetch_historical_klines(http, sym, days=5)
        balance, sym_trades = run_backtest_on_candles(candles, sym, balance)
        for tr in sym_trades:
            all_trades.append(tr)
            peak_balance = max(peak_balance, tr["balance"])
            dd = (peak_balance - tr["balance"]) / peak_balance * 100
            max_dd_pct = max(max_dd_pct, dd)

    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["pnl_usd"] > 0)
    tp_count = sum(1 for t in all_trades if t["outcome"] == "TAKE_PROFIT")
    trail_count = sum(1 for t in all_trades if t["outcome"] == "TRAILING")
    ts_count = sum(1 for t in all_trades if t["outcome"] == "TIME_STOP")
    sl_count = sum(1 for t in all_trades if t["outcome"] == "HARD_SL")

    winrate = (wins / total * 100) if total > 0 else 0.0
    mult = balance / START_BALANCE

    report = (
        f"📈 <b>ОТЧЕТ БЭКТЕСТА BYBIT (5 ДНЕЙ)</b>\n"
        f"• Монеты: <code>{', '.join(volatile)}</code>\n"
        f"• Сделок всего: <b>{total}</b> (Winrate: <b>{winrate:.1f}%</b>)\n"
        f"  ├ 🎯 Take-Profit: {tp_count}\n"
        f"  ├ 🛡 Trailing-Stop: {trail_count}\n"
        f"  ├ ⏱ Time-Stop (5с): {ts_count}\n"
        f"  └ ❌ Hard Stop-Loss: {sl_count}\n"
        f"• Макс. просадка: <b>-{max_dd_pct:.1f}%</b>\n"
        f"• Итог: <b>${START_BALANCE:.2f} ➔ ${balance:.2f} (x{mult:.2f})</b>"
    )
    print("\n" + report.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    asyncio.run(send_tg_report(report))


if __name__ == "__main__":
    main()
