import os
import sys
import time
import json
import urllib.request
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

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
# 1. CONFIGURARE, PIEȚE & ACTIVE DE REFUGIU
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
SAFE_HAVEN_STOCKS = ["GLD", "TLT"]
SAFE_HAVEN_CRYPTO = ["PAXG/USD"]

# ADN V5 (DETECȚIE PLAFON PROFIT + CONTROL SCĂDERE + SOCIAL MEDIA)
CHAMPION_DNA = {
    'ema_fast': 9,
    'ema_slow': 21,
    'max_positions': 6,
    'allocation_per_asset': 0.16,
    'max_single_exposure': 0.24,
    'base_take_profit': 0.025,      # Țintă de bază +2.5% (lasă să curgă dacă trendul continuă)
    'min_exhaustion_profit': 0.0045,# De la +0.45% în sus, activează senzorul de "Plafon de Profit"
    'normal_cut_pct': -0.018,       # Scădere maximă permisă în condiții normale (-1.8%)
    'news_cut_pct': -0.009,         # Scădere mică permisă când știrile/social media sunt negative (-0.9%)
    'stop_loss_pct': 0.12           # Gardianul extrem de pe serverul Alpaca
}

MAX_DRAWDOWN_PCT = 0.20             # MOARTE DEFINITIVĂ la -20% pe întregul portofoliu
DEATH_FILE = "DEAD.lock"
CYCLES_PER_RUN = 5

BULLISH_WORDS = ['surge', 'soar', 'bull', 'bullish', 'breakout', 'buy', 'moon', 'rally', 'pump', 'ath', 'gain', 'upgrade', 'record', 'adoption']
BEARISH_WORDS = ['crash', 'dump', 'bear', 'bearish', 'hack', 'ban', 'lawsuit', 'drop', 'plunge', 'sell', 'fear', 'panic', 'liquidation', 'sec']


# ==========================================
# 2. RADARUL SOCIAL MEDIA & ȘTIRI
# ==========================================
def fetch_json_url(url, headers=None, timeout=5):
    try:
        req_headers = {'User-Agent': 'Mozilla/5.0 (compatible; TradingChampionBot/5.0)'}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def scan_social_media_and_sentiment():
    social_scores = {}
    trending_tickers = set()
    fear_greed_val = 50

    fng_data = fetch_json_url("https://api.alternative.me/fng/?limit=1")
    if fng_data and 'data' in fng_data and len(fng_data['data']) > 0:
        fear_greed_val = int(fng_data['data'][0].get('value', 50))

    cg_data = fetch_json_url("https://api.coingecko.com/api/v3/search/trending")
    if cg_data and 'coins' in cg_data:
        for item in cg_data['coins']:
            sym = item.get('item', {}).get('symbol', '').upper()
            if sym:
                trending_tickers.add(sym)

    reddit_posts = []
    for sub in ["CryptoCurrency", "wallstreetbets", "Bitcoin"]:
        r_data = fetch_json_url(f"https://www.reddit.com/r/{sub}/hot.json?limit=20")
        if r_data and 'data' in r_data and 'children' in r_data['data']:
            for post in r_data['data']['children']:
                title = post.get('data', {}).get('title', '').lower()
                if title:
                    reddit_posts.append(title)

    alpaca_headers = {'APCA-API-KEY-ID': API_KEY, 'APCA-API-SECRET-KEY': SECRET_KEY}
    news_data = fetch_json_url("https://data.alpaca.markets/v1beta1/news?limit=25", headers=alpaca_headers)
    news_headlines = []
    if news_data and 'news' in news_data:
        for article in news_data['news']:
            headline = article.get('headline', '').lower()
            syms = [s.upper() for s in article.get('symbols', [])]
            news_headlines.append((headline, syms))

    all_assets = CRYPTO_SYMBOLS + STOCK_SYMBOLS + SAFE_HAVEN_STOCKS + SAFE_HAVEN_CRYPTO
    for asset in all_assets:
        base_ticker = asset.split("/")[0].upper()
        score = 0
        if base_ticker in trending_tickers:
            score += 12
        for title in reddit_posts:
            if base_ticker.lower() in title.split():
                bull_hits = sum(1 for w in BULLISH_WORDS if w in title)
                bear_hits = sum(1 for w in BEARISH_WORDS if w in title)
                score += (bull_hits - bear_hits) * 4
        for headline, syms in news_headlines:
            if base_ticker in syms or base_ticker.lower() in headline:
                bull_hits = sum(1 for w in BULLISH_WORDS if w in headline)
                bear_hits = sum(1 for w in BEARISH_WORDS if w in headline)
                score += (bull_hits - bear_hits) * 5

        social_scores[asset] = max(-20, min(25, score))

    print(f"🌐 [RADAR SOCIAL] Fear & Greed: {fear_greed_val}/100 | Trending: {', '.join(list(trending_tickers)[:6]) or 'Standard'}")
    return social_scores, fear_greed_val


