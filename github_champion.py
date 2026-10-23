#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADING CHAMPION V6  (Alpaca, paper trading implicit)

Rulare:
    python trading_bot_v6.py                 -> bot live (paper), CYCLES_PER_RUN cicluri
    python trading_bot_v6.py --backtest      -> backtest pe ultimele 30 de zile (cu comisioane)
    python trading_bot_v6.py --backtest --days 60

Ce s-a schimbat față de V5 (pe scurt):
  * Raport risc/câștig corect: stop pe bază de ATR + trailing stop (chandelier); câștigurile curg,
    nu le mai tăiem la +0.45% în timp ce pierderile ajung la -1.8%.
  * Comisioanele Alpaca crypto (0.25% taker / parte) sunt incluse în toate deciziile.
  * Bare de 15 minute (nu 1 minut) + filtru de trend (EMA50) + ignorăm bara în formare.
  * Dimensionare pe risc (0.5% din cont/tranzacție), limite de expunere, max 3 poziții crypto.
  * Fără "averaging down". Social media NU mai poate crea un trade singur, doar ajustează scorul.
  * Peak equity și kill-switch calculate din istoricul contului (nu se pierd între rulări GitHub).
  * Limită de pierdere zilnică, stop pe server pentru TOATE pozițiile (și acțiuni), stop care urcă.
  * Backtester integrat, folosește EXACT aceleași reguli ca botul live.
