#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADING CHAMPION V9.1 — SMC & ICT INSTITUTIONAL EDITION (FUSION COMPLETĂ)
Bazat pe manualul "Beyond The Markets" (OrientFX) + Optimizări Live Alpaca

Ce implementează V9.1:
  1. LIQUIDITY SWEEP ("Turtle Soup" / Stop-Hunt - Pag. 85-90, 103, 249) + MSS / CHoCH.
  2. FAIR VALUE GAPS (FVG 50% Equilibrium - Pag. 96-102, 206-211) & ORDER BLOCKS (OB - Pag. 80-83).
  3. PREMIUM / DISCOUNT & FIBONACCI OTE GOLDEN ZONE (50% - 78.6% - Pag. 201, 224-229, 259):
     Cumpără în Discount/OTE și strânge automat Trailing Stop-ul când prețul ajunge în Premium (>70%).
  4. ICT KILLZONES & SILVER BULLET (Pag. 231-236, 245-251, 263-266) + Filtru Asian Accumulation.
  5. ORDINE LIMIT LA INTRARE + RECYCLER MICRO-POZIȚII (<3.500$, ex: UNIUSD) + SL TO BE RAPID.
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
    LimitOrderRequest,
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
# 1. CONFIGURARE V9.1 (SMC & ICT)
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

MAX_DRAWDOWN_PCT = float(os.environ.get("MAX_DRAWDOWN_PCT", "0.20"))          # Kill-switch la -20% (80,000$)
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.03"))  # Pauză la -3% într-o zi
CYCLES_PER_RUN = int(os.environ.get("CYCLES_PER_RUN", "8"))
CYCLE_SLEEP_SEC = int(os.environ.get("CYCLE_SLEEP_SEC", "30"))
IGNORE_HISTORY_PEAK = os.environ.get("IGNORE_HISTORY_PEAK", "0") == "1"
DEATH_FILE = "DEAD.lock"

DUST_THRESHOLD_USD = 5.0      # Ignoră firimiturile crypto sub 5$ (AVAX, LTC, SOL dust)
MICRO_POS_USD = 3500.0        # Eliberează slotul dacă o poziție e sub 3.500$ (ex: UNIUSD 1.624$) și stagnează
MIN_POSITION_USD = 4500.0     # Nu deschide poziții noi sub 4.500$

CRYPTO_FEE_PCT = float(os.environ.get("CRYPTO_FEE_PCT", "0.0022"))
CRYPTO_SLIPPAGE_PCT = 0.0003
STOCK_SLIPPAGE_PCT = 0.0002

CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD",
    "AVAX/USD", "LINK/USD", "LTC/USD", "UNI/USD", "NEAR/USD"
]
STOCK_SYMBOLS = ["NVDA", "TSLA", "AAPL", "AMD", "META", "SPY", "QQQ"]
SAFE_HAVEN_STOCKS = ["GLD", "TLT"]
SAFE_HAVEN_CRYPTO = ["PAXG/USD"]
ALL_CRYPTO = CRYPTO_SYMBOLS + SAFE_HAVEN_CRYPTO
ALL_STOCKS = STOCK_SYMBOLS + SAFE_HAVEN_STOCKS

FLATTEN_STOCKS_BEFORE_CLOSE = True
FLATTEN_MIN = 10
NY_TZ = ZoneInfo("America/New_York")

DNA = {
    'bar_minutes': 5,
    'ema_fast': 9,
    'ema_slow': 21,
    'ema_trend': 50,
    'atr_period': 14,
    'min_confluences': 4,           # Pag. 67-68: Minim 4 confluențe pentru un setup instituțional!
    'entry_score_min': 70,          # Scor minim în sesiunile active
    'asian_accumulation_score': 78, # Pag. 248: Sesiunea asiatică e Acumulare (range); filtru strict!

    'base_risk_per_trade': 0.005,   # 0.50% risc standard
    'high_conviction_risk': 0.007,  # 0.70% pe setup-uri SMC + ICT Killzone (scor >= 82)
    'losing_streak_factor': 0.65,   # Pag. 137: Reducem miza la 65% după pierderi consecutive!
    'max_positions': 5,
    'max_crypto_positions': 4,
    'max_single_exposure': 0.22,
    'max_total_exposure': 0.85,

    'stop_atr_mult': 2.2,
    'min_stop_pct': 0.012,
    'max_stop_pct': 0.024,
    'legacy_cut_pct': -0.026,
    'trail_atr_mult': 2.0,          # Trailing normal (scade la 1.5x ATR când prețul intră în zona Premium!)
    'breakeven_trigger_r': 0.45,    # Pag. 273 (SL to BE): mutăm Stop-Loss la Break-Even rapid!

    'use_partial_tp': True,         # Pag. 254, 273 (Take Partials 50% + Runner)
    'partial_tp_r': 0.85,
    'spike_harvest_pct': 0.020,
    'use_exhaustion_exit': True,
    'exhaustion_min_r': 1.10,
    'soft_stop_frac_news': 0.4,
    'vol_cost_mult': 3.4,
    'reentry_cooldown_min': 30,
}

BULLISH_WORDS = ['surge', 'soar', 'bull', 'bullish', 'breakout', 'moon', 'rally', 'pump', 'ath',
                 'upgrade', 'record', 'adoption', 'approval', 'beats', 'inflow', 'accumulate', 'partner']
BEARISH_WORDS = ['crash', 'dump', 'bear', 'bearish', 'hack', 'hacked', 'ban', 'lawsuit', 'plunge',
                 'panic', 'liquidation', 'fraud', 'bankruptcy', 'sec', 'misses', 'downgrade', 'outflow']
