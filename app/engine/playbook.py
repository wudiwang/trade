"""预演(Playbook)监控：每根收盘K检查活跃剧本，价格到预演位/剧本成立即触发提醒。

trigger_type:
  price_reach   到价：本K区间触及 trigger_price
  sweep_reclaim 假突破回收：做多=下破trigger后收盘收回上方；做空镜像
"""
import time


def fmt(p) -> str:
    if p is None:
        return "?"
    p = float(p)
    return f"{p:,.2f}" if p >= 100 else f"{p:.6g}"


def check_bar(db, symbol: str, tf: str, bar: tuple) -> list[dict]:
    """bar=(open_time,o,h,l,c,v,qv,taker,closed)。返回已触发的剧本提醒文本列表。"""
    _, o, h, l, c, *_ = bar
    o, h, l, c = float(o), float(h), float(l), float(c)
    fired = []
    for r in db.active_playbooks(symbol):
        # tf 限定：剧本指定了级别就只在该级别K上判定
        if r["tf"] and r["tf"] != tf:
            continue
        tp_price = r["trigger_price"]
        if tp_price is None:
            continue
        tp_price = float(tp_price)
        d = (r["direction"] or "").lower()
        hit = False
        if r["trigger_type"] == "sweep_reclaim":
            if d == "short":
                hit = h > tp_price and c < tp_price
            else:  # long / 默认
                hit = l < tp_price and c > tp_price
        else:  # price_reach
            hit = l <= tp_price <= h
        if not hit:
            continue
        db.update_playbook(r["id"], status="triggered", triggered_at=int(time.time()))
        kind = "假突破回收✅" if r["trigger_type"] == "sweep_reclaim" else "到预演位"
        msg = (f"🎬 <b>预演触发</b> #{r['id']} {symbol} {tf or ''} {kind}\n"
               f"{r['title'] or ''}\n"
               f"关键位 {fmt(tp_price)} | 买点 {fmt(r['entry'])} 止盈 {fmt(r['tp'])} 止损 {fmt(r['sl'])}")
        fired.append({"id": r["id"], "symbol": symbol, "tf": tf, "message": msg})
    return fired
