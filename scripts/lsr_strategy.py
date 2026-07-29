#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LSR — Liquidity Sweep Reversal（流动性清扫反转）基石策略

形态定义（做多方向）：
  1) 前期存在一个低点 L_prior（流动性池，止损单堆积处）
  2) L_prior 之后有一波小反弹，形成小高点 H0
  3) 价格再度下跌，创出新低 L_sweep < L_prior —— 扫掉流动性
     · 该 K 线需带明显下影线（wick_ratio）
     · 该 K 线需放量（相对基准量 vol_mult 倍以上）
     · 之后 reclaim_bars 根内需收回 L_prior 上方（扫失败）
  4) 反弹在 breakout_bars 根内收盘突破 H0
  5) 突破后 consol_min~consol_max 根内做窄幅回调，横在 H0 上方
     · 回调深度 ≤ 冲击腿的 pullback_max
     · 最低价不跌破 H0 - hold_tol_atr * ATR
     · 波幅收缩、量能萎缩（可选条件）
  6) 决策 K 收盘出信号，下一根 K 开盘进场

设计原则：
  · 严格无未来函数：摆动点需 right_bars 根确认后才可用；信号在 t 收盘产生，t+1 开盘成交
  · 每个候选样本都会记录“各条件是否通过”，即使不满足也保留 —— 用于条件消融分析
    （不用人工看几百张图，直接跑 --ablate 看每个条件对期望的边际贡献）

用法：
  python lsr_strategy.py --csv btc_15m.csv
  python lsr_strategy.py --csv btc_15m.csv --ablate
  python lsr_strategy.py --csv btc_15m.csv --out trades.csv

CSV 需包含列：timestamp, open, high, low, close, volume
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
@dataclass
class Params:
    # --- 摆动点识别 ---
    swing_left: int = 3
    swing_right: int = 3            # 需要右侧 N 根确认，直接决定信号延迟

    # --- 结构搜索范围 ---
    lookback: int = 96              # 向前找 L_prior / H0 的最大范围（15m: 96 = 24h）

    # --- 清扫 K 线质量 ---
    sweep_wick_ratio: float = 0.45  # 下影线 / 整根振幅
    reclaim_bars: int = 3           # 几根内必须收回 L_prior 上方

    # --- 量能 ---
    vol_mult: float = 2.5
    vol_mode: str = "tod"           # "tod" = 按时段基准（推荐）| "rolling" = 滚动均量
    vol_window: int = 50            # rolling 模式的窗口
    tod_days: int = 20              # tod 模式：同一时段过去 N 天的中位数

    # --- 突破 ---
    breakout_bars: int = 16         # 从清扫低点算起，几根内必须收破 H0

    # --- 窄幅回调 ---
    consol_min: int = 2
    consol_max: int = 6
    pullback_max: float = 0.382     # 回调深度 / 冲击腿长度
    hold_tol_atr: float = 0.25      # 允许跌破 H0 的容忍度（ATR 倍数）
    range_compression: float = 0.60 # 整理区平均振幅 / 冲击段 ATR
    vol_contraction: float = 0.80   # 整理区均量 / 突破 K 量

    # --- 风险与出场 ---
    atr_window: int = 14
    stop_mode: str = "sweep"        # "sweep" = 清扫低点下方 | "consol" = 整理区下沿
    stop_buffer_atr: float = 0.15
    tp_r: float = 2.5               # 止盈 R 倍数
    time_stop_bars: int = 40        # 超时平仓
    be_at_r: Optional[float] = 1.0  # 到 +1R 移到保本；None 关闭

    # --- 必选条件（消融时会改动这个集合）---
    required: tuple = field(default=(
        "ok_wick", "ok_volume", "ok_reclaim", "ok_breakout",
        "ok_hold", "ok_pullback",
    ))


OPTIONAL_FLAGS = [
    "ok_wick", "ok_volume", "ok_reclaim",
    "ok_hold", "ok_pullback", "ok_compression", "ok_volcontract",
]