BULL_RE = re.compile(r"\b(" + "|".join(BULLISH_WORDS) + r")\b")
BEAR_RE = re.compile(r"\b(" + "|".join(BEARISH_WORDS) + r")\b")
ALIASES = {
    'BTC': ['btc', 'bitcoin'], 'ETH': ['eth', 'ethereum'], 'SOL': ['solana', 'sol'],
    'DOGE': ['doge', 'dogecoin'], 'XRP': ['xrp', 'ripple'], 'AVAX': ['avax', 'avalanche'],
    'LINK': ['chainlink'], 'LTC': ['ltc', 'litecoin'], 'UNI': ['uniswap', 'uni'],
    'NEAR': ['near'], 'PAXG': ['paxg', 'gold'],
    'NVDA': ['nvda', 'nvidia'], 'TSLA': ['tsla', 'tesla'], 'AAPL': ['aapl', 'apple'],
    'AMD': ['amd'], 'META': ['meta'], 'SPY': ['spy'], 'QQQ': ['qqq', 'nasdaq'],
    'GLD': ['gld'], 'TLT': ['tlt'],
}


# ==========================================
# 2. UTILITARE & ICT KILLZONES (PAG. 231-236, 263-265)
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


def truncate_qty(q, is_crypto, decimals=8):
    if not is_crypto:
        return int(Decimal(str(q)).to_integral_value(rounding=ROUND_DOWN))
    quant = Decimal("1." + "0" * decimals) if decimals > 0 else Decimal("1")
    d = Decimal(str(q)).quantize(quant, rounding=ROUND_DOWN)
    return float(d) if d > 0 else 0.0


def get_ict_session_info(now_utc):
    ny_time = now_utc.astimezone(NY_TZ).time()
    utc_t = now_utc.time()

    is_silver_bullet = (
        (dtime(3, 0) <= ny_time <= dtime(4, 30)) or
        (dtime(9, 50) <= ny_time <= dtime(11, 15)) or
        (dtime(13, 50) <= ny_time <= dtime(15, 45))
    )
    is_london_kz = dtime(6, 0) <= utc_t <= dtime(10, 0)
    is_ny_kz = dtime(12, 0) <= utc_t <= dtime(16, 30)
    is_asian_accum = dtime(0, 0) <= utc_t < dtime(5, 30)

    if is_silver_bullet:
        return "🎯 ICT Silver Bullet/Macro", 4, False
    if is_ny_kz:
        return "🗽 NY Killzone", 2, False
    if is_london_kz:
        return "🇬🇧 London Killzone", 2, False
    if is_asian_accum:
        return "🌙 Asian Accumulation (Filtru Range)", 0, True
    return "☀️ Standard Session", 0, False


def get_active_positions_and_sweep_dust(tc):
    real_held = {}
    for p in tc.get_all_positions():
        sym = norm(p.symbol)
        mkt_val = abs(float(getattr(p, 'market_value', 0.0) or 0.0))
        if mkt_val < DUST_THRESHOLD_USD:
            if mkt_val >= 0.01:
                try:
                    tc.close_position(sym)
                    print(f"🧹 [CURĂȚARE PRAF] Am închis restul rezidual pe {sym} ({mkt_val:.4f}$).")
                except Exception:
                    pass
            continue
        real_held[sym] = p
    return real_held


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
def fetch_url_raw(url, headers=None, timeout=7):
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