# ==========================================
# 3. ANALIZA TEHNICĂ & DETECTORUL DE EPUIZARE A PROFITULUI
# ==========================================
def analyze_market_structure(df, social_bonus=0):
    closes = df['close']
    highs = df['high']
    lows = df['low']
    opens = df['open']
    volumes = df['volume']
    price = float(closes.iloc[-1])

    ema_fast = float(closes.ewm(span=CHAMPION_DNA['ema_fast'], adjust=False).mean().iloc[-1])
    ema_slow = float(closes.ewm(span=CHAMPION_DNA['ema_slow'], adjust=False).mean().iloc[-1])

    # RSI & direcția RSI (pentru a vedea dacă impulsul cade din vârf)
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_series = (100 - (100 / (1 + rs))).fillna(50)
    rsi = float(rsi_series.iloc[-1])
    rsi_prev3 = float(rsi_series.iloc[-3]) if len(rsi_series) >= 3 else rsi
    rsi_falling = (rsi < rsi_prev3 - 3.5)

    # MACD & detectarea pierderii de viteză (2 lumânări în scădere)
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist_series = macd_line - signal_line
    macd_hist = float(hist_series.iloc[-1])
    macd_hist_prev = float(hist_series.iloc[-2])
    macd_hist_prev2 = float(hist_series.iloc[-3]) if len(hist_series) >= 3 else macd_hist_prev

    macd_rising = macd_hist >= macd_hist_prev
    macd_losing_steam = (macd_hist < macd_hist_prev) and (macd_hist_prev < macd_hist_prev2)

    # Benzile Bollinger
    sma20 = closes.rolling(window=20).mean()
    std20 = closes.rolling(window=20).std()
    bb_upper = float((sma20 + (std20 * 2)).iloc[-1])
    bb_lower = float((sma20 - (std20 * 2)).iloc[-1])
    bb_position = (price - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

    # Volum Relativ (RVOL)
    avg_vol = float(volumes.rolling(window=20).mean().iloc[-1])
    curr_vol = float(volumes.iloc[-1])
    rvol = (curr_vol / avg_vol) if avg_vol > 0 else 1.0

    # Vârful ultimelor 15 minute & Respingerea din rezistență (Upper Wick Rejection)
    recent_high_15m = float(highs.iloc[-15:].max())
    last_high = float(highs.iloc[-1])
    last_low = float(lows.iloc[-1])
    last_open = float(opens.iloc[-1])
    candle_range = max(0.000001, last_high - last_low)
    upper_wick_ratio = (last_high - max(price, last_open)) / candle_range
    wick_rejection = (upper_wick_ratio > 0.55 and bb_position > 0.75)

    # Detectăm dacă creșterea s-a plafonat (Profit Ceiling / Exhaustion)
    profit_ceiling_hit = False
    ceiling_reason = ""
    if macd_losing_steam and (rsi_falling or rvol < 0.85):
        profit_ceiling_hit = True
        ceiling_reason = "Impuls MACD + RSI în scădere (cumpărători epuizați)"
    elif wick_rejection and not macd_rising:
        profit_ceiling_hit = True
        ceiling_reason = "Respingere puternică din rezistență (Wick Rejection)"
    elif bb_position >= 0.96 and rsi_falling:
        profit_ceiling_hit = True
        ceiling_reason = "Plafon Bollinger Superior atins + RSI în întoarcere"

    # Calcul Scor de Intrare
    score = 0
    reasons = []
    if ema_fast > ema_slow and macd_hist > 0 and macd_rising:
        if 40 <= rsi <= 67 and bb_position < 0.85:
            score += 60
            reasons.append("Trend EMA+MACD")
            if rvol >= 1.1:
                score += 15
                reasons.append(f"Volum({rvol:.1f}x)")

    if (rsi < 34 or bb_position < 0.12) and macd_rising:
        score += 75
        reasons.append(f"Ricoșeu Bază(RSI {rsi:.0f})")

    if social_bonus != 0:
        score += social_bonus
        reasons.append(f"Social({social_bonus:+d})")

    return {
        'price': price,
        'ema_fast': ema_fast,
        'ema_slow': ema_slow,
        'rsi': rsi,
        'macd_hist': macd_hist,
        'macd_rising': macd_rising,
        'recent_high_15m': recent_high_15m,
        'profit_ceiling_hit': profit_ceiling_hit,
        'ceiling_reason': ceiling_reason,
        'score': score,
        'social': social_bonus,
        'reason': " + ".join(reasons) if reasons else "Neutru"
    }


def cancel_orders_for_symbol(trading_client, symbol, pos_symbol):
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    for order in open_orders:
        if order.symbol in [symbol, pos_symbol]:
            trading_client.cancel_order_by_id(order.id)


def ensure_server_stop_loss(trading_client, symbol, pos_symbol, qty, entry_price, is_crypto=True):
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    has_stop = any(o.symbol in [symbol, pos_symbol] and o.side == OrderSide.SELL for o in open_orders)
    if not has_stop and is_crypto and qty > 0:
        stop_price = round(entry_price * (1.0 - CHAMPION_DNA['stop_loss_pct']), 4 if entry_price < 10 else 2)
        limit_price = round(stop_price * 0.99, 4 if entry_price < 10 else 2)
        try:
            trading_client.submit_order(StopLimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, stop_price=stop_price, limit_price=limit_price
            ))
            print(f"   🛡️ [AUTO-GUARD] Stop-Loss atașat pe server pentru {symbol} la {stop_price}$")
        except Exception:
            pass


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