# ----------------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------------
def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(p.atr_window, min_periods=p.atr_window // 2).mean()

    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body_low = df[["open", "close"]].min(axis=1)
    df["wick_ratio"] = ((body_low - df["low"]) / rng).fillna(0.0)

    df["vol_base"] = _volume_baseline(df, p)
    df["rvol"] = df["volume"] / df["vol_base"]
    return df


def _volume_baseline(df: pd.DataFrame, p: Params) -> pd.Series:
    """量能基准。tod 模式按时段取基准，避免只是测出了'欧美盘开盘'这个因子。"""
    roll = df["volume"].shift(1).rolling(p.vol_window, min_periods=10).mean()
    if p.vol_mode != "tod" or not isinstance(df.index, pd.DatetimeIndex):
        return roll
    slot = pd.Series(df.index.hour * 60 + df.index.minute, index=df.index)
    tod = df["volume"].groupby(slot).transform(
        lambda s: s.shift(1).rolling(p.tod_days, min_periods=max(3, p.tod_days // 3)).median()
    )
    return tod.fillna(roll)


def find_swings(df: pd.DataFrame, left: int, right: int):
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    is_sh = np.zeros(n, dtype=bool)
    is_sl = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        wh = highs[i - left:i + right + 1]
        wl = lows[i - left:i + right + 1]
        if wh.argmax() == left:
            is_sh[i] = True
        if wl.argmin() == left:
            is_sl[i] = True
    return is_sh, is_sl


# ----------------------------------------------------------------------------
# 形态扫描
# ----------------------------------------------------------------------------
def scan_candidates(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """扫出所有'结构成立'的候选，并逐条记录各质量条件的通过情况。"""
    is_sh, is_sl = find_swings(df, p.swing_left, p.swing_right)
    sh_idx = np.flatnonzero(is_sh)
    sl_idx = np.flatnonzero(is_sl)

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    atr = df["atr"].to_numpy()
    wick = df["wick_ratio"].to_numpy()
    rvol = df["rvol"].to_numpy()
    n = len(df)

    rows = []
    for i_s in sl_idx:
        if i_s < p.lookback or i_s + p.swing_right + 2 >= n:
            continue
        if not np.isfinite(atr[i_s]) or atr[i_s] <= 0:
            continue

        # --- 1. 找被扫掉的前低 L_prior：最近的、比当前低点更高的摆动低点 ---
        prior_pool = [j for j in sl_idx
                      if i_s - p.lookback <= j < i_s - p.swing_right
                      and l[j] > l[i_s]]
        if not prior_pool:
            continue
        i_prior = prior_pool[-1]
        l_prior = l[i_prior]

        # --- 2. 找 L_prior 与清扫低点之间的小高点 H0 ---
        h0_pool = [j for j in sh_idx
                   if i_prior < j < i_s and j + p.swing_right <= i_s]
        if not h0_pool:
            continue
        i_h0 = max(h0_pool, key=lambda j: h[j])
        h0 = h[i_h0]
        if h0 <= l_prior:
            continue

        # --- 3. 清扫 K 线质量 ---
        ok_wick = bool(wick[i_s] >= p.sweep_wick_ratio)
        ok_volume = bool(np.isfinite(rvol[i_s]) and rvol[i_s] >= p.vol_mult)

        # 收回前低上方
        rec_end = min(i_s + p.reclaim_bars, n - 1)
        ok_reclaim = bool(np.any(c[i_s:rec_end + 1] > l_prior))

        # --- 4. 突破 H0（收盘价），且必须发生在清扫低点被确认之后 ---
        b = -1
        start = max(i_s + p.swing_right, i_s + 1)
        for j in range(start, min(i_s + p.breakout_bars, n - 1) + 1):
            if c[j] > h0:
                b = j
                break
        ok_breakout = b > 0
        if not ok_breakout:
            rows.append(_row(df, i_s, i_prior, i_h0, -1, -1, l_prior, h0,
                             np.nan, np.nan, np.nan, np.nan, np.nan,
                             ok_wick, ok_volume, ok_reclaim, False,
                             False, False, False, False, atr[i_s]))
            continue

        imp_high = h[i_s:b + 1].max()
        imp_len = imp_high - l[i_s]
        if imp_len <= 0:
            continue

        # --- 5. 窄幅回调：取第一个满足 hold + 浅回调 的窗口 ---
        chosen = None
        for cc in range(p.consol_min, p.consol_max + 1):
            e = b + cc
            if e >= n - 1:
                break
            seg_lo = l[b + 1:e + 1].min()
            depth = (imp_high - seg_lo) / imp_len
            hold = seg_lo >= h0 - p.hold_tol_atr * atr[b]
            if hold and depth <= p.pullback_max:
                chosen = (cc, e, seg_lo, depth)
                break
        if chosen is None:
            cc = p.consol_min
            e = b + cc
            if e >= n - 1:
                continue
            seg_lo = l[b + 1:e + 1].min()
            depth = (imp_high - seg_lo) / imp_len
        else:
            cc, e, seg_lo, depth = chosen

        seg_hi = h[b + 1:e + 1].max()
        ok_hold = bool(seg_lo >= h0 - p.hold_tol_atr * atr[b])
        ok_pullback = bool(depth <= p.pullback_max)

        seg_rng = float(np.mean(h[b + 1:e + 1] - l[b + 1:e + 1]))
        ok_compression = bool(atr[b] > 0 and seg_rng <= p.range_compression * atr[b])
        ok_volcontract = bool(v[b] > 0 and float(np.mean(v[b + 1:e + 1])) <= p.vol_contraction * v[b])

        rows.append(_row(df, i_s, i_prior, i_h0, b, e, l_prior, h0,
                         imp_high, depth, seg_lo, seg_hi, seg_rng,
                         ok_wick, ok_volume, ok_reclaim, True,
                         ok_hold, ok_pullback, ok_compression, ok_volcontract, atr[b]))

    return pd.DataFrame(rows)


def _row(df, i_s, i_prior, i_h0, b, e, l_prior, h0, imp_high, depth,
         seg_lo, seg_hi, seg_rng, ok_wick, ok_volume, ok_reclaim,
         ok_breakout, ok_hold, ok_pullback, ok_compression, ok_volcontract, atr_b):
    return dict(
        decision_idx=e, decision_time=(df.index[e] if e > 0 else pd.NaT),
        sweep_idx=i_s, sweep_time=df.index[i_s], sweep_low=df["low"].iloc[i_s],
        prior_low_idx=i_prior, prior_low=l_prior,
        h0_idx=i_h0, h0=h0,
        breakout_idx=b, imp_high=imp_high,
        consol_low=seg_lo, consol_high=seg_hi, consol_rng=seg_rng, pullback=depth,
        rvol=df["rvol"].iloc[i_s], wick=df["wick_ratio"].iloc[i_s], atr=atr_b,
        ok_wick=ok_wick, ok_volume=ok_volume, ok_reclaim=ok_reclaim,
        ok_breakout=ok_breakout, ok_hold=ok_hold, ok_pullback=ok_pullback,
        ok_compression=ok_compression, ok_volcontract=ok_volcontract,
    )


# ----------------------------------------------------------------------------
# 回测
# ----------------------------------------------------------------------------
def simulate(df: pd.DataFrame, cands: pd.DataFrame, p: Params,
             required=None) -> pd.DataFrame:
    if cands.empty:
        return pd.DataFrame()
    required = list(required if required is not None else p.required)

    sel = cands.copy()
    for flag in required:
        sel = sel[sel[flag]]
    sel = sel[sel["decision_idx"] > 0].sort_values("decision_idx")

    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    l = df["low"].to_numpy(); c = df["close"].to_numpy()
    n = len(df)

    trades = []
    busy_until = -1
    for _, r in sel.iterrows():
        t = int(r["decision_idx"])
        entry_i = t + 1
        if entry_i >= n or entry_i <= busy_until:
            continue

        entry = o[entry_i]
        buf = p.stop_buffer_atr * r["atr"]
        stop = (r["sweep_low"] - buf) if p.stop_mode == "sweep" else (r["consol_low"] - buf)
        risk = entry - stop
        if risk <= 0:
            continue
        target = entry + p.tp_r * risk

        exit_i, exit_px, reason = None, None, None
        cur_stop = stop
        for j in range(entry_i, min(entry_i + p.time_stop_bars, n - 1) + 1):
            # 保守假设：同一根 K 内先触止损
            if l[j] <= cur_stop:
                exit_i, exit_px, reason = j, cur_stop, "stop"
                break
            if h[j] >= target:
                exit_i, exit_px, reason = j, target, "target"
                break
            if p.be_at_r is not None and h[j] >= entry + p.be_at_r * risk:
                cur_stop = max(cur_stop, entry)
        if exit_i is None:
            exit_i = min(entry_i + p.time_stop_bars, n - 1)
            exit_px, reason = c[exit_i], "time"

        trades.append(dict(
            entry_time=df.index[entry_i], exit_time=df.index[exit_i],
            entry=entry, stop=stop, target=target, exit=exit_px,
            r=(exit_px - entry) / risk, reason=reason,
            bars_held=exit_i - entry_i, sweep_time=r["sweep_time"],
            rvol=r["rvol"], pullback=r["pullback"],
        ))
        busy_until = exit_i

    return pd.DataFrame(trades)


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return dict(n=0)
    r = trades["r"]
    wins, losses = r[r > 0], r[r <= 0]
    eq = r.cumsum()
    dd = (eq.cummax() - eq).max()
    return dict(
        n=len(r),
        win_rate=round(len(wins) / len(r), 4),
        avg_win_r=round(wins.mean(), 3) if len(wins) else 0.0,
        avg_loss_r=round(losses.mean(), 3) if len(losses) else 0.0,
        expectancy_r=round(r.mean(), 4),
        total_r=round(r.sum(), 2),
        profit_factor=round(wins.sum() / abs(losses.sum()), 3) if len(losses) and losses.sum() != 0 else float("inf"),
        max_dd_r=round(dd, 2),
        avg_bars=round(trades["bars_held"].mean(), 1),
    )


def ablate(df: pd.DataFrame, cands: pd.DataFrame, p: Params) -> pd.DataFrame:
    """条件消融：看每个条件对期望的边际贡献，决定哪些是必要条件、哪些是噪音。"""
    base_req = list(p.required)
    out = [dict(config="baseline(全部必选)", **summarize(simulate(df, cands, p, base_req)))]

    for flag in OPTIONAL_FLAGS:
        if flag in base_req:
            req = [f for f in base_req if f != flag]
            label = f"去掉 {flag}"
        else:
            req = base_req + [flag]
            label = f"加上 {flag}"
        out.append(dict(config=label, **summarize(simulate(df, cands, p, req))))

    out.append(dict(config="仅结构(ok_breakout)", **summarize(simulate(df, cands, p, ["ok_breakout"]))))
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------
def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [x.strip().lower() for x in df.columns]
    tcol = next((x for x in ("timestamp", "time", "datetime", "open_time", "date") if x in df.columns), None)
    if tcol is None:
        raise ValueError("CSV 缺少时间列（timestamp/time/datetime/open_time/date）")
    ts = df[tcol]
    if np.issubdtype(ts.dtype, np.number):
        unit = "ms" if ts.iloc[0] > 1e11 else "s"
        df["ts"] = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        df["ts"] = pd.to_datetime(ts, utc=True)
    df = df.set_index("ts").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise ValueError(f"CSV 缺少列: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["open", "high", "low", "close", "volume"]].dropna()


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """把 K 线上下翻转，同一套多头逻辑即可扫出做空形态（价格为负值，仅用于形态与 R 统计）。"""
    out = df.copy()
    out["open"], out["close"] = -df["open"], -df["close"]
    out["high"], out["low"] = -df["low"], -df["high"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--short", action="store_true", help="扫做空方向（翻转K线）")
    ap.add_argument("--ablate", action="store_true", help="跑条件消融")
    ap.add_argument("--out", default=None, help="导出成交明细 CSV")
    ap.add_argument("--dump-candidates", default=None, help="导出全部候选样本 CSV")
    args = ap.parse_args()

    p = Params()
    df = load_csv(args.csv)
    if args.short:
        df = mirror(df)
    df = add_indicators(df, p)

    cands = scan_candidates(df, p)
    print(f"数据 {len(df)} 根K线 | {df.index[0]} → {df.index[-1]}")
    print(f"结构候选 {len(cands)} 个（其中完成突破 {int(cands['ok_breakout'].sum()) if len(cands) else 0} 个）\n")

    if args.dump_candidates and len(cands):
        cands.to_csv(args.dump_candidates, index=False)
        print(f"候选已导出 → {args.dump_candidates}")

    trades = simulate(df, cands, p)
    stats = summarize(trades)
    print("=== 基线结果 ===")
    for k, val in stats.items():
        print(f"  {k:<14} {val}")

    if args.out and len(trades):
        trades.to_csv(args.out, index=False)
        print(f"\n成交明细 → {args.out}")

    if args.ablate:
        print("\n=== 条件消融 ===")
        print(ablate(df, cands, p).to_string(index=False))


if __name__ == "__main__":
    main()
