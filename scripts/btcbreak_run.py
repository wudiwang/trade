"""btcbreak_pump 形态实例提取: BTC 放量跌破两日低 → PUMP 首个底分型(+停顿)。

只找实例、只数个数; 【不结算、不算胜率/期望/平均R】—— CLAUDE.md 铁律七: 先出图审核, 后跑期望。
不下单、不连私有接口, 只读币安公开合约K线。

做的事:
  1. 找到目标品种(默认 PUMPUSDT)的第一根 5m 合约K, 下载它和 BTCUSDT 到现在的全部 5m K线
     (BTC 多带一段 lookback 预热, 让上市第一根起两日低就有效), 缓存到 .btcache/<SYMBOL>_<tf>_btcbreak.json;
     再次运行只补尾部。
  2. 跑 app.engine.strat_btcbreak.replay()。
  3. 把 params / events / episodes + 出图工具需要的元数据写到 --out。
  4. 只打印个数: 触发K、轮次、入场、到期。

用法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/btcbreak_run.py --out <结果.json>
  可选: --no-require-stall  --lookback-hours 48  --vol-mult 2  --ttl-hours 4  --rr 2 ...
        --offline(只用缓存, 不联网)  --refresh(丢弃缓存重下)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.engine.strat_btcbreak import (  # noqa: E402
    STATUS, BtcBreakParams, replay, tf_to_ms)

FAPI = "https://fapi.binance.com"
CACHE_DIR = os.path.join(ROOT, ".btcache")
CACHE_TAG = "btcbreak"
LIMIT = 1500
BKK = timezone(timedelta(hours=7))
ROW_KEYS = ("open_time", "open", "high", "low", "close", "volume")


def log(msg: str) -> None:
    """进度信息走 stderr, stdout 只留个数。"""
    print(msg, file=sys.stderr, flush=True)


def fmt_bkk(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, BKK).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 网络(只有公开行情)
def http_get_json(path: str, params: dict, retries: int = 5):
    """GET 公开接口 → (json, 已用权重)。429/418 按 Retry-After 等待。"""
    url = f"{FAPI}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                used = int(resp.headers.get("X-MBX-USED-WEIGHT-1M") or 0)
                return json.loads(resp.read().decode("utf-8")), used
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                wait = int(e.headers.get("Retry-After") or 30)
                log(f"  限频({e.code}), 等待 {wait}s")
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {path} failed after {retries} retries")


def server_time_ms(fetch=http_get_json) -> int:
    data, _ = fetch("/fapi/v1/time", {})
    return int(data["serverTime"])


def first_bar_time(symbol: str, tf: str, fetch=http_get_json) -> int:
    """该品种最早一根K的 open_time(startTime=0 取第一根)。"""
    data, _ = fetch("/fapi/v1/klines", {"symbol": symbol, "interval": tf,
                                        "startTime": 0, "limit": 1})
    if not data:
        raise RuntimeError(f"{symbol} {tf}: 没有任何K线")
    return int(data[0][0])


def _row(r) -> dict:
    return {"open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}


def fetch_range(symbol: str, tf: str, start_ms: int, end_ms: int,
                fetch=http_get_json, pace: float = 0.4, sleep=time.sleep) -> list:
    """下载 open_time ∈ [start_ms, end_ms) 且在 end_ms 之前已收盘的K。每次 1500 根, 请求间隔 ≥ pace 秒。"""
    tf_ms = tf_to_ms(tf)
    out = []
    cur = int(start_ms)
    while cur + tf_ms <= end_ms:
        data, used = fetch("/fapi/v1/klines", {"symbol": symbol, "interval": tf,
                                               "startTime": cur, "limit": LIMIT})
        if not data:
            break
        last_open = cur
        for r in data:
            t = int(r[0])
            last_open = max(last_open, t)
            if cur <= t and t + tf_ms <= end_ms:
                out.append(_row(r))
        if len(out) and len(out) % (LIMIT * 20) < LIMIT:
            log(f"  {symbol} {tf} 已下载 {len(out)} 根 → {fmt_bkk(out[-1]['open_time'])}")
        nxt = last_open + tf_ms
        if nxt <= cur:
            break
        cur = nxt
        sleep(max(pace, 0.25) if used < 1800 else 15.0)
    return out


# ---------------------------------------------------------------- 缓存
def cache_path(symbol: str, tf: str, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, f"{symbol}_{tf}_{CACHE_TAG}.json")


def load_cache(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return rows if isinstance(rows, list) else []


def save_cache(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    os.replace(tmp, path)


def ensure_history(symbol: str, tf: str, start_ms: int, now_ms: int, path: str,
                   fetch=http_get_json, pace: float = 0.4, offline: bool = False,
                   refresh: bool = False, sleep=time.sleep) -> list:
    """返回 [start_ms, now) 的已收盘K(升序、去重)。有缓存只补头尾; offline 只读缓存。"""
    tf_ms = tf_to_ms(tf)
    rows = [] if refresh else load_cache(path)
    by_time = {int(r["open_time"]): r for r in rows}
    if not offline:
        added = 0
        if by_time:
            first, last = min(by_time), max(by_time)
            if first > start_ms:                                   # 补头部(预热段变长时)
                for r in fetch_range(symbol, tf, start_ms, first, fetch, pace, sleep):
                    added += by_time.setdefault(r["open_time"], r) is r
            tail_from = last + tf_ms
        else:
            tail_from = start_ms
        for r in fetch_range(symbol, tf, tail_from, now_ms, fetch, pace, sleep):
            added += by_time.setdefault(r["open_time"], r) is r
        if added or refresh:
            save_cache(path, [by_time[t] for t in sorted(by_time)])
            log(f"  {symbol} {tf}: 新增 {added} 根, 缓存 {len(by_time)} 根 → {path}")
    out = [by_time[t] for t in sorted(by_time) if t >= start_ms and t + tf_ms <= now_ms]
    if not out:
        raise RuntimeError(f"{symbol} {tf}: 没有可用K线(offline={offline}, 缓存 {path})")
    return out


# ---------------------------------------------------------------- 结果
def count_events(result: dict) -> dict:
    """只数个数(不含任何盈亏口径)。"""
    ev = result["events"]
    n = {k: sum(1 for e in ev if e["type"] == k) for k in ("armed", "retrigger", "entry", "expired")}
    return {
        "trigger_bars": n["armed"] + n["retrigger"],
        "armed": n["armed"],
        "retrigger": n["retrigger"],
        "episodes": len(result["episodes"]),
        "entries": n["entry"],
        "expired": n["expired"],
        "open": 1 if result.get("open_episode") else 0,
        "entries_fx_low_broken_before_signal": sum(
            1 for e in ev if e["type"] == "entry" and not e["entry"].get("fx_low_intact", True)),
    }


def build_output(result: dict, params: BtcBreakParams, trig_rows: list, tgt_rows: list,
                 trig_path: str, tgt_path: str, now_ms: int) -> dict:
    tf_ms = tf_to_ms(params.tf)

    def _data(symbol, rows, path):
        return {"symbol": symbol, "path": os.path.abspath(path).replace("\\", "/"),
                "bars": len(rows), "first_open_time": rows[0]["open_time"],
                "last_open_time": rows[-1]["open_time"]}

    start = tgt_rows[0]["open_time"]
    end = max(trig_rows[-1]["open_time"], tgt_rows[-1]["open_time"]) + tf_ms
    meta = {
        "rule": params.name,
        "status": STATUS,
        "generated_at": now_ms,
        "generated_at_local": fmt_bkk(now_ms),
        "tz": "UTC+7",
        "tf": params.tf,
        "tf_ms": tf_ms,
        "data": {"trigger": _data(params.trigger_symbol, trig_rows, trig_path),
                 "target": _data(params.target_symbol, tgt_rows, tgt_path)},
        "kline_row_keys": list(ROW_KEYS),
        "range": {"start": start, "end": end,
                  "start_local": fmt_bkk(start), "end_local": fmt_bkk(end)},
        "counts": count_events(result),
        "time_convention": "time/*_time = K线 open_time(ms); armed_at/expires_at/expired_at = 时刻(ms), "
                           "K线在 open_time + tf_ms 收盘",
        "note": "只含形态实例, 未做任何结算或盈亏统计(铁律七: 先出图审核, 后跑期望)",
    }
    return {"meta": meta, "params": result["params"], "events": result["events"],
            "episodes": result["episodes"], "open_episode": result.get("open_episode")}


def parse_args(argv=None):
    d = BtcBreakParams()
    ap = argparse.ArgumentParser(description="btcbreak_pump 形态实例提取(只数个数, 不结算)")
    ap.add_argument("--out", required=True, help="结果 JSON 路径")
    ap.add_argument("--name", default=d.name)
    ap.add_argument("--trigger-symbol", default=d.trigger_symbol)
    ap.add_argument("--target-symbol", default=d.target_symbol)
    ap.add_argument("--tf", default=d.tf)
    ap.add_argument("--lookback-hours", type=float, default=d.lookback_hours)
    ap.add_argument("--vol-ma", type=int, default=d.vol_ma)
    ap.add_argument("--vol-mult", type=float, default=d.vol_mult)
    ap.add_argument("--ttl-hours", type=float, default=d.ttl_hours)
    ap.add_argument("--require-stall", action=argparse.BooleanOptionalAction, default=d.require_stall)
    ap.add_argument("--stall-max-gap", type=int, default=d.stall_max_gap)
    ap.add_argument("--sl-buffer-pct", type=float, default=d.sl_buffer_pct)
    ap.add_argument("--rr", type=float, default=d.rr)
    ap.add_argument("--require-low-intact", action=argparse.BooleanOptionalAction,
                    default=d.require_low_intact)
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--offline", action="store_true", help="只用缓存, 不联网")
    ap.add_argument("--refresh", action="store_true", help="丢弃缓存, 全部重下")
    ap.add_argument("--pace", type=float, default=0.4, help="请求间隔秒(最小 0.25)")
    return ap.parse_args(argv)


def params_from_args(a) -> BtcBreakParams:
    return BtcBreakParams(**{f.name: getattr(a, f.name) for f in fields(BtcBreakParams)})


def main(argv=None) -> int:
    a = parse_args(argv)
    p = params_from_args(a)
    tf_ms = tf_to_ms(p.tf)
    trig_path = cache_path(p.trigger_symbol, p.tf, a.cache_dir)
    tgt_path = cache_path(p.target_symbol, p.tf, a.cache_dir)

    if a.offline:
        cached = load_cache(tgt_path)
        if not cached:
            raise SystemExit(f"--offline 但没有缓存: {tgt_path}")
        listing = int(cached[0]["open_time"])
        now_ms = int(time.time() * 1000)
    else:
        now_ms = server_time_ms()
        listing = first_bar_time(p.target_symbol, p.tf)
        log(f"{p.target_symbol} {p.tf} 第一根K: {fmt_bkk(listing)} (UTC+7)")

    # 触发品种多带一段预热: 两日低窗口 + 均量窗口
    warm_ms = int(p.lookback_hours * 3_600_000) + (int(p.vol_ma) + 2) * tf_ms
    trig_start = (listing - warm_ms) // tf_ms * tf_ms
    tgt_rows = ensure_history(p.target_symbol, p.tf, listing, now_ms, tgt_path,
                              pace=a.pace, offline=a.offline, refresh=a.refresh)
    trig_rows = ensure_history(p.trigger_symbol, p.tf, trig_start, now_ms, trig_path,
                               pace=a.pace, offline=a.offline, refresh=a.refresh)

    result = replay(trig_rows, tgt_rows, p)
    out = build_output(result, p, trig_rows, tgt_rows, trig_path, tgt_path, now_ms)
    out_path = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    c, rng, dat = out["meta"]["counts"], out["meta"]["range"], out["meta"]["data"]
    print(f"规则 {p.name} | {STATUS}")
    print(f"数据 {p.trigger_symbol} {dat['trigger']['bars']} 根 / {p.target_symbol} "
          f"{dat['target']['bars']} 根 ({p.tf}) | {rng['start_local']} → {rng['end_local']} (UTC+7)")
    print(f"参数 {json.dumps(asdict(p), ensure_ascii=False)}")
    print(f"触发K {c['trigger_bars']} (首次 {c['armed']} + 再次 {c['retrigger']}) | 轮次 {c['episodes']} | "
          f"入场 {c['entries']} | 到期 {c['expired']} | 仍在武装 {c['open']}")
    print(f"入场中停顿前分型低点已被跌破的个数 {c['entries_fx_low_broken_before_signal']}")
    print(f"输出 {out_path.replace(chr(92), '/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
