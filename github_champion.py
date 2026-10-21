import os
import sys
import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# ==========================================
# 1. CITIRE CREDENȚIALE DIN GITHUB SECRETS
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

if not API_KEY or not SECRET_KEY:
    print("❌ EROARE: Cheile ALPACA_API_KEY și ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
    sys.exit(1)

# ADN-ul Campionului
CHAMPION_DNA = {
    'fast_ma': 12,
    'slow_ma': 45,
    'risk_pct': 0.85,
    'take_profit_pct': 0.15
}

SYMBOL = "BTC/USD"
MAX_DRAWDOWN_PCT = 0.20
DEATH_FILE = "DEAD.lock"


# ==========================================
# 2. EXECUȚIA SINGLE-SHOT (TREZIRE LA 15 MIN)
# ==========================================
def run_single_cycle():
    if os.path.exists(DEATH_FILE):
        print("💀 Agentul este deja MORT (fișierul DEAD.lock există). Nu se execută nicio acțiune.")
        sys.exit(0)

    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    data_client = CryptoHistoricalDataClient()
    position_symbol = SYMBOL.replace("/", "")
    death_threshold = STARTING_CAPITAL * (1.0 - MAX_DRAWDOWN_PCT)

    # 1. VERIFICARE SUPRAVIEȚUIRE (KILL-SWITCH)
    account = trading_client.get_account()
    current_equity = float(account.equity)
    available_cash = float(account.cash)
    drawdown_pct = ((STARTING_CAPITAL - current_equity) / STARTING_CAPITAL) * 100

    print(f"⏰ Rulare GitHub Actions: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"💰 Equity curent: {current_equity:.2f}$ | Start: {STARTING_CAPITAL:.2f}$ | Prag Moarte: {death_threshold:.2f}$ ({drawdown_pct:+.2f}%)")

    if current_equity <= death_threshold:
        print("\n🚨 [KILL-SWITCH ACTIVAT] Pierderea a atins sau depășit 20%!")
        trading_client.cancel_orders()
        time.sleep(1)
        trading_client.close_all_positions(cancel_orders=True)
        
        with open(DEATH_FILE, "w") as f:
            f.write(f"Killed at {datetime.now(timezone.utc)} with equity {current_equity:.2f}$\n")
        print("💀 Toate pozițiile au fost lichidate. Certificatul DEAD.lock a fost creat.")
        sys.exit(0)

    # 2. PRELUARE DATE PIAȚĂ & CALCUL MEDII MOBILE
    lookback_bars = CHAMPION_DNA['slow_ma'] + 20
    request_params = CryptoBarsRequest(
        symbol_or_symbols=[SYMBOL],
        timeframe=TimeFrame.Hour,
        start=datetime.now(timezone.utc) - timedelta(hours=lookback_bars)
    )
    bars = data_client.get_crypto_bars(request_params).df
    if bars.empty or len(bars) < CHAMPION_DNA['slow_ma']:
        print("⚠️ Date insuficiente pentru calculul indicatorilor.")
        return

    closes = bars['close']
    fast_ma = closes.rolling(window=CHAMPION_DNA['fast_ma']).mean().iloc[-1]
    slow_ma = closes.rolling(window=CHAMPION_DNA['slow_ma']).mean().iloc[-1]
    price = float(closes.iloc[-1])

    try:
        pos = trading_client.get_open_position(position_symbol)
        held_qty = float(pos.qty)
        entry_price = float(pos.avg_entry_price)
    except Exception:
        held_qty, entry_price = 0.0, 0.0

    print(f"📊 Preț {SYMBOL}: {price:.2f}$ | Fast MA: {fast_ma:.2f} | Slow MA: {slow_ma:.2f} | Deținut: {held_qty:.6f}")

    # 3. LOGICA DE TRANZACȚIONARE
    if held_qty > 0.0:
        tp_target = entry_price * (1.0 + CHAMPION_DNA['take_profit_pct'])
        if price >= tp_target:
            print(f"💰 [TAKE-PROFIT] Țintă atinsă ({price:.2f}$ >= {tp_target:.2f}$)! Vând poziția...")
            trading_client.cancel_orders()
            time.sleep(1)
            trading_client.close_position(position_symbol)
        elif fast_ma < slow_ma:
            print("🔴 [SELL SIGNAL] Trend inversat! Anulez Stop-Loss și închid poziția...")
            trading_client.cancel_orders()
            time.sleep(1)
            trading_client.close_position(position_symbol)
        else:
            print("⏳ Poziție activă în trend. Stop-Loss-ul de pe server veghează.")

    elif fast_ma > slow_ma and held_qty == 0.0 and available_cash > 20.0:
        invest_usd = available_cash * CHAMPION_DNA['risk_pct']
        qty_to_buy = round(invest_usd / price, 5)
        if qty_to_buy > 0:
            print(f"🟢 [BUY SIGNAL] Cumpăr {qty_to_buy} {SYMBOL} (~{invest_usd:.2f}$)...")
            trading_client.submit_order(MarketOrderRequest(
                symbol=SYMBOL, qty=qty_to_buy, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
            ))
            time.sleep(3)

            account = trading_client.get_account()
            rem_cash = float(account.cash)
            pos = trading_client.get_open_position(position_symbol)
            actual_qty, actual_entry = float(pos.qty), float(pos.avg_entry_price)

            min_pos_val = death_threshold - rem_cash
            stop_price = round(actual_entry * 0.80, 2) if min_pos_val <= 0 else round(min_pos_val / actual_qty, 2)
            limit_price = round(stop_price * 0.99, 2)

            print(f"🛡️ [SERVER GUARD] Plantez Stop-Loss pe server la {stop_price}$...")
            trading_client.submit_order(StopLimitOrderRequest(
                symbol=SYMBOL, qty=actual_qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, stop_price=stop_price, limit_price=limit_price
            ))
            print("✅ Ciclu complet. Containerul se poate închide.")

if __name__ == "__main__":
    run_single_cycle()