def fetch_json_url(url, headers=None, timeout=7):
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
# 4. MOTORUL SMC + ICT + PRICE ACTION
# ==========================================
def compute_indicators(df):
    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    o = df['open'].astype(float)
    v = df['volume'].astype(float)

    ind = pd.DataFrame(index=df.index)
    ind['close'], ind['high'], ind['low'], ind['open'], ind['volume'] = c, h, l, o, v
    ind['ema_fast'] = c.ewm(span=DNA['ema_fast'], adjust=False).mean()
    ind['ema_slow'] = c.ewm(span=DNA['ema_slow'], adjust=False).mean()
    ind['ema_trend'] = c.ewm(span=DNA['ema_trend'], adjust=False).mean()

    ind['ema_1h_fast'] = c.ewm(span=108, adjust=False).mean()
    ind['ema_1h_slow'] = c.ewm(span=252, adjust=False).mean()
    ind['ret_2h'] = c.pct_change(24).fillna(0.0)

    typ_price = (h + l + c) / 3.0
    vol_sum = v.rolling(48, min_periods=5).sum().replace(0, np.nan)
    ind['vwap'] = ((typ_price * v).rolling(48, min_periods=5).sum() / vol_sum).fillna(c)

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
    atr = tr.ewm(alpha=1 / DNA['atr_period'], adjust=False).mean()
    ind['atr'] = atr

    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_safe = atr.replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(15.0)
    ind['adx'] = dx.ewm(alpha=1 / 14, adjust=False).mean().fillna(15.0)

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    width = (4 * std20).replace(0, np.nan)
    bb_pos = ((c - (sma20 - 2 * std20)) / width).fillna(0.5)
    ind['bb_pos'] = bb_pos
    ind['std_zscore'] = ((c - sma20) / std20.replace(0, np.nan)).fillna(0.0)

    avg_vol = v.rolling(20).mean()
    ind['rvol'] = (v / avg_vol.replace(0, np.nan)).fillna(1.0)

    # A. LUMÂNĂRI JAPONEZE & INSTITUTIONAL DISPLACEMENT
    rng = (h - l).clip(lower=1e-12)
    body = (c - o).abs()
    body_top = pd.concat([c, o], axis=1).max(axis=1)
    body_bot = pd.concat([c, o], axis=1).min(axis=1)
    upper_wick = (h - body_top) / rng
    lower_wick = (body_bot - l) / rng
    body_ratio = body / rng
    ind['wick'] = upper_wick

    prev_o = o.shift(1)
    prev_body = body.shift(1)
    prev2_o, prev2_c = o.shift(2), c.shift(2)
    prev2_body = body.shift(2)

    ind['bull_hammer'] = (lower_wick >= 0.50) & (upper_wick <= 0.22) & (bb_pos <= 0.60)
    ind['dragonfly_doji'] = (body_ratio <= 0.18) & (lower_wick >= 0.60) & (upper_wick <= 0.20)
    ind['bull_engulf'] = (prev_c < prev_o) & (c > o) & (c >= prev_o) & (o <= prev_c * 1.001) & (body >= prev_body * 1.05)
    ind['morning_star'] = (prev2_c < prev2_o) & (prev2_body > atr * 0.4) & (prev_body < prev2_body * 0.55) \
                          & (c > o) & (c >= (prev2_o + prev2_c) / 2.0)

    ind['shooting_star'] = (upper_wick >= 0.52) & (lower_wick <= 0.22) & (body_ratio <= 0.38) & (bb_pos >= 0.70)
    ind['gravestone_doji'] = (body_ratio <= 0.18) & (upper_wick >= 0.60) & (bb_pos >= 0.70)
    ind['bear_engulf'] = (prev_c > prev_o) & (c < o) & (c <= prev_o) & (body >= prev_body * 1.05) & (bb_pos >= 0.62)
    ind['bull_displacement'] = (c > o) & (body >= atr * 1.25) & (body_ratio >= 0.62)

    # B. SMART MONEY CONCEPTS: FVG, ORDER BLOCKS, LIQUIDITY SWEEP, PD ARRAYS
    swing_hi = h.rolling(36, min_periods=12).max()
    swing_lo = l.rolling(36, min_periods=12).min()
    deal_rng = (swing_hi - swing_lo).replace(0, np.nan)
    ind['pd_pos'] = ((c - swing_lo) / deal_rng).fillna(0.5)
    ind['fib_retrace'] = ((swing_hi - c) / deal_rng).fillna(0.5)

    ind['hh12'] = h.shift(1).rolling(12).max()
    ind['ssl_low12'] = l.shift(2).rolling(14).min()
    ind['low3'] = l.rolling(3).min()

    swept_recent = (l <= ind['ssl_low12'] * 1.0005) | (l.shift(1) <= ind['ssl_low12'].shift(1) * 1.0005)
    ind['liquidity_sweep'] = swept_recent & (c > ind['ssl_low12']) & (c > o)

    ind['bos_up'] = (c > ind['hh12']) & (c > o)
    minor_lh = h.shift(1).rolling(5).max()
    ind['mss_choch'] = ind['liquidity_sweep'] & ((c > minor_lh) | ind['bull_engulf'] | ind['bull_displacement'])

    fvg_created = (l > h.shift(2)) & (c.shift(1) > o.shift(1)) & (body.shift(1) >= atr.shift(1) * 0.7)
    fvg_bot_series = h.shift(2).where(fvg_created, np.nan).ffill(limit=18)
    fvg_top_series = l.where(fvg_created, np.nan).ffill(limit=18)
    fvg_mid_series = (fvg_bot_series + fvg_top_series) / 2.0
    ind['fvg_retest'] = fvg_bot_series.notna() & (l <= fvg_top_series * 1.002) & (c >= fvg_mid_series * 0.998)

    ob_created = (c.shift(1) < o.shift(1)) & (ind['bull_displacement'] | ind['bull_engulf'])
    ob_bot_series = l.shift(1).where(ob_created, np.nan).ffill(limit=24)
    ob_top_series = h.shift(1).where(ob_created, np.nan).ffill(limit=24)
    ind['ob_retest'] = ob_bot_series.notna() & (l <= ob_top_series * 1.002) & (c >= ob_bot_series) & (c > o)

    return ind


