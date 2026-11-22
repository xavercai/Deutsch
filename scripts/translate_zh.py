#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给「当天新闻 + 本周新语料」补中文翻译（可选，用 OpenAI 兼容接口）
================================================================
内置精编语料是人工写好的，自带中文；周更/新闻句是机器从正文挑的，本来没有翻译。
跑这一步之后，它们也会有和内置语料一样的中文，卡片就不再出现“有的有解释、有的空白”。

**没配置 Key 时直接跳过**（什么都不做，不会失败）。
在 GitHub 仓库里加一个 Secret 即可启用：

    Settings → Secrets and variables → Actions → New repository secret
      AI_KEY      必填，接口密钥
      AI_ENDPOINT 可选，默认 https://api.openai.com/v1/chat/completions
      AI_MODEL    可选，默认 gpt-4o-mini

只要接口是 OpenAI 兼容的 /chat/completions（OpenAI、DeepSeek、通义、Azure OpenAI、
本地 vLLM / Ollama 的兼容层…）都能直接用。

用法：
    AI_KEY=xxx python scripts/translate_zh.py
    python scripts/translate_zh.py            # 没 Key → 跳过
    python scripts/translate_zh.py --force    # 已有翻译也重翻
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DAILY = ROOT / "news" / "daily.json"
WEEKLY = ROOT / "news" / "weekly.json"

DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
BATCH = 12          # 每次请求翻多少句
MAX_ITEMS = 80      # 一轮最多翻多少句（防意外刷量）


def log(msg: str) -> None:
    print(msg, flush=True)


def load(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ask(pairs: list[dict]) -> dict[int, str]:
    """pairs: [{"i":0,"de":"..."}] → {0: "中文"}"""
    prompt = (
        "你是专业的德语到中文翻译。下面是一个 JSON 数组，每项形如 {\"i\":序号,\"de\":德语句子}。\n"
        "请为每一条给出自然、准确、忠实的简体中文翻译：\n"
        "- 只翻译原意，不要解释、不要补充背景、不要加括号注释；\n"
        "- 保留原句的信息量，不要简化成大意；\n"
        "- 专业名词（车型、机构、人名）按中文习惯译，必要时保留原文。\n"
        "只输出 JSON 数组，形如 [{\"i\":0,\"zh\":\"…\"}]，不要输出任何其他文字。\n"
        "输入：" + json.dumps(pairs, ensure_ascii=False)
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + KEY,
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        raise RuntimeError("模型没有返回 JSON 数组：" + (text or "")[:120])
    out: dict[int, str] = {}
    for row in json.loads(m.group(0)):
        try:
            out[int(row["i"])] = str(row["zh"]).strip()
        except Exception:
            continue
    return out


def main() -> int:
    global KEY, ENDPOINT, MODEL
    KEY = os.environ.get("AI_KEY", "").strip()
    ENDPOINT = os.environ.get("AI_ENDPOINT", "").strip() or DEFAULT_ENDPOINT
    MODEL = os.environ.get("AI_MODEL", "").strip() or "gpt-4o-mini"
    force = "--force" in sys.argv

    if not KEY:
        log("未配置 AI_KEY，跳过翻译（卡片会自动改用逐词中文注释，不影响使用）。")
        return 0

    daily = load(DAILY, {})
    weekly = load(WEEKLY, {})
    buckets = [("daily", daily, daily.get("cards") or []), ("weekly", weekly, weekly.get("items") or [])]

    todo: list[tuple[str, dict]] = []
    for _label, _doc, rows in buckets:
        for x in rows:
            text = str(x.get("text", "")).strip()
            if not text:
                continue
            if force or not str(x.get("zh", "")).strip():
                todo.append((text, x))
    if not todo:
        log("所有句子都已有中文翻译，无需调用接口。")
        return 0

    todo = todo[:MAX_ITEMS]
    log(f"=== 需要翻译 {len(todo)} 句（{ENDPOINT} / {MODEL}）===")

    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        pairs = [{"i": i, "de": t} for i, (t, _x) in enumerate(chunk)]
        try:
            got = ask(pairs)
        except urllib.error.HTTPError as e:
            log(f"  × 接口返回 {e.code}：{e.read()[:160]!r}")
            continue
        except Exception as e:                     # noqa: BLE001
            log(f"  × {type(e).__name__}: {e}")
            continue
        for i, (text, x) in enumerate(chunk):
            zh = got.get(i, "")
            if zh:
                x["zh"] = zh
                done += 1
        log(f"  ✓ 已翻译 {done}/{len(todo)}")

    if done:
        DAILY.write_text(json.dumps(daily, ensure_ascii=False, indent=2), encoding="utf-8")
        WEEKLY.write_text(json.dumps(weekly, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"✓ 写回 {done} 条中文翻译 → news/daily.json / news/weekly.json")
    else:
        log("没有拿到任何翻译，文件保持不变。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
