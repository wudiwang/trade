"""Summary image for Telegram: the same headline and rule checklist as the text."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageFont

from .report import VERDICT_ORDER, _empty_day_text, _fee, _money, day_view, headline

# 摘要图全是中文：只接受确实带中文字形的字体。找不到就不出图（NoCjkFont），由调用方改发文字，
# 绝不画一排方框发出去。想指定字体：环境变量 REVIEW_CJK_FONT=字体文件路径，或把一个开源中文
# 字体（.ttf/.otf/.ttc）放进 app/review/fonts/。服务器上装 fonts-noto-cjk 或 fonts-wqy-microhei 即可。
FONT_ENV = "REVIEW_CJK_FONT"
_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONT_CANDIDATES = {
    False: (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ),
    True: (
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    ),
}


class NoCjkFont(RuntimeError):
    """这台机器上没有能画中文的字体：不生成摘要图。"""


def has_cjk_glyphs(font) -> bool:
    """字体里有没有中文字形。缺字形的字都会画成同一个「缺字框」，所以两个不同的汉字
    画出来一模一样（或什么都没画）就说明没有。"""
    def glyph(char: str):
        image = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(image).text((8, 8), char, font=font, fill=255)
        return image

    try:
        first, second = glyph("规"), glyph("矩")
        if first.getbbox() is None or second.getbbox() is None:
            return False
        return first.tobytes() != second.tobytes()
    except Exception:  # noqa: BLE001 - 字体文件有问题就当它不可用
        return False


def _candidates(bold: bool) -> list[str]:
    paths: list[str] = []
    if os.environ.get(FONT_ENV):
        paths.append(os.environ[FONT_ENV])
    if _FONT_DIR.is_dir():
        local = sorted(str(item) for item in _FONT_DIR.iterdir()
                       if item.suffix.lower() in (".ttf", ".otf", ".ttc"))
        paths += sorted(local, key=lambda name: ("bold" in Path(name).stem.lower()) != bold)
    return paths + list(_FONT_CANDIDATES[bold])


@lru_cache(maxsize=4)
def _font_path(bold: bool, env: str) -> str | None:
    for candidate in _candidates(bold):
        if not Path(candidate).is_file():
            continue
        try:
            if has_cjk_glyphs(ImageFont.truetype(candidate, 40)):
                return candidate
        except OSError:
            continue
    return None


def cjk_font_path(bold: bool = False) -> str | None:
    """能画中文的字体文件；没有返回 None。"""
    return _font_path(bool(bold), os.environ.get(FONT_ENV, ""))
_INK, _MUTED, _OK, _BAD, _WARN, _NA = "#e9eef6", "#8b97aa", "#2fd39a", "#ff6b76", "#f0b452", "#8fa3c2"
_BG, _CARD, _INSET = "#0b1018", "#131b27", "#0e151f"
_VERDICT_COLORS = {"合规": _OK, "中途下车": _BAD, "无止损": _BAD, "止损被放宽": _BAD,
                   "逆向加仓": _BAD, "证据不足": _WARN}
_MAX_RULE_ROWS = 6


@lru_cache(maxsize=64)
def _load(path: str, size: int):
    return ImageFont.truetype(path, size)


def _font(size: int, bold: bool = False):
    path = cjk_font_path(bold) or cjk_font_path(not bold)
    if path is None:
        raise NoCjkFont(
            "没有找到带中文字形的字体，摘要图不生成（文字摘要照发）。在服务器上装 fonts-noto-cjk，"
            f"或用环境变量 {FONT_ENV} 指向一个中文字体文件。")
    return _load(path, size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _pill(draw: ImageDraw.ImageDraw, right: int, top: int, word: str, count: int, color: str) -> int:
    """Draw a count pill whose right edge is at ``right``; return its left edge."""
    font = _font(26, True)
    text = f"{word} {count}"
    width = int(draw.textlength(text, font=font)) + 36
    left = right - width
    draw.rounded_rectangle((left, top, right, top + 48), radius=12, fill=_INSET)
    draw.text((left + 18, top + 7), text, font=font, fill=color if count else "#5c6779")
    return left


def render_summary_png(report: Mapping, output: str | Path) -> Path:
    """画摘要图。没有中文字体时抛 NoCjkFont，不写任何文件。"""
    output = Path(output)
    _font(20)                         # 先确认字体可用，再动文件
    output.parent.mkdir(parents=True, exist_ok=True)
    view = day_view(report)
    score, trades = view["score"], view["trades"]
    image = Image.new("RGB", (1080, 1350), _BG)
    draw = ImageDraw.Draw(image)

    draw.text((64, 48), "今天我有没有按规矩做？", font=_font(50, True), fill=_INK)
    subtitle = view["window_label"] or view["date"]
    draw.text((64, 122), _fit(draw, f"每日实盘复盘 · {subtitle}", _font(25), 952),
              font=_font(25), fill=_MUTED)
    if not view["reconciled"]:
        draw.rounded_rectangle((64, 166, 1016, 212), radius=12, fill="#3a1c22")
        reason = view["reasons"][0] if view["reasons"] else ""
        banner = "未核准 · 对账未通过" + (f"：{reason}" if reason else "")
        draw.text((82, 173), _fit(draw, banner, _font(24, True), 916),
                  font=_font(24, True), fill=_BAD)

    # Headline card: how many trades followed the rules, and the most-broken rule.
    draw.rounded_rectangle((64, 232, 1016, 520), radius=28, fill=_CARD)
    if not trades:
        draw.text((104, 300), "今天没有成交", font=_font(76, True), fill=_INK)
        draw.text((104, 420), _fit(draw, _empty_day_text(view), _font(28), 880),
                  font=_font(28), fill=_MUTED)
    else:
        big = _font(120, True)
        number = str(score["compliant"])
        draw.text((104, 250), number, font=big, fill=_INK)
        offset = 104 + int(draw.textlength(number, font=big)) + 24
        draw.text((offset, 318), f"/ {score['total']} 笔合规", font=_font(46, True), fill=_INK)
        note = f"参与评价 {score['judged']} 笔" + ("" if view["reconciled"] else " · 初步结果，未核准")
        draw.text((offset, 264), note, font=_font(25), fill=_MUTED)
        text, tone = headline(view)       # 与文字摘要、HTML 同一句话、同一语气
        draw.text((104, 430), _fit(draw, text, _font(34, True), 880),
                  font=_font(34, True), fill={"bad": _BAD, "warn": _WARN}.get(tone, _OK))

    # Rule checklist: per rule, how many trades passed / broke / lacked records.
    y = 556
    rules = view["rules"]
    if trades:
        draw.text((64, y), "规矩清单", font=_font(34, True), fill=_INK)
        draw.text((250, y + 9), "过 = 通过 · 破 = 没过 · 缺 = 记录不足", font=_font(22), fill=_MUTED)
        y += 62
        if not rules:
            draw.text((64, y + 6), "今天没有逐笔规矩检查结果", font=_font(27), fill=_MUTED)
            y += 60
    for rule in rules[:_MAX_RULE_ROWS]:
        draw.rounded_rectangle((64, y, 1016, y + 72), radius=16, fill=_CARD)
        left = _pill(draw, 1000, y + 12, "缺", rule["unknown"], _WARN)
        left = _pill(draw, left - 10, y + 12, "破", rule["fail"], _BAD)
        left = _pill(draw, left - 10, y + 12, "过", rule["pass"], _OK)
        draw.text((88, y + 17), _fit(draw, rule["label"], _font(28), left - 108),
                  font=_font(28), fill=_INK)
        y += 82
    if len(rules) > _MAX_RULE_ROWS:
        draw.text((64, y), f"另有 {len(rules) - _MAX_RULE_ROWS} 条，见完整报告", font=_font(22), fill=_MUTED)
        y += 36

    # Verdict distribution and day-level flags.
    y += 8
    x = 64
    ordered = sorted(score["by_verdict"].items(),
                     key=lambda item: (VERDICT_ORDER.index(item[0]) if item[0] in VERDICT_ORDER
                                       else len(VERDICT_ORDER)))
    chip_font = _font(26, True)
    for name, count in ordered:
        if not count:
            continue
        text = f"{name} {count}"
        width = int(draw.textlength(text, font=chip_font)) + 40
        if x + width > 1016:
            x, y = 64, y + 62
        draw.rounded_rectangle((x, y, x + width, y + 50), radius=25,
                               outline=_VERDICT_COLORS.get(name, _NA), width=2)
        draw.text((x + 20, y + 8), text, font=chip_font, fill=_VERDICT_COLORS.get(name, _NA))
        x += width + 12
    if ordered:
        y += 66
    for check in [item for item in view["day_checks"] if item["state"] == "fail"][:2]:
        if y > 1160:
            break
        text = f"当日：{check['label']}" + (f"（{check['detail']}）" if check["detail"] else "")
        draw.text((64, y), _fit(draw, text, _font(27, True), 952), font=_font(27, True), fill=_WARN)
        y += 44

    # Money last, in one neutral line; it is never coloured as good or bad.
    draw.line((64, 1218, 1016, 1218), fill="#263243", width=2)
    if view["reconciled"]:
        ledger = (f"当日净盈亏 {_money(view['net_pnl'])} · 手续费 {_fee(view['fees'])} · "
                  f"资金费 {_money(view['funding'])}")
    else:
        ledger = "净盈亏未通过对账，不给数字"
    draw.text((64, 1238), _fit(draw, ledger, _font(27), 952), font=_font(27), fill=_INK)
    draw.text((64, 1288), "盈亏不参与纪律判定 · 离场只有止盈触发或止损触发", font=_font(22), fill=_MUTED)
    image.save(output, format="PNG", optimize=True)
    return output
