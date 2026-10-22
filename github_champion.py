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
# 1. CREDENȚIALE & PIEȚE MONITORIZATE
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

if not API_KEY or not SECRET_KEY:
    print("❌ EROARE: Cheile ALPACA_API_KEY și ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
    sys.exit(1)

# Lista de Criptomonede (tranzacționate 24/7)
CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD",
    "XRP/USD", "AVAX/USD", "LINK/USD", "LTC/USD"
]

# Lista de Acțiuni SUA & ETF-uri (tranzacționate când bursa din New York e deschisă)
STOCK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "SPY"]

# ADN-ul Campionului (Multi-Asset)
CHAMPION_DNA = {
    'fast_ma': 12,               # Media rapidă (12 ore)
    'slow_ma': 45,               # Media lentă (45 ore)
    'max_positions': 5,          # Deține simultan cel mult 5 monede/acțiuni
    'allocation_per_asset': 0.19,# Alocă ~19% din capital per activ (ca să poată cumpăra 5 diferite)
    'take_profit_pct': 0.15,     # Încasează profitul la +15%
    'stop_loss_pct': 0.20        # Stop-Loss individual pe server la -20%
}

MAX_DRAWDOWN_PCT = 0.20
DEATH_FILE = "DEAD.lock"


# ==========================================
# 2. FUNCȚII AJUTĂTOARE PENTRU ORDINE
# ==========================================
def cancel_orders_for_symbol(trading_client, symbol, pos_symbol):
    """Anulează DOAR ordinele Stop-Loss ale monedei pe care vrem să o vindem, fără să strice protecția celorlalte."""
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    for order in open_orders:
        if order.symbol in [symbol, pos_symbol]:
            trading_client.cancel_order_by_id(order.id)


