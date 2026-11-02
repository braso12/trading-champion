#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADING CHAMPION V11.0 — ORIENTFX INSTITUTIONAL EDITION (SMC + ICT + Order Flow)

NOU în V11.0 (bazat pe manualul instituțional "Beyond The Markets - OrientFX"):
  1. DEBLOCARE PORTOFOLIU & EXPUNERE DINAMICĂ:
     - MAX_TOTAL_EXPOSURE = 75% (era 40%, care bloca toate intrările la 38.2% expunere cu "FĂRĂ LOC").
     - MAX_SINGLE_EXPOSURE = 18%, MAX_POSITIONS = 5, MIN_POSITION_USD = 3,500$.
     - Raportare completă a pozițiilor deținute la fiecare ciclu (inclusiv acțiuni când bursa NY e închisă).
  2. STOP-LOSS SCALAT REAL LA ATR (FĂRĂ PRAGUL FIX DE 1.8% PE ACȚIUNI) & REPARARE BUG TRAILING:
     - Pe 15m, SPY/QQQ/TLT/GLD au ATR de 0.15%-0.25%. Vechiul min_stop_pct=1.8% era 8x-12x ATR (ținte imposibile intraday).
     - Acum: stop_atr_mult = 2.2 cu limite dedicate Acțiuni/ETF (0.40% - 1.10%) vs Crypto (1.80% - 3.20%).
     - REPARAT BUG CRITIC în compute_trailing_stop: plafonul `live_price * 0.992` (-0.80%) împingea stopul de Breakeven
       ÎNAPOI PE MINUS pe orice activ cu stop < 0.80%! Acum bufferul tehnic este `live_price * 0.9988` (-0.12%).
  3. MANAGEMENT INSTITUȚIONAL AL PROFITULUI (OrientFX Pag. 130, 261):
     - Take-Partial 30% la +1.40R (mută SL la Breakeven + costuri).
     - Runner-ul de 70% are țintă completă de Take-Profit la +2.50R (Fib -0.618 / Opposing Liquidity) și trailing ATR 2.4x.
  4. SELECȚIE SNIPER ÎN H1 DISCOUNT POCKET & FILTRU WATERFALL (OrientFX Pag. 33, 50-52, 68, 206-222, 243-262):
     - Cumpără DOAR în trend H1 crescător (EMA80 > EMA200 cu pantă pozitivă), când prețul a făcut pullback în
       buzunarul de valoare H1 (sub EMA80, aproape de EMA200).
     - Filtru Waterfall vs Pullback Controlat: scăderea pe ultimele 12-16 bare trebuie să fie între -0.6 ATR și -4.5 ATR
       (blochează prăbușirile libere >4.5 ATR și stagnarea fără manipulare).
     - Fereastră Killzone NY pentru Acțiuni: 09:55 - 14:45 NY (evită capcana Judas Swing de la deschiderea 09:30-09:55 NY).
     - Eliminat PAXG/USD (ATR 0.10% vs comision crypto 0.56% = pierdere garantată); aurul se tranzacționează prin GLD (0% comision).
     - Crypto se tranzacționează DOAR în regim Macro Bull confirmat atât pe BTC/USD cât și pe simbol.

Rezultat Backtest 30 zile (17 simboluri, comisioane + slippage incluse):
  - V10.3: 229 tranzacții | Win Rate 34.9% | Profit Factor 0.34 | Suma: -198.4%
  - V11.0:  11 tranzacții | Win Rate 72.7% | Profit Factor 5.01 | Suma: +8.2% (Train PF=3.54, Test OOS PF=99.90)
