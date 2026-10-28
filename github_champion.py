#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BACKTESTER pentru TRADING CHAMPION V8.3 — doar crypto, bare de 5 minute.

Cerințe:
  - trading_champion.py (botul V8.3) în același director
  - pip install alpaca-py pandas numpy
  - ALPACA_API_KEY și ALPACA_SECRET_KEY în mediu (datele crypto istorice sunt gratuite)

Refolosește direct din botul live (import, deci o singură sursă de adevăr pentru semnale):
  compute_indicators, signal_at, entry_allowed, exit_decision, position_size_usd,
  stop_distance_pct, effective_init_stop_pct, round_trip_cost

Reimplementează ca funcții pure (de ținut sincronizate cu botul live):
  target_stop      <- ratchet_stop (logica stop-ului, fără apeluri API)
  lock_stop_price  <- execute_partial_scale_out (nivelul stop-ului după scale-out)

Reguli de simulare:
  - Decizia se ia la închiderea barei i; intrarea se execută la deschiderea barei i+1, cu slippage.
  - Stop-ul se verifică pe minimul barei; dacă deschiderea e sub stop, ieșirea e la deschidere.
  - Ieșirile și scale-out-ul se execută la închiderea barei, cu slippage.
  - Comisioane pe fiecare parte, kill-switch și limita zilnică, ca în botul live.

Limitări:
  - Radarul social nu are istoric -> social = 0.
  - Fear & Greed nu e inclus -> doar regimul BTC (scădere > 1.2% în 30 de minute).
  - Doar crypto. Acțiunile cer feed IEX/SIP și ore de piață.
  - Fără latență, fill-uri parțiale sau stop-limit-uri neexecutate.

Utilizare:
  python backtest_v83.py --days 365                  # baseline + raport IS/OOS (ultimele 30% = OOS)
  python backtest_v83.py --days 365 --ablation       # variante fără componente + control aleatoriu