def evaluate_scorecard(trading_client, current_equity):
    closed_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200))
    filled = [o for o in closed_orders if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0]
    filled.sort(key=lambda x: x.filled_at)

    last_buy_price = {}
    wins, losses = 0, 0
    for o in filled:
        sym = o.symbol.replace("/", "")
        price = float(o.filled_avg_price)
        if o.side == OrderSide.BUY:
            last_buy_price[sym] = price
        elif o.side == OrderSide.SELL and sym in last_buy_price:
            if price > last_buy_price[sym]:
                wins += 1
            else:
                losses += 1
            del last_buy_price[sym]

    total = wins + losses
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    net_profit = current_equity - STARTING_CAPITAL
    profit_pct = (net_profit / STARTING_CAPITAL) * 100.0

    print("┌────────────────────────────────────────────────────────────────────────────┐")
    print(f"│ 🏆 OBIECTIV ABSOLVIRE: Profit {profit_pct:+.2f}% (Țintă >= +2.5%..+10%) | ({net_profit:+.2f}$) │")
    print(f"│ 🎯 RATA DE SUCCES (WIN RATE): {win_rate:.1f}% ({wins}W / {losses}L) | Țintă: >60%              │")
    print("└────────────────────────────────────────────────────────────────────────────┘")


# ==========================================
# 4. GESTIONAREA POZIȚIILOR: PLAFON PROFIT & SCĂDERE CONTROLATĂ
# ==========================================
def manage_existing_position(trading_client, symbol, pos_symbol, pos, metrics, current_equity, is_crypto=True):
    price = metrics['price']
    entry_price = float(pos.avg_entry_price)
    held_qty = float(pos.qty)
    market_val = float(pos.market_value)
    profit_pct = (price - entry_price) / entry_price

    # Calculăm care a fost profitul maxim atins în ultimele 15 minute
    peak_price_15m = max(price, metrics['recent_high_15m'])
    peak_profit_15m = (peak_price_15m - entry_price) / entry_price

    ensure_server_stop_loss(trading_client, symbol, pos_symbol, held_qty, entry_price, is_crypto)

    print(f"💼 [DEȚINUT] {symbol:<8} | Preț: {price:<9.3f}$ | P/L: {profit_pct*100:+.2f}% (Vârf 15m: {peak_profit_15m*100:+.2f}%) | Social: {metrics['social']:+d}")

    # 1. PESTE +2.5% PROFIT: Îl lăsăm să urce DOAR cât timp nu dă semne de plafonare!
    if profit_pct >= CHAMPION_DNA['base_take_profit']:
        if not metrics['profit_ceiling_hit'] and metrics['ema_fast'] > metrics['ema_slow'] and metrics['rsi'] < 75:
            print(f"   🚀 [SUPER-TREND] {symbol} e la {profit_pct*100:+.2f}% și încă are putere! Îl lăsăm să urce mai sus.")
            return 0
        else:
            print(f"   💰 [VÂRF DE PROFIT ÎNCASAT] {symbol} a atins {profit_pct*100:+.2f}% și s-a plafonat ({metrics['ceiling_reason'] or 'Trend încetinit'}). Vindem!")
            cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
            time.sleep(1)
            trading_client.close_position(pos_symbol)
            return -1

    # 2. DETECTORUL DE PLAFON ("PROFITUL NU POATE AJUNGE LA MAI MULT")
    if profit_pct >= CHAMPION_DNA['min_exhaustion_profit']:
        # A. Cumpărătorii s-au epuizat (MACD scade + RSI cade / Wick rejection)
        if metrics['profit_ceiling_hit']:
            print(f"   🎯 [PLAFON DETECTAT] Profitul pe {symbol} ({profit_pct*100:+.2f}%) nu mai poate urca ({metrics['ceiling_reason']}). Încasăm acum!")
            cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
            time.sleep(1)
            trading_client.close_position(pos_symbol)
            return -1

        # B. Poziția a urcat bine (ex: > +1.0%), dar a început să alunece înapoi cu 25% din câștigul maxim
        if peak_profit_15m >= 0.010 and profit_pct <= (peak_profit_15m * 0.75) and not metrics['macd_rising']:
            print(f"   🔒 [BLOCARE VÂRF] {symbol} a atins {peak_profit_15m*100:+.2f}% dar nu poate trece mai sus și scade spre {profit_pct*100:+.2f}%. Marcăm profitul!")
            cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
            time.sleep(1)
            trading_client.close_position(pos_symbol)
            return -1

    # 3. ÎMBUNĂTĂȚIREA POZIȚIEI EXISTENTE (Scale-In când prinde un ricoșeu puternic)
    max_allowed_val = current_equity * CHAMPION_DNA['max_single_exposure']
    if market_val < max_allowed_val * 0.75:
        if (-0.015 <= profit_pct <= -0.003 and metrics['score'] >= 75) or (profit_pct > 0.002 and metrics['score'] >= 82 and not metrics['profit_ceiling_hit']):
            account = trading_client.get_account()
            avail_cash = float(account.cash)
            boost_usd = min(avail_cash * 0.5, max_allowed_val - market_val)
            if boost_usd >= 100.0:
                add_qty = get_precision_qty(price, boost_usd, is_crypto)
                if add_qty > 0:
                    print(f"   ⚡ [BOOST POZIȚIE] Adaug {add_qty} {symbol} (~{boost_usd:.0f}$) pe semnal puternic!")
                    cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
                    time.sleep(1)
                    trading_client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=add_qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                    ))
                    time.sleep(2)
                    try:
                        updated_pos = trading_client.get_open_position(pos_symbol)
                        ensure_server_stop_loss(trading_client, symbol, pos_symbol, float(updated_pos.qty), float(updated_pos.avg_entry_price), is_crypto)
                    except Exception:
                        pass
                    return 0

    # 4. GESTIONAREA SCĂDERILOR (Nu vinde în panică, dar oprește scăderea înainte să devină mare)
    # Dacă știrile/social media sunt negative (< 0), permitem doar o scădere mică (-0.9%). Altfel, -1.8%.
    allowed_drop = CHAMPION_DNA['news_cut_pct'] if metrics['social'] < 0 else CHAMPION_DNA['normal_cut_pct']

    if profit_pct <= allowed_drop and metrics['ema_fast'] < metrics['ema_slow'] and not metrics['macd_rising']:
        reason_str = "Știri negative + confirmare scădere pe grafic" if metrics['social'] < 0 else "Trend descendent confirmat"
        print(f"   🔴 [STOP CONTROLAT] {symbol} a scăzut la {profit_pct*100:+.2f}% (Prag admis: {allowed_drop*100:.1f}% - {reason_str}). Ieșim înainte de o scădere mare!")
        cancel_orders_for_symbol(trading_client, symbol, pos_symbol)
        time.sleep(1)
        trading_client.close_position(pos_symbol)
        return -1

    return 0