"""
import os
import re
import sys
import time
import json
import uuid
import subprocess
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
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

# ==========================================
# 1. CONFIGURARE V11.0 ORIENTFX
# ==========================================
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "100000.0"))

INVESTED_MAX_LOSS_PCT = float(os.environ.get("INVESTED_MAX_LOSS_PCT", "0.20"))  # Moare și învață la -20% din banii băgați în poziții
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.025"))   # Pauză la -2.5% pe zi
AUTO_REBIRTH = os.environ.get("AUTO_REBIRTH", "true").lower() != "false"

CYCLES_PER_RUN = int(os.environ.get("CYCLES_PER_RUN", "90"))
CYCLE_SLEEP_SEC = int(os.environ.get("CYCLE_SLEEP_SEC", "45"))

MEMORY_FILE = "LEARNED_MEMORY.json"
DUST_THRESHOLD_USD = 5.0
MICRO_POS_USD = 2500.0
MIN_POSITION_USD = 3500.0

BLACKLIST_HOURS = 72          # cât durează un ban după rebirth
SETUP_WINDOW_DAYS = 7         # fereastra pentru statistica pe setup
SETUP_MIN_TRADES = 8          # minim de tranzacții înainte să dezactivăm un setup
SETUP_MIN_PF = 0.85           # profit factor sub care un setup e dezactivat
PEAK_PERSIST_STEP = 0.005     # commit pe GitHub când peak-ul urcă cu >= 0.5%
BACKTEST_FILE = "BACKTEST_REPORT.json"
USE_BACKTEST_GATE = os.environ.get("USE_BACKTEST_GATE", "true").lower() != "false"

# Alpaca crypto Tier 1: taker 0.25%, maker 0.15% (scade la 0.22% taker peste 100k$ volum / 30 zile)
CRYPTO_FEE_PCT = float(os.environ.get("CRYPTO_FEE_PCT", "0.0025"))
CRYPTO_SLIPPAGE_PCT = 0.0003
STOCK_SLIPPAGE_PCT = 0.0002

CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "LINK/USD", "AVAX/USD", "LTC/USD", "UNI/USD"]
STOCK_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "META"]
SAFE_HAVEN_STOCKS = ["GLD", "TLT"]
# PAXG/USD eliminat în V11: ATR 15m de doar 0.10% vs 0.56% comision crypto = expectanță negativă; folosim GLD (0% comision)
SAFE_HAVEN_CRYPTO = []
ALL_CRYPTO = CRYPTO_SYMBOLS + SAFE_HAVEN_CRYPTO
ALL_STOCKS = STOCK_SYMBOLS + SAFE_HAVEN_STOCKS

# Grupuri corelate: nu cumpărăm niciodată două active din același grup simultan
CORRELATED_GROUPS = [
    {"GLD", "PAXGUSD"},
    {"SPY", "QQQ"},
    {"NVDA", "AMD"},
]

FLATTEN_STOCKS_BEFORE_CLOSE = False
FLATTEN_MIN = 10
NY_TZ = ZoneInfo("America/New_York")

DNA = {
    'bar_minutes': 15,
    'ema_fast': 9,
    'ema_slow': 21,
    'ema_trend': 50,
    'atr_period': 14,
    'min_confluences': 4,
    'entry_score_min': 76,
    'asian_accumulation_score': 84,
    'crypto_score_premium': 4,     # crypto plătește ~0.56% round-trip -> cere semnal de calitate superioară
    'chase_atr_mult': 0.5,         # nu intrăm dacă prețul live e > închidere semnal + 0.5 ATR

    'base_risk_per_trade': 0.006,
    'high_conviction_risk': 0.009,
    'losing_streak_factor': 0.65,
    'max_positions': int(os.environ.get("MAX_POSITIONS", "5")),
    'max_crypto_positions': int(os.environ.get("MAX_CRYPTO_POSITIONS", "2")),
    'max_single_exposure': float(os.environ.get("MAX_SINGLE_EXPOSURE", "0.18")),
    'max_total_exposure': float(os.environ.get("MAX_TOTAL_EXPOSURE", "0.75")),

    'stop_atr_mult': 2.2,
    'min_stop_pct': 0.0040,        # Prag minim ATR pentru Acțiuni/ETF-uri (0.40%)
    'max_stop_pct': 0.0110,        # Prag maxim ATR pentru Acțiuni/ETF-uri (1.10%)
    'crypto_min_stop_pct': 0.0180, # Prag minim ATR pentru Crypto (1.80%)
    'crypto_max_stop_pct': 0.0320, # Prag maxim ATR pentru Crypto (3.20%)
    'legacy_cut_pct': -0.025,
    'trail_atr_mult': 2.4,
    'breakeven_trigger_r': 1.50,
    'half_risk_trigger_r': 1.10,

    'use_partial_tp': True,
    'partial_tp_r': 1.40,
    'partial_tp_frac': 0.30,       # 30% take-partial la +1.40R, 70% runner până la +2.50R
    'full_tp_r': 2.50,             # Țintă completă OrientFX (+2.50R / Fib -0.618)
    'max_hold_bars': 44,           # Limită de timp swing eficient (44 bare de 15m)
    'spike_harvest_pct': 0.028,
    'use_exhaustion_exit': True,
    'exhaustion_min_r': 1.80,
    'soft_stop_frac_news': 0.5,
    'vol_cost_mult': 3.5,
    'reentry_cooldown_min': 60,
}

BULLISH_WORDS = ['surge', 'soar', 'bull', 'bullish', 'breakout', 'moon', 'rally', 'pump', 'ath',
                 'upgrade', 'record', 'adoption', 'approval', 'beats', 'inflow', 'accumulate', 'partner']
BEARISH_WORDS = ['crash', 'dump', 'bear', 'bearish', 'hack', 'hacked', 'ban', 'lawsuit', 'plunge',
                 'panic', 'liquidation', 'fraud', 'bankruptcy', 'sec', 'misses', 'downgrade', 'outflow']
BULL_RE = re.compile(r"\b(" + "|".join(BULLISH_WORDS) + r")\b")
BEAR_RE = re.compile(r"\b(" + "|".join(BEARISH_WORDS) + r")\b")
ALIASES = {
    'BTC': ['btc', 'bitcoin'], 'ETH': ['eth', 'ethereum'], 'SOL': ['solana', 'sol'],
    'XRP': ['xrp', 'ripple'], 'AVAX': ['avax', 'avalanche'], 'LINK': ['chainlink'],
    'LTC': ['ltc', 'litecoin'], 'UNI': ['uniswap', 'uni'], 'PAXG': ['paxg', 'gold'],
    'NVDA': ['nvda', 'nvidia'], 'TSLA': ['tsla', 'tesla'], 'AAPL': ['aapl', 'apple'],
    'AMD': ['amd'], 'META': ['meta'], 'SPY': ['spy'], 'QQQ': ['qqq', 'nasdaq'],
    'GLD': ['gld'], 'TLT': ['tlt'],
}


# ==========================================
# 2. MEMORIE PERSISTENTĂ, ÎNVĂȚARE & REPORNIRE AUTOMATĂ (PHOENIX)
# ==========================================
def norm(sym):
    return sym.replace("/", "")


CRYPTO_NORM = {norm(s) for s in ALL_CRYPTO}


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


def net_trade_pnl(sym, avg_entry, exit_px, qty):
    """P/L în $ net de comision (crypto plătește CRYPTO_FEE_PCT la intrare ȘI la ieșire)."""
    pnl = (exit_px - avg_entry) * qty
    if sym in CRYPTO_NORM:
        pnl -= CRYPTO_FEE_PCT * (avg_entry + exit_px) * qty
    return pnl


def parse_setup_tag(client_order_id):
    """client_order_id = 'TC-<SETUP>-<uuid>' -> '<SETUP>' (altfel None)."""
    cid = str(client_order_id or "")
    if cid.startswith("TC-"):
        parts = cid.split("-")
        if len(parts) >= 3 and parts[1]:
            return parts[1]
    return None


def active_blacklist(learned_mem, now=None):
    """Returnează setul de simboluri banate ACUM și curăță ban-urile expirate."""
    now = now or datetime.now(timezone.utc)
    active, keep = set(), {}
    for sym, until in (learned_mem.get("blacklist_until") or {}).items():
        try:
            dt = datetime.fromisoformat(str(until))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt > now:
            active.add(sym)
            keep[sym] = until
    learned_mem["blacklist_until"] = keep
    return active


def add_to_blacklist(learned_mem, syms, hours=BLACKLIST_HOURS):
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    bl = learned_mem.setdefault("blacklist_until", {})
    for s in syms:
        bl[s] = until


def load_learned_memory(current_equity):
    """Încarcă memoria din LEARNED_MEMORY.json și inițializează baza Generației curente."""
    default_mem = {
        "generation": 1,
        "generation_start_equity": round(current_equity, 2),   # = vârful de equity al generației (trailing peak)
        "avg_invested_capital": 60000.0,
        "blacklist_until": {},
        "disabled_setups": [],
        "preferred_symbols": ["SPY", "QQQ", "GLD", "BTCUSD", "NVDA", "AAPL", "TSLA", "TLT"],
        "score_boost_req": 0,
        "min_stop_override": DNA['min_stop_pct'],
        "partial_tp_r_override": DNA['partial_tp_r'],
        "lessons_learned": [],
        "autopsy_history": []
    }
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_mem.update(saved)
        except Exception:
            pass
    else:
        save_memory_file(default_mem)

    # Migrare din V10.1: lista veche de ban-uri permanente -> ban-uri cu expirare
    old_list = default_mem.pop("blacklisted_symbols", None)
    if old_list and not default_mem.get("blacklist_until"):
        add_to_blacklist(default_mem, old_list)

    if current_equity > float(default_mem.get("generation_start_equity", current_equity)):
        default_mem["generation_start_equity"] = round(current_equity, 2)

    # Migrare V10.3 -> V11.0: ignoră vechiul min_stop_override=0.018 care strica stopul ATR pe acțiuni
    saved_stop_ov = float(default_mem.get("min_stop_override", DNA['min_stop_pct']))
    if saved_stop_ov >= 0.010:
        saved_stop_ov = DNA['min_stop_pct']
        default_mem["min_stop_override"] = saved_stop_ov

    DNA['entry_score_min'] = min(82, max(DNA['entry_score_min'], 76 + int(default_mem.get("score_boost_req", 0))))
    DNA['min_stop_pct'] = max(DNA['min_stop_pct'], saved_stop_ov)
    DNA['partial_tp_r'] = max(DNA['partial_tp_r'], float(default_mem.get("partial_tp_r_override", DNA['partial_tp_r'])))
    return default_mem


def save_memory_file(learned_mem):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(learned_mem, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"   ⚠️ Eroare salvare {MEMORY_FILE}: {e}")


def persist_files_to_github(files, commit_msg):
    """Commit & push automat, ca memoria să supraviețuiască între rulări (workflow-ul are nevoie de contents: write)."""
    def _run(*args):
        return subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _run("git", "config", "user.name", "TradingChampionBot")
        _run("git", "config", "user.email", "bot@users.noreply.github.com")
        _run("git", "add", *files)
        _run("git", "commit", "-m", f"{commit_msg} [skip ci]")
        _run("git", "pull", "--rebase", "--autostash")
        _run("git", "push")
    except Exception:
        pass


def analyze_closed_trades_and_learn(tc, learned_mem):
    """
    Citește ultimele 500 de ordine închise și învață:
      - banuri pe simbol (doar pe date RECENTE, expiră singure);
      - statistică pe SETUP (via client_order_id) -> dezactivează setup-urile cu expectanță negativă.
    """
    mem = {}
    now = datetime.now(timezone.utc)
    cooldown_syms = active_blacklist(learned_mem, now)
    portfolio_loss_streak = 0
    sym_stats = {}
    setup_stats = {}
    setup_of = {}
    win_recent = timedelta(hours=72)
    win_setup = timedelta(days=SETUP_WINDOW_DAYS)
    win_loss_streak = timedelta(hours=48)

    try:
        closed = tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500))
        filled = [o for o in closed if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0
                  and o.filled_at is not None]
        filled.sort(key=lambda o: o.filled_at)

        book = {}
        for o in filled:
            sym = norm(o.symbol)
            px = float(o.filled_avg_price)
            qty = float(o.filled_qty)
            val = qty * px
            if val < DUST_THRESHOLD_USD:
                continue

            st = sym_stats.setdefault(sym, {
                'wins': 0, 'losses': 0, 'net_usd': 0.0, 'streak_losses': 0, 'invested': 0.0,
                'recent_n': 0, 'recent_wins': 0, 'recent_net': 0.0, 'last_loss_at': None})
            b = book.setdefault(sym, [0.0, 0.0])

            if o.side == OrderSide.BUY:
                mem[sym] = {'entry_time': o.filled_at, 'scaled_out': False, 'last_sell_time': None}
                b[0] += qty
                b[1] += val
                st['invested'] = max(st['invested'], b[1])
                tag = parse_setup_tag(getattr(o, 'client_order_id', None))
                if tag:
                    setup_of[sym] = tag
            elif o.side == OrderSide.SELL and b[0] > 1e-7:
                mem.setdefault(sym, {'entry_time': None, 'scaled_out': True, 'last_sell_time': o.filled_at})
                mem[sym]['scaled_out'] = True
                mem[sym]['last_sell_time'] = o.filled_at

                sell_qty = min(qty, b[0])
                avg_entry = b[1] / b[0]
                pnl_usd = net_trade_pnl(sym, avg_entry, px, sell_qty)
                b[0] -= sell_qty
                b[1] -= avg_entry * sell_qty
                age = now - o.filled_at

                st['net_usd'] += pnl_usd
                if pnl_usd > 0:
                    st['wins'] += 1
                    st['streak_losses'] = 0
                    portfolio_loss_streak = 0
                else:
                    st['losses'] += 1
                    st['streak_losses'] += 1
                    st['last_loss_at'] = o.filled_at
                    portfolio_loss_streak += 1

                if age < win_recent:
                    st['recent_n'] += 1
                    st['recent_net'] += pnl_usd
                    if pnl_usd > 0:
                        st['recent_wins'] += 1

                if age < win_setup:
                    ss = setup_stats.setdefault(setup_of.get(sym, "UNKNOWN"),
                                                {'wins': 0, 'losses': 0, 'gross_win': 0.0, 'gross_loss': 0.0})
                    if pnl_usd > 0:
                        ss['wins'] += 1
                        ss['gross_win'] += pnl_usd
                    else:
                        ss['losses'] += 1
                        ss['gross_loss'] += abs(pnl_usd)

                if age.total_seconds() < DNA['reentry_cooldown_min'] * 60:
                    cooldown_syms.add(sym)

        # --- BAN PE SIMBOL: doar dacă pierderile sunt RECENTE (se debanează singur după 48-72h) ---
        newly_banned = []
        for sym, st in sym_stats.items():
            if sym in ("BTCUSD", "PAXGUSD", "SPY", "QQQ", "GLD"):
                continue
            recent_loss = st['last_loss_at'] is not None and (now - st['last_loss_at']) < win_loss_streak
            recent_wr = (st['recent_wins'] / st['recent_n']) if st['recent_n'] > 0 else 0.5
            bad_streak = st['streak_losses'] >= 3
            bad_recent = st['recent_n'] >= 4 and st['recent_net'] <= -250.0 and recent_wr < 0.35
            if recent_loss and (bad_streak or bad_recent):
                if sym not in cooldown_syms:
                    newly_banned.append(f"{sym} ({st['recent_net']:+.0f}$)")
                cooldown_syms.add(sym)
        if newly_banned:
            print(f"🧠 [AUTO-ÎNVĂȚARE] Monede blocate temporar pentru pierderi recente: {', '.join(newly_banned)}")

        # --- STATISTICĂ PE SETUP: dezactivăm ce pierde de fapt bani ---
        disabled = []
        for tag, ss in sorted(setup_stats.items()):
            n = ss['wins'] + ss['losses']
            pf = (ss['gross_win'] / ss['gross_loss']) if ss['gross_loss'] > 0 else (99.0 if ss['gross_win'] > 0 else 0.0)
            net = ss['gross_win'] - ss['gross_loss']
            flag = ""
            if tag != "UNKNOWN" and n >= SETUP_MIN_TRADES and pf < SETUP_MIN_PF and net < 0:
                disabled.append(tag)
                flag = " ⛔ DEZACTIVAT"
            print(f"   📈 [SETUP {tag}] {n} tranzacții/{SETUP_WINDOW_DAYS}z | Net: {net:+.0f}$ | PF: {pf:.2f}{flag}")
        learned_mem["disabled_setups"] = sorted(disabled)
    except Exception as e:
        print(f"   ⚠️ Eroare la analiza istoricului: {e}")

    return mem, cooldown_syms, portfolio_loss_streak, sym_stats


def execute_death_learn_and_rebirth(tc, equity, invested_ref, loss_usd, sym_stats, learned_mem, cooldown_syms, trigger_reason):
    """
    Declanșat când agentul pierde 20% din banii băgați în poziții:
    1. Închide toate pozițiile curente.
    2. Analizează ce monede au produs pierderea și le interzice 72h.
    3. Își întărește filtrele (scor minim, stop-loss, take-profit).
    4. Resetează baza pentru noua Generație și REPORNEȘTE SINGUR.
    """
    print("\n" + "=" * 75)
    print(f"🚨🧠 [MOARTE, ÎNVĂȚARE & REPORNIRE AUTOMATĂ] Motiv: {trigger_reason}")
    print("=" * 75)

    bad_coins = [sym for sym, st in sym_stats.items() if st['recent_net'] < -100.0 and sym not in ("SPY", "GLD")]
    try:
        for p in tc.get_all_positions():
            s = norm(p.symbol)
            upl = float(getattr(p, 'unrealized_pl', 0.0) or 0.0)
            if upl < -80.0 and s not in ("SPY", "GLD"):
                bad_coins.append(s)
    except Exception:
        pass
    bad_coins = sorted(list(set(bad_coins)))
    good_coins = [sym for sym, st in sym_stats.items() if st['recent_net'] > 0]

    new_gen = int(learned_mem.get("generation", 1)) + 1
    lessons = [
        f"Gen #{new_gen - 1} s-a încheiat la {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}: {trigger_reason}.",
        f"Monede interzise {BLACKLIST_HOURS}h în urma autopsiei: {', '.join(bad_coins) or 'niciuna (pierderea a venit din mai multe poziții mici)'}.",
        f"Setup-uri dezactivate acum: {', '.join(learned_mem.get('disabled_setups', [])) or 'niciunul'}.",
        f"Noua Generație #{new_gen} pornește de la Equity {equity:,.2f}$ cu filtre mai stricte (+2 pct scor, stop lărgit).",
    ]

    learned_mem["generation"] = new_gen
    learned_mem["generation_start_equity"] = round(equity, 2)
    add_to_blacklist(learned_mem, bad_coins)
    if good_coins:
        learned_mem["preferred_symbols"] = sorted(list(set(learned_mem.get("preferred_symbols", []) + good_coins)))
    learned_mem["score_boost_req"] = min(6, int(learned_mem.get("score_boost_req", 0)) + 2)
    learned_mem["min_stop_override"] = round(min(0.0065, float(learned_mem.get("min_stop_override", 0.0040)) + 0.0005), 4)
    learned_mem["partial_tp_r_override"] = round(min(1.80, float(learned_mem.get("partial_tp_r_override", 1.40)) + 0.10), 2)
    learned_mem["lessons_learned"] = lessons
    learned_mem.setdefault("autopsy_history", []).append({
        "timestamp": str(datetime.now(timezone.utc)),
        "new_generation": new_gen,
        "equity_at_rebirth": round(equity, 2),
        "invested_capital_ref": round(invested_ref, 2),
        "loss_on_invested_usd": round(loss_usd, 2),
        "trigger": trigger_reason,
        "banned": bad_coins,
        "disabled_setups": list(learned_mem.get("disabled_setups", [])),
    })
    learned_mem["autopsy_history"] = learned_mem["autopsy_history"][-30:]

    for line in lessons:
        print(f"   📘 [LECȚIE ÎNVĂȚATĂ] {line}")

    print("   🧹 Lichidez toate pozițiile vechi pentru a reporni curat în noua Generație...")
    try:
        tc.cancel_orders()
        time.sleep(1.5)
        tc.close_all_positions(cancel_orders=True)
        time.sleep(2.0)
    except Exception as e:
        print(f"   ⚠️ Eroare la lichidare: {e}")

    cooldown_syms.update(bad_coins)
    DNA['entry_score_min'] = min(82, 76 + int(learned_mem["score_boost_req"]))
    DNA['min_stop_pct'] = float(learned_mem["min_stop_override"])
    DNA['partial_tp_r'] = float(learned_mem["partial_tp_r_override"])

    save_memory_file(learned_mem)
    persist_files_to_github([MEMORY_FILE], f"🔥 Auto-Rebirth Gen #{new_gen} & învățare din -20% investit")
    print(f"🔥 [REPORNIRE AUTOMATĂ COMPLETĂ] Agentul rulează acum în Generația #{new_gen} (Scor min: {DNA['entry_score_min']})!")


# ==========================================
# 3. CURĂȚARE PRAF, CORELAȚII & SESIUNI ICT
# ==========================================
def is_correlated_with_held(pos_symbol, held_symbols):
    """Previne cumpărarea simultană de active din același grup corelat (GLD+PAXG, SPY+QQQ, NVDA+AMD)."""
    for grp in CORRELATED_GROUPS:
        if pos_symbol in grp and any(h in grp for h in held_symbols):
            return True
    return False


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
                except Exception:
                    pass
            continue
        real_held[sym] = p
    return real_held


def round_trip_cost(is_crypto):
    if is_crypto:
        return 2.0 * (CRYPTO_FEE_PCT + CRYPTO_SLIPPAGE_PCT)
    return 2.0 * STOCK_SLIPPAGE_PCT


def stop_distance_pct(m, is_crypto=False):
    if is_crypto or m.get('is_crypto', False):
        raw = max(DNA['stop_atr_mult'] * 1.25 * m['atr'] / m['price'], 0.0)
        return clamp(raw, DNA['crypto_min_stop_pct'], DNA['crypto_max_stop_pct'])
    raw = DNA['stop_atr_mult'] * m['atr'] / m['price']
    return clamp(raw, DNA['min_stop_pct'], DNA['max_stop_pct'])


def filter_regular_stock_hours(df):
    if df.empty:
        return df
    ny_times = df.index.tz_convert(NY_TZ)
    mask = (ny_times.weekday < 5) & (ny_times.time >= dtime(9, 30)) & (ny_times.time < dtime(16, 0))
    return df.loc[mask]


# ==========================================
# 4. RADAR SOCIAL MEDIA & ȘTIRI
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
# 5. MOTORUL SMC + ICT ORIENTFX PE 15 MINUTE
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

    ind['ema_1h_fast'] = c.ewm(span=80, adjust=False).mean()
    ind['ema_1h_slow'] = c.ewm(span=200, adjust=False).mean()
    ind['ema_1h_slow_slope'] = (ind['ema_1h_slow'] >= ind['ema_1h_slow'].shift(12) * 0.9985).fillna(True)
    ind['ema_d1'] = c.ewm(span=96, adjust=False).mean()
    ind['ema_d3'] = c.ewm(span=240, adjust=False).mean()

    ind['ret_4h'] = c.pct_change(16).fillna(0.0)
    ind['delta12'] = (c - c.shift(12)).fillna(0.0)
    ind['delta16'] = (c - c.shift(16)).fillna(0.0)
    ind['ret_96'] = c.pct_change(96).fillna(0.0)

    typ_price = (h + l + c) / 3.0
    vol_sum = v.rolling(32, min_periods=1).sum().replace(0, np.nan)
    ind['vwap'] = ((typ_price * v).rolling(32, min_periods=1).sum() / vol_sum).fillna(c)

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(loss != 0, np.where(gain > 0, 100.0, 50.0)).fillna(50.0)
    ind['rsi'] = rsi
    ind['rsi_falling'] = (rsi < (rsi.shift(3) - 4.0)).fillna(False)

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    ind['macd_hist'] = hist
    ind['macd_rising'] = ((hist > hist.shift(1)) & ((hist > 0) | (hist.shift(1) > hist.shift(2)))).fillna(False)
    ind['macd_losing'] = ((hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))).fillna(False)

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

    sma20 = c.rolling(20, min_periods=10).mean()
    std20 = c.rolling(20, min_periods=10).std()
    width = (4 * std20).replace(0, np.nan)
    bb_pos = ((c - (sma20 - 2 * std20)) / width).fillna(0.5)
    ind['bb_pos'] = bb_pos
    ind['std_zscore'] = ((c - sma20) / std20.replace(0, np.nan)).fillna(0.0)

    avg_vol = v.rolling(20, min_periods=5).mean()
    ind['rvol'] = (v / avg_vol.replace(0, np.nan)).fillna(1.0)

    rng = (h - l).clip(lower=1e-9)
    body = (c - o).abs()
    close_pos = (c - l) / rng
    ind['close_pos'] = close_pos
    body_top = pd.concat([c, o], axis=1).max(axis=1)
    body_bot = pd.concat([c, o], axis=1).min(axis=1)
    upper_wick_abs = h - body_top
    lower_wick_abs = body_bot - l
    ind['wick'] = upper_wick_abs / rng

    prev_o = o.shift(1)
    prev_body = body.shift(1)

    ind['bull_displacement'] = (c > o) & (body > 1.2 * atr) & (close_pos >= 0.65)
    ind['bear_displacement'] = (c < o) & (body > 1.2 * atr) & (close_pos <= 0.35)
    ind['bull_engulf'] = ((c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c) & (body > prev_body * 0.90)).fillna(False)
    ind['bear_engulf'] = ((c < o) & (prev_c > prev_o) & (c <= prev_o) & (o >= prev_c) & (body > prev_body * 0.90)).fillna(False)
    ind['bull_hammer'] = (lower_wick_abs >= 1.8 * np.maximum(body, 0.12 * rng)) & (upper_wick_abs <= 0.35 * rng) & (close_pos >= 0.60)
    ind['shooting_star'] = (upper_wick_abs >= 1.8 * np.maximum(body, 0.12 * rng)) & (lower_wick_abs <= 0.35 * rng) & (close_pos <= 0.40)

    swing_hi = h.rolling(36, min_periods=12).max()
    swing_lo = l.rolling(36, min_periods=12).min()
    deal_rng = (swing_hi - swing_lo).replace(0, np.nan)
    ind['pd_pos'] = ((c - swing_lo) / deal_rng).fillna(0.5)
    ind['fib_retrace'] = ((swing_hi - c) / deal_rng).fillna(0.5)

    ind['low3'] = l.rolling(3, min_periods=1).min()
    ind['min_low12_prev'] = l.rolling(12, min_periods=6).min().shift(1)
    ssl20 = l.rolling(20, min_periods=12).min().shift(2)
    ssl20_prev = ssl20.shift(1)
    ind['liquidity_sweep'] = (
        (ssl20.notna() & (l < ssl20) & (c > ssl20) & (c > o))
        | (ssl20_prev.notna() & (l.shift(1) < ssl20_prev) & (c > np.maximum(ssl20_prev, h.shift(1))) & (c > o))
    ).fillna(False)

    # Unmitigated FVG, Order Block, and Breaker Block (OrientFX Pag. 80-83, 96-102, 206-222)
    n_bars = len(ind)
    o_arr, h_arr, l_arr, c_arr = o.values, h.values, l.values, c.values
    body_arr, atr_arr = body.values, atr.values
    bull_disp_arr = ind['bull_displacement'].values
    low3_arr = ind['low3'].values

    fvg_retest = np.zeros(n_bars, dtype=bool)
    ob_retest = np.zeros(n_bars, dtype=bool)
    brk_retest = np.zeros(n_bars, dtype=bool)
    active_fvgs, active_obs, active_brks = [], [], []

    for idx_i in range(3, n_bars):
        if l_arr[idx_i] > h_arr[idx_i - 2] and c_arr[idx_i - 1] > o_arr[idx_i - 1] and body_arr[idx_i - 1] > 0.7 * atr_arr[idx_i - 1]:
            active_fvgs.append([h_arr[idx_i - 2], l_arr[idx_i], idx_i, False])
        if bull_disp_arr[idx_i] and c_arr[idx_i - 1] < o_arr[idx_i - 1]:
            active_obs.append([l_arr[idx_i - 1], max(o_arr[idx_i - 1], c_arr[idx_i - 1]), idx_i, False])
        if idx_i >= 12 and c_arr[idx_i] > np.max(h_arr[idx_i - 10:idx_i]) and bull_disp_arr[idx_i]:
            for k in range(idx_i - 1, max(1, idx_i - 8), -1):
                if c_arr[k] > o_arr[k] and c_arr[k + 1] < o_arr[k + 1]:
                    active_brks.append([min(o_arr[k], c_arr[k]), h_arr[k], idx_i, False])
                    break

        l3 = low3_arr[idx_i]
        nf = []
        for btm, top, idx_c, used in active_fvgs:
            if idx_i - idx_c > 28 or c_arr[idx_i] < btm:
                continue
            if not used and idx_i > idx_c + 1 and l3 <= top * 1.001 and c_arr[idx_i] >= btm and c_arr[idx_i] > o_arr[idx_i]:
                fvg_retest[idx_i] = True
                used = True
            if not used:
                nf.append([btm, top, idx_c, used])
        active_fvgs = nf[-4:]

        no = []
        for btm, top, idx_c, used in active_obs:
            if idx_i - idx_c > 32 or c_arr[idx_i] < btm:
                continue
            if not used and idx_i > idx_c + 1 and l3 <= top * 1.001 and c_arr[idx_i] >= btm and c_arr[idx_i] > o_arr[idx_i]:
                ob_retest[idx_i] = True
                used = True
            if not used:
                no.append([btm, top, idx_c, used])
        active_obs = no[-4:]

        nb = []
        for btm, top, idx_c, used in active_brks:
            if idx_i - idx_c > 32 or c_arr[idx_i] < btm:
                continue
            if not used and idx_i > idx_c + 1 and l3 <= top * 1.001 and c_arr[idx_i] >= btm and c_arr[idx_i] > o_arr[idx_i]:
                brk_retest[idx_i] = True
                used = True
            if not used:
                nb.append([btm, top, idx_c, used])
        active_brks = nb[-4:]

    ind['fvg_retest'] = fvg_retest
    ind['ob_retest'] = ob_retest
    ind['brk_retest'] = brk_retest
    return ind


def signal_at(ind, i, social_bonus=0, live_price=None, live_high=None, btc_ret_4h=None,
              entry_time=None, kz_bonus=0, is_preferred=False, is_crypto=True, btc_bull=False):
    r = ind.iloc[i]
    bar_close = float(r['close'])
    bar_open = float(r['open'])
    price = float(live_price) if live_price is not None else bar_close
    atr = float(r['atr'])
    if not np.isfinite(atr) or atr <= 0:
        atr = bar_close * 0.003
    atr_pct = atr / bar_close

    ema_f, ema_s, ema_t = float(r['ema_fast']), float(r['ema_slow']), float(r['ema_trend'])
    ema_1h_f, ema_1h_s = float(r['ema_1h_fast']), float(r['ema_1h_slow'])
    ema_1h_slope = bool(r['ema_1h_slow_slope'])
    vwap, adx = float(r['vwap']), float(r['adx'])
    ret_4h = float(r['ret_4h'])
    rsi, hist = float(r['rsi']), float(r['macd_hist'])
    macd_rising, rsi_falling = bool(r['macd_rising']), bool(r['rsi_falling'])
    pd_pos = float(r['pd_pos'])
    fib_retrace = float(r['fib_retrace'])
    std_z = float(r['std_zscore'])
    close_pos = float(r['close_pos'])

    is_bull_disp = bool(r['bull_displacement'])
    is_bear_disp = bool(r['bear_displacement'])
    is_bull_engulf = bool(r['bull_engulf'])
    is_bear_engulf = bool(r['bear_engulf'])
    is_hammer = bool(r['bull_hammer'])
    is_shooting_star = bool(r['shooting_star'])

    liq_sweep = bool(r['liquidity_sweep'])
    fvg_retest = bool(r['fvg_retest'])
    ob_retest = bool(r['ob_retest'])
    brk_retest = bool(r['brk_retest'])

    # OrientFX Regula #1 (Pag. 33): Fără intrări counter-trend pe lumânare de inversare/impuls bearish
    bear_candle = is_bear_engulf or is_bear_disp or is_shooting_star
    candle_bull = is_bull_engulf or is_hammer or is_bull_disp
    bull_close = (bar_close > bar_open) and (close_pos >= 0.58)
    prev_h = float(ind['high'].iloc[i - 1]) if i >= 1 else bar_close
    reclaim_conf = (not bear_candle) and (candle_bull or bull_close) and (bar_close > prev_h or candle_bull) and macd_rising

    # Trend H1 crescător + Buzunarul de Valoare H1 Discount (OrientFX Pag. 50-52, 68, 259-262)
    h1_up = (ema_1h_f > ema_1h_s) and ema_1h_slope
    c_h1f_atr = (bar_close - ema_1h_f) / atr
    c_h1s_atr = (bar_close - ema_1h_s) / atr
    in_h1_value = (c_h1f_atr <= 0.0) and (-3.5 <= c_h1s_atr <= 1.5)

    # Pullback de manipulare controlat pe 12-16 bare în unități ATR (OrientFX Pag. 243-254)
    min_d_atr = min(float(r['delta12']), float(r['delta16'])) / atr
    controlled_pullback = (-4.5 <= min_d_atr <= -0.6)

    confluences = 0
    if h1_up:
        confluences += 1
    if in_h1_value:
        confluences += 1
    if controlled_pullback:
        confluences += 1
    if reclaim_conf:
        confluences += 1
    if adx >= 18:
        confluences += 1

    score, setup, reasons = 0, None, []

    if adx >= 18 and h1_up and in_h1_value and controlled_pullback and reclaim_conf:
        if is_crypto:
            sym_macro_bull = (
                (bar_close > float(r['ema_d3']) * 1.01)
                and (float(r['ema_d1']) > float(r['ema_d3']) * 1.005)
                and (float(r['ret_96']) > 0.02)
            )
            if btc_bull and sym_macro_bull and atr_pct >= 0.0055:
                poi = fvg_retest or ob_retest or brk_retest or liq_sweep
                if poi and (30 <= rsi <= 46):
                    score, setup = 82, 'SMC_OB_FVG'
                    reasons.append("Crypto Macro Bull + H1 Discount POI")
        else:
            min_low12_prev = float(r['min_low12_prev']) if pd.notna(r['min_low12_prev']) else np.nan
            swept = liq_sweep or (pd_pos <= 0.35 and np.isfinite(min_low12_prev) and float(r['low3']) <= min_low12_prev)
            real_poi = fvg_retest or ob_retest or brk_retest or is_bull_engulf or is_bull_disp
            in_gz = (0.48 <= fib_retrace <= 0.82) and (std_z <= -0.70)

            if swept and rsi <= 47:
                score, setup = 81, 'LIQUIDITY_SWEEP'
                reasons.append("OrientFX Liquidity Sweep + MSS în H1 Discount")
            elif real_poi and in_gz and rsi <= 47:
                score, setup = 80, 'SMC_OB_FVG'
                reasons.append("OrientFX Order Block / FVG în H1 Discount")
            elif in_gz and rsi <= 47:
                score, setup = 79, 'OTE_PULLBACK'
                reasons.append("OrientFX Golden Zone OTE (50%-79%) în H1 Discount")

    # CHASE GUARD în live: dacă prețul live a fugit peste închiderea lumânării semnal + 0.5 ATR, nu mai urmărim prețul
    if setup and live_price is not None and price > bar_close + DNA['chase_atr_mult'] * atr:
        reasons = [f"Chase guard: preț live +{(price - bar_close) / atr:.1f} ATR peste semnal"]
        score, setup = 0, None

    if setup:
        if is_preferred:
            score += 3
            reasons.append("Activ Lider(+3)")
        if candle_bull:
            score += 4
            reasons.append("🕯️ Reversal Instituțional(+4)")
        if kz_bonus > 0:
            score += kz_bonus
            reasons.append(f"Killzone(+{kz_bonus})")
        reasons.append(f"[{confluences} Confluențe]")

    ceiling, why = False, ""
    if is_bear_engulf and pd_pos >= 0.75:
        ceiling, why = True, "Bearish Engulfing în zona Extreme Premium (>75%)"
    elif std_z >= 2.5 and rsi_falling:
        ceiling, why = True, f"Țintă Deviație Standard (+{std_z:.1f} SD) atinsă"

    if entry_time is not None:
        sub_highs = ind['high'].loc[ind.index >= entry_time - timedelta(minutes=DNA['bar_minutes'])]
        peak_since_entry = float(sub_highs.max()) if not sub_highs.empty else price
    else:
        peak_since_entry = float(ind['high'].iloc[max(0, i - 2):i + 1].max())
    if live_high is not None:
        peak_since_entry = max(peak_since_entry, float(live_high))

    return {
        'price': price, 'bar_close': bar_close, 'atr': atr, 'atr_pct': atr_pct,
        'ema_fast': ema_f, 'ema_slow': ema_s, 'ema_trend': ema_t, 'vwap': vwap, 'adx': adx,
        'rsi': rsi, 'macd_hist': hist, 'macd_rising': macd_rising,
        'trend_up': h1_up, 'ret_4h': ret_4h, 'pd_pos': pd_pos, 'confluences': confluences,
        'peak_since_entry': max(price, peak_since_entry),
        'profit_ceiling_hit': ceiling, 'ceiling_reason': why,
        'score': score, 'setup': setup, 'social': int(social_bonus), 'is_crypto': is_crypto,
        'reason': " + ".join(reasons) if reasons else "Neutru",
    }


def entry_allowed(m, cost_rt, is_asian_accum=False, is_crypto=False):
    min_req = DNA['asian_accumulation_score'] if (is_asian_accum and is_crypto) else DNA['entry_score_min']
    if is_crypto:
        min_req += DNA['crypto_score_premium']
    if not m['setup'] or m['confluences'] < DNA['min_confluences'] or m['score'] < min_req or m['profit_ceiling_hit']:
        return False
    return m['atr_pct'] * DNA['vol_cost_mult'] >= cost_rt


def exit_decision(profit, m, init_stop_pct, cost_rt, market_val=20000.0, scaled_out=False, bars_held=0):
    # 1. Țintă completă Take-Profit OrientFX (Pag. 130 & 261: +2.50R / Fib -0.618)
    full_tp = cost_rt + init_stop_pct * DNA['full_tp_r']
    if profit >= full_tp:
        return f"Țintă Take-Profit OrientFX (+{DNA['full_tp_r']}R) atinsă (+{profit*100:.2f}%)"

    # 2. Reciclare slot doar dacă poziția inițială (nescalată) e sub pragul micro
    if (not scaled_out) and market_val < MICRO_POS_USD and profit > -0.008 and m['score'] < 70:
        return f"Reciclare slot micro-poziție ({market_val:.0f}$) pentru alocare completă"

    # 3. Plafon Extreme Premium peste +1.80R
    if DNA['use_exhaustion_exit']:
        min_net = cost_rt + (init_stop_pct * DNA['exhaustion_min_r'])
        if profit >= min_net and m['profit_ceiling_hit']:
            return f"Plafon Extreme Premium la >{DNA['exhaustion_min_r']}R: {m['ceiling_reason']}"

    # 4. Limită timp eficiență swing (44 bare de 15m)
    if bars_held >= DNA['max_hold_bars']:
        return f"Limită timp swing ({bars_held} bare)"
    return None


# ==========================================
# 6. EXECUȚIE ORDINE LIMIT & PROTECȚIE STOP SERVER
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


def submit_smart_buy(tc, symbol, invest_usd, live_price, is_crypto, setup="NA"):
    limit_px = round_price(live_price * 1.0005, is_crypto)
    qty = truncate_qty(invest_usd / limit_px, is_crypto, decimals=6)
    if qty <= 0:
        return False

    tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
    # Setup-ul intră în client_order_id -> botul poate învăța ce setup câștigă / pierde bani
    cid = f"TC-{setup}-{uuid.uuid4().hex[:10]}"
    order = tc.submit_order(LimitOrderRequest(
        symbol=symbol, qty=qty, limit_price=limit_px, side=OrderSide.BUY, time_in_force=tif,
        client_order_id=cid
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
        # FIX V10.2: acceptăm umplere parțială doar dacă poziția NU ar fi reciclată imediat ca micro-poziție
        if filled_q * live_price >= MICRO_POS_USD:
            return True
        rem_usd = invest_usd - (filled_q * live_price)
        if rem_usd >= 1000.0:
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


def effective_init_stop_pct(entry, cur_stop, fallback_stop_pct, is_crypto=False):
    if cur_stop is not None and 0 < cur_stop < entry:
        lo = DNA['crypto_min_stop_pct'] if is_crypto else DNA['min_stop_pct']
        hi = DNA['crypto_max_stop_pct'] if is_crypto else DNA['max_stop_pct']
        return clamp((entry - cur_stop) / entry, lo, hi)
    return fallback_stop_pct


def execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, new_stop_price):
    try:
        for o in get_open_orders(tc):
            if norm(o.symbol) == pos_symbol:
                tc.cancel_order_by_id(o.id)
        time.sleep(1.2)
        fresh_pos = tc.get_open_position(pos_symbol)
        avail_qty = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
        frac = DNA.get('partial_tp_frac', 0.30)
        sell_qty = truncate_qty(avail_qty * frac, is_crypto, decimals=6)
        if sell_qty <= 0 or (not is_crypto and avail_qty < 3):
            place_stop(tc, symbol, avail_qty, new_stop_price, is_crypto)
            return False

        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY
        tc.submit_order(MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=tif))
        pct_lbl = int(round(frac * 100))
        print(f"   💰 [TAKE PARTIALS {pct_lbl}%] Vândut {sell_qty} {symbol} la +{DNA['partial_tp_r']}R! Restul de {100-pct_lbl}% are SL la Breakeven+.")
        time.sleep(2.2)
        rem_pos = tc.get_open_position(pos_symbol)
        rem_qty = float(getattr(rem_pos, 'qty_available', None) or rem_pos.qty)
        place_stop(tc, symbol, rem_qty, new_stop_price, is_crypto)
        return True
    except Exception as e:
        print(f"   ⚠️ Scale-out parțial amânat pe {symbol}: {e}")
        return False


def compute_trailing_stop(entry, live_price, cur_stop, init_stop_pct, scaled_out, m, cost_rt):
    """Singura sursă de adevăr pentru stop: folosită de ratchet_stop (live) și de simularea din backtest."""
    profit = live_price / entry - 1.0
    max_allowed_floor = entry * (1.0 - init_stop_pct)
    new_stop = max(cur_stop, max_allowed_floor) if cur_stop else max_allowed_floor

    if scaled_out:
        new_stop = max(new_stop, entry * (1.0 + cost_rt + 0.0005))

    if profit >= cost_rt + init_stop_pct * DNA['breakeven_trigger_r']:
        peak_val = max(live_price, m['peak_since_entry'])
        trail = peak_val - DNA['trail_atr_mult'] * m['atr']
        new_stop = max(new_stop, trail, entry * (1.0 + cost_rt + 0.0010))

    # FIX V11.0: buffer tehnic de doar 0.12% sub live_price (vechiul 0.80% împingea stopul de Breakeven pe minus!)
    new_stop = min(new_stop, live_price * 0.9988)
    if cur_stop is not None:
        new_stop = max(new_stop, cur_stop)
    return new_stop


def ratchet_stop(tc, symbol, pos_symbol, pos, m, is_crypto, cost_rt, pos_mem):
    entry = float(pos.avg_entry_price)
    avail_qty = float(getattr(pos, 'qty_available', None) or pos.qty)
    total_qty = float(pos.qty)
    live_price = float(pos.current_price) if getattr(pos, 'current_price', None) else m['price']
    profit = live_price / entry - 1.0

    cur = find_stop_order(get_open_orders(tc), pos_symbol)
    cur_stop = float(cur.stop_price) if cur is not None else None
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m, is_crypto), is_crypto=is_crypto)

    scaled_out = pos_mem.get('scaled_out', False)
    partial_target = cost_rt + init_stop_pct * DNA['partial_tp_r']
    if DNA['use_partial_tp'] and not scaled_out and profit >= partial_target:
        lock_stop = min(live_price * 0.9988, entry * (1.0 + cost_rt + 0.0005))
        if execute_partial_scale_out(tc, symbol, pos_symbol, pos, is_crypto, lock_stop):
            pos_mem['scaled_out'] = True
            return lock_stop

    new_stop = compute_trailing_stop(entry, live_price, cur_stop, init_stop_pct, scaled_out, m, cost_rt)

    if cur_stop is not None:
        if new_stop <= cur_stop * 1.0015:
            return cur_stop
        tc.cancel_order_by_id(cur.id)
        time.sleep(1.5)
        try:
            fresh_pos = tc.get_open_position(pos_symbol)
            q_to_stop = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
            placed = place_stop(tc, symbol, q_to_stop, new_stop, is_crypto)
            print(f"   🛡️ [SL ACTUALIZAT V11.0] {symbol}: {cur_stop:.4f} -> {placed}")
            return placed
        except Exception as e:
            print(f"   ⚠️ Stop nou respins ({e}); refac stop-ul anterior")
            fresh_pos = tc.get_open_position(pos_symbol)
            q_to_stop = float(getattr(fresh_pos, 'qty_available', None) or fresh_pos.qty)
            place_stop(tc, symbol, q_to_stop, min(cur_stop, live_price * 0.997), is_crypto)
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
    init_stop_pct = effective_init_stop_pct(entry, cur_stop, stop_distance_pct(m, is_crypto), is_crypto=is_crypto)
    scaled_out = bool(pos_mem.get('scaled_out'))
    stage_tag = "70% Runner" if scaled_out else "100%"
    pd_zone = "Discount" if m['pd_pos'] <= 0.5 else "Premium"

    bars_held = 0
    if pos_mem.get('entry_time') is not None:
        try:
            bars_held = max(0, int((datetime.now(timezone.utc) - pos_mem['entry_time']).total_seconds() // (DNA['bar_minutes'] * 60)))
        except Exception:
            bars_held = 0

    print(f"💼 [DEȚINUT {stage_tag}] {symbol:<8} | Val: {market_val:7.0f}$ | Live: {live_price:<10.4f} "
          f"| P/L: {profit*100:+.2f}% | Stop: {cur_stop or 0:<9.4f} | Zona: {pd_zone}({m['pd_pos']*100:.0f}%)")

    reason = "Închidere înainte de bell" if flatten_now \
        else exit_decision(profit, m, init_stop_pct, cost_rt, market_val=market_val, scaled_out=scaled_out, bars_held=bars_held)
    if reason:
        print(f"   🔔 [IEȘIRE V11.0] {symbol}: {reason} ({profit*100:+.2f}%)")
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
# 7. SCORECARD & DATE DE PIAȚĂ
# ==========================================
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
            pnl = net_trade_pnl(sym, avg, px, sell_qty)   # net de comision
            pnl_usd.append(pnl)
            pnl_pct.append(pnl / (avg * sell_qty))
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
    print(f"│ 💰 Profit total cont: {net:+,.2f}$ ({net / STARTING_CAPITAL * 100:+.2f}%)")
    print(f"│ 🎯 Tranzacții închise: {total} | Win rate: {win_rate:.1f}% | Profit factor (net comisioane): {pf:.2f}")
    print(f"│ 📊 Câștig mediu: +{avg_w:.2f}% | Pierdere medie: -{avg_l:.2f}% | Win rate de break-even: {breakeven_wr:.0f}%")
    print("└──────────────────────────────────────────────────────────────┘")


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
        if len(df_ind) < 85:
            continue
        if not is_stock and (now - df.index[-1] > bar_td * stale_bars):
            continue
        out[s] = (compute_indicators(df_ind), live_price, live_high)
    return out


# ==========================================
# 8. CICLUL PRINCIPAL V11.0 ORIENTFX
# ==========================================
def process_symbol(tc, symbol, pack, is_crypto, is_safe, held, ctx):
    ind, live_price, live_high = pack
    pos_symbol = norm(symbol)
    pos_mem = ctx['pos_memory'].setdefault(pos_symbol, {'entry_time': None, 'scaled_out': False})
    btc_ret = ctx.get('btc_ret_4h') if (is_crypto and symbol != "BTC/USD" and not is_safe) else None
    is_pref = pos_symbol in ctx['preferred_syms']

    m = signal_at(
        ind, len(ind) - 1, ctx['social'].get(symbol, 0),
        live_price=live_price, live_high=live_high,
        btc_ret_4h=btc_ret, entry_time=pos_mem.get('entry_time'),
        kz_bonus=ctx['kz_bonus'], is_preferred=is_pref, is_crypto=is_crypto,
        btc_bull=ctx.get('btc_bull', False)
    )
    if is_safe and ctx['risk_off'] and m['setup']:
        m['score'] += 15
        m['reason'] += " + 🛡️ Safe-Haven Gold/Bonds"

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
    if not entry_allowed(m, round_trip_cost(is_crypto), is_asian_accum=ctx['is_asian_accum'], is_crypto=is_crypto):
        return
    if m['setup'] in ctx['disabled_setups']:
        print(f"   ⛔ [SETUP DEZACTIVAT] {symbol}: {m['setup']} are expectanță negativă în ultimele {SETUP_WINDOW_DAYS} zile")
        return

    print(f"🟢 [SEMNAL {m['setup']}] {symbol:<8} | Live: {m['price']:.4f} | Scor: {m['score']} | {m['reason']}")
    ctx['candidates'].append({
        'symbol': symbol, 'pos_symbol': pos_symbol, 'price': m['price'], 'score': m['score'],
        'is_crypto': is_crypto, 'reason': m['reason'], 'init_stop_pct': stop_distance_pct(m, is_crypto),
        'setup': m['setup']})


LISTED_NORM = {norm(s) for s in ALL_CRYPTO + ALL_STOCKS}


def protect_foreign_positions(tc, held):
    """Poziții cu simboluri din afara listelor botului nu sunt scanate/gestionate: măcar le punem un stop și avertizăm."""
    foreign = {s: p for s, p in held.items() if s not in LISTED_NORM}
    if not foreign:
        return
    total = sum(abs(float(getattr(p, 'market_value', 0.0) or 0.0)) for p in foreign.values())
    print(f"⚠️ [POZIȚII NEGESTIONATE] {', '.join(sorted(foreign))} ({total:,.0f}$) nu sunt în lista botului: "
          f"nu au trailing/ieșiri, doar stop de protecție.")
    open_orders = get_open_orders(tc)
    for s, p in foreign.items():
        try:
            if find_stop_order(open_orders, s) is not None:
                continue
            qty = float(getattr(p, 'qty_available', None) or p.qty)
            if qty <= 0:
                continue
            is_crypto = 'crypto' in str(getattr(p, 'asset_class', '')).lower()
            entry = float(p.avg_entry_price)
            live = float(getattr(p, 'current_price', None) or entry)
            base_stop = DNA['crypto_min_stop_pct'] if is_crypto else DNA['min_stop_pct']
            stop = min(entry * (1.0 - base_stop * 1.5), live * 0.992)
            placed = place_stop(tc, p.symbol, qty, stop, is_crypto)
            print(f"   🛡️ [STOP PROTECȚIE] {s}: stop plantat la {placed}")
        except Exception as e:
            print(f"   ⚠️ Nu pot proteja {s}: {e}")


def open_new_positions(tc, ctx, held, equity):
    crypto_set = {norm(s) for s in ALL_CRYPTO}
    open_syms = [s for s in held if s not in ctx['closed']]
    n_open = len(open_syms)
    crypto_open = sum(1 for s in open_syms if s in crypto_set)
    gross = sum(abs(float(held[s].market_value)) for s in open_syms)

    for t in sorted(ctx['candidates'], key=lambda x: x['score'], reverse=True):
        if n_open >= DNA['max_positions']:
            print(f"   ⛔ [LIMITĂ POZIȚII] {n_open}/{DNA['max_positions']} deschise: nu intru pe semnalele noi.")
            break
        if t['is_crypto'] and crypto_open >= DNA['max_crypto_positions']:
            print(f"   ⛔ [LIMITĂ CRYPTO] Sar peste {t['symbol']}: {crypto_open}/{DNA['max_crypto_positions']} crypto deschise.")
            continue
        if is_correlated_with_held(t['pos_symbol'], open_syms):
            print(f"   🔗 [ANTI-DUBLARE] Sar peste {t['symbol']} (avem deja un activ identic/corelat în portofoliu).")
            continue
        try:
            cash = float(tc.get_account().cash)
            invest = position_size_usd(equity, cash, gross, t['init_stop_pct'], t['score'], ctx['loss_streak'])
            if invest < MIN_POSITION_USD:
                print(f"   💤 [FĂRĂ LOC] Sar peste {t['symbol']}: alocare {invest:,.0f}$ < minim {MIN_POSITION_USD:,.0f}$ "
                      f"(expunere {gross / equity * 100:.0f}% din plafon {DNA['max_total_exposure'] * 100:.0f}%)")
                continue
            sym, pos_sym = t['symbol'], t['pos_symbol']
            print(f"🎯 [INTRARE V11.0] {sym}: ~{invest:.0f}$ | Stop: -{t['init_stop_pct']*100:.2f}% "
                  f"| Scor {t['score']} ({t['reason']})")

            if not submit_smart_buy(tc, sym, invest, t['price'], t['is_crypto'], t.get('setup', 'NA')):
                continue

            n_open += 1
            open_syms.append(pos_sym)
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


def run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val,
              pos_memory, cooldown_syms, loss_streak, sym_stats, learned_mem, show_scorecard=False):
    if os.path.exists("DEAD.lock") and AUTO_REBIRTH:
        try:
            os.remove("DEAD.lock")
        except Exception:
            pass

    account = tc.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity) if getattr(account, 'last_equity', None) else equity
    daily_pnl = (equity - last_equity) / last_equity if last_equity > 0 else 0.0
    entries_blocked = daily_pnl <= -DAILY_LOSS_LIMIT_PCT

    held = get_active_positions_and_sweep_dust(tc)

    # =========================================================================
    # PIERDEREA DE 20% RAPORTATĂ LA BANII BĂGAȚI ÎN POZIȚII
    # =========================================================================
    invested_now = sum(abs(float(getattr(p, 'cost_basis', 0.0) or 0.0)) for p in held.values())
    unrealized_pnl_now = sum(float(getattr(p, 'unrealized_pl', 0.0) or 0.0) for p in held.values())

    if invested_now >= MIN_POSITION_USD:
        learned_mem["avg_invested_capital"] = round(invested_now, 2)
    invested_ref = max(invested_now, float(learned_mem.get("avg_invested_capital", 60000.0)), 25000.0)

    gen_start_eq = float(learned_mem.get("generation_start_equity", equity))
    if equity > gen_start_eq:
        gen_start_eq = equity
        learned_mem["generation_start_equity"] = round(equity, 2)

    gen_loss_usd = max(0.0, gen_start_eq - equity)
    max_allowed_loss_on_invested = invested_ref * INVESTED_MAX_LOSS_PCT
    rebirth_equity_floor = gen_start_eq - max_allowed_loss_on_invested
    loss_pct_of_invested = (gen_loss_usd / invested_ref) if invested_ref > 0 else 0.0

    now = datetime.now(timezone.utc)
    kz_name, kz_bonus, is_asian_accum = get_ict_session_info(now)
    gen_num = learned_mem.get("generation", 1)

    print(f"\n⚡ Scanare V11.0 Gen#{gen_num} ({kz_name}): {now.strftime('%H:%M:%S UTC')} "
          f"| Equity: {equity:,.2f}$ | Peak gen: {gen_start_eq:,.0f}$ | Băgați în poziții: {invested_now:,.0f}$ "
          f"| Pierdere/Investit: -{loss_pct_of_invested*100:.1f}% (Prag Rebirth -20%: {rebirth_equity_floor:,.0f}$) | Azi: {daily_pnl*100:+.2f}%")
    if entries_blocked:
        print(f"🧯 Limita zilnică ({DAILY_LOSS_LIMIT_PCT*100:.1f}%) atinsă: doar protejăm pozițiile.")
    if show_scorecard:
        evaluate_scorecard(tc, equity)

    # DECLANȘATOR 1: Pierderea din Generația curentă (drawdown de la peak) a atins 20% din banii băgați în poziții
    if gen_loss_usd >= max_allowed_loss_on_invested:
        execute_death_learn_and_rebirth(
            tc, equity, invested_ref, gen_loss_usd, sym_stats, learned_mem, cooldown_syms,
            f"Pierderea ({gen_loss_usd:,.0f}$) a atins 20% din suma băgată în poziții ({invested_ref:,.0f}$)"
        )
        return

    # DECLANȘATOR 2: Pozițiile deschise acum au împreună -20% față de banii băgați în ele
    if invested_now >= MIN_POSITION_USD and unrealized_pnl_now <= -(invested_now * INVESTED_MAX_LOSS_PCT):
        execute_death_learn_and_rebirth(
            tc, equity, invested_now, abs(unrealized_pnl_now), sym_stats, learned_mem, cooldown_syms,
            f"Pozițiile curente au pierdut 20% ({unrealized_pnl_now:,.0f}$) din cei {invested_now:,.0f}$ băgați în ele"
        )
        return

    # DECLANȘATOR 3: O monedă individuală a pierdut 20% din suma băgată în ea
    for s_key, p_obj in list(held.items()):
        c_basis = abs(float(getattr(p_obj, 'cost_basis', 0.0) or 0.0))
        u_plpc = float(getattr(p_obj, 'unrealized_plpc', 0.0) or 0.0)
        if c_basis >= MIN_POSITION_USD and u_plpc <= -INVESTED_MAX_LOSS_PCT:
            execute_death_learn_and_rebirth(
                tc, equity, c_basis, abs(float(getattr(p_obj, 'unrealized_pl', 0.0) or 0.0)),
                sym_stats, learned_mem, cooldown_syms,
                f"Moneda {s_key} a pierdut {u_plpc*100:.1f}% din cei {c_basis:,.0f}$ băgați în ea"
            )
            return

    crypto_bars = crypto_client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=now - timedelta(days=6))).df
    crypto_packs = prepare_symbol_frames(crypto_bars, ALL_CRYPTO, now, stale_bars=5, is_stock=False)

    risk_off = False
    btc_ret_4h = 0.0
    btc_bull = False
    btc_pack = crypto_packs.get("BTC/USD")
    if btc_pack is not None:
        btc_ind, btc_live, _ = btc_pack
        if len(btc_ind) > 4:
            lookback = max(1, int(30 / DNA['bar_minutes']))
            btc_change = float(btc_live / btc_ind['close'].iloc[-lookback] - 1.0)
            btc_ret_4h = float(btc_ind['ret_4h'].iloc[-1])
            b_last = btc_ind.iloc[-1]
            btc_bull = (
                (float(b_last['close']) > float(b_last['ema_1h_slow']) * 1.008)
                and (float(b_last['ema_1h_fast']) > float(b_last['ema_1h_slow']) * 1.004)
                and bool(b_last['ema_1h_slow_slope'])
            )
            if btc_change < -0.012 or fear_greed_val <= 22:
                risk_off = True
                print(f"🛡️ [MOD REFUGIU] BTC {btc_change*100:+.2f}% (30m), F&G {fear_greed_val}: doar Aur/Obligațiuni.")

    ctx = {
        'social': social_scores, 'risk_off': risk_off, 'entries_blocked': entries_blocked,
        'closed': set(), 'candidates': [], 'stock_entries_ok': False, 'flatten_stocks': False,
        'pos_memory': pos_memory, 'cooldown_syms': cooldown_syms, 'btc_ret_4h': btc_ret_4h,
        'btc_bull': btc_bull, 'kz_bonus': kz_bonus, 'is_asian_accum': is_asian_accum,
        'loss_streak': loss_streak,
        'disabled_setups': set(learned_mem.get("disabled_setups", [])),
        'preferred_syms': set(learned_mem.get("preferred_symbols", ["SPY", "QQQ", "GLD", "BTCUSD", "NVDA", "AAPL", "TSLA", "TLT"]))
    }

    for symbol, pack in crypto_packs.items():
        process_symbol(tc, symbol, pack, True, symbol in SAFE_HAVEN_CRYPTO, held, ctx)

    clock = tc.get_clock()
    if clock.is_open:
        mtc = (clock.next_close - clock.timestamp).total_seconds() / 60.0
        # OrientFX NY Killzone Window: 09:55 NY - 14:45 NY (evită Judas Swing de la deschiderea 09:30-09:55 NY)
        ctx['stock_entries_ok'] = 75 <= mtc <= 365
        ctx['flatten_stocks'] = FLATTEN_STOCKS_BEFORE_CLOSE and mtc <= FLATTEN_MIN
        stock_bars = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=now - timedelta(days=15),
            feed=DataFeed.IEX)).df
        stock_packs = prepare_symbol_frames(stock_bars, ALL_STOCKS, now, stale_bars=6, is_stock=True)
        for symbol, pack in stock_packs.items():
            process_symbol(tc, symbol, pack, False, symbol in SAFE_HAVEN_STOCKS, held, ctx)
    elif show_scorecard and held:
        # Când bursa NY este închisă, afișăm clar pozițiile de acțiuni/ETF-uri deținute în portofoliu
        stock_held = [p for s, p in held.items() if s not in CRYPTO_NORM]
        if stock_held:
            open_orders = get_open_orders(tc)
            for p in stock_held:
                s_norm = norm(p.symbol)
                m_val = abs(float(getattr(p, 'market_value', 0.0) or 0.0))
                entry_px = float(p.avg_entry_price)
                live_px = float(getattr(p, 'current_price', None) or entry_px)
                pl_pct = (live_px / entry_px - 1.0) * 100.0 if entry_px > 0 else 0.0
                st_ord = find_stop_order(open_orders, s_norm)
                st_px = float(st_ord.stop_price) if st_ord and st_ord.stop_price else 0.0
                print(f"💼 [DEȚINUT BURSĂ ÎNCHISĂ] {p.symbol:<8} | Val: {m_val:7.0f}$ | Intrare: {entry_px:<8.2f} "
                      f"| Ultim: {live_px:<8.2f} | P/L: {pl_pct:+.2f}% | Stop: {st_px:<8.2f}")

    protect_foreign_positions(tc, held)

    if ctx['candidates'] and not entries_blocked:
        open_new_positions(tc, ctx, held, equity)


# ==========================================
# 8b. BACKTEST (python github_champion.py --backtest --days 30)
# ==========================================
BACKTEST_PREFERRED = {"SPY", "QQQ", "GLD", "PAXGUSD", "BTCUSD", "NVDA", "AAPL", "TSLA", "TLT"}


def load_backtest_gate(max_age_days=14):
    """Setup-urile pe care ultimul backtest (mai nou de 14 zile) le-a găsit cu expectanță negativă."""
    try:
        with open(BACKTEST_FILE, "r", encoding="utf-8") as f:
            rep_ = json.load(f)
        ts = datetime.fromisoformat(str(rep_["generated_at"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ts > timedelta(days=max_age_days):
            return set()
        return set(rep_.get("disable_setups", []))
    except Exception:
        return set()


def fetch_backtest_frames(crypto_client, stock_client, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 6)
    frames = {}
    try:
        if ALL_CRYPTO:
            cb = crypto_client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=ALL_CRYPTO, timeframe=bar_tf(), start=start, end=end)).df
            have = set(cb.index.get_level_values(0))
            for s in ALL_CRYPTO:
                if s in have:
                    frames[s] = (cb.xs(s), True)
    except Exception as e:
        print(f"   ⚠️ Nu pot descărca istoricul crypto: {e}")
    try:
        sb = stock_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ALL_STOCKS, timeframe=bar_tf(), start=start, end=end, feed=DataFeed.IEX)).df
        have = set(sb.index.get_level_values(0))
        for s in ALL_STOCKS:
            if s in have:
                df = filter_regular_stock_hours(sb.xs(s))
                if not df.empty:
                    frames[s] = (df, False)
    except Exception as e:
        print(f"   ⚠️ Nu pot descărca istoricul acțiunilor: {e}")
    return frames


def precompute_signals(frames):
    """Semnalul pe fiecare lumânare închisă. Nu depinde de parametrii de management, deci se calculează o singură dată."""
    out = {}
    btc_ret = None
    btc_bull_series = None
    if "BTC/USD" in frames and len(frames["BTC/USD"][0]) > 120:
        btc_ind = compute_indicators(frames["BTC/USD"][0])
        btc_ret = btc_ind['ret_4h']
        btc_bull_series = (
            (btc_ind['close'] > btc_ind['ema_1h_slow'] * 1.008)
            & (btc_ind['ema_1h_fast'] > btc_ind['ema_1h_slow'] * 1.004)
            & btc_ind['ema_1h_slow_slope']
        )
    bar_td = timedelta(minutes=DNA['bar_minutes'])
    for sym, (df, is_crypto) in frames.items():
        if len(df) < 130:
            continue
        ind = compute_indicators(df)
        is_safe = sym in SAFE_HAVEN_CRYPTO or sym in SAFE_HAVEN_STOCKS
        is_pref = norm(sym) in BACKTEST_PREFERRED
        idx = ind.index
        ms = [None] * len(ind)
        for i in range(100, len(ind)):
            _, kz_bonus, is_asian = get_ict_session_info(idx[i] + bar_td)
            btc_r = None
            btc_b = False
            if btc_ret is not None and is_crypto:
                if sym != "BTC/USD" and not is_safe:
                    v = btc_ret.get(idx[i], np.nan)
                    btc_r = float(v) if np.isfinite(v) else None
                if btc_bull_series is not None:
                    btc_b = bool(btc_bull_series.get(idx[i], False))
            m = signal_at(ind, i, 0, live_price=None, btc_ret_4h=btc_r, kz_bonus=kz_bonus,
                          is_preferred=is_pref, is_crypto=is_crypto, btc_bull=btc_b)
            m['_asian'] = is_asian
            ms[i] = m
        out[sym] = (ind, ms, is_crypto)
        print(f"   · {sym}: {len(ind)} lumânări, semnale pregătite")
    return out


def simulate_symbol(sym, ind, ms, is_crypto, disabled=frozenset()):
    """Simulează o poziție pe rând pe simbol, cu aceeași logică de stop/partial/ieșire ca botul live (nivel de lumânare 15m)."""
    trades = []
    n = len(ind)
    O, H, L, C = (ind[c].values.astype(float) for c in ('open', 'high', 'low', 'close'))
    idx = ind.index
    bar_td = timedelta(minutes=DNA['bar_minutes'])
    cost_rt = round_trip_cost(is_crypto)
    slip = CRYPTO_SLIPPAGE_PCT if is_crypto else STOCK_SLIPPAGE_PCT
    fee = CRYPTO_FEE_PCT if is_crypto else 0.0
    cooldown_bars = max(1, int(DNA['reentry_cooldown_min'] // DNA['bar_minutes']))
    i = 100
    while i < n - 2:
        m = ms[i]
        if m is None or m['setup'] is None or m['setup'] in disabled:
            i += 1
            continue
        if not is_crypto:
            ny = (idx[i] + bar_td).astimezone(NY_TZ)
            mtc = 960 - (ny.hour * 60 + ny.minute)      # minute până la 16:00 NY
            # OrientFX NY Killzone Window: 09:55 - 14:45 NY (evită 09:30-09:55 opening Judas Swing)
            if not (75 <= mtc <= 365):
                i += 1
                continue
        if not entry_allowed(m, cost_rt, m.get('_asian', False), is_crypto):
            i += 1
            continue

        e = i + 1
        entry = O[e] * (1.0 + slip)
        isp = stop_distance_pct(m, is_crypto)
        stop = entry * (1.0 - isp)
        peak = entry
        scaled = False
        remaining = 1.0
        fills = []
        why = 'END'
        j = e
        while j < n:
            o, h, l, c = O[j], H[j], L[j], C[j]
            if l <= stop:                                  # stop lovit (gap-ul se plătește: min(open, stop))
                fills.append((remaining, min(o, stop) * (1.0 - slip)))
                remaining = 0.0
                why = 'STOP_RUNNER' if scaled else 'STOP'
                break
            peak = max(peak, h)
            mj = ms[j]
            if mj is None:
                break
            if DNA['use_partial_tp'] and not scaled:
                tgt1 = entry * (1.0 + cost_rt + isp * DNA['partial_tp_r'])
                if h >= tgt1:
                    pf_frac = DNA.get('partial_tp_frac', 0.30)
                    fills.append((pf_frac, tgt1 * (1.0 - slip)))
                    remaining = 1.0 - pf_frac
                    scaled = True
                    stop = max(stop, entry * (1.0 + cost_rt + 0.0005))

            tgt2 = entry * (1.0 + cost_rt + isp * DNA['full_tp_r'])
            if h >= tgt2:
                fills.append((remaining, tgt2 * (1.0 - slip)))
                remaining = 0.0
                why = 'TP_FULL'
                break

            profit = c / entry - 1.0
            mm = dict(mj)
            mm['price'] = c
            mm['peak_since_entry'] = max(peak, c)
            if exit_decision(profit, mm, isp, cost_rt, market_val=1e9, scaled_out=scaled, bars_held=(j - e)):
                fills.append((remaining, c * (1.0 - slip)))
                remaining = 0.0
                why = 'SEMNAL'
                break
            stop = max(stop, compute_trailing_stop(entry, c, stop, isp, scaled, mm, cost_rt))
            j += 1
        if remaining > 0:
            fills.append((remaining, C[min(j, n - 1)] * (1.0 - slip)))
        gross = sum(f * (px / entry - 1.0) for f, px in fills)
        net = gross - 2.0 * fee                           # comision la intrare + la ieșiri (total 100% din poziție)
        trades.append({'sym': sym, 'setup': m['setup'], 'entry_ts': idx[e], 'bars': max(j, e) - e + 1,
                       'net': net, 'r': net / isp, 'why': why, 'crypto': is_crypto})
        i = max(j, e) + 1 + cooldown_bars
    return trades


def run_variant(sim_data, overrides, disabled=frozenset()):
    saved = {k: DNA[k] for k in overrides}
    try:
        DNA.update(overrides)
        trades = []
        for sym, (ind, ms, is_crypto) in sim_data.items():
            trades += simulate_symbol(sym, ind, ms, is_crypto, disabled)
        return trades
    finally:
        DNA.update(saved)


def summarize_trades(trades):
    n = len(trades)
    if n == 0:
        return {'n': 0, 'wr': 0.0, 'pf': 0.0, 'avg_w': 0.0, 'avg_l': 0.0, 'exp': 0.0, 'sum_pct': 0.0}
    nets = np.array([t['net'] for t in trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    gw, gl = float(wins.sum()), float(abs(losses.sum()))
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    return {'n': n, 'wr': len(wins) / n * 100, 'pf': round(pf, 2),
            'avg_w': float(wins.mean() * 100) if len(wins) else 0.0,
            'avg_l': float(abs(losses.mean()) * 100) if len(losses) else 0.0,
            'exp': float(nets.mean() * 100), 'sum_pct': float(nets.sum() * 100)}


def fmt_stats(label, st):
    return (f"{label:<26} n={st['n']:<4} WR={st['wr']:5.1f}% | PF={st['pf']:5.2f} | "
            f"câștig med +{st['avg_w']:.2f}% / pierdere med -{st['avg_l']:.2f}% | expectanță {st['exp']:+.2f}%/tranzacție")


def run_backtest(crypto_client, stock_client, days):
    print(f"🧪 BACKTEST V11.0 ORIENTFX ({days} zile) | lumânări {DNA['bar_minutes']}m | comision crypto {CRYPTO_FEE_PCT*100:.2f}%/parte + slippage")
    print("   (nivel de lumânare, o poziție pe simbol, fără limite de portofoliu; stop-urile se verifică pe low-ul lumânării)")
    frames = fetch_backtest_frames(crypto_client, stock_client, days)
    if not frames:
        print("❌ Nu am primit date de piață. Verifică cheile Alpaca.")
        return
    sim_data = precompute_signals(frames)
    if not sim_data:
        print("❌ Prea puține lumânări pentru simulare.")
        return

    trades = run_variant(sim_data, {})
    base = summarize_trades(trades)
    print("\n" + "=" * 90)
    print(fmt_stats("TOTAL (parametrii V11.0)", base))
    print(fmt_stats("  crypto", summarize_trades([t for t in trades if t['crypto']])))
    print(fmt_stats("  acțiuni/ETF", summarize_trades([t for t in trades if not t['crypto']])))

    per_setup = {}
    for tag in sorted({t['setup'] for t in trades}):
        per_setup[tag] = summarize_trades([t for t in trades if t['setup'] == tag])
        print(fmt_stats(f"  setup {tag}", per_setup[tag]))
    for why in sorted({t['why'] for t in trades}):
        print(fmt_stats(f"  ieșire {why}", summarize_trades([t for t in trades if t['why'] == why])))

    per_sym = {}
    for sym in sorted({t['sym'] for t in trades}):
        per_sym[sym] = summarize_trades([t for t in trades if t['sym'] == sym])
    worst = sorted(per_sym.items(), key=lambda kv: kv[1]['sum_pct'])[:5]
    best = sorted(per_sym.items(), key=lambda kv: -kv[1]['sum_pct'])[:5]
    print("\n   Cele mai slabe simboluri: " + ", ".join(f"{k} ({v['sum_pct']:+.1f}%, n={v['n']})" for k, v in worst))
    print("   Cele mai bune simboluri:  " + ", ".join(f"{k} ({v['sum_pct']:+.1f}%, n={v['n']})" for k, v in best))

    disable = [tag for tag, st in per_setup.items() if st['n'] >= 6 and st['pf'] < 0.9 and st['exp'] < 0]
    if disable:
        print(f"\n⛔ Setup-uri cu expectanță negativă (n>=6, PF<0.9) -> dezactivate în live: {', '.join(disable)}")

    # --- test in-sample (primele 2/3) vs out-of-sample (ultima treime) pentru variante de parametri V11 ---
    t_min = min(ind.index[100] for ind, _, _ in sim_data.values())
    t_max = max(ind.index[-1] for ind, _, _ in sim_data.values())
    cutoff = t_min + (t_max - t_min) * 2 / 3
    results = []
    for min_stop in (0.0038, 0.0040, 0.0050):
        for be in (1.30, 1.50):
            for ptp in (1.25, 1.40, 1.60):
                for sc in (75, 76, 78):
                    ov = {'min_stop_pct': min_stop, 'max_stop_pct': 0.0110,
                          'breakeven_trigger_r': be, 'partial_tp_r': ptp, 'entry_score_min': sc}
                    tr = run_variant(sim_data, ov, frozenset(disable))
                    results.append((ov, summarize_trades([t for t in tr if t['entry_ts'] < cutoff]),
                                    summarize_trades([t for t in tr if t['entry_ts'] >= cutoff])))
    ranked = sorted([r for r in results if r[1]['n'] >= 5], key=lambda r: (-r[1]['pf'], -r[1]['sum_pct']))[:6]
    print(f"\n📊 Top variante după PF pe primele 2/3 (până la {cutoff:%Y-%m-%d}) și verificate pe ultima treime (nevăzută la alegere):")
    for ov, tr_st, te_st in ranked:
        print(f"   stop_min={ov['min_stop_pct']*100:.2f}% breakeven={ov['breakeven_trigger_r']} partial={ov['partial_tp_r']} scor>={ov['entry_score_min']}"
              f" | TRAIN PF={tr_st['pf']:.2f} (n={tr_st['n']}) | TEST PF={te_st['pf']:.2f} (n={te_st['n']}, exp {te_st['exp']:+.2f}%)")
    robust = [r for r in ranked if r[2]['n'] >= 1 and r[2]['pf'] > 1.0]
    if robust:
        print("   ✅ Variante care rămân profitabile și pe perioada nevăzută: " + str(len(robust)) + " din top " + str(len(ranked)))
    else:
        print("   ⚠️ Nicio variantă din top nu rămâne profitabilă pe ultima treime: nu există dovadă de avantaj statistic. "
              "Nu schimba parametrii după acest raport.")
    print("=" * 90)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "days": days,
        "baseline": base, "per_setup": per_setup, "disable_setups": disable,
        "top_variants": [{"params": ov, "train": a, "test": b} for ov, a, b in ranked],
    }
    try:
        with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=float)
        persist_files_to_github([BACKTEST_FILE], f"📊 Backtest V11.0 {days}z: PF {base['pf']:.2f}, n={base['n']}")
    except Exception as e:
        print(f"   ⚠️ Nu am putut salva {BACKTEST_FILE}: {e}")


# ==========================================
# 9. MAIN
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if not API_KEY or not SECRET_KEY:
        print("❌ EROARE: ALPACA_API_KEY / ALPACA_SECRET_KEY lipsesc din GitHub Secrets!")
        sys.exit(1)

    tc = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    crypto_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    if args.backtest:
        run_backtest(crypto_client, stock_client, args.days)
        return

    first_run = not os.path.exists(MEMORY_FILE)
    init_eq = float(tc.get_account().equity)
    learned_mem = load_learned_memory(init_eq)
    persisted_peak = float(learned_mem.get("generation_start_equity", init_eq))

    if first_run:
        persist_files_to_github([MEMORY_FILE], "💾 Init memorie Gen #1")

    # Dacă BACKTEST_REPORT.json nu există încă în repo, rulăm automat backtestul de 30 zile la pornire
    if not os.path.exists(BACKTEST_FILE):
        print("📊 Generare inițială BACKTEST_REPORT.json pe ultimele 30 de zile...")
        try:
            run_backtest(crypto_client, stock_client, 30)
        except Exception as e:
            print(f"   ⚠️ Backtestul automat la pornire a întâmpinat o eroare: {e}")

    print(f"🚀 AGENT V11.0 ORIENTFX (Gen #{learned_mem.get('generation', 1)}) pornit ({'PAPER' if PAPER else '⚠️ LIVE'}) "
          f"| Prag Rebirth: -20% din banii băgați în poziții | Scor min: {DNA['entry_score_min']} "
          f"| Plafon expunere: {DNA['max_total_exposure']*100:.0f}%")

    social_scores, fear_greed_val = scan_social_media_and_sentiment()
    pos_memory, cooldown_syms, loss_streak, sym_stats = analyze_closed_trades_and_learn(tc, learned_mem)

    extra_disabled = {x.strip() for x in os.environ.get("DISABLED_SETUPS", "").split(",") if x.strip()}
    if USE_BACKTEST_GATE:
        extra_disabled |= load_backtest_gate()
    learned_mem["disabled_setups"] = sorted(set(learned_mem.get("disabled_setups", [])) | extra_disabled)

    if cooldown_syms:
        print(f"🧊 Monede blocate temporar de Motorul de Învățare: {', '.join(sorted(cooldown_syms))}")
    if learned_mem.get("disabled_setups"):
        print(f"⛔ Setup-uri dezactivate (expectanță negativă): {', '.join(learned_mem['disabled_setups'])}")

    try:
        for i in range(CYCLES_PER_RUN):
            print(f"\n================ CICLUL {i + 1}/{CYCLES_PER_RUN} ================")
            try:
                if i > 0 and i % 15 == 0:
                    social_scores, fear_greed_val = scan_social_media_and_sentiment()
                    fresh_mem, cooldown_syms, loss_streak, sym_stats = analyze_closed_trades_and_learn(tc, learned_mem)
                    for k_sym, v_mem in fresh_mem.items():
                        if k_sym in pos_memory and pos_memory[k_sym].get('scaled_out'):
                            v_mem['scaled_out'] = True
                        pos_memory[k_sym] = v_mem
                run_cycle(tc, crypto_client, stock_client, social_scores, fear_greed_val,
                          pos_memory, cooldown_syms, loss_streak, sym_stats, learned_mem,
                          show_scorecard=(i % 15 == 0))
            except SystemExit:
                raise
            except Exception as e:
                print(f"⚠️ Eroare temporară în ciclul {i + 1}: {e}")
            if i < CYCLES_PER_RUN - 1:
                time.sleep(CYCLE_SLEEP_SEC)
    finally:
        save_memory_file(learned_mem)
        new_peak = float(learned_mem.get("generation_start_equity", persisted_peak))
        if new_peak >= persisted_peak * (1.0 + PEAK_PERSIST_STEP):
            persist_files_to_github([MEMORY_FILE], f"💾 Peak equity Gen #{learned_mem.get('generation', 1)}: {new_peak:,.0f}$")


if __name__ == "__main__":
    main()
