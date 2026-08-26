from __future__ import annotations

import html
import json
from pathlib import Path


def _money(value: float) -> str:
    return f"{value:+.2f}U"


def telegram_summary(report: dict) -> str:
    counts = report.get("verdict_counts", {})
    return (
        f"🎬 <b>Bybit 每日复盘 · {html.escape(report['date'])}</b>\n"
        f"净盈亏 <b>{_money(float(report['net_pnl']))}</b> · "
        f"账户收益 {float(report.get('account_return_pct', 0)):+.2f}%\n"
        f"手续费 {float(report.get('fees', 0)):.2f}U · 资金费 {float(report.get('funding', 0)):+.2f}U\n"
        f"合规 {counts.get('合规', 0)} · 部分合规 {counts.get('部分合规', 0)} · "
        f"冲动 {counts.get('冲动交易', 0)} · 严重变形 {counts.get('严重变形', 0)}\n"
        f"明天只改一件事：{html.escape(report.get('tomorrow_action', '保持纪律'))}"
    )


def _chart_svg(bars: list, fills: list[dict], tf: str) -> str:
    width, height = 720, 230
    if not bars:
        return '<div class="empty">K线数据缺失</div>'
    lows = [float(row[3]) for row in bars]
    highs = [float(row[2]) for row in bars]
    low, high = min(lows), max(highs)
    span = high - low or max(abs(high), 1) * 0.01
    left, top, bottom = 12, 12, 22
    inner_w, inner_h = width - 72, height - top - bottom
    x = lambda i: left + (i + 0.5) * inner_w / len(bars)
    y = lambda p: top + (high - float(p)) * inner_h / span
    candle_w = max(1.5, inner_w / len(bars) * 0.62)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{tf} K线">']
    for i, row in enumerate(bars):
        _, open_, high_, low_, close = row[:5]
        color = "#18b77c" if float(close) >= float(open_) else "#ef5b66"
        body_top = y(max(float(open_), float(close)))
        body_bottom = y(min(float(open_), float(close)))
        parts.append(f'<line x1="{x(i):.2f}" x2="{x(i):.2f}" y1="{y(high_):.2f}" y2="{y(low_):.2f}" stroke="{color}"/>')
        parts.append(f'<rect x="{x(i)-candle_w/2:.2f}" y="{body_top:.2f}" width="{candle_w:.2f}" height="{max(body_bottom-body_top,1):.2f}" fill="{color}"/>')
    times = [int(row[0]) for row in bars]
    for fill in fills:
        idx = min(range(len(times)), key=lambda i: abs(times[i] - int(fill["time"])))
        color = "#efb44a" if fill["action"] in ("entry", "add") else "#75a7ff"
        parts.append(f'<circle cx="{x(idx):.2f}" cy="{y(fill["price"]):.2f}" r="5" fill="{color}" data-exec-id="{html.escape(fill["exec_id"])}"/>')
        parts.append(f'<text x="{x(idx):.2f}" y="{max(10,y(fill["price"])-8):.2f}" text-anchor="middle" fill="{color}" font-size="10">{html.escape(fill["action"])}</text>')
    parts.append(f'<text x="{width-55}" y="14" fill="#8d98aa" font-size="11">{html.escape(tf)}</text></svg>')
    return "".join(parts)