# ==========================================
# 5. MOTORUL PRINCIPAL V5
# ==========================================
def run_v5_cycle(trading_client, crypto_client, stock_client, social_scores, fear_greed_val, show_scorecard=False):
    if os.path.exists(DEATH_FILE):
        print("💀 Agentul este MORT (DEAD.lock există). Oprire.")
        sys.exit(0)

    account = trading_client.get_account()
    current_equity = float(account.equity)
    available_cash = float(account.cash)

    peak_equity = max(STARTING_CAPITAL, current_equity)
    death_threshold = max(STARTING_CAPITAL * (1.0 - MAX_DRAWDOWN_PCT), peak_equity * (1.0 - MAX_DRAWDOWN_PCT))

    print(f"\n⚡ Scanare V5: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | Equity: {current_equity:.2f}$ | Cash: {available_cash:.2f}$ | Kill-Switch: {death_threshold:.2f}$")

    if show_scorecard:
        evaluate_scorecard(trading_client, current_equity)

    if current_equity <= death_threshold:
        print("🚨 [KILL-SWITCH ACTIVAT] Pragul critic de -20% a fost atins!")
        trading_client.cancel_orders()
        time.sleep(1)
        trading_client.close_all_positions(cancel_orders=True)
        with open(DEATH_FILE, "w") as f:
            f.write(f"Killed at {datetime.now(timezone.utc)} with equity {current_equity:.2f}$\n")
        sys.exit(0)

    all_positions = trading_client.get_all_positions()
    held_map = {p.symbol: p for p in all_positions}
    open_positions_count = len(held_map)

    start_time = datetime.now(timezone.utc) - timedelta(minutes=90)
    all_crypto_list = CRYPTO_SYMBOLS + SAFE_HAVEN_CRYPTO
    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=all_crypto_list,
        timeframe=TimeFrame.Minute,
        start=start_time
    )).df

    risk_off_mode = False
    if not crypto_bars.empty and "BTC/USD" in crypto_bars.index.get_level_values(0):
        btc_df = crypto_bars.xs("BTC/USD")
        if len(btc_df) >= 30:
            btc_change_30m = (float(btc_df['close'].iloc[-1]) - float(btc_df['close'].iloc[-30])) / float(btc_df['close'].iloc[-30])
            if btc_change_30m < -0.010 or fear_greed_val <= 24:
                risk_off_mode = True
                print(f"🛡️ [MODUL REFUGIU ACTIV] Piața scade (BTC 30m: {btc_change_30m*100:+.2f}%, F&G: {fear_greed_val}). Prioritizăm activele de refugiu!")

    buy_candidates = []

    # A. ANALIZA CRIPTOMONEDELOR & AURULUI CRYPTO (PAXG/USD)
    for symbol in all_crypto_list:
        pos_symbol = symbol.replace("/", "")
        is_safe_haven = symbol in SAFE_HAVEN_CRYPTO
        if crypto_bars.empty or symbol not in crypto_bars.index.get_level_values(0):
            continue

        df = crypto_bars.xs(symbol)
        if len(df) < 30:
            continue

        metrics = analyze_market_structure(df, social_scores.get(symbol, 0))
        if is_safe_haven and risk_off_mode:
            metrics['score'] += 30
            metrics['reason'] += " + 🛡️ Refugiu Aur"

        if pos_symbol in held_map:
            delta_pos = manage_existing_position(trading_client, symbol, pos_symbol, held_map[pos_symbol], metrics, current_equity, is_crypto=True)
            open_positions_count += delta_pos
        else:
            if metrics['profit_ceiling_hit']:
                continue
            if is_safe_haven and metrics['score'] >= 55:
                print(f"🛡️ [SEMNAL REFUGIU] {symbol:<8} | Scor: {metrics['score']} | {metrics['reason']}")
                buy_candidates.append({'symbol': symbol, 'pos_symbol': pos_symbol, 'price': metrics['price'], 'score': metrics['score'], 'is_crypto': True, 'reason': metrics['reason']})
            elif not risk_off_mode and metrics['score'] >= 65:
                print(f"🟢 [SEMNAL ATAC]    {symbol:<8} | Scor: {metrics['score']} | {metrics['reason']}")
                buy_candidates.append({'symbol': symbol, 'pos_symbol': pos_symbol, 'price': metrics['price'], 'score': metrics['score'], 'is_crypto': True, 'reason': metrics['reason']})

    # B. ANALIZA ACȚIUNILOR SUA & REFUGIILOR BURSIERE (GLD, TLT)
    clock = trading_client.get_clock()
    if clock.is_open:
        all_stocks_list = STOCK_SYMBOLS + SAFE_HAVEN_STOCKS
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=all_stocks_list,
            timeframe=TimeFrame.Minute,
            start=datetime.now(timezone.utc) - timedelta(minutes=120),
            feed=DataFeed.IEX
        )).df

        for symbol in all_stocks_list:
            is_safe_haven = symbol in SAFE_HAVEN_STOCKS
            if stock_bars.empty or symbol not in stock_bars.index.get_level_values(0):
                continue
            df = stock_bars.xs(symbol)
            if len(df) < 30:
                continue

            metrics = analyze_market_structure(df, social_scores.get(symbol, 0))
            if is_safe_haven and risk_off_mode:
                metrics['score'] += 35
                metrics['reason'] += " + 🛡️ Refugiu SUA"

            if symbol in held_map:
                delta_pos = manage_existing_position(trading_client, symbol, symbol, held_map[symbol], metrics, current_equity, is_crypto=False)
                open_positions_count += delta_pos
            else:
                if metrics['profit_ceiling_hit']:
                    continue
                if (is_safe_haven and metrics['score'] >= 55) or (not risk_off_mode and metrics['score'] >= 65):
                    buy_candidates.append({'symbol': symbol, 'pos_symbol': symbol, 'price': metrics['price'], 'score': metrics['score'] + 5, 'is_crypto': False, 'reason': metrics['reason']})

    # C. DESCHIDEREA POZIȚIILOR NOI
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

            print(f"🎯 [INTRARE V5] Cumpăr {qty} {sym} (~{invest_usd:.0f}$) | Scor: {target['score']} ({target['reason']})")
            if target['is_crypto']:
                trading_client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                ))
                time.sleep(2)
                try:
                    pos = trading_client.get_open_position(pos_sym)
                    ensure_server_stop_loss(trading_client, sym, pos_sym, float(pos.qty), float(pos.avg_entry_price), is_crypto=True)
                except Exception:
                    pass
            else:
                stop_price = round(price * (1.0 - CHAMPION_DNA['stop_loss_pct']), 2)
                tp_price = round(price * (1.0 + CHAMPION_DNA['base_take_profit'] * 1.5), 2)
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

    print("🚀 PORNIT AGENT V5: Detector Plafon Profit + Control Scădere + Social Radar")
    social_scores, fear_greed_val = scan_social_media_and_sentiment()

    for i in range(CYCLES_PER_RUN):
        print(f"\n================ CICLUL {i+1}/{CYCLES_PER_RUN} ================")
        try:
            run_v5_cycle(trading_client, crypto_client, stock_client, social_scores, fear_greed_val, show_scorecard=(i == 0))
        except Exception as e:
            print(f"⚠️ Eroare temporară în ciclul {i+1}: {e}")

        if i < CYCLES_PER_RUN - 1:
            time.sleep(55)