"""
import os
import sys
import random
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import trading_champion as tcm   # botul V8.3, același director

BTC = "BTC/USD"
CACHE_DIR = "bt_cache"
CHUNK_DAYS = 20           # ~5.8k bare de 5 min per cerere, sub limita API-ului
WARMUP_BARS = 90          # același minim ca în botul live
SLIP = tcm.CRYPTO_SLIPPAGE_PCT
STOP_EXTRA_SLIP = 0.0010  # penalizare pentru stop-limit în execuție
START_CAPITAL = tcm.STARTING_CAPITAL
MAX_DD = tcm.MAX_DRAWDOWN_PCT
DAILY_LOSS = tcm.DAILY_LOSS_LIMIT_PCT
D = tcm.DNA               # același dicționar; overrides-urile îl modifică temporar

VARIANTS = {
    "V8.3 (baseline)": ({}, {}),
    "fără scale-out 50%": ({"use_partial_tp": False}, {}),
    "fără tightening stop": ({}, {"tighten": False}),
    "fără filtru de noapte": ({"night_score_min": tcm.DNA["entry_score_min"]}, {}),
    "fără plafon de epuizare": ({"use_exhaustion_exit": False}, {}),
    "entries aleatorii (control)": ({}, {"random_p": 0.01}),
}


# ==========================================
# 1. DATE ISTORICE
# ==========================================
def _symbol_frame(raw, sym):
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    if isinstance(raw.index, pd.MultiIndex):
        if sym not in raw.index.get_level_values(0):
            return pd.DataFrame()
        raw = raw.xs(sym, level=0)
    idx = pd.DatetimeIndex(raw.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df = raw[["open", "high", "low", "close", "volume"]].astype(float).copy()
    df.index = idx
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_history(client, symbols, start, end):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tf = TimeFrame(D["bar_minutes"], TimeFrameUnit.Minute)
    data = {}
    for sym in symbols:
        path = os.path.join(CACHE_DIR, f"{tcm.norm(sym)}_{start:%Y%m%d}_{end:%Y%m%d}.pkl")
        if os.path.exists(path):
            data[sym] = pd.read_pickle(path)
            continue
        parts, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
            raw = client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=sym, timeframe=tf, start=cur, end=nxt)).df
            part = _symbol_frame(raw, sym)
            if not part.empty:
                parts.append(part)
            cur = nxt
        if not parts:
            print(f"⚠️ Fără date pentru {sym}")
            continue
        df = pd.concat(parts).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df.to_pickle(path)
        data[sym] = df
        print(f"📥 {sym}: {len(df)} bare")
    return data


# ==========================================
# 2. LOGICA DE STOP (replică pură a botului live)
# ==========================================
def target_stop(entry, cur_stop, init_pct, price, peak, atr, cost_rt, scaled_out, tighten=True):
    """Stop-ul dorit, conform ratchet_stop. Nu scade niciodată sub cur_stop."""
    profit = price / entry - 1.0
    floor = min(entry * (1.0 - init_pct), price * (1.0 - init_pct))
    if tighten and profit >= 0.003:
        floor = max(floor, entry * (1.0 - min(init_pct * 0.55, 0.012)))
    new_stop = max(cur_stop, floor)
    already_in_profit = cur_stop >= entry or scaled_out
    if profit >= init_pct * D["breakeven_trigger_r"] or already_in_profit:
        trail = max(price, peak) - D["trail_atr_mult"] * atr
        new_stop = max(new_stop, trail, entry * (1.0 + cost_rt))
    new_stop = min(new_stop, price * 0.994)
    return max(new_stop, cur_stop)


def lock_stop_price(entry, price, peak, atr, cost_rt):
    """Nivelul stop-ului după scale-out 50%, conform execute_partial_scale_out."""
    trail = peak - D["trail_atr_mult"] * atr
    return min(price * 0.994, max(entry * (1.0 + cost_rt + 0.0015), trail))


# ==========================================
# 3. STARE ȘI EXECUȚIE SIMULATĂ
# ==========================================
def _equity(st):
    return st["cash"] + sum(p["qty"] * st["last_close"][s] for s, p in st["pos"].items())


def _open(st, sym, invest, init_pct, score, px_open, ts):
    fill = px_open * (1.0 + SLIP)
    qty = invest * (1.0 - tcm.CRYPTO_FEE_PCT) / fill
    st["cash"] -= invest
    st["pos"][sym] = dict(qty=qty, entry=fill, cost=invest, proceeds=0.0, init_pct=init_pct,
                          stop=fill * (1.0 - init_pct), peak=fill, scaled=False,
                          entry_ts=ts, score=score)


def _close(st, sym, fill, ts, reason, fraction=1.0):
    p = st["pos"][sym]
    qty_sell = p["qty"] * fraction
    proceeds = qty_sell * fill * (1.0 - tcm.CRYPTO_FEE_PCT)
    st["cash"] += proceeds
    p["proceeds"] += proceeds
    p["qty"] = p["qty"] - qty_sell if fraction < 1.0 else 0.0
    if p["qty"] > 1e-12:
        return  # scale-out parțial: poziția rămâne deschisă
    pnl = p["proceeds"] - p["cost"]
    pnl_pct = pnl / p["cost"]
    st["trades"].append(dict(sym=sym, entry_ts=p["entry_ts"], exit_ts=ts, pnl=pnl,
                             pnl_pct=pnl_pct, r=pnl_pct / p["init_pct"], reason=reason))
    del st["pos"][sym]
    st["cooldown"][sym] = ts + timedelta(minutes=D["reentry_cooldown_min"])
    if pnl < 0:
        st["streak"][sym] = st["streak"].get(sym, 0) + 1
        if st["streak"][sym] >= 2:
            st["cooldown"][sym] = ts + timedelta(hours=3)
    else:
        st["streak"][sym] = 0


def _alt_btc(s, btc_r):
    """Forța relativă vs BTC se aplică doar altcoin-urilor, ca în botul live."""
    if s == BTC or s in tcm.SAFE_HAVEN_CRYPTO:
        return None
    return btc_r


# ==========================================
# 4. BUCLA DE BACKTEST
# ==========================================
def run_backtest(data, all_ts, cfg):
    rng = random.Random(cfg.get("seed", 42))
    tighten = cfg.get("tighten", True)
    random_p = cfg.get("random_p")
    cost_rt = tcm.round_trip_cost(True)

    ind, arr, row_of = {}, {}, {}
    for s, df in data.items():
        ind[s] = tcm.compute_indicators(df)
        arr[s] = {k: df[k].values for k in ("open", "high", "low", "close")}
        row_of[s] = {ts: i for i, ts in enumerate(df.index)}
    btc_ret = ind[BTC]["ret_2h"].values
    btc_close = arr[BTC]["close"]

    st = dict(cash=START_CAPITAL, pos={}, pending=[], pending_syms=set(), cooldown={},
              streak={}, last_close={}, trades=[], curve=[], peak=START_CAPITAL)
    day_cur, day_start_eq = None, START_CAPITAL

    for ts in all_ts:
        # 0) bara curentă: prețuri de închidere și regimul BTC
        rows = {}
        for s in data:
            i = row_of[s].get(ts)
            if i is not None:
                rows[s] = i
                st["last_close"][s] = arr[s]["close"][i]
        ib = rows.get(BTC)
        btc_r_now = float(btc_ret[ib]) if ib is not None else None
        risk_off = (ib is not None and ib >= 6
                    and btc_close[ib] / btc_close[ib - 6] - 1.0 < -0.012)

        # 1) intrările pending, executate la deschiderea barei curente
        still = []
        for it in st["pending"]:
            s = it["sym"]
            if s not in rows:
                still.append(it)
                continue
            st["pending_syms"].discard(s)
            if s in st["pos"] or len(st["pos"]) >= D["max_crypto_positions"]:
                continue
            invest = min(it["invest"], st["cash"] * 0.95)
            if invest < tcm.MIN_POSITION_USD:
                continue
            _open(st, s, invest, it["init_pct"], it["score"], arr[s]["open"][rows[s]], ts)
        st["pending"] = still

        # 2) gestionarea pozițiilor deschise
        for s in list(st["pos"].keys()):
            if s not in rows:
                continue
            i = rows[s]
            p = st["pos"][s]
            o, h, l, c = (arr[s]["open"][i], arr[s]["high"][i],
                          arr[s]["low"][i], arr[s]["close"][i])

            # a) stop intrabar
            if l <= p["stop"]:
                _close(st, s, min(o, p["stop"]) * (1.0 - STOP_EXTRA_SLIP), ts, "STOP")
                continue

            # b) decizii la închiderea barei
            p["peak"] = max(p["peak"], h)
            m = tcm.signal_at(ind[s], i, 0, live_price=c, live_high=h,
                              btc_ret_2h=_alt_btc(s, btc_r_now))
            m["peak_since_entry"] = p["peak"]
            init = tcm.effective_init_stop_pct(p["entry"], p["stop"], tcm.stop_distance_pct(m))
            profit = c / p["entry"] - 1.0

            reason = tcm.exit_decision(profit, m, init, cost_rt, market_val=p["qty"] * c)
            if reason:
                _close(st, s, c * (1.0 - SLIP), ts, reason)
                continue

            if (D["use_partial_tp"] and not p["scaled"]
                    and profit >= cost_rt + init * D["partial_tp_r"]):
                _close(st, s, c * (1.0 - SLIP), ts, "SCALE-OUT 50%", fraction=0.5)
                p["scaled"] = True
                p["stop"] = lock_stop_price(p["entry"], c, p["peak"], m["atr"], cost_rt)
                continue

            new_stop = target_stop(p["entry"], p["stop"], init, c, p["peak"], m["atr"],
                                   cost_rt, p["scaled"], tighten)
            if new_stop > p["stop"] * 1.0025:
                p["stop"] = new_stop

        # 3) equity, kill-switch, limita zilnică
        eq = _equity(st)
        if day_cur != ts.date():
            day_cur, day_start_eq = ts.date(), eq
        st["peak"] = max(st["peak"], eq)
        if eq <= st["peak"] * (1.0 - MAX_DD):
            for s in list(st["pos"].keys()):
                _close(st, s, st["last_close"][s] * (1.0 - SLIP), ts, "KILL-SWITCH")
            st["curve"].append((ts, _equity(st), 0))
            print(f"🚨 Kill-switch la {ts}: equity {_equity(st):,.0f}$")
            break
        st["curve"].append((ts, eq, len(st["pos"])))
        blocked = eq <= day_start_eq * (1.0 - DAILY_LOSS)

        # 4) candidați noi (decizia se ia la închiderea barei, intrarea e la următoarea)
        if not blocked:
            committed = sum(it["invest"] for it in st["pending"])
            cands = []
            for s in data:
                i = rows.get(s)
                if i is None or i < WARMUP_BARS or s in st["pos"] or s in st["pending_syms"]:
                    continue
                if s in st["cooldown"] and ts < st["cooldown"][s]:
                    continue
                is_safe = s in tcm.SAFE_HAVEN_CRYPTO
                if risk_off and not is_safe:
                    continue
                m = tcm.signal_at(ind[s], i, 0, btc_ret_2h=_alt_btc(s, btc_r_now))
                if is_safe and risk_off and m["setup"]:
                    m["score"] += 15
                if random_p is not None:
                    if rng.random() >= random_p:
                        continue
                    m["score"] = 70
                elif not tcm.entry_allowed(m, cost_rt, now_utc=ts):
                    continue
                cands.append((m["score"], s, tcm.stop_distance_pct(m)))

            cands.sort(key=lambda x: x[0], reverse=True)
            gross = sum(p["qty"] * st["last_close"][k] for k, p in st["pos"].items())
            for score, s, init_pct in cands:
                if len(st["pos"]) + len(st["pending"]) >= D["max_crypto_positions"]:
                    break
                invest = tcm.position_size_usd(eq, st["cash"] - committed, gross, init_pct, score)
                if invest < tcm.MIN_POSITION_USD:
                    continue
                st["pending"].append(dict(sym=s, invest=invest, init_pct=init_pct, score=score))
                st["pending_syms"].add(s)
                committed += invest
                gross += invest

    # închidem ce a rămas deschis, la ultimul preț
    for s in list(st["pos"].keys()):
        _close(st, s, st["last_close"][s] * (1.0 - SLIP), all_ts[-1], "FINAL")
    st["curve"].append((all_ts[-1], _equity(st), 0))
    return dict(trades=st["trades"], curve=st["curve"])


# ==========================================
# 5. RAPORTARE
# ==========================================
def _stats(trades):
    if trades.empty:
        return dict(n=0, win=0.0, pf=0.0, exp_r=0.0, avg_w=0.0, avg_l=0.0)
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    gross_win, gross_loss = wins["pnl"].sum(), -losses["pnl"].sum()
    return dict(
        n=len(trades),
        win=100.0 * len(wins) / len(trades),
        pf=gross_win / gross_loss if gross_loss > 0 else float("inf"),
        exp_r=float(trades["r"].mean()),
        avg_w=100.0 * wins["pnl_pct"].mean() if len(wins) else 0.0,
        avg_l=100.0 * losses["pnl_pct"].mean() if len(losses) else 0.0,
    )


def report(name, res, split_ts):
    curve = pd.DataFrame(res["curve"], columns=["ts", "equity", "npos"]).set_index("ts")
    trades = pd.DataFrame(res["trades"])
    eq = curve["equity"].astype(float)
    final = float(eq.iloc[-1])
    ret = (final / START_CAPITAL - 1.0) * 100
    dd = float((eq / eq.cummax() - 1.0).min()) * 100
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    sharpe = (float(daily.mean() / daily.std() * np.sqrt(365))
              if len(daily) > 2 and daily.std() > 0 else 0.0)
    expo = float((curve["npos"] > 0).mean()) * 100
    s = _stats(trades)

    print(f"\n=== {name} ===")
    print(f"Randament {ret:+.2f}% | Max drawdown {dd:.2f}% | Sharpe {sharpe:.2f} | Expunere {expo:.0f}%")
    print(f"Tranzacții {s['n']} | Win rate {s['win']:.1f}% | Profit factor {s['pf']:.2f} | "
          f"Expectanță {s['exp_r']:+.3f}R | Câștig mediu {s['avg_w']:+.2f}% | "
          f"Pierdere medie {s['avg_l']:+.2f}%")
    if split_ts is not None and not trades.empty:
        eq_split = float(eq.asof(split_ts))
        si = _stats(trades[trades["entry_ts"] < split_ts])
        so = _stats(trades[trades["entry_ts"] >= split_ts])
        print(f"  IS  (până la {split_ts:%Y-%m-%d}): ret {(eq_split / START_CAPITAL - 1) * 100:+.2f}% "
              f"| {si['n']} tranz. | PF {si['pf']:.2f} | exp {si['exp_r']:+.3f}R")
        print(f"  OOS (de la {split_ts:%Y-%m-%d}): ret {(final / eq_split - 1) * 100:+.2f}% "
              f"| {so['n']} tranz. | PF {so['pf']:.2f} | exp {so['exp_r']:+.3f}R")
    if not trades.empty:
        print("  Ieșiri:", trades["reason"].str[:24].value_counts().head(6).to_dict())
    return dict(name=name, ret=ret, dd=dd, sharpe=sharpe, n=s["n"], pf=s["pf"], exp=s["exp_r"])


def print_table(rows):
    print("\n" + "=" * 92)
    print(f"{'Variantă':<32}{'Ret %':>9}{'MaxDD %':>9}{'Sharpe':>8}{'Tranz':>7}{'PF':>8}{'Exp R':>9}")
    print("-" * 92)
    for r in rows:
        print(f"{r['name']:<32}{r['ret']:>+9.2f}{r['dd']:>9.2f}{r['sharpe']:>8.2f}"
              f"{r['n']:>7}{r['pf']:>8.2f}{r['exp']:>+9.3f}")


@contextmanager
def dna_overrides(over):
    saved = dict(tcm.DNA)
    tcm.DNA.update(over)
    try:
        yield
    finally:
        tcm.DNA.clear()
        tcm.DNA.update(saved)


# ==========================================
# 6. MAIN
# ==========================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--test-frac", type=float, default=0.3,
                    help="fracțiunea finală a perioadei raportată ca OOS (0 = dezactivat)")
    ap.add_argument("--ablation", action="store_true")
    args = ap.parse_args()

    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("❌ Lipsesc ALPACA_API_KEY / ALPACA_SECRET_KEY")

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    start = end - timedelta(days=args.days)
    client = CryptoHistoricalDataClient(key, secret)
    data = fetch_history(client, tcm.ALL_CRYPTO, start, end)
    if BTC not in data:
        sys.exit("❌ Lipsește BTC/USD (referința de regim)")

    all_ts = sorted(set().union(*(set(df.index) for df in data.values())))
    split_ts = (all_ts[int(len(all_ts) * (1.0 - args.test_frac))]
                if args.test_frac > 0 else None)

    print(f"\nUniversul: {', '.join(data)}")
    print(f"Perioada: {start:%Y-%m-%d} → {end:%Y-%m-%d} | bare unice: {len(all_ts)}")
    bh = {s: float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100 for s, df in data.items()}
    print("Buy & hold: " + ", ".join(f"{s} {v:+.1f}%" for s, v in bh.items()))
    print(f"Buy & hold egal-ponderat: {np.mean(list(bh.values())):+.2f}%")

    variants = VARIANTS if args.ablation else {"V8.3 (baseline)": ({}, {})}
    rows = []
    for name, (dna_over, cfg) in variants.items():
        print(f"\n▶ Rulez: {name} ...")
        with dna_overrides(dna_over):
            res = run_backtest(data, all_ts, cfg)
        rows.append(report(name, res, split_ts))
    if len(rows) > 1:
        print_table(rows)


if __name__ == "__main__":
    main()