def signal_at(ind, i, social_bonus=0, live_price=None, live_high=None, btc_ret_2h=None, entry_time=None, kz_bonus=0):
    r = ind.iloc[i]
    bar_close = float(r['close'])
    bar_open = float(r['open'])
    price = float(live_price) if live_price is not None else bar_close
    atr = float(r['atr'])
    if not np.isfinite(atr) or atr <= 0:
        atr = price * 0.003

    ema_f, ema_s, ema_t = float(r['ema_fast']), float(r['ema_slow']), float(r['ema_trend'])
    ema_1h_f, ema_1h_s = float(r['ema_1h_fast']), float(r['ema_1h_slow'])
    vwap, adx = float(r['vwap']), float(r['adx'])
    ret_2h = float(r['ret_2h'])
    rsi, hist = float(r['rsi']), float(r['macd_hist'])
    macd_rising, rsi_falling = bool(r['macd_rising']), bool(r['rsi_falling'])
    bb, rvol, wick = float(r['bb_pos']), float(r['rvol']), float(r['wick'])
    hh12 = float(r['hh12'])
    pd_pos = float(r['pd_pos'])
    fib_retrace = float(r['fib_retrace'])
    std_z = float(r['std_zscore'])

    is_hammer = bool(r['bull_hammer']) or bool(r['dragonfly_doji'])
    is_bull_engulf = bool(r['bull_engulf'])
    is_morning_star = bool(r['morning_star'])
    is_shooting_star = bool(r['shooting_star']) or bool(r['gravestone_doji'])
    is_bear_engulf = bool(r['bear_engulf'])

    liq_sweep = bool(r['liquidity_sweep'])
    mss_choch = bool(r['mss_choch'])
    fvg_retest = bool(r['fvg_retest'])
    ob_retest = bool(r['ob_retest'])
    bos_up = bool(r['bos_up'])

    candle_bull_name = ""
    if is_bull_engulf:
        candle_bull_name = "Bullish Engulfing"
    elif is_morning_star:
        candle_bull_name = "Morning Star"
    elif is_hammer:
        candle_bull_name = "Hammer/Dragonfly"

    trend_up = (ema_f >= ema_s * 0.999) and (ema_s >= ema_t * 0.998) and (price > ema_t)
    htf_1h_ok = (price >= ema_1h_f * 0.993) and (ema_1h_f >= ema_1h_s * 0.990)
    above_vwap = price >= vwap
    bullish_action = (price > bar_open) or (bar_close > bar_open)

    in_discount = pd_pos <= 0.55
    in_ote_golden_zone = 0.45 <= fib_retrace <= 0.79

    ceiling, why = False, ""
    if is_bear_engulf:
        ceiling, why = True, "Bearish Engulfing în vârf (Regula Nr. 1 Pag. 33)"
    elif is_shooting_star and not macd_rising:
        ceiling, why = True, "Shooting Star / Gravestone Doji la rezistență (Pag. 34, 39)"
    elif std_z >= 2.3 and rsi_falling:
        ceiling, why = True, f"Țintă Deviație Standard (+{std_z:.1f} SD - Pag. 254) atinsă"
    elif bool(r['macd_losing']) and (rsi_falling or rvol < 0.80):
        ceiling, why = True, "MACD pierde viteză + cumpărători epuizați"
    elif wick > 0.58 and bb > 0.78 and not macd_rising:
        ceiling, why = True, "Respingere din zona de Supply (Wick Rejection)"

    confluences = 0
    if htf_1h_ok:
        confluences += 1
    if trend_up or mss_choch:
        confluences += 1
    if in_discount or in_ote_golden_zone or above_vwap:
        confluences += 1
    if fvg_retest or ob_retest or liq_sweep or bos_up:
        confluences += 1
    if candle_bull_name or (bullish_action and macd_rising):
        confluences += 1
    if rvol >= 1.10 or adx >= 19:
        confluences += 1

    score, setup, reasons = 0, None, []

    if (liq_sweep or mss_choch) and htf_1h_ok and bullish_action and macd_rising:
        score, setup = 78, 'LIQUIDITY_SWEEP'
        reasons.append("SMC Liquidity Sweep (Stop-Hunt) + MSS")
    elif (fvg_retest or ob_retest) and htf_1h_ok and bullish_action and macd_rising and 38 <= rsi <= 68:
        score, setup = 76, 'SMC_OB_FVG'
        zone_tag = "OrderBlock+FVG" if (fvg_retest and ob_retest) else ("FVG 50%" if fvg_retest else "OrderBlock")
        if in_ote_golden_zone:
            score += 4
            zone_tag += "+OTE(61.8%)"
        reasons.append(f"Retest {zone_tag}")
    elif trend_up and htf_1h_ok and above_vwap and macd_rising and 48 <= rsi <= 72 \
            and (bos_up or price > hh12) and pd_pos <= 0.90 and (rvol >= 1.15 or adx >= 20):
        score, setup = 74, 'BOS_BREAKOUT'
        reasons.append(f"BOS Structure+VWAP(Vol {rvol:.1f}x)")
    elif trend_up and htf_1h_ok and bullish_action and macd_rising and 40 <= rsi <= 62 \
            and (in_discount or in_ote_golden_zone or float(r['low3']) <= max(ema_s, vwap) * 1.003) \
            and price > ema_f and bb < 0.80:
        score, setup = 72, 'OTE_PULLBACK'
        reasons.append("Pullback în Discount/OTE")
    elif candle_bull_name and htf_1h_ok and in_discount and price > ema_f and macd_rising and rvol >= 1.05:
        score, setup = 71, 'PRICE_ACTION'
        reasons.append(f"🕯️ {candle_bull_name} în Discount")

    if setup:
        if candle_bull_name and setup != 'PRICE_ACTION':
            score += 5
            reasons.append(f"🕯️ {candle_bull_name}(+5)")
        if kz_bonus > 0:
            score += kz_bonus
            reasons.append(f"Killzone(+{kz_bonus})")
        if btc_ret_2h is not None:
            rel_strength = ret_2h - btc_ret_2h
            if rel_strength >= 0.004:
                score += 4
                reasons.append(f"Lider vs BTC({rel_strength*100:+.1f}%)")
            elif rel_strength <= -0.010:
                score -= 6
                reasons.append(f"Slab vs BTC({rel_strength*100:+.1f}%)")
        if social_bonus:
            score += social_bonus
            reasons.append(f"Social({social_bonus:+d})")
        reasons.append(f"[{confluences} Confluențe]")

    if entry_time is not None:
        sub_highs = ind['high'].loc[ind.index >= entry_time - timedelta(minutes=DNA['bar_minutes'])]
        peak_since_entry = float(sub_highs.max()) if not sub_highs.empty else price
    else:
        peak_since_entry = float(ind['high'].iloc[max(0, i - 2):i + 1].max())
    if live_high is not None:
        peak_since_entry = max(peak_since_entry, float(live_high))

    return {
        'price': price, 'bar_close': bar_close, 'atr': atr, 'atr_pct': atr / price,
        'ema_fast': ema_f, 'ema_slow': ema_s, 'ema_trend': ema_t, 'vwap': vwap, 'adx': adx,
        'rsi': rsi, 'macd_hist': hist, 'macd_rising': macd_rising,
        'trend_up': trend_up, 'ret_2h': ret_2h, 'pd_pos': pd_pos, 'confluences': confluences,
        'peak_since_entry': max(price, peak_since_entry),
        'profit_ceiling_hit': ceiling, 'ceiling_reason': why,
        'score': score, 'setup': setup, 'social': int(social_bonus),
        'reason': " + ".join(reasons) if reasons else "Neutru",
    }