def get_precision_qty(price, invest_usd, is_crypto=True):
    """Calculează cantitatea rotunjită corect în funcție de prețul monedei/acțiunii."""
    if not is_crypto:
        return int(invest_usd // price)  # La acțiuni cu ordin Bracket cumpărăm acțiuni întregi
    raw_qty = invest_usd / price
    if price >= 1000:
        return round(raw_qty, 5)
    elif price >= 10:
        return round(raw_qty, 3)
    else:
        return round(raw_qty, 1)


# ==========================================
# 3. MOTORUL MULTI-PIAȚĂ (SINGLE-SHOT)
# ==========================================
def run_multi_market_cycle():
    if os.path.exists(DEATH_FILE):
        print("💀 Agentul este deja MORT (DEAD.lock există). Oprire.")
        sys.exit(0)

    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    crypto_client = CryptoHistoricalDataClient()
    stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    death_threshold = STARTING_CAPITAL * (1.0 - MAX_DRAWDOWN_PCT)

    # 1. VERIFICARE KILL-SWITCH GLOBAL (-20% PE TOT PORTOFOLIUL)
    account = trading_client.get_account()
    current_equity = float(account.equity)
    available_cash = float(account.cash)
    drawdown_pct = ((STARTING_CAPITAL - current_equity) / STARTING_CAPITAL) * 100

    print(f"⏰ Rulare Multi-Piață: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"💰 Equity Total: {current_equity:.2f}$ | Cash liber: {available_cash:.2f}$ | Prag Moarte: {death_threshold:.2f}$ ({drawdown_pct:+.2f}%)\n")

    if current_equity <= death_threshold:
        print("🚨 [KILL-SWITCH ACTIVAT] Portofoliul total a scăzut cu 20%!")
        trading_client.cancel_orders()
        time.sleep(1)
        trading_client.close_all_positions(cancel_orders=True)
        with open(DEATH_FILE, "w") as f:
            f.write(f"Killed at {datetime.now(timezone.utc)} with equity {current_equity:.2f}$\n")
        print("💀 Toate monedele și acțiunile au fost vândute. Certificatul DEAD.lock a fost creat.")
        sys.exit(0)

    # Preluăm toate pozițiile deja deschise în portofoliu
    all_positions = trading_client.get_all_positions()
    held_map = {p.symbol: p for p in all_positions}
    open_positions_count = len(held_map)

    # Verificăm dacă bursa de acțiuni SUA este deschisă acum
    clock = trading_client.get_clock()
    is_stock_market_open = clock.is_open
    print(f"📂 Poziții deschise acum: {open_positions_count}/{CHAMPION_DNA['max_positions']} | Bursa SUA deschisă: {'DA 🟢' if is_stock_market_open else 'NU (Doar Crypto 24/7) 🌙'}")

    lookback_hours = CHAMPION_DNA['slow_ma'] + 25
    start_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    buy_candidates = []

    # 2. SCANARE CRIPTOMONEDE
    print("\n--- 🔍 SCANARE CRIPTOMONEDE ---")
    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=CRYPTO_SYMBOLS,
        timeframe=TimeFrame.Hour,
        start=start_time
    )).df

    for symbol in CRYPTO_SYMBOLS:
        pos_symbol = symbol.replace("/", "")
        if crypto_bars.empty or symbol not in crypto_bars.index.get_level_values(0):
            continue

        closes = crypto_bars.xs(symbol)['close']
        if len(closes) < CHAMPION_DNA['slow_ma']:
            continue

        fast_ma = float(closes.rolling(window=CHAMPION_DNA['fast_ma']).mean().iloc[-1])
        slow_ma = float(closes.rolling(window=CHAMPION_DNA['slow_ma']).mean().iloc[-1])
        price = float(closes.iloc[-1])
        trend_score = ((fast_ma - slow_ma) / slow_ma) * 100

        if pos_symbol in held_map:
            pos = held_map[pos_symbol]
            entry_price = float(pos.avg_entry_price)
            profit_pct = ((price - entry_price) / entry_price) * 100
            tp_target = entry_price * (1.0 + CHAMPION_DNA['take_profit_pct'])

            print(f"💼 [DEȚINUT] {symbol:<8} | Preț: {price:<9.3f}$ | Profit: {profit_pct:+.2f}% | Trend: {trend_score:+.2f}%")

            if price >= tp_target:
                print(f"   💰 [TAKE-PROFIT] {symbol} a atins +{CHAMPION_DNA['take_profit_pct']*100:.0f}%! Vând și încasez...")
                cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                time.sleep(1)
                trading_client.close_position(pos_symbol)
                open_positions_count -= 1
            elif fast_ma < slow_ma:
                print(f"   🔴 [SELL] Trendul pe {symbol} s-a inversat! Închid poziția...")
                cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                time.sleep(1)
                trading_client.close_position(pos_symbol)
                open_positions_count -= 1
        else:
            status = "🟢 SEMNAL BUY" if fast_ma > slow_ma else "⚪ În așteptare"
            print(f"👀 [SCANAT]  {symbol:<8} | Preț: {price:<9.3f}$ | FastMA: {fast_ma:<9.3f} | SlowMA: {slow_ma:<9.3f} | Scor Trend: {trend_score:+.2f}% -> {status}")
            if fast_ma > slow_ma:
                buy_candidates.append({
                    'symbol': symbol,
                    'pos_symbol': pos_symbol,
                    'price': price,
                    'score': trend_score,
                    'is_crypto': True
                })

    # 3. SCANARE ACȚIUNI SUA (Dacă bursa este deschisă)
    if is_stock_market_open:
        print("\n--- 🏛️ SCANARE ACȚIUNI SUA ---")
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=STOCK_SYMBOLS,
            timeframe=TimeFrame.Hour,
            start=start_time - timedelta(days=5),
            feed=DataFeed.IEX
        )).df

        for symbol in STOCK_SYMBOLS:
            if stock_bars.empty or symbol not in stock_bars.index.get_level_values(0):
                continue
            closes = stock_bars.xs(symbol)['close']
            if len(closes) < CHAMPION_DNA['slow_ma']:
                continue

            fast_ma = float(closes.rolling(window=CHAMPION_DNA['fast_ma']).mean().iloc[-1])
            slow_ma = float(closes.rolling(window=CHAMPION_DNA['slow_ma']).mean().iloc[-1])
            price = float(closes.iloc[-1])
            trend_score = ((fast_ma - slow_ma) / slow_ma) * 100

            if symbol in held_map:
                pos = held_map[symbol]
                entry_price = float(pos.avg_entry_price)
                profit_pct = ((price - entry_price) / entry_price) * 100
                print(f"💼 [DEȚINUT] {symbol:<8} | Preț: {price:<9.2f}$ | Profit: {profit_pct:+.2f}% | Trend: {trend_score:+.2f}%")
                if fast_ma < slow_ma:
                    print(f"   🔴 [SELL] Trend inversat pe acțiunea {symbol}! Vând poziția...")
                    cancel_orders_for_symbol(trading_client, symbol, symbol)
                    time.sleep(1)
                    trading_client.close_position(symbol)
                    open_positions_count -= 1
            else:
                status = "🟢 SEMNAL BUY" if fast_ma > slow_ma else "⚪ În așteptare"
                print(f"👀 [SCANAT]  {symbol:<8} | Preț: {price:<9.2f}$ | Scor Trend: {trend_score:+.2f}% -> {status}")
                if fast_ma > slow_ma:
                    buy_candidates.append({
                        'symbol': symbol,
                        'pos_symbol': symbol,
                        'price': price,
                        'score': trend_score,
                        'is_crypto': False
                    })

    # 4. ALOCAREA CAPITALULUI ÎN CELE MAI PUTERNICE PIEȚE
    slots_available = CHAMPION_DNA['max_positions'] - open_positions_count
    if slots_available > 0 and buy_candidates:
        # Sortăm oportunitățile descrescător după puterea trendului
        buy_candidates.sort(key=lambda x: x['score'], reverse=True)
        selected_targets = buy_candidates[:slots_available]

        print(f"\n🎯 Top oportunități selectate pentru cumpărare ({len(selected_targets)} locuri libere în portofoliu):")
        for target in selected_targets:
            account = trading_client.get_account()
            available_cash = float(account.cash)
            # Fiecare monedă primește ~19% din valoarea portofoliului (sau cash-ul rămas)
            invest_usd = min(available_cash * 0.95, current_equity * CHAMPION_DNA['allocation_per_asset'])

            if invest_usd < 50.0:
                print("⚠️ Cash insuficient pentru o nouă poziție.")
                break

            sym = target['symbol']
            pos_sym = target['pos_symbol']
            price = target['price']
            qty = get_precision_qty(price, invest_usd, target['is_crypto'])

            if qty <= 0:
                continue

            if target['is_crypto']:
                print(f"🟢 [CRYPTO BUY] Cumpăr {qty} {sym} (~{invest_usd:.2f}$) | Putere Trend: {target['score']:+.2f}%")
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                time.sleep(3)

                # Plantăm Stop-Loss-ul de -20% pe serverul Alpaca pentru această monedă
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
                    print(f"   🛡️ Stop-Loss plantat pe server pentru {sym} la {stop_price}$")
                except Exception as e:
                    print(f"   ⚠️ Nu s-a putut atașa Stop-Loss imediat pentru {sym}: {e}")

            else:
                # Pentru Acțiuni SUA folosim ordinul BRACKET nativ (Take-Profit + Stop-Loss incluse)
                stop_price = round(price * (1.0 - CHAMPION_DNA['stop_loss_pct']), 2)
                tp_price = round(price * (1.0 + CHAMPION_DNA['take_profit_pct']), 2)
                print(f"🟢 [STOCK BRACKET BUY] Cumpăr {qty} acțiuni {sym} (~{invest_usd:.2f}$) | TP: {tp_price}$ | SL: {stop_price}$")
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=tp_price),
                    stop_loss=StopLossRequest(stop_price=stop_price)
                ))
    else:
        print("\n✅ Nicio achiziție nouă necesară în acest ciclu (fie portofoliul are deja 5 poziții, fie piețele sunt în scădere).")


if __name__ == "__main__":
    run_multi_market_cycle()
