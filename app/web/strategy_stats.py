import json
from collections import OrderedDict


def strategy_name(track: str, extra_json: str | None) -> str:
    try:
        extra = json.loads(extra_json or "{}")
    except Exception:
        extra = {}
    path = str(extra.get("path") or "")
    typ = str(extra.get("type") or track or "")
    raw = f"{path} {typ} {track}".lower()
    if "macro_chan_pullback" in raw or typ in ("second_buy", "second_sell"):
        return "反转战法"
    if "小转大" in path or "smallbig" in raw or "small_to_big" in raw:
        return "小转大战法"
    if path:
        return path
    return track or "未命名策略"


def build_strategy_stats(rows: list[dict]) -> list[dict]:
    groups: OrderedDict[str, dict] = OrderedDict()
    for row in rows:
        name = strategy_name(str(row.get("track") or ""), row.get("sig_extra"))
        item = groups.setdefault(name, {
            "strategy": name, "signals": 0, "open": 0, "closed": 0,
            "wins": 0, "total_pnl": 0.0, "total_r": 0.0,
        })
        item["signals"] += 1
        result = row.get("result")
        if result == "open":
            item["open"] += 1
            continue
        if result in ("tp", "sl", "rev"):
            item["closed"] += 1
            pnl = float(row.get("pnl") or 0)
            pnl_r = float(row.get("pnl_r") or 0)
            item["total_pnl"] += pnl
            item["total_r"] += pnl_r
            if pnl_r > 0:
                item["wins"] += 1
    out = []
    for item in groups.values():
        closed = item["closed"]
        item["win_rate"] = round(item["wins"] / closed * 100, 1) if closed else 0.0
        item["expectancy_r"] = round(item["total_r"] / closed, 3) if closed else 0.0
        item["total_pnl"] = round(item["total_pnl"], 2)
        item["total_r"] = round(item["total_r"], 3)
        out.append(item)
    return out
