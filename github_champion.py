#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADING CHAMPION V7  (Alpaca, paper trading implicit)

Ce aduce V7 față de V6.2:
  1. FIX CRITIC CANTITATE CRYPTO (ROUND_DOWN): trunchiază cantitatea în jos (ex: 4.778607741 -> 4.778607)
     pentru a elimina eroarea "insufficient balance" pe PAXG/USD și a planta imediat Stop-Loss-ul.
  2. PROFIT ÎN 2 TREPTE (Scale-Out 50% / 50%): la +1.2R profit net peste comisioane, vinde 50% din
     poziție (crește imediat Win Rate-ul) și lasă restul de 50% cu Stop pe profit (Runner).
  3. MULTI-TIMEFRAME (15m + 1H): agregă barele de 15m în bare de 1 Oră și verifică trendul major (1H).
  4. PUTERE RELATIVĂ VS. BITCOIN (Leader Filter): prioritizează monedele care bat performanța BTC pe 4h.
  5. MEMORIE EXACTĂ DE INTRARE: citește din ordinele Alpaca momentul exact al ultimului BUY și
     calculează vârful (Trailing Stop) strict de după cumpărare.
"""
import os
import re
import sys
import time
import json
import argparse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

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
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

MAX_DRAWDOWN_PCT = float(os.environ.get("MAX_DRAWDOWN_PCT", "0.10"))          # kill-switch la -10% de la vârf
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.03"))  # fără intrări noi la -3% pe zi
CYCLES_PER_RUN = int(os.environ.get("CYCLES_PER_RUN", "5"))
IGNORE_HISTORY_PEAK = os.environ.get("IGNORE_HISTORY_PEAK", "0") == "1"
DEATH_FILE = "DEAD.lock"

CRYPTO_FEE_PCT = float(os.environ.get("CRYPTO_FEE_PCT", "0.0025"))
CRYPTO_SLIPPAGE_PCT = 0.0005
STOCK_SLIPPAGE_PCT = 0.0003

CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "LINK/USD", "LTC/USD"]
STOCK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "SPY"]
SAFE_HAVEN_STOCKS = ["GLD", "TLT"]
SAFE_HAVEN_CRYPTO = ["PAXG/USD"]
ALL_CRYPTO = CRYPTO_SYMBOLS + SAFE_HAVEN_CRYPTO
ALL_STOCKS = STOCK_SYMBOLS + SAFE_HAVEN_STOCKS

FLATTEN_STOCKS_BEFORE_CLOSE = True
FLATTEN_MIN = 10
NY_TZ = ZoneInfo("America/New_York")

DNA = {
    'bar_minutes': 15,
    'ema_fast': 9,
    'ema_slow': 21,
    'ema_trend': 50,
    'atr_period': 14,
    'entry_score_min': 68,

    'risk_per_trade': 0.005,        # riscăm 0.5% din cont pe tranzacție
    'max_positions': 4,
    'max_crypto_positions': 3,
    'max_single_exposure': 0.20,
    'max_total_exposure': 0.65,

    'stop_atr_mult': 2.2,           # stop inițial = 2.2 x ATR
    'min_stop_pct': 0.015,          # minim 1.5%
    'max_stop_pct': 0.032,          # maxim 3.2%
    'legacy_cut_pct': -0.032,       # taie pozițiile vechi sub -3.2% dacă trendul e rupt
    'trail_atr_mult': 2.5,          # trailing Chandelier din maximul atins DUPĂ intrare
    'breakeven_trigger_r': 0.85,    # la +0.85R mutăm stop-ul pe breakeven + comisioane

    'use_partial_tp': True,         # Scale-Out 50% la primul prag de profit!
    'partial_tp_r': 1.2,            # vinde 50% la +1.2R câștig net peste comisioane
    'use_exhaustion_exit': True,
    'exhaustion_min_r': 1.6,        # pe restul de 50% (sau 100%) marchează vârful la epuizare >= +1.6R
    'soft_stop_frac_news': 0.4,
    'vol_cost_mult': 4.0,           # 4 x ATR% >= costul round-trip (0.60%)
}

BULLISH_WORDS = ['surge', 'soar', 'bull', 'bullish', 'breakout', 'moon', 'rally', 'pump', 'ath',
                 'upgrade', 'record', 'adoption', 'approval', 'beats', 'inflow', 'accumulate']
BEARISH_WORDS = ['crash', 'dump', 'bear', 'bearish', 'hack', 'hacked', 'ban', 'lawsuit', 'plunge',
                 'panic', 'liquidation', 'fraud', 'bankruptcy', 'sec', 'misses', 'downgrade', 'outflow']
BULL_RE = re.compile(r"\b(" + "|".join(BULLISH_WORDS) + r")\b")
BEAR_RE = re.compile(r"\b(" + "|".join(BEARISH_WORDS) + r")\b")
ALIASES = {
    'BTC': ['btc', 'bitcoin'], 'ETH': ['eth', 'ethereum'], 'SOL': ['solana', 'sol'],
    'DOGE': ['doge', 'dogecoin'], 'XRP': ['xrp', 'ripple'], 'AVAX': ['avax', 'avalanche'],
    'LINK': ['chainlink'], 'LTC': ['ltc', 'litecoin'], 'PAXG': ['paxg', 'gold'],
    'NVDA': ['nvda', 'nvidia'], 'TSLA': ['tsla', 'tesla'], 'AAPL': ['aapl', 'apple'],
    'SPY': ['spy'], 'GLD': ['gld'], 'TLT': ['tlt'],
}


# ==========================================
# 2. UTILITARE (CU FIX ROUND_DOWN PENTRU PAXG/CRYPTO)
# ==========================================
def norm(sym):
    return sym.replace("/", "")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def bar_tf():
    return TimeFrame(DNA['bar_minutes'], TimeFrameUnit.Minute)


def round_price(p, is_crypto):
    if not is_crypto or p >= 10:
        return round(float(p), 2)
    if p >= 1:
        return round(float(p), 4)
    return round(float(p), 5)


def truncate_qty(q, is_crypto):
    """
    FIX CRITIC V7: Nu rotunjește NICIODATĂ în sus cantitatea!
    Trunchiază strict în jos (ROUND_DOWN) la 6 zecimale pentru a evita eroarea
    'insufficient balance (requested: 4.778608, available: 4.778607741)'.
    """
    if not is_crypto:
        return int(Decimal(str(q)).to_integral_value(rounding=ROUND_DOWN))
    d = Decimal(str(q)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return float(d) if d > 0 else 0.0


def round_trip_cost(is_crypto):
    if is_crypto:
        return 2.0 * (CRYPTO_FEE_PCT + CRYPTO_SLIPPAGE_PCT)
    return 2.0 * STOCK_SLIPPAGE_PCT


def stop_distance_pct(m):
    raw = DNA['stop_atr_mult'] * m['atr'] / m['price']
    return clamp(raw, DNA['min_stop_pct'], DNA['max_stop_pct'])


def filter_regular_stock_hours(df):
    if df.empty:
        return df
    ny_times = df.index.tz_convert(NY_TZ)
    mask = (ny_times.weekday < 5) & (ny_times.time >= dtime(9, 30)) & (ny_times.time < dtime(16, 0))
    return df.loc[mask]


# ==========================================
# 3. RADAR SOCIAL MEDIA & ȘTIRI
# ==========================================
def fetch_url_raw(url, headers=None, timeout=8):
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36'
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='ignore')
    except Exception:
        return None


def fetch_json_url(url, headers=None, timeout=8):
    raw = fetch_url_raw(url, headers=headers, timeout=timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def fetch_reddit_rss_titles(sub):
    raw = fetch_url_raw(f"https://www.reddit.com/r/{sub}/hot.rss?limit=20")
    titles = []
    if not raw:
        return titles
    try:
        root = ET.fromstring(raw)
        for elem in root.iter():
            if elem.tag.endswith('title') and elem.text:
                t = elem.text.strip().lower()
                if len(t) > 12:
                    titles.append(t)
    except Exception:
        pass
    return titles


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

    social_posts = []
    for sub in ["CryptoCurrency", "Bitcoin", "wallstreetbets"]:
        social_posts.extend(fetch_reddit_rss_titles(sub))

    cc_news = fetch_json_url("https://min-api.cryptocompare.com/data/v2/news/?lang=EN")
    if cc_news and isinstance(cc_news.get('Data'), list):
        for item in cc_news['Data'][:35]:
            title = item.get('title', '').lower()
            if title:
                social_posts.append(title)

    news = []
    nd = fetch_json_url("https://data.alpaca.markets/v1beta1/news?limit=40",
                        headers={'APCA-API-KEY-ID': API_KEY or '', 'APCA-API-SECRET-KEY': SECRET_KEY or ''})
    if nd and 'news' in nd:
        for a in nd['news']:
            news.append((a.get('headline', '').lower(), {clean_news_symbol(s) for s in a.get('symbols', [])}))

    for asset in ALL_CRYPTO + ALL_STOCKS:
        base = asset.split("/")[0].upper()
        rx = alias_regex(base)
        score = 3 if base in trending else 0
        s_hits = sum(sentiment_hits(t) for t in social_posts if rx.search(t))
        n_hits = sum(sentiment_hits(h) for h, syms in news if base in syms or rx.search(h))
        score += clamp(s_hits * 2, -6, 6) + clamp(n_hits * 3, -6, 6)
        social_scores[asset] = int(clamp(score, -10, 10))

    print(f"🌐 [RADAR] Fear&Greed: {fear_greed_val}/100 | Trending: {', '.join(list(trending)[:6]) or '-'} "
          f"| Flux Social: {len(social_posts)} postări | Știri Alpaca: {len(news)}")
    return social_scores, fear_greed_val


# ==========================================
# 4. INDICATORI (15M + 1H MULTI-TIMEFRAME + PUTERE RELATIVĂ)
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

    # Filtru Multi-Timeframe 1H (echivalentul EMA21 și EMA50 pe 1 Oră = 84 și 200 bare de 15m)
    ind['ema_1h_fast'] = c.ewm(span=84, adjust=False).mean()
    ind['ema_1h_slow'] = c.ewm(span=200, adjust=False).mean()
    ind['ret_4h'] = c.pct_change(16).fillna(0.0)

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(loss != 0, np.where(gain > 0, 100.0, 50.0)).fillna(50.0)
    ind['rsi'] = rsi
    ind['rsi_falling'] = rsi < (rsi.shift(3) - 3.5)
    ind['rsi_min3'] = rsi.rolling(3).min()

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    ind['macd_hist'] = hist
    ind['macd_rising'] = hist >= hist.shift(1)
    ind['macd_losing'] = (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    ind['atr'] = tr.ewm(alpha=1 / DNA['atr_period'], adjust=False).mean()

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    width = (4 * std20).replace(0, np.nan)
    ind['bb_pos'] = ((c - (sma20 - 2 * std20)) / width).fillna(0.5)

    avg_vol = v.rolling(20).mean()
    ind['rvol'] = (v / avg_vol.replace(0, np.nan)).fillna(1.0)
    ind['hh20'] = h.shift(1).rolling(20).max()
    ind['low3'] = l.rolling(3).min()
    rng = (h - l).clip(lower=1e-12)
    ind['wick'] = (h - pd.concat([c, o], axis=1).max(axis=1)) / rng
    return ind


def signal_at(ind, i, social_bonus=0, live_price=None, live_high=None, btc_ret_4h=None, entry_time=None):
    r = ind.iloc[i]
    bar_close = float(r['close'])
    bar_open = float(r['open'])
    price = float(live_price) if live_price is not None else bar_close
    atr = float(r['atr'])
    if not np.isfinite(atr) or atr <= 0:
        atr = price * 0.004
    ema_f, ema_s, ema_t = float(r['ema_fast']), float(r['ema_slow']), float(r['ema_trend'])
    ema_1h_f, ema_1h_s = float(r['ema_1h_fast']), float(r['ema_1h_slow'])
    ret_4h = float(r['ret_4h'])
    rsi, hist = float(r['rsi']), float(r['macd_hist'])
    macd_rising, rsi_falling = bool(r['macd_rising']), bool(r['rsi_falling'])
    bb, rvol, wick = float(r['bb_pos']), float(r['rvol']), float(r['wick'])
    hh20 = float(r['hh20'])

    trend_up = (ema_f > ema_s > ema_t) and bar_close > ema_t
    # Filtru 1H: trendul pe 1 Oră susține creșterea (nu suntem în prăbușire majoră pe 1H)
    htf_1h_ok = (bar_close >= ema_1h_f * 0.995) and (ema_1h_f >= ema_1h_s * 0.992)
    bullish_candle = bar_close > bar_open

    ceiling, why = False, ""
    if bool(r['macd_losing']) and (rsi_falling or rvol < 0.85):
        ceiling, why = True, "MACD pierde viteză + RSI/volum în scădere"
    elif wick > 0.55 and bb > 0.75 and not macd_rising:
        ceiling, why = True, "Respingere din rezistență (wick)"
    elif bb >= 0.96 and rsi_falling:
        ceiling, why = True, "Bollinger superior + RSI în întoarcere"

    score, setup, reasons = 0, None, []
    if trend_up and htf_1h_ok and hist > 0 and macd_rising and 52 <= rsi <= 72 and bar_close > hh20 and rvol >= 1.25:
        score, setup = 75, 'BREAKOUT'
        reasons.append(f"Breakout 15m+1H Vol({rvol:.1f}x)")
    elif trend_up and htf_1h_ok and bullish_candle and macd_rising and 42 <= rsi <= 60 \
            and float(r['low3']) <= ema_s * 1.002 and bar_close > ema_f and bb < 0.82:
        score, setup = 70, 'PULLBACK'
        reasons.append("Pullback confirmat 15m+1H")
    elif bullish_candle and htf_1h_ok and bar_close > ema_t * 0.99 and bar_close > ema_f \
            and float(r['rsi_min3']) < 30 and rsi > float(r['rsi_min3']) + 4 and macd_rising and rvol >= 1.1:
        score, setup = 68, 'REBOUND'
        reasons.append(f"Ricoșeu(RSI {rsi:.0f}, Vol {rvol:.1f}x)")

    # Putere relativă față de Bitcoin pe ultimele 4 ore (Leader Filter)
    if setup and btc_ret_4h is not None:
        rel_strength = ret_4h - btc_ret_4h
        if rel_strength >= 0.005:
            score += 4
            reasons.append(f"Lider vs BTC({rel_strength*100:+.1f}%)")
        elif rel_strength <= -0.012:
            score -= 6
            reasons.append(f"Slab vs BTC({rel_strength*100:+.1f}%)")

    if setup and social_bonus:
        score += social_bonus
        reasons.append(f"Social({social_bonus:+d})")

    # Calculăm maximul atins STRICT de la deschiderea poziției (dacă avem entry_time din Alpaca)
    if entry_time is not None:
        sub_highs = ind['high'].loc[ind.index >= entry_time - timedelta(minutes=DNA['bar_minutes'])]
        peak_since_entry = float(sub_highs.max()) if not sub_highs.empty else price
    else:
        peak_since_entry = float(ind['high'].iloc[max(0, i - 1):i + 1].max())
    if live_high is not None:
        peak_since_entry = max(peak_since_entry, float(live_high))

    return {
        'price': price, 'bar_close': bar_close, 'atr': atr, 'atr_pct': atr / price,
        'ema_fast': ema_f, 'ema_slow': ema_s, 'ema_trend': ema_t,
        'rsi': rsi, 'macd_hist': hist, 'macd_rising': macd_rising,
        'trend_up': trend_up, 'ret_4h': ret_4h,
        'peak_since_entry': max(price, peak_since_entry),
        'profit_ceiling_hit': ceiling, 'ceiling_reason': why,
        'score': score, 'setup': setup, 'social': int(social_bonus),
        'reason': " + ".join(reasons) if reasons else "Neutru",
    }


def entry_allowed(m, cost_rt):
    if not m['setup'] or m['score'] < DNA['entry_score_min'] or m['profit_ceiling_hit']:
        return False
    return m['atr_pct'] * DNA['vol_cost_mult'] >= cost_rt


def exit_decision(profit, m, init_stop_pct, cost_rt):
    trend_broken = m['ema_fast'] < m['ema_slow'] and not m['macd_rising']

    if profit <= DNA['legacy_cut_pct'] and trend_broken:
        return f"Tăiere poziție veche sub {DNA['legacy_cut_pct']*100:.1f}% (trend rupt)"

    if DNA['use_exhaustion_exit']:
        min_net = cost_rt + (init_stop_pct * DNA['exhaustion_min_r'])
        if profit >= min_net and m['profit_ceiling_hit']:
            return f"Plafon de profit la >{DNA['exhaustion_min_r']}R: {m['ceiling_reason']}"

    if m['social'] <= -5 and trend_broken and profit <= -init_stop_pct * DNA['soft_stop_frac_news']:
        return "Stop controlat: trend rupt + știri negative"
    return None


# ==========================================
# 5. EXECUȚIE: MEMORIE ORDINE, SCALE-OUT 50/50 & STOP SERVER
# ==========================================
def get_open_orders(tc):
    try:
        return list(tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))
    except Exception as e:
        print(f"   ⚠️ Nu pot citi ordinele deschise: {e}")
        return []


def build_position_memory(tc):
    """
    Citește ultimele ordine executate pentru a ști pentru fiecare poziție deschisă:
    1. Ora exactă a ultimului BUY (entry_time)
    2. Dacă am încasat deja prima jumătate de profit (scaled_out = True/False)
    """
    mem = {}
    try:
        closed = tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=250))
        filled = [o for o in closed if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0
                  and o.filled_at is not None]
        filled.sort(key=lambda o: o.filled_at)
        for o in filled:
            sym = norm(o.symbol)
            if o.side == OrderSide.BUY:
                mem[sym] = {'entry_time': o.filled_at, 'scaled_out': False}
            elif o.side == OrderSide.SELL and sym in mem:
                mem[sym]['scaled_out'] = True
    except Exception:
        pass
    return mem


def find_stop_order(open_orders, pos_symbol):
    for o in open_orders:
        if norm(o.symbol) == pos_symbol and o.side == OrderSide.SELL and o.stop_price is not None:
            return o
    return None


def place_stop(tc, symbol, qty, stop_price, is_crypto):
    stop_price = round_price(stop_price, is_crypto)
    clean_qty = truncate_qty(qty, is_crypto)
    if clean_qty <= 0:
        return None
    if is_crypto:
        req = StopLimitOrderRequest(
            symbol=symbol, qty=clean_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=round_price(stop_price * 0.99, True))
    else:
        req = StopOrderRequest(
            symbol=symbol, qty=clean_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop_price)
    tc.submit_order(req)
    return stop_price


def effective_init_stop_pct(entry, cur_stop, fallback_stop_pct):
    if cur_stop is not None and 0 < cur_stop < entry:
        return clamp((entry - cur_stop) / entry, DNA['min_stop_pct'], DNA['max_stop_pct'])
    return fallback_stop_pct


def execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, new_stop_price):
    """Vinde 50% din poziție la Market și plantează Stop-Loss pe profit pentru restul de 50%."""
    try:
        for o in get_open_orders(tc):
            if norm(o.symbol) == pos_symbol:
                tc.cancel_order_by_id(o.id)
        time.sleep(1.5)
        fresh_pos = tc.get_open_position(pos_symbol)
        total_qty = float(fresh_pos.qty)
        sell_qty = truncate_qty(total_qty * 0.5, is_crypto)
        if sell_qty <= 0 or (not is_crypto and total_qty < 2):
            place_stop(tc, symbol, total_qty, new_stop_price, is_crypto)
            return False

        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        tc.submit_order(MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=tif))
        print(f"   💰 [PROFIT TREAPTA 1 - 50%] Vândut 50% ({sell_qty} {symbol})! Restul de 50% rămâne cu Stop pe plus.")
        time.sleep(2.5)
        rem_pos = tc.get_open_position(pos_symbol)
        place_stop(tc, symbol, float(rem_pos.qty), new_stop_price, is_crypto)
        return True
    except Exception as e:
        print(f"   ⚠️ Scale-out parțial amânat pe {symbol}: {e}")
        return False


def ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, cost_rt, pos_mem):
    entry = float(pos.avg_entry_price)
    qty = float(pos.qty)
    live_price = float(pos.current_price) if getattr(pos, 'current_price', None) else m['price']
    profit = live_price / entry - 1.0

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m))

    # 1. Verificăm dacă am atins Treapta 1 de Profit (+1.2R net) pentru a vinde 50% (Scale-Out)
    scaled_out = pos_mem.get('scaled_out', False)
    partial_target = cost_rt + init_stop_pct * DNA['partial_tp_r']
    if DNA['use_partial_tp'] and not scaled_out and profit >= partial_target:
        lock_stop = min(live_price * 0.993, max(entry * (1.0 + cost_rt + 0.002),
                                                m['peak_since_entry'] - DNA['trail_atr_mult'] * m['atr']))
        if execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, lock_stop):
            pos_mem['scaled_out'] = True
            return lock_stop

    # 2. Calculăm Stop-Loss-ul normal sau Trailing Stop-ul
    max_allowed_floor = min(entry * (1.0 - init_stop_pct), live_price * (1.0 - init_stop_pct))
    new_stop = max(cur_stop, max_allowed_floor) if cur_stop else max_allowed_floor

    already_in_profit_stop = (cur_stop is not None and cur_stop >= entry) or scaled_out
    if profit >= init_stop_pct * DNA['breakeven_trigger_r'] or already_in_profit_stop:
        peak_val = max(live_price, m['peak_since_entry'])
        trail = peak_val - DNA['trail_atr_mult'] * m['atr']
        new_stop = max(new_stop, trail, entry * (1.0 + cost_rt))

    new_stop = min(new_stop, live_price * 0.994)

    if cur_stop is not None:
        new_stop = max(new_stop, cur_stop)
        if new_stop <= cur_stop * 1.003:
            return cur_stop
        tc.cancel_order_by_id(cur.id)
        time.sleep(1.5)
        try:
            fresh_pos = tc.get_open_position(pos_symbol)
            placed = place_stop(tc, symbol, float(fresh_pos.qty), new_stop, is_crypto)
            print(f"   🛡️ [STOP ACTUALIZAT] {symbol}: stop urcat de la {cur_stop:.4f} -> {placed}")
            return placed
        except Exception as e:
            print(f"   ⚠️ Stop nou respins ({e}); refac stop-ul anterior")
            fresh_pos = tc.get_open_position(pos_symbol)
            place_stop(tc, symbol, float(fresh_pos.qty), min(cur_stop, live_price * 0.992), is_crypto)
            return cur_stop

    placed = place_stop(tc, symbol, qty, new_stop, is_crypto)
    print(f"   🛡️ [STOP SERVER] {symbol}: stop plantat la {placed}")
    return placed


def close_position_safe(tc, symbol, pos_symbol):
    try:
        for o in get_open_orders(tc):
            if norm(o.symbol) == pos_symbol:
                tc.cancel_order_by_id(o.id)
        time.sleep(1.2)
        tc.close_position(pos_symbol)
        return True
    except Exception as e:
        print(f"   ⚠️ Nu am putut închide {symbol}: {e}")
        return False


def manage_position(tc, symbol, pos, m, is_crypto, pos_mem, flatten_now=False):
    pos_symbol = norm(symbol)
    entry = float(pos.avg_entry_price)
    live_price = float(pos.current_price) if getattr(pos, 'current_price', None) else m['price']
    m['price'] = live_price
    profit = live_price / entry - 1.0
    cost_rt = round_trip_cost(is_crypto)

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m))
    stage_tag = "50% Runner" if pos_mem.get('scaled_out') else "100%"

    print(f"💼 [DEȚINUT {stage_tag}] {symbol:<8} | Live: {live_price:<10.4f} | P/L: {profit*100:+.2f}% "
          f"| Stop: {cur_stop or 0:<9.4f} | ATR: {m['atr_pct']*100:.2f}% | Social: {m['social']:+d}")

    reason = "Închidere înainte de bell (fără risc overnight)" if flatten_now \
        else exit_decision(profit, m, init_stop_pct, cost_rt)
    if reason:
        print(f"   🔔 [IEȘIRE] {symbol}: {reason} ({profit*100:+.2f}%)")
        return close_position_safe(tc, symbol, pos_symbol)

    try:
        ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, cost_rt, pos_mem)
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
        hist = tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
        values = [float(e) for e in (hist.equity or []) if e is not None and float(e) > 0]
        if values:
            peak = max(peak, max(values))
    except Exception:
        pass
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
        b = book.setdefault(sym, [0.0, 0.0])
        if o.side == OrderSide.BUY:
            b[0] += qty
            b[1] += qty * px
        elif b[0] > 1e-7:
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
    print(f"│ 💰 Profit total: {net:+,.2f}$ ({net / STARTING_CAPITAL * 100:+.2f}%)")
    print(f"│ 🎯 Tranzacții închise: {total} | Win rate: {win_rate:.1f}% | Profit factor: {pf:.2f}")
    print(f"│ 📊 Câștig mediu: +{avg_w:.2f}% | Pierdere medie: -{avg_l:.2f}% | Win rate de break-even: {breakeven_wr:.0f}%")
    print("└──────────────────────────────────────────────────────────────┘")


# ==========================================
# 7. DATE DE PIAȚĂ
# ==========================================
def prepare_symbol_frames(bars_df, symbols, now, stale_bars, is_stock=False):
    out = {}
    if bars_df is None or bars_df.empty:
        return out
    available = set(bars_df.index.get_level_values(0))
    bar_td = timedelta(minutes=DNA['bar_minutes'])
    for s in symbols:
        if s not in available:
            continue
        df = bars_df.xs(s)
        if is_stock:
            df = filter_regular_stock_hours(df)
        if df.empty:
            continue
        live_price = float(df['close'].iloc[-1])
        live_high = float(df['high'].iloc[-1])
        if len(df) and df.index[-1] + bar_td > now:
            df = df.iloc[:-1]
        if len(df) < 80:
            continue
        if not is_stock and (now - df.index[-1] > bar_td * stale_bars):
            print(f"   ⏳ {s}: date vechi, sar peste.")
            continue
        out[s] = (compute_indicators(df), live_price, live_high)
    return out


# ==========================================
# 8. CICLUL PRINCIPAL V7
# ==========================================
def process_symbol(tc, symbol, pack, is_crypto, is_safe, held, ctx, allow_new_signals=True):
    ind, live_price, live_high = pack
    pos_symbol = norm(symbol)
    pos_mem = ctx['pos_memory'].setdefault(pos_symbol, {'entry_time': None, 'scaled_out': False})
    btc_ret = ctx.get('btc_ret_4h') if (is_crypto and symbol != "BTC/USD" and not is_safe) else None

    m = signal_at(
        ind, len(ind) - 1, ctx['social'].get(symbol, 0),
        live_price=live_price, live_high=live_high,
        btc_ret_4h=btc_ret, entry_time=pos_mem.get('entry_time')
    )
    if is_safe and ctx['risk_off'] and m['setup']:
        m['score'] += 15
        m['reason'] += " + 🛡️ Refugiu"

    if pos_symbol in held:
        flatten = (not is_crypto) and ctx['flatten_stocks']
        if manage_position(tc, symbol, held[pos_symbol], m, is_crypto, pos_mem, flatten_now=flatten):
            ctx['closed'].add(pos_symbol)
        return

    if not allow_new_signals:
        return
    if ctx['entries_blocked'] or (not is_crypto and not ctx['stock_entries_ok']):
        return
    if ctx['risk_off'] and not is_safe:
        return
    if not entry_allowed(m, round_trip_cost(is_crypto)):
        return
    print(f"🟢 [SEMNAL {m['setup']}] {symbol:<8} | Preț: {m['price']:.4f} | Scor: {m['score']} | {m['reason']}")
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
            print(f"🎯 [INTRARE V7] {sym}: ~{invest:.0f}$ | Stop inițial: -{t['init_stop_pct']*100:.2f}% "
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
            ctx['pos_memory'][pos_sym] = {'entry_time': datetime.now(timezone.utc), 'scaled_out': False}
            time.sleep(3.0)
            try:
                pos = tc.get_open_position(pos_sym)
                place_stop(tc, sym, float(pos.qty),
                           float(pos.avg_entry_price) * (1.0 - t['init_stop_pct']), t['is_crypto'])
            except Exception as e:
                print(f"   ⚠️ Stop-ul pentru {sym} va fi pus la ciclul următor ({e})")
        except Exception as e:
            print(f"   ⚠️ Intrare eșuată pe {t['symbol']}: {e}")


def run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val, seen_bar_times, pos_memory, show_scorecard=False):
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

    # Formatăm cu virgulă (100,000.00$) ca GitHub Actions să nu mai cenzureze numărul cu ***
    print(f"\n⚡ Scanare V7: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | Equity: {equity:,.2f}$ "
          f"| Vârf: {peak:,.2f}$ | Kill-switch: {death_threshold:,.2f}$ | Azi: {daily_pnl*100:+.2f}%")
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

    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=now - timedelta(days=5))).df
    crypto_packs = prepare_symbol_frames(crypto_bars, ALL_CRYPTO, now, stale_bars=5, is_stock=False)

    risk_off = False
    btc_ret_4h = 0.0
    btc_pack = crypto_packs.get("BTC/USD")
    if btc_pack is not None:
        btc_ind, btc_live, _ = btc_pack
        if len(btc_ind) > 3:
            lookback = max(1, int(30 / DNA['bar_minutes']))
            btc_change = float(btc_live / btc_ind['close'].iloc[-lookback] - 1.0)
            btc_ret_4h = float(btc_ind['ret_4h'].iloc[-1])
            if btc_change < -0.010 or fear_greed_val <= 24:
                risk_off = True
                print(f"🛡️ [MOD REFUGIU] BTC {btc_change*100:+.2f}% (30m), F&G {fear_greed_val}: doar active de refugiu.")

    ctx = {
        'social': social_scores, 'risk_off': risk_off, 'entries_blocked': entries_blocked,
        'closed': set(), 'candidates': [], 'stock_entries_ok': False, 'flatten_stocks': False,
        'pos_memory': pos_memory, 'btc_ret_4h': btc_ret_4h
    }

    for symbol, pack in crypto_packs.items():
        last_bar_t = pack[0].index[-1]
        is_new_bar = seen_bar_times.get(symbol) != last_bar_t
        seen_bar_times[symbol] = last_bar_t
        process_symbol(tc, symbol, pack, True, symbol in SAFE_HAVEN_CRYPTO, held, ctx, allow_new_signals=is_new_bar)

    clock = tc.get_clock()
    if clock.is_open:
        mtc = (clock.next_close - clock.timestamp).total_seconds() / 60.0
        ctx['stock_entries_ok'] = 40 <= mtc <= 375
        ctx['flatten_stocks'] = FLATTEN_STOCKS_BEFORE_CLOSE and mtc <= FLATTEN_MIN
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=now - timedelta(days=12),
            feed=DataFeed.IEX)).df
        stock_packs = prepare_symbol_frames(stock_bars, ALL_STOCKS, now, stale_bars=6, is_stock=True)
        for symbol, pack in stock_packs.items():
            last_bar_t = pack[0].index[-1]
            is_new_bar = seen_bar_times.get(symbol) != last_bar_t
            seen_bar_times[symbol] = last_bar_t
            process_symbol(tc, symbol, pack, False, symbol in SAFE_HAVEN_STOCKS, held, ctx, allow_new_signals=is_new_bar)

    if ctx['candidates'] and not entries_blocked:
        open_new_positions(tc, ctx, held, equity)


# ==========================================
# 9. BACKTEST V7 (INCLUSIV SCALE-OUT 50/50 & 1H FILTER)
# ==========================================
WARMUP_BARS = 210


def backtest_symbol(symbol, df, is_crypto, regime_off=None, is_safe=False):
    if not is_crypto:
        df = filter_regular_stock_hours(df)
    if len(df) <= WARMUP_BARS + 10:
        return []
    ind = compute_indicators(df)
    n = len(ind)
    opens, highs, lows, closes = (ind[c].values for c in ('open', 'high', 'low', 'close'))
    times = ind.index
    ny_times = times.tz_convert(NY_TZ) if not is_crypto else None
    cost_rt = round_trip_cost(is_crypto)
    trades, pos = [], None

    def record_leg(fill, i, weight, why):
        gross = fill / pos['entry'] - 1.0
        net = gross - cost_rt
        base_frac = min(DNA['risk_per_trade'] / pos['stop_pct'], DNA['max_single_exposure'])
        trades.append({'symbol': symbol, 'exit_time': times[i], 'net': net,
                       'frac': base_frac * weight, 'R': net / pos['stop_pct'], 'why': why})

    for i in range(WARMUP_BARS, n - 1):
        if pos is not None:
            if lows[i] <= pos['stop']:
                record_leg(min(pos['stop'], opens[i]), i, pos['rem_weight'], 'stop')
                pos = None
                continue
            pos['hh'] = max(pos['hh'], highs[i])

            if not is_crypto and FLATTEN_STOCKS_BEFORE_CLOSE and ny_times[i].time() >= dtime(15, 45):
                record_leg(closes[i], i, pos['rem_weight'], 'eod_flatten')
                pos = None
                continue

            m = signal_at(ind, i, 0)
            profit = closes[i] / pos['entry'] - 1.0

            # Scale-out 50% la +1.2R net
            partial_target = cost_rt + pos['stop_pct'] * DNA['partial_tp_r']
            if DNA['use_partial_tp'] and not pos['scaled_out'] and profit >= partial_target:
                record_leg(closes[i], i, 0.5, 'partial_tp_50%')
                pos['scaled_out'] = True
                pos['rem_weight'] = 0.5
                pos['stop'] = max(pos['stop'], pos['entry'] * (1.0 + cost_rt + 0.002))

            if profit >= pos['stop_pct'] * DNA['breakeven_trigger_r'] or pos['stop'] >= pos['entry']:
                trail = pos['hh'] - DNA['trail_atr_mult'] * m['atr']
                new_stop = max(pos['stop'], trail, pos['entry'] * (1.0 + cost_rt))
                pos['stop'] = min(new_stop, closes[i] * 0.994) if new_stop > pos['stop'] else pos['stop']

            why = exit_decision(profit, m, pos['stop_pct'], cost_rt)
            if why:
                record_leg(opens[i + 1], i + 1, pos['rem_weight'], why.split(':')[0])
                pos = None
            continue

        is_risk_off = bool(regime_off[i]) if regime_off is not None else False
        if is_risk_off and not is_safe:
            continue
        if not is_crypto and ny_times[i].time() >= dtime(15, 15):
            continue

        m = signal_at(ind, i, 0)
        if is_safe and is_risk_off and m['setup']:
            m['score'] += 15
        if entry_allowed(m, cost_rt):
            entry = opens[i + 1]
            sp = stop_distance_pct(m)
            pos = {'entry': entry, 'stop': entry * (1.0 - sp), 'stop_pct': sp,
                   'hh': entry, 'scaled_out': False, 'rem_weight': 1.0}

    if pos is not None:
        record_leg(closes[-1], n - 1, pos['rem_weight'], 'final')
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
    print(f"   Tranzacții/Tranșe: {len(trades)} | Win rate: {len(wins) / len(trades) * 100:.1f}% | Profit factor: {pf:.2f}")
    print(f"   Câștig mediu: +{avg_w:.2f}% | Pierdere medie: -{avg_l:.2f}% | Așteptare: {nets.mean()*100:+.3f}% "
          f"({np.mean([t['R'] for t in trades]):+.2f}R)")
    print(f"   Randament cont (cu dimensionare pe risc): {(equity[-1] - 1) * 100:+.2f}% | Max drawdown: {max_dd:.2f}%")


def run_backtest(days):
    print(f"📈 BACKTEST V7 ({days} zile) | bare de {DNA['bar_minutes']}m + filtru 1H | Scale-Out 50/50 activ")
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
        t = backtest_symbol(s, df, True, regime, is_safe=(s in SAFE_HAVEN_CRYPTO))
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
            t = backtest_symbol(s, df, False, None, is_safe=(s in SAFE_HAVEN_STOCKS))
            all_trades += t
            summarize_trades(t, s)

    summarize_trades(all_trades, "TOTAL PORTOFOLIU V7")


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

    print(f"🚀 AGENT V7 pornit ({'PAPER' if PAPER else '⚠️ LIVE'}) | 15m+1H | Scale-Out 50/50 | "
          f"cost round-trip crypto {round_trip_cost(True)*100:.2f}%")
    social_scores, fear_greed_val = scan_social_media_and_sentiment()
    seen_bar_times = {}
    pos_memory = build_position_memory(tc)

    for i in range(CYCLES_PER_RUN):
        print(f"\n================ CICLUL {i + 1}/{CYCLES_PER_RUN} ================")
        try:
            run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val,
                      seen_bar_times, pos_memory, show_scorecard=(i == 0))
        except SystemExit:
            raise
        except Exception as e:
            print(f"⚠️ Eroare temporară în ciclul {i + 1}: {e}")
        if i < CYCLES_PER_RUN - 1:
            time.sleep(55)


if __name__ == "__main__":
    main()
