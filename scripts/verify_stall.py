"""验证新停顿逻辑(合并K右K + 放宽死线)在XRP圈定区域是否触发。对比旧(原始K+2根死线)。"""
import json, sys, time, urllib.request, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.engine.chan import find_fractals, merge_klines

L1_t = 1785246300000; entry_t = 1785300300000
url = f"https://fapi.binance.com/fapi/v1/klines?symbol=XRPUSDT&interval=15m&startTime={L1_t-5*900000}&endTime={entry_t+5*900000}&limit=200"
raw = json.load(urllib.request.urlopen(url, timeout=20))
kl = [{"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in raw]
def T(ms): return time.strftime("%m-%d %H:%M", time.gmtime(ms/1000))
merged = merge_klines(kl); fxs = find_fractals(kl, merged)
MAX_GAP = 5
print(f"新逻辑: 停顿=分型右【合并K】高点被收盘突破, 分型确认后≤{MAX_GAP}根内; 入场=停顿次根\n")
print(f"{'底分型@UTC':18}{'低':>8}{'合并右K高':>10}{'旧(原始+2根)':>14}{'新(合并+放宽)':>22}")
for fx in fxs:
    if fx.kind != "bottom": continue
    t = kl[fx.extreme_src_idx]["open_time"]
    if not (1785243600000 <= t <= 1785267000000): continue   # 圈定区域附近
    si = fx.extreme_src_idx
    rk = fx.mid_merged_idx + 1
    ref = merged[rk].high if rk < len(merged) else None
    # 旧
    old = "触发" if (si+2 < len(kl) and kl[si+2]["close"] > kl[si+1]["high"]) else "✗漏"
    # 新: 扫 confirm+1 .. confirm+MAX_GAP
    new = "✗漏"
    for j in range(fx.confirm_src_idx+1, min(fx.confirm_src_idx+MAX_GAP+1, len(kl)-1)):
        if ref is not None and kl[j]["close"] > ref:
            ent = kl[j+1]["close"]
            new = f"✅触发→入场{ent:.4f}(停顿@{T(kl[j]['open_time'])})"
            break
    print(f"  {T(t):16}{kl[si]['low']:>8.4f}{(ref or 0):>10.4f}{old:>12}{new:>24}")
print(f"\n对照: 旧逻辑那笔实际入场=1.08(贴H1前高1.0821)")