def entry_allowed(m, cost_rt, is_asian_accum=False):
    min_req = DNA['asian_accumulation_score'] if is_asian_accum else DNA['entry_score_min']
    if not m['setup'] or m['confluences'] < DNA['min_confluences'] or m['score'] < min_req or m['profit_ceiling_hit']:
        return False
    return m['atr_pct'] * DNA['vol_cost_mult'] >= cost_rt


def exit_decision(profit, m, init_stop_pct, cost_rt, market_val=20000.0):
    trend_broken = m['ema_fast'] < m['ema_slow'] and not m['macd_rising'] and m['price'] < m['vwap']

    # 1. RECYCLER MICRO-POZIȚII: eliberează slotul dacă o poziție e sub 3.500$ (ex: UNIUSD 1.624$) și nu are setup SMC
    if market_val < MICRO_POS_USD and profit > -0.010 and m['score'] < 70:
        return f"Reciclare slot micro-poziție ({market_val:.0f}$) pentru alocare completă"

    # 2. Țintă Distribuție / Spike Harvester
    if profit >= DNA['spike_harvest_pct'] and (m['rsi'] > 73 or m['profit_ceiling_hit']):
        return f"Țintă Distribuție / Spike încasat (+{profit*100:.2f}%)"

    # 3. CHoCH Bearish pe poziții vechi
    if profit <= DNA['legacy_cut_pct'] and trend_broken:
        return f"CHoCH Bearish sub {DNA['legacy_cut_pct']*100:.1f}% (structură + VWAP pierdute)"

    # 4. Plafon SMC / Price Action
    if DNA['use_exhaustion_exit']:
        min_net = cost_rt + (init_stop_pct * DNA['exhaustion_min_r'])
        if profit >= min_net and m['profit_ceiling_hit']:
            return f"Plafon SMC/PriceAction la >{DNA['exhaustion_min_r']}R: {m['ceiling_reason']}"

    # 5. Stop la știri negative
    if m['social'] <= -5 and trend_broken and profit <= -init_stop_pct * DNA['soft_stop_frac_news']:
        return "Stop controlat: structură ruptă + știri negative"
    return None


# ==========================================
# 5. EXECUȚIE V9.1: LIMIT ENTRY, SL TO BE, PARTIALS & PREMIUM TRAIL
# ==========================================
def get_open_orders(tc):
    try:
        return list(tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))
    except Exception as e:
        print(f"   ⚠️ Nu pot citi ordinele deschise: {e}")
        return []


def build_position_memory_and_cooldowns(tc):
    mem = {}
    cooldown_syms = set()
    portfolio_loss_streak = 0
    now = datetime.now(timezone.utc)
    try:
        closed = tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=300))
        filled = [o for o in closed if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0
                  and o.filled_at is not None]
        filled.sort(key=lambda o: o.filled_at)

        last_buy_px = {}
        streak_losses = {}
        last_loss_time = {}

        for o in filled:
            sym = norm(o.symbol)
            px = float(o.filled_avg_price)
            qty = float(o.filled_qty)
            if qty * px < DUST_THRESHOLD_USD:
                continue
            if o.side == OrderSide.BUY:
                mem[sym] = {'entry_time': o.filled_at, 'scaled_out': False, 'last_sell_time': None}
                last_buy_px[sym] = px
            elif o.side == OrderSide.SELL:
                if sym in mem:
                    mem[sym]['scaled_out'] = True
                    mem[sym]['last_sell_time'] = o.filled_at
                if (now - o.filled_at).total_seconds() < DNA['reentry_cooldown_min'] * 60:
                    cooldown_syms.add(sym)
                if sym in last_buy_px:
                    if px < last_buy_px[sym]:
                        streak_losses[sym] = streak_losses.get(sym, 0) + 1
                        last_loss_time[sym] = o.filled_at
                        portfolio_loss_streak += 1
                    else:
                        streak_losses[sym] = 0
                        portfolio_loss_streak = 0

        for sym, losses in streak_losses.items():
            if losses >= 2 and sym in last_loss_time:
                if (now - last_loss_time[sym]).total_seconds() < 10800:
                    cooldown_syms.add(sym)
    except Exception:
        pass
    return mem, cooldown_syms, portfolio_loss_streak


def find_stop_order(open_orders, pos_symbol):
    for o in open_orders:
        if norm(o.symbol) == pos_symbol and o.side == OrderSide.SELL and o.stop_price is not None:
            return o
    return None


def place_stop(tc, symbol, qty, stop_price, is_crypto):
    stop_price = round_price(stop_price, is_crypto)
    clean_qty = truncate_qty(qty, is_crypto, decimals=8)
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