def render_html(report: dict) -> str:
    cards = []
    for index, episode in enumerate(report.get("episodes", []), 1):
        evidence = "".join(
            f'<li class="{html.escape(item["state"])}"><span>{html.escape(item["label"])}</span><b>{html.escape(item["state"])}</b></li>'
            for item in episode.get("evidence", [])
        )
        fills = "".join(
            f'<tr data-exec-id="{html.escape(item["exec_id"])}"><td>{html.escape(item["action"])}</td><td>{item["price"]}</td><td>{item["qty"]}</td><td>{html.escape(item["exec_id"])}</td></tr>'
            for item in episode.get("fills", [])
        )
        cards.append(f"""
        <article class="trade" id="trade-{index}">
          <header><div><b>{html.escape(episode['symbol'])}</b> · {'做空' if episode['direction']=='short' else '做多'}</div><span class="verdict">{html.escape(episode['verdict'])}</span></header>
          <div class="columns"><section><h3>交易结果</h3><div class="money">{_money(float(episode['net_pnl']))}</div><p>标的价格收益 {float(episode.get('price_return_pct',0)):+.2f}%</p></section>
          <section><h3>纪律复盘</h3><p>{html.escape(episode['narrative']['summary'])}</p><p class="action">下一次：{html.escape(episode['narrative']['next_action'])}</p></section></div>
          <div class="tabs"><button onclick="showTf(this,'one-{index}')">1分钟</button><button onclick="showTf(this,'fifteen-{index}')">15分钟</button></div>
          <div id="one-{index}" class="chart">{_chart_svg(episode.get('k1m',[]), episode.get('fills',[]), '1分钟')}</div>
          <div id="fifteen-{index}" class="chart hidden">{_chart_svg(episode.get('k15m',[]), episode.get('fills',[]), '15分钟')}</div>
          <ul class="evidence">{evidence}</ul>
          <details><summary>查看全部成交</summary><table><thead><tr><th>动作</th><th>价格</th><th>数量</th><th>执行ID</th></tr></thead><tbody>{fills}</tbody></table></details>
        </article>""")
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bybit 每日交易电影 · {html.escape(report['date'])}</title>
<style>
:root{{--bg:#0c1119;--card:#151d29;--line:#293546;--text:#eef3fa;--muted:#8d98aa;--green:#18b77c;--red:#ef5b66;--gold:#efb44a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:980px;margin:auto;padding:22px 14px 70px}}h1{{font-size:25px;margin:0}}.sub{{color:var(--muted)}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}}.stat,.trade{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:15px}}.stat b,.money{{display:block;font-size:23px;font-variant-numeric:tabular-nums}}.trade{{margin:16px 0}}.trade header,.columns{{display:flex;justify-content:space-between;gap:14px}}.columns section{{flex:1;border-top:1px solid var(--line);margin-top:12px}}.verdict{{color:var(--gold)}}.action{{color:var(--gold)}}.chart svg{{width:100%;display:block}}.hidden{{display:none}}button{{background:#263247;color:var(--text);border:0;border-radius:8px;padding:7px 12px;margin-right:6px}}.evidence{{padding:0;list-style:none;display:grid;grid-template-columns:repeat(2,1fr);gap:7px}}.evidence li{{display:flex;justify-content:space-between;background:#101721;padding:8px;border-radius:8px}}.present b{{color:var(--green)}}.absent b{{color:var(--red)}}.unknown b{{color:var(--muted)}}table{{width:100%;border-collapse:collapse}}td,th{{padding:7px;border-bottom:1px solid var(--line);text-align:left}}@media(max-width:650px){{.stats{{grid-template-columns:repeat(2,1fr)}}.columns{{display:block}}}}
</style><main><header><div class="sub">BYBIT DAILY REVIEW · 交易结果与纪律分开</div><h1>{html.escape(report['date'])} 每日交易电影</h1><p class="sub">{html.escape(report['window_label'])}</p></header>
<section class="stats"><div class="stat">净盈亏<b>{_money(float(report['net_pnl']))}</b></div><div class="stat">账户收益<b>{float(report.get('account_return_pct',0)):+.2f}%</b></div><div class="stat">手续费<b>{float(report.get('fees',0)):.2f}U</b></div><div class="stat">交易回合<b>{len(report.get('episodes',[]))}</b></div></section>
{''.join(cards)}<script>const REPORT={payload};function showTf(btn,id){{const trade=btn.closest('.trade');trade.querySelectorAll('.chart').forEach(x=>x.classList.add('hidden'));document.getElementById(id).classList.remove('hidden')}}</script></main></html>"""


async def deliver_review(bot, report: dict, png_path: Path, html_path: Path) -> None:
    caption = telegram_summary(report)
    photo = await bot.send_photo(str(png_path), caption=caption)
    if not photo.get("ok"):
        await bot.notify(caption)
        await bot.send_document(str(html_path), caption="完整交易电影")