"""
import os
import re
import sys
import time
import json
import argparse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

# ==========================================
# 1. CONFIGURARE
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"   # implicit: PAPER (sigur)
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

MAX_DRAWDOWN_PCT = float(os.environ.get("MAX_DRAWDOWN_PCT", "0.10"))        # kill-switch la -10% de la vârf
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.03"))  # fără intrări noi la -3% pe zi
CYCLES_PER_RUN = int(os.environ.get("CYCLES_PER_RUN", "5"))
IGNORE_HISTORY_PEAK = os.environ.get("IGNORE_HISTORY_PEAK", "0") == "1"      # =1 -> "resetează" vârful după kill-switch
DEATH_FILE = "DEAD.lock"

# Costuri (Alpaca crypto, tier 1: 0.25% taker pe market/stop; verifică pagina de fees)
CRYPTO_FEE_PCT = float(os.environ.get("CRYPTO_FEE_PCT", "0.0025"))   # pe fiecare parte (cumpărare / vânzare)
CRYPTO_SLIPPAGE_PCT = 0.0005
STOCK_SLIPPAGE_PCT = 0.0003

CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "LINK/USD", "LTC/USD"]
STOCK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "SPY"]
SAFE_HAVEN_STOCKS = ["GLD", "TLT"]
SAFE_HAVEN_CRYPTO = ["PAXG/USD"]
ALL_CRYPTO = CRYPTO_SYMBOLS + SAFE_HAVEN_CRYPTO
ALL_STOCKS = STOCK_SYMBOLS + SAFE_HAVEN_STOCKS

FLATTEN_STOCKS_BEFORE_CLOSE = True     # acțiunile se închid cu 10 min înainte de bell (fără gap overnight)
FLATTEN_MIN = 10

DNA = {
    'bar_minutes': 15,
    'ema_fast': 9,
    'ema_slow': 21,
    'ema_trend': 50,
    'atr_period': 14,
    'entry_score_min': 65,

    'risk_per_trade': 0.005,        # riscăm 0.5% din cont pe tranzacție (dacă se lovește stop-ul)
    'max_positions': 4,
    'max_crypto_positions': 3,      # cripto-urile sunt corelate între ele
    'max_single_exposure': 0.20,
    'max_total_exposure': 0.60,     # minim 40% cash tot timpul

    'stop_atr_mult': 2.0,           # stop inițial = 2 x ATR
    'min_stop_pct': 0.015,
    'max_stop_pct': 0.050,
    'trail_atr_mult': 3.0,          # trailing: maximul recent - 3 x ATR
    'breakeven_trigger_r': 1.0,     # după +1R activăm trailing-ul (și stop-ul nu mai coboară sub breakeven + costuri)
    'trail_lookback_bars': 16,

    'soft_stop_frac_news': 0.4,     # când știrile sunt clar negative (social <= -5): ieșim la 40% din stop dacă trendul s-a rupt
    'use_exhaustion_exit': False,   # ieșirea pe "plafon de profit" - în teste tăia câștigurile; o poți activa și testa în backtest
    'min_net_profit': 0.003,        # profit net minim peste costuri (folosit doar dacă use_exhaustion_exit=True)
    'vol_cost_mult': 3.0,           # intrăm doar dacă 3 x ATR% >= costul round-trip
}

BULLISH_WORDS = ['surge', 'soar', 'bull', 'bullish', 'breakout', 'moon', 'rally', 'pump', 'ath',
                 'upgrade', 'record', 'adoption', 'approval', 'beats']
BEARISH_WORDS = ['crash', 'dump', 'bear', 'bearish', 'hack', 'hacked', 'ban', 'lawsuit', 'plunge',
                 'panic', 'liquidation', 'fraud', 'bankruptcy', 'sec', 'misses', 'downgrade']
BULL_RE = re.compile(r"\b(" + "|".join(BULLISH_WORDS) + r")\b")
BEAR_RE = re.compile(r"\b(" + "|".join(BEARISH_WORDS) + r")\b")
ALIASES = {
    'BTC': ['btc', 'bitcoin'], 'ETH': ['eth', 'ethereum'], 'SOL': ['solana', 'sol'],
    'DOGE': ['doge', 'dogecoin'], 'XRP': ['xrp', 'ripple'], 'AVAX': ['avax', 'avalanche'],
    'LINK': ['chainlink'], 'LTC': ['ltc', 'litecoin'], 'PAXG': ['paxg'],
    'NVDA': ['nvda', 'nvidia'], 'TSLA': ['tsla', 'tesla'], 'AAPL': ['aapl', 'apple'],
    'SPY': ['spy'], 'GLD': ['gld'], 'TLT': ['tlt'],
}


# ==========================================
# 2. UTILITARE
# ==========================================
def norm(sym):
    return sym.replace("/", "")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def bar_tf():
    return TimeFrame(DNA['bar_minutes'], TimeFrameUnit.Minute)


def round_price(p, is_crypto):
    if not is_crypto or p >= 10:
        return round(p, 2)
    if p >= 1:
        return round(p, 4)
    return round(p, 5)


def round_trip_cost(is_crypto):
    """Cost total cumpărare + vânzare, ca fracție din preț (comisioane + alunecare)."""
    if is_crypto:
        return 2.0 * (CRYPTO_FEE_PCT + CRYPTO_SLIPPAGE_PCT)
    return 2.0 * STOCK_SLIPPAGE_PCT


def stop_distance_pct(m):
    raw = DNA['stop_atr_mult'] * m['atr'] / m['price']
    return clamp(raw, DNA['min_stop_pct'], DNA['max_stop_pct'])


# ==========================================
# 3. RADAR SOCIAL MEDIA & ȘTIRI (cu potrivire pe cuvinte întregi)
# ==========================================
def fetch_json_url(url, headers=None, timeout=8):
    try:
        req_headers = {'User-Agent': 'script:trading-champion-bot:v6 (paper trading)'}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def sentiment_hits(text):
    return len(set(BULL_RE.findall(text))) - len(set(BEAR_RE.findall(text)))


def alias_regex(base):
    names = ALIASES.get(base, [base.lower()])
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")


def clean_news_symbol(s):
    s = s.upper().split("/")[0]
    if s.endswith("USD") and len(s) > 3:
        s = s[:-3]
    return s


def scan_social_media_and_sentiment():
    """Întoarce scoruri între -10 și +10. Rolul lor: să ajusteze un semnal tehnic, nu să-l creeze."""
    social_scores = {}
    trending = set()
    fear_greed_val = 50

    fng = fetch_json_url("https://api.alternative.me/fng/?limit=1")
    if fng and fng.get('data'):
        fear_greed_val = int(fng['data'][0].get('value', 50))

    cg = fetch_json_url("https://api.coingecko.com/api/v3/search/trending")
    if cg and 'coins' in cg:
        for item in cg['coins']:
            sym = item.get('item', {}).get('symbol', '').upper()
            if sym:
                trending.add(sym)

    reddit_posts = []
    for sub in ["CryptoCurrency", "wallstreetbets", "Bitcoin"]:
        r = fetch_json_url(f"https://www.reddit.com/r/{sub}/hot.json?limit=25")
        if r and 'data' in r:
            for post in r['data'].get('children', []):
                title = post.get('data', {}).get('title', '').lower()
                if title:
                    reddit_posts.append(title)

    news = []
    nd = fetch_json_url("https://data.alpaca.markets/v1beta1/news?limit=40",
                        headers={'APCA-API-KEY-ID': API_KEY or '', 'APCA-API-SECRET-KEY': SECRET_KEY or ''})
    if nd and 'news' in nd:
        for a in nd['news']:
            news.append((a.get('headline', '').lower(), {clean_news_symbol(s) for s in a.get('symbols', [])}))

    for asset in ALL_CRYPTO + ALL_STOCKS:
        base = asset.split("/")[0].upper()
        rx = alias_regex(base)
        score = 4 if base in trending else 0
        r_hits = sum(sentiment_hits(t) for t in reddit_posts if rx.search(t))
        n_hits = sum(sentiment_hits(h) for h, syms in news if base in syms or rx.search(h))
        score += clamp(r_hits * 2, -6, 6) + clamp(n_hits * 3, -6, 6)
        social_scores[asset] = int(clamp(score, -10, 10))

    print(f"🌐 [RADAR] Fear&Greed: {fear_greed_val}/100 | Trending: {', '.join(list(trending)[:6]) or '-'} "
          f"| Reddit: {len(reddit_posts)} postări | Știri: {len(news)}")
    return social_scores, fear_greed_val


# ==========================================
# 4. INDICATORI & SEMNALE (aceeași logică pentru live și backtest)
# ==========================================
def compute_indicators(df):
    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    o = df['open'].astype(float)
    v = df['volume'].astype(float)

    ind = pd.DataFrame(index=df.index)
    ind['close'], ind['high'], ind['low'], ind['open'] = c, h, l, o
    ind['ema_fast'] = c.ewm(span=DNA['ema_fast'], adjust=False).mean()
    ind['ema_slow'] = c.ewm(span=DNA['ema_slow'], adjust=False).mean()
    ind['ema_trend'] = c.ewm(span=DNA['ema_trend'], adjust=False).mean()

    # RSI (Wilder)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(loss != 0, np.where(gain > 0, 100.0, 50.0)).fillna(50.0)
    ind['rsi'] = rsi
    ind['rsi_falling'] = rsi < (rsi.shift(3) - 3.5)
    ind['rsi_min3'] = rsi.rolling(3).min()

    # MACD
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    ind['macd_hist'] = hist
    ind['macd_rising'] = hist >= hist.shift(1)
    ind['macd_losing'] = (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))

    # ATR
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    ind['atr'] = tr.ewm(alpha=1 / DNA['atr_period'], adjust=False).mean()

    # Bollinger
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    width = (4 * std20).replace(0, np.nan)
    ind['bb_pos'] = ((c - (sma20 - 2 * std20)) / width).fillna(0.5)

    # Volum relativ, breakout, wick, vârf recent
    avg_vol = v.rolling(20).mean()
    ind['rvol'] = (v / avg_vol.replace(0, np.nan)).fillna(1.0)
    ind['hh20'] = h.shift(1).rolling(20).max()
    ind['low3'] = l.rolling(3).min()
    rng = (h - l).clip(lower=1e-12)
    ind['wick'] = (h - pd.concat([c, o], axis=1).max(axis=1)) / rng
    ind['hi_trail'] = h.rolling(DNA['trail_lookback_bars']).max()
    return ind


def signal_at(ind, i, social_bonus=0):
    r = ind.iloc[i]
    price = float(r['close'])
    atr = float(r['atr'])
    if not np.isfinite(atr) or atr <= 0:
        atr = price * 0.004
    ema_f, ema_s, ema_t = float(r['ema_fast']), float(r['ema_slow']), float(r['ema_trend'])
    rsi, hist = float(r['rsi']), float(r['macd_hist'])
    macd_rising, rsi_falling = bool(r['macd_rising']), bool(r['rsi_falling'])
    bb, rvol, wick = float(r['bb_pos']), float(r['rvol']), float(r['wick'])
    hh20 = float(r['hh20'])
    hi_trail = float(r['hi_trail']) if np.isfinite(r['hi_trail']) else price

    trend_up = (ema_f > ema_s > ema_t) and price > ema_t

    # Detectorul de "plafon de profit" (epuizarea cumpărătorilor)
    ceiling, why = False, ""
    if bool(r['macd_losing']) and (rsi_falling or rvol < 0.85):
        ceiling, why = True, "MACD pierde viteză + RSI/volum în scădere"
    elif wick > 0.55 and bb > 0.75 and not macd_rising:
        ceiling, why = True, "Respingere din rezistență (wick)"
    elif bb >= 0.96 and rsi_falling:
        ceiling, why = True, "Bollinger superior + RSI în întoarcere"

    score, setup, reasons = 0, None, []
    if trend_up and hist > 0 and macd_rising and 50 <= rsi <= 72 and price > hh20 and rvol >= 1.2:
        score, setup = 75, 'BREAKOUT'
        reasons.append(f"Breakout+Volum({rvol:.1f}x)")
    elif trend_up and macd_rising and 40 <= rsi <= 60 and float(r['low3']) <= ema_s * 1.002 \
            and price > ema_f and bb < 0.85:
        score, setup = 68, 'PULLBACK'
        reasons.append("Pullback în trend")
    elif price > ema_t * 0.97 and float(r['rsi_min3']) < 32 and rsi > float(r['rsi_min3']) + 2 and macd_rising:
        score, setup = 66, 'REBOUND'
        reasons.append(f"Ricoșeu(RSI {rsi:.0f})")

    if setup and social_bonus:            # social media doar ajustează un semnal existent
        score += social_bonus
        reasons.append(f"Social({social_bonus:+d})")

    return {
        'price': price, 'atr': atr, 'atr_pct': atr / price,
        'ema_fast': ema_f, 'ema_slow': ema_s, 'ema_trend': ema_t,
        'rsi': rsi, 'macd_hist': hist, 'macd_rising': macd_rising,
        'trend_up': trend_up, 'hi_trail': hi_trail,
        'profit_ceiling_hit': ceiling, 'ceiling_reason': why,
        'score': score, 'setup': setup, 'social': int(social_bonus),
        'reason': " + ".join(reasons) if reasons else "Neutru",
    }


def entry_allowed(m, cost_rt):
    if not m['setup'] or m['score'] < DNA['entry_score_min'] or m['profit_ceiling_hit']:
        return False
    # volatilitatea trebuie să poată acoperi comisioanele
    return m['atr_pct'] * DNA['vol_cost_mult'] >= cost_rt


def exit_decision(profit, m, init_stop_pct, cost_rt):
    """Ieșiri "soft". Stop-ul real și trailing-ul stau pe server; câștigurile se lasă să curgă.
    În teste, ieșirile timpurii (plafon de profit / trend întors / stop la 60%) au înrăutățit rezultatul,
    de aceea sunt oprite implicit. Rămâne doar protecția la știri negative (ideea din V5)."""
    trend_broken = m['ema_fast'] < m['ema_slow'] and not m['macd_rising']

    if DNA['use_exhaustion_exit']:
        min_net = max(cost_rt + DNA['min_net_profit'], init_stop_pct)       # cel puțin +1R
        if profit >= min_net and m['profit_ceiling_hit']:
            return f"Plafon de profit: {m['ceiling_reason']}"
    if m['social'] <= -5 and trend_broken and profit <= -init_stop_pct * DNA['soft_stop_frac_news']:
        return "Stop controlat: trend rupt + știri negative"
    return None


# ==========================================
# 5. EXECUȚIE: ORDINE, STOP PE SERVER, TRAILING
# ==========================================
def get_open_orders(tc):
    try:
        return list(tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))
    except Exception as e:
        print(f"   ⚠️ Nu pot citi ordinele deschise: {e}")
        return []


def find_stop_order(open_orders, pos_symbol):
    for o in open_orders:
        if norm(o.symbol) == pos_symbol and o.side == OrderSide.SELL and o.stop_price is not None:
            return o
    return None


def place_stop(tc, symbol, qty, stop_price, is_crypto):
    stop_price = round_price(stop_price, is_crypto)
    if is_crypto:
        req = StopLimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=round_price(stop_price * 0.99, True))
    else:
        req = StopOrderRequest(
            symbol=symbol, qty=int(qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop_price)
    tc.submit_order(req)
    return stop_price


def ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, init_stop_pct, cost_rt):
    """Asigură un stop pe server și îl urcă (niciodată nu îl coboară). Starea = chiar ordinul de stop."""
    entry = float(pos.avg_entry_price)
    qty = float(pos.qty)
    price = m['price']
    profit = price / entry - 1.0

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    new_stop = cur_stop if cur_stop else entry * (1.0 - init_stop_pct)

    if profit >= init_stop_pct * DNA['breakeven_trigger_r']:
        trail = max(m['hi_trail'], price) - DNA['trail_atr_mult'] * m['atr']
        new_stop = max(new_stop, trail, entry * (1.0 + cost_rt))
    new_stop = min(new_stop, price * 0.998)          # stop-ul trebuie să fie sub preț
    if cur_stop is not None:
        new_stop = max(new_stop, cur_stop)
        if new_stop <= cur_stop * 1.002:             # fără modificări mărunte (evităm spam de ordine)
            return cur_stop
        tc.cancel_order_by_id(cur.id)
        time.sleep(0.8)
        try:
            placed = place_stop(tc, symbol, qty, new_stop, is_crypto)
            print(f"   🛡️ [TRAILING] {symbol}: stop {cur_stop:.4f} -> {placed}")
            return placed
        except Exception as e:
            print(f"   ⚠️ Stop nou respins ({e}); refac stop-ul vechi")
            place_stop(tc, symbol, qty, cur_stop, is_crypto)
            return cur_stop
    placed = place_stop(tc, symbol, qty, new_stop, is_crypto)
    print(f"   🛡️ [STOP SERVER] {symbol}: stop la {placed}")
    return placed


def close_position_safe(tc, symbol, pos_symbol):
    try:
        for o in get_open_orders(tc):
            if norm(o.symbol) == pos_symbol:
                tc.cancel_order_by_id(o.id)
        time.sleep(1.0)
        tc.close_position(pos_symbol)
        return True
    except Exception as e:
        print(f"   ⚠️ Nu am putut închide {symbol}: {e}")
        return False


def manage_position(tc, symbol, pos, m, is_crypto, flatten_now=False):
    """Întoarce True dacă poziția a fost închisă."""
    pos_symbol = norm(symbol)
    entry = float(pos.avg_entry_price)
    profit = m['price'] / entry - 1.0
    cost_rt = round_trip_cost(is_crypto)
    init_stop_pct = stop_distance_pct(m)

    print(f"💼 [DEȚINUT] {symbol:<8} | Preț: {m['price']:<10.4f} | P/L: {profit*100:+.2f}% "
          f"| ATR: {m['atr_pct']*100:.2f}% | Social: {m['social']:+d}")

    reason = "Închidere înainte de bell (fără risc overnight)" if flatten_now \
        else exit_decision(profit, m, init_stop_pct, cost_rt)
    if reason:
        print(f"   🔔 [IEȘIRE] {symbol}: {reason} ({profit*100:+.2f}%)")
        return close_position_safe(tc, symbol, pos_symbol)

    try:
        ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, init_stop_pct, cost_rt)
    except Exception as e:
        print(f"   ⚠️ Nu am putut actualiza stop-ul pentru {symbol}: {e}")
    return False


def position_size_usd(equity, cash, gross, init_stop_pct):
    by_risk = equity * DNA['risk_per_trade'] / init_stop_pct
    room = equity * DNA['max_total_exposure'] - gross
    return min(by_risk, equity * DNA['max_single_exposure'], cash * 0.95, room)


# ==========================================
# 6. CONT: VÂRF, KILL-SWITCH, SCORECARD
# ==========================================
def get_peak_equity(tc, equity):
    peak = max(STARTING_CAPITAL, equity)
    if IGNORE_HISTORY_PEAK:
        return peak
    try:
        hist = tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
        values = [float(e) for e in (hist.equity or []) if e is not None and float(e) > 0]
        if values:
            peak = max(peak, max(values))
    except Exception as e:
        print(f"   ⚠️ Istoricul contului indisponibil ({e}); folosesc vârful curent.")
    return peak


def evaluate_scorecard(tc, equity):
    try:
        closed = tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500))
    except Exception as e:
        print(f"   ⚠️ Scorecard indisponibil: {e}")
        return
    filled = [o for o in closed if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0
              and o.filled_at is not None]
    filled.sort(key=lambda o: o.filled_at)

    book, pnl_usd, pnl_pct = {}, [], []
    for o in filled:
        sym, qty, px = norm(o.symbol), float(o.filled_qty), float(o.filled_avg_price)
        b = book.setdefault(sym, [0.0, 0.0])           # [cantitate, cost total]
        if o.side == OrderSide.BUY:
            b[0] += qty
            b[1] += qty * px
        elif b[0] > 0:
            sell_qty = min(qty, b[0])
            avg = b[1] / b[0]
            pnl_usd.append((px - avg) * sell_qty)
            pnl_pct.append(px / avg - 1.0)
            b[0] -= sell_qty
            b[1] -= avg * sell_qty

    wins = [p for p in pnl_pct if p > 0]
    losses = [p for p in pnl_pct if p <= 0]
    total = len(pnl_pct)
    win_rate = len(wins) / total * 100 if total else 0.0
    gross_win = sum(p for p in pnl_usd if p > 0)
    gross_loss = abs(sum(p for p in pnl_usd if p <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf') if gross_win > 0 else 0.0
    avg_w = np.mean(wins) * 100 if wins else 0.0
    avg_l = abs(np.mean(losses)) * 100 if losses else 0.0
    breakeven_wr = avg_l / (avg_w + avg_l) * 100 if (avg_w + avg_l) > 0 else 0.0
    net = equity - STARTING_CAPITAL

    print("┌──────────────────────────────────────────────────────────────┐")
    print(f"│ 💰 Profit total: {net:+.2f}$ ({net / STARTING_CAPITAL * 100:+.2f}%)")
    print(f"│ 🎯 Tranzacții închise: {total} | Win rate: {win_rate:.1f}% | Profit factor: {pf:.2f}")
    print(f"│ 📊 Câștig mediu: +{avg_w:.2f}% | Pierdere medie: -{avg_l:.2f}% | Win rate de break-even: {breakeven_wr:.0f}%")
    print("└──────────────────────────────────────────────────────────────┘")


# ==========================================
# 7. DATE DE PIAȚĂ
# ==========================================
def prepare_symbol_frames(bars_df, symbols, now, stale_bars):
    """Din rezultatul unui request multi-simbol face {simbol: indicatori}. Ignoră bara încă în formare."""
    out = {}
    if bars_df is None or bars_df.empty:
        return out
    available = set(bars_df.index.get_level_values(0))
    bar_td = timedelta(minutes=DNA['bar_minutes'])
    for s in symbols:
        if s not in available:
            continue
        df = bars_df.xs(s)
        if len(df) and df.index[-1] + bar_td > now:      # ultima bară nu s-a încheiat încă
            df = df.iloc[:-1]
        if len(df) < 80:
            continue
        if now - df.index[-1] > bar_td * stale_bars:
            print(f"   ⏳ {s}: date vechi, sar peste.")
            continue
        out[s] = compute_indicators(df)
    return out


# ==========================================
# 8. CICLUL PRINCIPAL
# ==========================================
def process_symbol(tc, symbol, ind, is_crypto, is_safe, held, ctx):
    pos_symbol = norm(symbol)
    m = signal_at(ind, len(ind) - 1, ctx['social'].get(symbol, 0))
    if is_safe and ctx['risk_off'] and m['setup']:
        m['score'] += 15
        m['reason'] += " + 🛡️ Refugiu"

    if pos_symbol in held:
        flatten = (not is_crypto) and ctx['flatten_stocks']
        if manage_position(tc, symbol, held[pos_symbol], m, is_crypto, flatten_now=flatten):
            ctx['closed'].add(pos_symbol)
        return

    if ctx['entries_blocked'] or (not is_crypto and not ctx['stock_entries_ok']):
        return
    if ctx['risk_off'] and not is_safe:
        return
    if not entry_allowed(m, round_trip_cost(is_crypto)):
        return
    print(f"🟢 [SEMNAL {m['setup']}] {symbol:<8} | Scor: {m['score']} | {m['reason']}")
    ctx['candidates'].append({
        'symbol': symbol, 'pos_symbol': pos_symbol, 'price': m['price'], 'score': m['score'],
        'is_crypto': is_crypto, 'reason': m['reason'], 'init_stop_pct': stop_distance_pct(m)})


def open_new_positions(tc, ctx, held, equity):
    crypto_set = {norm(s) for s in ALL_CRYPTO}
    open_syms = [s for s in held if s not in ctx['closed']]
    n_open = len(open_syms)
    crypto_open = sum(1 for s in open_syms if s in crypto_set)
    gross = sum(abs(float(held[s].market_value)) for s in open_syms)

    for t in sorted(ctx['candidates'], key=lambda x: x['score'], reverse=True):
        if n_open >= DNA['max_positions']:
            break
        if t['is_crypto'] and crypto_open >= DNA['max_crypto_positions']:
            continue
        try:
            cash = float(tc.get_account().cash)
            invest = position_size_usd(equity, cash, gross, t['init_stop_pct'])
            if invest < 50.0:
                continue
            sym, pos_sym = t['symbol'], t['pos_symbol']
            print(f"🎯 [INTRARE] {sym}: ~{invest:.0f}$ | Stop inițial: -{t['init_stop_pct']*100:.2f}% "
                  f"| Scor {t['score']} ({t['reason']})")
            if t['is_crypto']:
                tc.submit_order(MarketOrderRequest(
                    symbol=sym, notional=round(invest, 2), side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
            else:
                qty = int(invest // t['price'])
                if qty < 1:
                    continue
                tc.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
            n_open += 1
            crypto_open += 1 if t['is_crypto'] else 0
            gross += invest
            time.sleep(2.5)
            try:      # stop imediat; dacă nu reușește acum, ratchet_stop îl pune la ciclul următor
                pos = tc.get_open_position(pos_sym)
                place_stop(tc, sym, float(pos.qty),
                           float(pos.avg_entry_price) * (1.0 - t['init_stop_pct']), t['is_crypto'])
            except Exception as e:
                print(f"   ⚠️ Stop-ul pentru {sym} va fi pus la ciclul următor ({e})")
        except Exception as e:
            print(f"   ⚠️ Intrare eșuată pe {t['symbol']}: {e}")


def run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val, show_scorecard=False):
    if os.path.exists(DEATH_FILE):
        print("💀 Agentul este MORT (DEAD.lock există). Oprire.")
        sys.exit(0)

    account = tc.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity) if getattr(account, 'last_equity', None) else equity
    peak = get_peak_equity(tc, equity)
    death_threshold = peak * (1.0 - MAX_DRAWDOWN_PCT)
    daily_pnl = (equity - last_equity) / last_equity if last_equity > 0 else 0.0
    entries_blocked = daily_pnl <= -DAILY_LOSS_LIMIT_PCT

    print(f"\n⚡ Scanare V6: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | Equity: {equity:.2f}$ "
          f"| Vârf: {peak:.2f}$ | Kill-switch: {death_threshold:.2f}$ | Azi: {daily_pnl*100:+.2f}%")
    if entries_blocked:
        print(f"🧯 Limita zilnică de pierdere ({DAILY_LOSS_LIMIT_PCT*100:.0f}%) atinsă: doar gestionăm pozițiile.")
    if show_scorecard:
        evaluate_scorecard(tc, equity)

    if equity <= death_threshold:
        print(f"🚨 [KILL-SWITCH] Scădere de {MAX_DRAWDOWN_PCT*100:.0f}% față de vârf. Închid tot.")
        tc.cancel_orders()
        time.sleep(1)
        tc.close_all_positions(cancel_orders=True)
        with open(DEATH_FILE, "w") as f:
            f.write(f"Killed at {datetime.now(timezone.utc)} with equity {equity:.2f}$ (peak {peak:.2f}$)\n")
        sys.exit(0)

    held = {norm(p.symbol): p for p in tc.get_all_positions()}
    now = datetime.now(timezone.utc)

    # Date crypto
    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=now - timedelta(days=4))).df
    crypto_ind = prepare_symbol_frames(crypto_bars, ALL_CRYPTO, now, stale_bars=5)

    # Regim de piață: BTC cade puternic sau frică extremă -> mod refugiu
    risk_off = False
    btc = crypto_ind.get("BTC/USD")
    if btc is not None and len(btc) > 3:
        lookback = max(1, int(30 / DNA['bar_minutes']))
        closes = btc['close']
        btc_change = float(closes.iloc[-1] / closes.iloc[-1 - lookback] - 1.0)
        if btc_change < -0.010 or fear_greed_val <= 24:
            risk_off = True
            print(f"🛡️ [MOD REFUGIU] BTC {btc_change*100:+.2f}% (30m), F&G {fear_greed_val}: doar active de refugiu.")

    ctx = {'social': social_scores, 'risk_off': risk_off, 'entries_blocked': entries_blocked,
           'closed': set(), 'candidates': [], 'stock_entries_ok': False, 'flatten_stocks': False}

    for symbol, ind in crypto_ind.items():
        process_symbol(tc, symbol, ind, True, symbol in SAFE_HAVEN_CRYPTO, held, ctx)

    # Date acțiuni (doar când piața e deschisă)
    clock = tc.get_clock()
    if clock.is_open:
        mtc = (clock.next_close - clock.timestamp).total_seconds() / 60.0   # minute până la închidere
        ctx['stock_entries_ok'] = 40 <= mtc <= 375
        ctx['flatten_stocks'] = FLATTEN_STOCKS_BEFORE_CLOSE and mtc <= FLATTEN_MIN
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=now - timedelta(days=10),
            feed=DataFeed.IEX)).df
        stock_ind = prepare_symbol_frames(stock_bars, ALL_STOCKS, now, stale_bars=6)
        for symbol, ind in stock_ind.items():
            process_symbol(tc, symbol, ind, False, symbol in SAFE_HAVEN_STOCKS, held, ctx)

    if ctx['candidates'] and not entries_blocked:
        open_new_positions(tc, ctx, held, equity)


# ==========================================
# 9. BACKTEST (aceleași reguli ca live, cu costuri)
# ==========================================
WARMUP_BARS = 200


def backtest_symbol(symbol, df, is_crypto, regime_off=None):
    ind = compute_indicators(df)
    n = len(ind)
    opens, highs, lows, closes = (ind[c].values for c in ('open', 'high', 'low', 'close'))
    times = ind.index
    cost_rt = round_trip_cost(is_crypto)
    trades, pos = [], None

    def close_trade(fill, i, why):
        gross = fill / pos['entry'] - 1.0
        net = gross - cost_rt
        frac = min(DNA['risk_per_trade'] / pos['stop_pct'], DNA['max_single_exposure'])
        trades.append({'symbol': symbol, 'exit_time': times[i], 'net': net, 'frac': frac,
                       'R': net / pos['stop_pct'], 'why': why})

    for i in range(WARMUP_BARS, n - 1):
        if pos is not None:
            # 1) stop lovit în bara curentă (la gap în jos, ieșim la open)
            if lows[i] <= pos['stop']:
                close_trade(min(pos['stop'], opens[i]), i, 'stop')
                pos = None
                continue
            pos['hh'] = max(pos['hh'], highs[i])
            # 2) la închiderea barei: trailing + reguli soft, execuție la open-ul barei următoare
            m = signal_at(ind, i, 0)
            profit = closes[i] / pos['entry'] - 1.0
            if profit >= pos['stop_pct'] * DNA['breakeven_trigger_r']:
                trail = max(m['hi_trail'], pos['hh']) - DNA['trail_atr_mult'] * m['atr']
                new_stop = max(pos['stop'], trail, pos['entry'] * (1.0 + cost_rt))
                pos['stop'] = min(new_stop, closes[i] * 0.998) if new_stop > pos['stop'] else pos['stop']
            why = exit_decision(profit, m, pos['stop_pct'], cost_rt)
            if why:
                close_trade(opens[i + 1], i + 1, why.split(':')[0])
                pos = None
            continue

        if regime_off is not None and regime_off[i]:
            continue
        m = signal_at(ind, i, 0)
        if entry_allowed(m, cost_rt):
            entry = opens[i + 1]
            sp = stop_distance_pct(m)
            pos = {'entry': entry, 'stop': entry * (1.0 - sp), 'stop_pct': sp, 'hh': entry}

    if pos is not None:
        close_trade(closes[-1], n - 1, 'final')
    return trades


def summarize_trades(trades, title):
    print(f"\n=== {title} ===")
    if not trades:
        print("   Nicio tranzacție.")
        return
    nets = np.array([t['net'] for t in trades])
    wins, losses = nets[nets > 0], nets[nets <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
    avg_w = wins.mean() * 100 if len(wins) else 0.0
    avg_l = abs(losses.mean()) * 100 if len(losses) else 0.0
    ordered = sorted(trades, key=lambda t: t['exit_time'])
    equity = np.cumprod([1.0 + t['frac'] * t['net'] for t in ordered])
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min()) * 100
    print(f"   Tranzacții: {len(trades)} | Win rate: {len(wins) / len(trades) * 100:.1f}% | Profit factor: {pf:.2f}")
    print(f"   Câștig mediu: +{avg_w:.2f}% | Pierdere medie: -{avg_l:.2f}% | Așteptare/tranzacție: {nets.mean()*100:+.3f}% "
          f"({np.mean([t['R'] for t in trades]):+.2f}R)")
    print(f"   Randament cont (aprox., cu dimensionare pe risc): {(equity[-1] - 1) * 100:+.2f}% | Max drawdown: {max_dd:.2f}%")


def run_backtest(days):
    print(f"📈 BACKTEST {days} zile | bare de {DNA['bar_minutes']} min | comision crypto {CRYPTO_FEE_PCT*100:.2f}%/parte "
          f"+ alunecare {CRYPTO_SLIPPAGE_PCT*100:.2f}%")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    cc = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    bars = cc.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=start)).df
    all_trades = []

    btc_regime = None
    if "BTC/USD" in bars.index.get_level_values(0):
        btc_ret = bars.xs("BTC/USD")['close'].pct_change(max(1, int(30 / DNA['bar_minutes'])))
        btc_regime = btc_ret

    for s in ALL_CRYPTO:
        if s not in bars.index.get_level_values(0):
            continue
        df = bars.xs(s)
        if len(df) <= WARMUP_BARS + 10:
            continue
        regime = None
        if btc_regime is not None:
            regime = (btc_regime.reindex(df.index, method='ffill').fillna(0.0) < -0.010).values
        t = backtest_symbol(s, df, True, regime)
        all_trades += t
        summarize_trades(t, s)

    if API_KEY and SECRET_KEY:
        sc = StockHistoricalDataClient(API_KEY, SECRET_KEY)
        sbars = sc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=start, feed=DataFeed.IEX)).df
        for s in ALL_STOCKS:
            if s not in sbars.index.get_level_values(0):
                continue
            df = sbars.xs(s)
            if len(df) <= WARMUP_BARS + 10:
                continue
            t = backtest_symbol(s, df, False, None)
            all_trades += t
            summarize_trades(t, s)

    summarize_trades(all_trades, "TOTAL")
    print("\nℹ️ Backtestul nu include sentimentul social și ignoră suprapunerea pozițiilor. "
          "Un profit factor sub ~1.2 sau sub 30 de tranzacții NU e o dovadă că strategia funcționează.")


# ==========================================
# 10. MAIN
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if args.backtest:
        run_backtest(args.days)
        return

    if not API_KEY or not SECRET_KEY:
        print("❌ EROARE: ALPACA_API_KEY / ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
        sys.exit(1)

    tc = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    crypto_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    print(f"🚀 AGENT V6 pornit ({'PAPER' if PAPER else '⚠️ LIVE'}) | bare {DNA['bar_minutes']}m | "
          f"cost round-trip crypto {round_trip_cost(True)*100:.2f}%")
    social_scores, fear_greed_val = scan_social_media_and_sentiment()

    for i in range(CYCLES_PER_RUN):
        print(f"\n================ CICLUL {i + 1}/{CYCLES_PER_RUN} ================")
        try:
            run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val, show_scorecard=(i == 0))
        except SystemExit:
            raise
        except Exception as e:
            print(f"⚠️ Eroare temporară în ciclul {i + 1}: {e}")
        if i < CYCLES_PER_RUN - 1:
            time.sleep(55)


if __name__ == "__main__":
    main()