def submit_smart_buy(tc, symbol, invest_usd, live_price, is_crypto):
    """Plasează ordin LIMIT la intrarea în FVG/OB pentru a evita slippage-ul, cu fallback rapid."""
    limit_px = round_price(live_price * 1.0006, is_crypto)
    qty = truncate_qty(invest_usd / limit_px, is_crypto, decimals=6)
    if qty <= 0:
        return False

    tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
    order = tc.submit_order(LimitOrderRequest(
        symbol=symbol, qty=qty, limit_price=limit_px, side=OrderSide.BUY, time_in_force=tif
    ))
    time.sleep(3.5)
    try:
        updated = tc.get_order_by_id(order.id)
        filled_q = float(getattr(updated, 'filled_qty', 0.0) or 0.0)
        status_str = str(getattr(updated, 'status', '')).lower()
        if 'filled' in status_str and filled_q >= qty * 0.95:
            return True
        tc.cancel_order_by_id(order.id)
        time.sleep(1.0)
        if filled_q * live_price >= MIN_POSITION_USD * 0.5:
            return True
        rem_usd = invest_usd - (filled_q * live_price)
        if rem_usd >= 500.0:
            if is_crypto:
                tc.submit_order(MarketOrderRequest(
                    symbol=symbol, notional=round(rem_usd, 2), side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
            else:
                rem_qty = int(rem_usd // live_price)
                if rem_qty >= 1:
                    tc.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=rem_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
            time.sleep(2.0)
            return True
    except Exception:
        return True
    return False


def effective_init_stop_pct(entry, cur_stop, fallback_stop_pct):
    if cur_stop is not None and 0 < cur_stop < entry:
        return clamp((entry - cur_stop) / entry, DNA['min_stop_pct'], DNA['max_stop_pct'])
    return fallback_stop_pct


def execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, new_stop_price):
    try:
        for o in get_open_orders(tc):
            if norm(o.symbol) == pos_symbol:
                tc.cancel_order_by_id(o.id)
        time.sleep(1.2)
        fresh_pos = tc.get_open_position(pos_symbol)
        avail_qty = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
        sell_qty = truncate_qty(avail_qty * 0.5, is_crypto, decimals=6)
        if sell_qty <= 0 or (not is_crypto and avail_qty < 2):
            place_stop(tc, symbol, avail_qty, new_stop_price, is_crypto)
            return False

        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        tc.submit_order(MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=tif))
        print(f"   💰 [TAKE PARTIALS 50% - Pag. 273] Vândut {sell_qty} {symbol}! Restul de 50% are SL to BE+.")
        time.sleep(2.2)
        rem_pos = tc.get_open_position(pos_symbol)
        rem_qty = float(getattr(rem_pos, 'qty_available', None) or rem_pos.qty)
        place_stop(tc, symbol, rem_qty, new_stop_price, is_crypto)
        return True
    except Exception as e:
        print(f"   ⚠️ Scale-out parțial amânat pe {symbol}: {e}")
        return False


def ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, cost_rt, pos_mem):
    entry = float(pos.avg_entry_price)
    avail_qty = float(getattr(pos, 'qty_available', None) or pos.qty)
    total_qty = float(pos.qty)
    live_price = float(pos.current_price) if getattr(pos, 'current_price', None) else m['price']
    profit = live_price / entry - 1.0

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m))

    # 1. Take Partials 50% + SL to BE (Pag. 254, 273)
    scaled_out = pos_mem.get('scaled_out', False)
    partial_target = cost_rt + init_stop_pct * DNA['partial_tp_r']
    if DNA['use_partial_tp'] and not scaled_out and profit >= partial_target:
        lock_stop = min(live_price * 0.994, max(entry * (1.0 + cost_rt + 0.0015),
                                                m['peak_since_entry'] - DNA['trail_atr_mult'] * m['atr']))
        if execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, lock_stop):
            pos_mem['scaled_out'] = True
            return lock_stop

    # 2. FAST FLOOR SHIELD: dacă o poziție e deja pe plus (>= +0.30%, ex: PAXGUSD și SPY),
    # ridicăm imediat stop-ul la cel mult -0.60% sub intrare!
    max_allowed_floor = min(entry * (1.0 - init_stop_pct), live_price * (1.0 - init_stop_pct))
    if profit >= 0.0030:
        tighter_floor = entry * (1.0 - 0.0060)
        max_allowed_floor = max(max_allowed_floor, tighter_floor)

    new_stop = max(cur_stop, max_allowed_floor) if cur_stop else max_allowed_floor

    # 3. SL to BE & Trailing Stop adaptiv (mai strâns dacă prețul a ajuns în Extreme Premium > 70%)
    already_in_profit_stop = (cur_stop is not None and cur_stop >= entry) or scaled_out
    if profit >= init_stop_pct * DNA['breakeven_trigger_r'] or already_in_profit_stop:
        peak_val = max(live_price, m['peak_since_entry'])
        eff_trail_mult = DNA['trail_atr_mult'] * (0.75 if m['pd_pos'] >= 0.70 else 1.0)
        trail = peak_val - eff_trail_mult * m['atr']
        new_stop = max(new_stop, trail, entry * (1.0 + cost_rt))

    new_stop = min(new_stop, live_price * 0.994)

    if cur_stop is not None:
        new_stop = max(new_stop, cur_stop)
        if new_stop <= cur_stop * 1.0020:
            return cur_stop
        tc.cancel_order_by_id(cur.id)
        time.sleep(1.5)
        try:
            fresh_pos = tc.get_open_position(pos_symbol)
            q_to_stop = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
            placed = place_stop(tc, symbol, q_to_stop, new_stop, is_crypto)
            print(f"   🛡️ [SL ACTUALIZAT V9.1] {symbol}: {cur_stop:.4f} -> {placed}")
            return placed
        except Exception as e:
            print(f"   ⚠️ Stop nou respins ({e}); refac stop-ul anterior")
            fresh_pos = tc.get_open_position(pos_symbol)
            q_to_stop = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
            place_stop(tc, symbol, q_to_stop, min(cur_stop, live_price * 0.992), is_crypto)
            return cur_stop

    q_initial = avail_qty if avail_qty > 0 else total_qty
    placed = place_stop(tc, symbol, q_initial, new_stop, is_crypto)
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
    market_val = abs(float(getattr(pos, 'market_value', 20000.0) or 20000.0))
    live_price = float(pos.current_price) if getattr(pos, 'current_price', None) else m['price']
    m['price'] = live_price
    profit = live_price / entry - 1.0
    cost_rt = round_trip_cost(is_crypto)

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m))
    stage_tag = "50% Runner" if pos_mem.get('scaled_out') else "100%"
    pd_zone = "Discount" if m['pd_pos'] <= 0.5 else "Premium"

    print(f"💼 [DEȚINUT {stage_tag}] {symbol:<8} | Val: {market_val:7.0f}$ | Live: {live_price:<10.4f} "
          f"| P/L: {profit*100:+.2f}% | Stop: {cur_stop or 0:<9.4f} | Zona: {pd_zone}({m['pd_pos']*100:.0f}%)")

    reason = "Închidere înainte de bell (fără risc overnight)" if flatten_now \
        else exit_decision(profit, m, init_stop_pct, cost_rt, market_val=market_val)
    if reason:
        print(f"   🔔 [IEȘIRE V9.1] {symbol}: {reason} ({profit*100:+.2f}%)")
        return close_position_safe(tc, symbol, pos_symbol)

    try:
        ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, cost_rt, pos_mem)
    except Exception as e:
        print(f"   ⚠️ Nu am putut actualiza stop-ul pentru {symbol}: {e}")
    return False


