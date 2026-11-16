from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_summary_png(report: dict, output: str | Path) -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1080, 1350), "#0c1119")
    draw = ImageDraw.Draw(image)
    ink, muted, green, red, gold, card = "#eef3fa", "#8d98aa", "#18b77c", "#ef5b66", "#efb44a", "#151d29"
    draw.text((64, 55), "BYBIT 每日交易复盘", font=_font(48, True), fill=ink)
    draw.text((64, 120), report["window_label"], font=_font(24), fill=muted)
    pnl = float(report["net_pnl"])
    draw.rounded_rectangle((64, 190, 1016, 430), radius=28, fill=card)
    draw.text((105, 225), "今日交易净盈亏", font=_font(25), fill=muted)
    draw.text((105, 280), f"{pnl:+.2f} U", font=_font(74, True), fill=green if pnl >= 0 else red)
    account_return = report.get("account_return_pct")
    return_text = "收益率 暂缺快照" if account_return is None else f"收益率 {float(account_return):+.2f}%"
    draw.text((700, 250), return_text, font=_font(28, True), fill=ink)
    draw.text((700, 310), f"手续费 {float(report.get('fees',0)):.2f}U", font=_font(23), fill=muted)
    draw.text((700, 350), f"资金费 {float(report.get('funding',0)):+.2f}U", font=_font(23), fill=muted)
    draw.text((64, 500), "纪律分布", font=_font(32, True), fill=ink)
    labels = [("合规", green), ("部分合规", gold), ("冲动交易", "#ff8e53"), ("严重变形", red)]
    counts = report.get("verdict_counts", {})
    for i, (label, color) in enumerate(labels):
        x = 64 + (i % 2) * 490
        y = 570 + (i // 2) * 130
        draw.rounded_rectangle((x, y, x + 450, y + 100), radius=18, fill=card)
        draw.text((x + 28, y + 30), label, font=_font(25), fill=muted)
        draw.text((x + 370, y + 23), str(counts.get(label, 0)), font=_font(38, True), fill=color)
    draw.rounded_rectangle((64, 865, 1016, 1190), radius=28, fill=card)
    draw.text((100, 910), "明天只改一件事", font=_font(29, True), fill=gold)
    action = str(report.get("tomorrow_action", "保持纪律"))
    chunks = [action[i:i + 24] for i in range(0, len(action), 24)] or [action]
    for i, line in enumerate(chunks[:4]):
        draw.text((100, 980 + i * 54), line, font=_font(31), fill=ink)
    draw.text((64, 1280), "结果不等于纪律 · 盈利不自动代表做对", font=_font(22), fill=muted)
    if report.get("status") != "reconciled":
        draw.rectangle((64, 155, 1016, 185), fill="#4a2026")
        draw.text((76, 156), "数据未通过对账 · 仅供排查", font=_font(20, True), fill=red)
    image.save(output, format="PNG", optimize=True)
    return output
