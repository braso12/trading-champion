import os
import sys
import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# ==========================================
# 1. CREDENȚIALE & CONFIGURARE TURBO (1 MINUT)
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

if not API_KEY or not SECRET_KEY:
    print("❌ EROARE: Cheile ALPACA_API_KEY și ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
    sys.exit(1)

CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD",
    "XRP/USD", "AVAX/USD", "LINK/USD", "LTC/USD"
]
STOCK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "SPY"]

# ADN SCALPING (Acțiune rapidă pe graficul de 1 minut)
CHAMPION_DNA = {
    'fast_ma': 5,                # Media pe ultimele 5 minute (reacție instantanee)
    'slow_ma': 15,               # Media pe ultimele 15 minute
    'rsi_period': 14,            # Perioada RSI pentru a cumpăra scăderile bruște (Buy the Dip)
    'max_positions': 5,          # Maximum 5 poziții simultane
    'allocation_per_asset': 0.19,# ~19% din capital per monedă/acțiune
    'take_profit_pct': 0.008,    # Încasează profitul rapid la +0.8% creștere!
    'quick_cut_pct': -0.025,     # Taie rapid poziția dacă scade cu -2.5% ca să elibereze banii
    'stop_loss_pct': 0.15        # Gardianul de urgență pe serverul Alpaca
}

MAX_DRAWDOWN_PCT = 0.20          # MOARTE DEFINITIVĂ dacă tot contul pierde 20% (80.000$)
DEATH_FILE = "DEAD.lock"
CYCLES_PER_RUN = 5               # Execută 5 scanări la 60 de secunde distanță în fiecare rulare


# ==========================================
# 2. FUNCȚII AJUTĂTOARE & INDICATORI RAPIZI
# ==========================================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def cancel_orders_for_symbol(trading_client, symbol, pos_symbol):
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    for order in open_orders:
        if order.symbol in [symbol, pos_symbol]:
            trading_client.cancel_order_by_id(order.id)


