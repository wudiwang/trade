"""Volume Profile：用近 N 根K构建价格-成交量分布，找密集成交区（HVN）做止盈目标。"""


def build_profile(klines: list, bins: int = 50) -> list[tuple[float, float]]:
    """返回 [(bin中心价, 量), ...]。每根K的量按其高低区间均匀摊到覆盖的桶里。"""
    if not klines:
        return []
    lo = min(float(k["low"]) for k in klines)
    hi = max(float(k["high"]) for k in klines)
    if hi <= lo:
        return []
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for k in klines:
        kl, kh, v = float(k["low"]), float(k["high"]), float(k["volume"])
        if kh <= kl:
            idx = min(int((kl - lo) / width), bins - 1)
            vols[idx] += v
            continue
        b0 = max(0, min(int((kl - lo) / width), bins - 1))
        b1 = max(0, min(int((kh - lo) / width), bins - 1))
        per = v / (b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            vols[b] += per
    return [(lo + (i + 0.5) * width, vols[i]) for i in range(bins)]


def nearest_hvn_above(profile: list[tuple[float, float]], price: float,
                      percentile: float = 0.70) -> float | None:
    """price 上方最近的高量节点：桶量 >= 全部桶量的 percentile 分位，且是局部峰。"""
    if not profile:
        return None
    vols = sorted(v for _, v in profile)
    thresh = vols[int(len(vols) * percentile)] if vols else 0
    for i, (p, v) in enumerate(profile):
        if p <= price or v < thresh:
            continue
        left = profile[i - 1][1] if i > 0 else 0
        right = profile[i + 1][1] if i < len(profile) - 1 else 0
        if v >= left and v >= right:
            return p
    return None


def hvn_list_above(profile: list[tuple[float, float]], price: float,
                   percentile: float = 0.70) -> list[float]:
    """price 上方全部高量节点（由近到远）。用于把止盈设到第二密集区。"""
    if not profile:
        return []
    vols = sorted(v for _, v in profile)
    thresh = vols[int(len(vols) * percentile)] if vols else 0
    out = []
    for i, (p, v) in enumerate(profile):
        if p <= price or v < thresh:
            continue
        left = profile[i - 1][1] if i > 0 else 0
        right = profile[i + 1][1] if i < len(profile) - 1 else 0
        if v >= left and v >= right:
            out.append(p)
    return out


def hvn_list_below(profile: list[tuple[float, float]], price: float,
                   percentile: float = 0.70) -> list[float]:
    if not profile:
        return []
    vols = sorted(v for _, v in profile)
    thresh = vols[int(len(vols) * percentile)] if vols else 0
    out = []
    for i in range(len(profile) - 1, -1, -1):
        p, v = profile[i]
        if p >= price or v < thresh:
            continue
        left = profile[i - 1][1] if i > 0 else 0
        right = profile[i + 1][1] if i < len(profile) - 1 else 0
        if v >= left and v >= right:
            out.append(p)
    return out


def nearest_hvn_below(profile: list[tuple[float, float]], price: float,
                      percentile: float = 0.70) -> float | None:
    if not profile:
        return None
    vols = sorted(v for _, v in profile)
    thresh = vols[int(len(vols) * percentile)] if vols else 0
    for i in range(len(profile) - 1, -1, -1):
        p, v = profile[i]
        if p >= price or v < thresh:
            continue
        left = profile[i - 1][1] if i > 0 else 0
        right = profile[i + 1][1] if i < len(profile) - 1 else 0
        if v >= left and v >= right:
            return p
    return None