def position_size_usd(equity, cash, gross, init_stop_pct, score, loss_streak=0):
    risk_pct = DNA['high_conviction_risk'] if score >= 82 else DNA['base_risk_per_trade']
    if loss_streak >= 2:
        risk_pct *= DNA['losing_streak_factor']
    by_risk = equity * risk_pct / init_stop_pct
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
        if qty * px < DUST_THRESHOLD_USD:
            continue
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
# 7. DATE DE PIAȚĂ LA 5 MINUTE + LIVE
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
        if len(df) > 100 and df.index[-1] + bar_td > now:
            df_ind = df.iloc[:-1]
        else:
            df_ind = df
        if len(df_ind) < 90:
            continue
        if not is_stock and (now - df.index[-1] > bar_td * stale_bars):
            continue
        out[s] = (compute_indicators(df_ind), live_price, live_high)
    return out


# ==========================================
# 8. CICLUL PRINCIPAL V9.1 (SMC & ICT)
# ==========================================
def process_symbol(tc, symbol, pack, is_crypto, is_safe, held, ctx):
    ind, live_price, live_high = pack
    pos_symbol = norm(symbol)
    pos_mem = ctx['pos_memory'].setdefault(pos_symbol, {'entry_time': None, 'scaled_out': False})
    btc_ret = ctx.get('btc_ret_2h') if (is_crypto and symbol != "BTC/USD" and not is_safe) else None

    m = signal_at(
        ind, len(ind) - 1, ctx['social'].get(symbol, 0),
        live_price=live_price, live_high=live_high,
        btc_ret_2h=btc_ret, entry_time=pos_mem.get('entry_time'),
        kz_bonus=ctx['kz_bonus']
    )
    if is_safe and ctx['risk_off'] and m['setup']:
        m['score'] += 15
        m['reason'] += " + 🛡️ Safe-Haven Gold/Bonds (Pag. 188)"

    if pos_symbol in held:
        flatten = (not is_crypto) and ctx['flatten_stocks']
        if manage_position(tc, symbol, held[pos_symbol], m, is_crypto, pos_mem, flatten_now=flatten):
            ctx['closed'].add(pos_symbol)
            ctx['cooldown_syms'].add(pos_symbol)
        return

    if pos_symbol in ctx['cooldown_syms']:
        return
    if ctx['entries_blocked'] or (not is_crypto and not ctx['stock_entries_ok']):
        return
    if ctx['risk_off'] and not is_safe:
        return
    if not entry_allowed(m, round_trip_cost(is_crypto), is_asian_accum=ctx['is_asian_accum']):
        return

    print(f"🟢 [SEMNAL {m['setup']}] {symbol:<8} | Live: {m['price']:.4f} | Scor: {m['score']} | {m['reason']}")
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
            invest = position_size_usd(equity, cash, gross, t['init_stop_pct'], t['score'], ctx['loss_streak'])
            if invest < MIN_POSITION_USD:
                continue
            sym, pos_sym = t['symbol'], t['pos_symbol']
            print(f"🎯 [INTRARE V9.1 SMC/ICT] {sym}: ~{invest:.0f}$ | Stop: -{t['init_stop_pct']*100:.2f}% "
                  f"| Scor {t['score']} ({t['reason']})")

            if not submit_smart_buy(tc, sym, invest, t['price'], t['is_crypto']):
                continue

            n_open += 1
            crypto_open += 1 if t['is_crypto'] else 0
            gross += invest
            ctx['pos_memory'][pos_sym] = {'entry_time': datetime.now(timezone.utc), 'scaled_out': False}
            time.sleep(1.5)
            try:
                pos = tc.get_open_position(pos_sym)
                q_avail = float(getattr(pos, 'qty_available', None) or pos.qty)
                place_stop(tc, sym, q_avail,
                           float(pos.avg_entry_price) * (1.0 - t['init_stop_pct']), t['is_crypto'])
            except Exception as e:
                print(f"   ⚠️ Stop-ul pentru {sym} va fi pus la ciclul următor ({e})")
        except Exception as e:
            print(f"   ⚠️ Intrare eșuată pe {t['symbol']}: {e}")