def get_precision_qty(price, invest_usd, is_crypto=True):
    if not is_crypto:
        return int(invest_usd // price)
    raw_qty = invest_usd / price
    if price >= 1000:
        return round(raw_qty, 5)
    elif price >= 10:
        return round(raw_qty, 3)
    else:
        return round(raw_qty, 1)


# ==========================================
# 3. MOTORUL SCALPING (LA 60 DE SECUNDE)
# ==========================================
def run_fast_cycle(trading_client, crypto_client, stock_client):
    if os.path.exists(DEATH_FILE):
        print("💀 Agentul este deja MORT (DEAD.lock există). Oprire.")
        sys.exit(0)

    death_threshold = STARTING_CAPITAL * (1.0 - MAX_DRAWDOWN_PCT)

    # 1. VERIFICARE KILL-SWITCH GLOBAL (-20%)
    account = trading_client.get_account()
    current_equity = float(account.equity)
    available_cash = float(account.cash)
    pnl_total = current_equity - STARTING_CAPITAL
    drawdown_pct = ((STARTING_CAPITAL - current_equity) / STARTING_CAPITAL) * 100

    print(f"\n⚡ Scanare Rapidă (1m): {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"💰 Equity: {current_equity:.2f}$ (Profit/Pierdere Totală: {pnl_total:+.2f}$) | Cash: {available_cash:.2f}$ | Prag Moarte: {death_threshold:.2f}$")

    if current_equity <= death_threshold:
        print("🚨 [KILL-SWITCH ACTIVAT] Portofoliul a scăzut cu 20%!")
        trading_client.cancel_orders()
        time.sleep(1)
        trading_client.close_all_positions(cancel_orders=True)
        with open(DEATH_FILE, "w") as f:
            f.write(f"Killed at {datetime.now(timezone.utc)} with equity {current_equity:.2f}$\n")
        print("💀 Toate pozițiile au fost lichidate. DEAD.lock creat.")
        sys.exit(0)

    all_positions = trading_client.get_all_positions()
    held_map = {p.symbol: p for p in all_positions}
    open_positions_count = len(held_map)

    clock = trading_client.get_clock()
    is_stock_market_open = clock.is_open

    # Luăm ultimele 60 de minute de lumânări la 1 MINUT
    start_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    buy_candidates = []

    # 2. SCANARE CRIPTOMONEDE LA 1 MINUT
    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=CRYPTO_SYMBOLS,
        timeframe=TimeFrame.Minute,
        start=start_time
    )).df

    for symbol in CRYPTO_SYMBOLS:
        pos_symbol = symbol.replace("/", "")
        if crypto_bars.empty or symbol not in crypto_bars.index.get_level_values(0):
            continue

        closes = crypto_bars.xs(symbol)['close']
        if len(closes) < CHAMPION_DNA['slow_ma'] + 2:
            continue

        fast_ma = float(closes.rolling(window=CHAMPION_DNA['fast_ma']).mean().iloc[-1])
        slow_ma = float(closes.rolling(window=CHAMPION_DNA['slow_ma']).mean().iloc[-1])
        rsi = calculate_rsi(closes, CHAMPION_DNA['rsi_period'])
        price = float(closes.iloc[-1])
        trend_score = ((fast_ma - slow_ma) / slow_ma) * 100

        if pos_symbol in held_map:
            pos = held_map[pos_symbol]
            entry_price = float(pos.avg_entry_price)
            profit_pct = (price - entry_price) / entry_price

            print(f"💼 [ACTIV]  {symbol:<8} | Preț: {price:<9.3f}$ | Intrare: {entry_price:<9.3f}$ | P/L: {profit_pct*100:+.2f}% | Țintă: +{CHAMPION_DNA['take_profit_pct']*100:.2f}%")

            # A. Încasare rapidă a profitului (+0.8%)
            if profit_pct >= CHAMPION_DNA['take_profit_pct']:
                print(f"   💰 [PROFIT ÎNCASAT] {symbol} a atins {profit_pct*100:+.2f}%! Vând imediat...")
                cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                time.sleep(1)
                trading_client.close_position(pos_symbol)
                open_positions_count -= 1

            # B. Ieșire dacă trendul de 1 minut se întoarce și suntem deja pe plus (protejare câștig)
            elif profit_pct > 0.002 and fast_ma < slow_ma:
                print(f"   💵 [PROFIT SECURIZAT] Trendul pe {symbol} încetinește la {profit_pct*100:+.2f}%. Încasez câștigul!")
                cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                time.sleep(1)
                trading_client.close_position(pos_symbol)
                open_positions_count -= 1

            # C. Tăiere rapidă dacă poziția scade sub -2.5% și trendul e negativ
            elif profit_pct <= CHAMPION_DNA['quick_cut_pct'] and fast_ma < slow_ma:
                print(f"   🔴 [STOP RAPID] Eliberez capitalul blocat în {symbol} ({profit_pct*100:+.2f}%)...")
                cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                time.sleep(1)
                trading_client.close_position(pos_symbol)
                open_positions_count -= 1
        else:
            # Semnal 1: Trend rapid în urcare (Momentum) | Semnal 2: Supravândut pe 1 minut (Buy the Dip)
            is_momentum = fast_ma > slow_ma and rsi < 72
            is_dip_buy = rsi < 32
            if is_momentum or is_dip_buy:
                reason = "🚀 MOMENTUM" if is_momentum else f"💎 BUY THE DIP (RSI {rsi:.0f})"
                score = trend_score if is_momentum else (35 - rsi) * 0.1
                print(f"🟢 [SEMNAL] {symbol:<8} | Preț: {price:<9.3f}$ | RSI: {rsi:<4.1f} | Motiv: {reason}")
                buy_candidates.append({
                    'symbol': symbol,
                    'pos_symbol': pos_symbol,
                    'price': price,
                    'score': score,
                    'is_crypto': True,
                    'reason': reason
                })

    # 3. SCANARE ACȚIUNI SUA LA 1 MINUT (Când bursa e deschisă)
    if is_stock_market_open:
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=STOCK_SYMBOLS,
            timeframe=TimeFrame.Minute,
            start=datetime.now(timezone.utc) - timedelta(minutes=120),
            feed=DataFeed.IEX
        )).df

        for symbol in STOCK_SYMBOLS:
            if stock_bars.empty or symbol not in stock_bars.index.get_level_values(0):
                continue
            closes = stock_bars.xs(symbol)['close']
            if len(closes) < CHAMPION_DNA['slow_ma'] + 2:
                continue

            fast_ma = float(closes.rolling(window=CHAMPION_DNA['fast_ma']).mean().iloc[-1])
            slow_ma = float(closes.rolling(window=CHAMPION_DNA['slow_ma']).mean().iloc[-1])
            rsi = calculate_rsi(closes, CHAMPION_DNA['rsi_period'])
            price = float(closes.iloc[-1])
            trend_score = ((fast_ma - slow_ma) / slow_ma) * 100

            if symbol in held_map:
                pos = held_map[symbol]
                entry_price = float(pos.avg_entry_price)
                profit_pct = (price - entry_price) / entry_price
                print(f"💼 [ACTIV]  {symbol:<8} | Preț: {price:<9.2f}$ | P/L: {profit_pct*100:+.2f}%")
                if profit_pct >= CHAMPION_DNA['take_profit_pct'] or (profit_pct > 0.002 and fast_ma < slow_ma):
                    print(f"   💰 [PROFIT ÎNCASAT ACȚIUNI] Vând {symbol} la {profit_pct*100:+.2f}%!")
                    cancel_orders_for_symbol(trading_client, symbol, symbol)
                    time.sleep(1)
                    trading_client.close_position(symbol)
                    open_positions_count -= 1
            else:
                if (fast_ma > slow_ma and rsi < 72) or rsi < 32:
                    buy_candidates.append({
                        'symbol': symbol,
                        'pos_symbol': symbol,
                        'price': price,
                        'score': trend_score + 0.5,
                        'is_crypto': False,
                        'reason': "🏛️ STOCK SCALP"
                    })

    # 4. EXECUȚIA CUMPĂRĂRILOR RAPIDE
    slots_available = CHAMPION_DNA['max_positions'] - open_positions_count
    if slots_available > 0 and buy_candidates:
        buy_candidates.sort(key=lambda x: x['score'], reverse=True)
        for target in buy_candidates[:slots_available]:
            account = trading_client.get_account()
            available_cash = float(account.cash)
            invest_usd = min(available_cash * 0.95, current_equity * CHAMPION_DNA['allocation_per_asset'])

            if invest_usd < 50.0:
                break

            sym = target['symbol']
            pos_sym = target['pos_symbol']
            price = target['price']
            qty = get_precision_qty(price, invest_usd, target['is_crypto'])

            if qty <= 0:
                continue

            print(f"⚡ [CUMPĂRARE RAPIDĂ] {qty} {sym} (~{invest_usd:.0f}$) -> {target['reason']}")
            if target['is_crypto']:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                time.sleep(2)
                try:
                    pos = trading_client.get_open_position(pos_sym)
                    actual_qty = float(pos.qty)
                    actual_entry = float(pos.avg_entry_price)
                    stop_price = round(actual_entry * (1.0 - CHAMPION_DNA['stop_loss_pct']), 4 if price < 10 else 2)
                    limit_price = round(stop_price * 0.99, 4 if price < 10 else 2)
                    trading_client.submit_order(StopLimitOrderRequest(
                        symbol=sym, qty=actual_qty, side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC, stop_price=stop_price, limit_price=limit_price
                    ))
                except Exception:
                    pass
            else:
                stop_price = round(price * (1.0 - CHAMPION_DNA['stop_loss_pct']), 2)
                tp_price = round(price * (1.0 + CHAMPION_DNA['take_profit_pct']), 2)
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=tp_price),
                    stop_loss=StopLossRequest(stop_price=stop_price)
                ))


if __name__ == "__main__":
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    crypto_client = CryptoHistoricalDataClient()
    stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    print(f"🚀 PORNIT MODUL TURBO: {CYCLES_PER_RUN} scanări la interval de 60 secunde...")
    for i in range(CYCLES_PER_RUN):
        print(f"\n================ CICLUL {i+1}/{CYCLES_PER_RUN} ================")
        try:
            run_fast_cycle(trading_client, crypto_client, stock_client)
        except Exception as e:
            print(f"⚠️ Eroare temporară în ciclul {i+1}: {e}")
        
        if i < CYCLES_PER_RUN - 1:
            time.sleep(55)
