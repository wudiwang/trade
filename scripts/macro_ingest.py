"""宏观研究采集脚手架(Phase1 Task11)。

接收一个文字稿文件(如提阿非罗每日BTC研判的转写)→ 生成结构化摘要 JSON,存到 artifacts/macro/。
YouTube 自动采集尚未接入(需先选定获取方式)→ 标记为 BLOCKED。

用法:
  .venv/Scripts/python scripts/macro_ingest.py <transcript.txt> [--source 提阿非罗]
  .venv/Scripts/python scripts/macro_ingest.py --youtube <url>     # 当前会提示 BLOCKED

纯解析助手(extract_bias/extract_price_levels/summarize_transcript)无副作用, 便于单测。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "artifacts", "macro")

BULL = ("看多", "做多", "多头", "上涨", "突破", "反弹", "企稳", "利好", "bullish", "long", "buy", "breakout")
BEAR = ("看空", "做空", "空头", "下跌", "破位", "回调", "利空", "bearish", "short", "sell", "breakdown")


def extract_bias(text: str):
    """返回 (bias, bull_hits, bear_hits)。bias ∈ long/short/neutral。纯函数。"""
    t = text.lower()
    bull = sum(t.count(w.lower()) for w in BULL)
    bear = sum(t.count(w.lower()) for w in BEAR)
    if bull > bear * 1.2:
        bias = "long"
    elif bear > bull * 1.2:
        bias = "short"
    else:
        bias = "neutral"
    return bias, bull, bear


def extract_price_levels(text: str, max_n: int = 12):
    """抽取疑似价位数字(支持 BTC 的大整数与山寨的小数)。去重保序。纯函数。"""
    nums = re.findall(r"\d[\d,]*\.?\d*", text)
    out = []
    for n in nums:
        n = n.replace(",", "")
        try:
            v = float(n)
        except ValueError:
            continue
        if v <= 0:
            continue
        if v not in out:
            out.append(v)
    return out[:max_n]


def summarize_transcript(text: str, source: str = "unknown", ts: int = 0):
    """把文字稿压成结构化摘要 dict。纯函数(ts 由调用方传入, 便于可重复测试)。"""
    bias, bull, bear = extract_bias(text)
    return {"source": source, "generated": int(ts), "chars": len(text),
            "bias": bias, "bull_hits": bull, "bear_hits": bear,
            "price_levels": extract_price_levels(text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", nargs="?", help="文字稿文件路径")
    ap.add_argument("--source", default="unknown")
    ap.add_argument("--youtube", help="YouTube 链接(当前未启用)")
    a = ap.parse_args()

    if a.youtube:
        print("BLOCKED: YouTube 自动采集尚未接入(需先选定获取方式: 字幕API / 第三方转写 / 人工粘贴)。"
              "请先用文字稿文件方式: macro_ingest.py <transcript.txt>", file=sys.stderr)
        sys.exit(2)
    if not a.transcript:
        ap.error("需要一个文字稿文件路径(或用 --youtube, 当前 BLOCKED)")

    text = open(a.transcript, encoding="utf-8").read()
    summary = summarize_transcript(text, a.source, int(time.time()))
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"macro_{summary['generated']}.json")
    json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"摘要已写入 {os.path.relpath(out, ROOT)}: bias={summary['bias']} "
          f"(多{summary['bull_hits']}/空{summary['bear_hits']}) 价位{summary['price_levels'][:5]}")


if __name__ == "__main__":
    main()