def run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val, pos_memory, cooldown_syms, loss_streak, show_scorecard=False):
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

    held = get_active_positions_and_sweep_dust(tc)
    now = datetime.now(timezone.utc)
    kz_name, kz_bonus, is_asian_accum = get_ict_session_info(now)

    print(f"\n⚡ Scanare V9.1 ({kz_name}): {now.strftime('%H:%M:%S UTC')} "
          f"| Equity: {equity:,.2f}$ | Poziții reale: {len(held)}/{DNA['max_positions']} | Kill-switch: {death_threshold:,.2f}$ | Azi: {daily_pnl*100:+.2f}%")
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
            f.write(f"Killed at {now} with equity {equity:.2f}$ (peak {peak:.2f}$)\n")
        sys.exit(0)

    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=now - timedelta(days=2))).df
    crypto_packs = prepare_symbol_frames(crypto_bars, ALL_CRYPTO, now, stale_bars=6, is_stock=False)

    risk_off = False
    btc_ret_2h = 0.0
    btc_pack = crypto_packs.get("BTC/USD")
    if btc_pack is not None:
        btc_ind, btc_live, _ = btc_pack
        if len(btc_ind) > 6:
            lookback = max(1, int(30 / DNA['bar_minutes']))
            btc_change = float(btc_live / btc_ind['close'].iloc[-lookback] - 1.0)
            btc_ret_2h = float(btc_ind['ret_2h'].iloc[-1])
            if btc_change < -0.012 or fear_greed_val <= 22:
                risk_off = True
                print(f"🛡️ [MOD REFUGIU - Pag. 188] BTC {btc_change*100:+.2f}% (30m), F&G {fear_greed_val}: doar Aur/Obligațiuni.")

    ctx = {
        'social': social_scores, 'risk_off': risk_off, 'entries_blocked': entries_blocked,
        'closed': set(), 'candidates': [], 'stock_entries_ok': False, 'flatten_stocks': False,
        'pos_memory': pos_memory, 'cooldown_syms': cooldown_syms, 'btc_ret_2h': btc_ret_2h,
        'kz_bonus': kz_bonus, 'is_asian_accum': is_asian_accum, 'loss_streak': loss_streak
    }

    for symbol, pack in crypto_packs.items():
        process_symbol(tc, symbol, pack, True, symbol in SAFE_HAVEN_CRYPTO, held, ctx)

    clock = tc.get_clock()
    if clock.is_open:
        mtc = (clock.next_close - clock.timestamp).total_seconds() / 60.0
        ctx['stock_entries_ok'] = 30 <= mtc <= 380
        ctx['flatten_stocks'] = FLATTEN_STOCKS_BEFORE_CLOSE and mtc <= FLATTEN_MIN
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=now - timedelta(days=7),
            feed=DataFeed.IEX)).df
        stock_packs = prepare_symbol_frames(stock_bars, ALL_STOCKS, now, stale_bars=8, is_stock=True)
        for symbol, pack in stock_packs.items():
            process_symbol(tc, symbol, pack, False, symbol in SAFE_HAVEN_STOCKS, held, ctx)

    if ctx['candidates'] and not entries_blocked:
        open_new_positions(tc, ctx, held, equity)


# ==========================================
# 9. MAIN
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    if not API_KEY or not SECRET_KEY:
        print("❌ EROARE: ALPACA_API_KEY / ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
        sys.exit(1)

    tc = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    crypto_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    print(f"🚀 AGENT V9.1 SMC & ICT pornit ({'PAPER' if PAPER else '⚠️ LIVE'}) | Liquidity Sweep + FVG + OB + Limit Entry | "
          f"cost round-trip crypto {round_trip_cost(True)*100:.2f}%")
    social_scores, fear_greed_val = scan_social_media_and_sentiment()
    pos_memory, cooldown_syms, loss_streak = build_position_memory_and_cooldowns(tc)
    if cooldown_syms:
        print(f"🧊 Monede în Cooldown temporar: {', '.join(sorted(cooldown_syms))}")
    if loss_streak >= 2:
        print(f"🛡️ [PROTECȚIE SERIE PIERZĂTOARE - Pag. 137] {loss_streak} pierderi recente: reducem miza la 65% până la primul câștig.")

    for i in range(CYCLES_PER_RUN):
        print(f"\n================ CICLUL {i + 1}/{CYCLES_PER_RUN} ================")
        try:
            run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val,
                      pos_memory, cooldown_syms, loss_streak, show_scorecard=(i == 0))
        except SystemExit:
            raise
        except Exception as e:
            print(f"⚠️ Eroare temporară în ciclul {i + 1}: {e}")
        if i < CYCLES_PER_RUN - 1:
            time.sleep(CYCLE_SLEEP_SEC)


if __name__ == "__main__":
    main()
