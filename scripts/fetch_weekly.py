#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每周语料池：每周刷新一次 news/weekly.json
================================================
从德语新闻里挑出一批句子（生活 / 汽车各若干条）作为「本周新语料」。
网页每天从这里取 1 条生活 + 1 条汽车，其余用内置精编 A1 语料补足，
这样既有新鲜内容，又保证每句都有中文、语法和词汇讲解。

同一周内重复运行会直接跳过（除非加 --force），所以挂在每天的定时任务里也很安全。
用法：
    python scripts/fetch_weekly.py
    python scripts/fetch_weekly.py --force
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fetch_daily import FEEDS, TZ, auto_chunks, fetch, norm, parse_items, pick_sentence  # noqa: E402

ROOT = HERE.parent
OUT = ROOT / "news" / "weekly.json"

# 选句/切块算法的版本号。改了选择逻辑就 +1：
# 这样即使还在同一周，也会重新生成一次（否则“本周已有”会把旧结果一直缓下去）。
GEN_VERSION = 2

PER_TOPIC = 12          # 每个主题最多收多少条（够一周每天一条不重复）
MIN_LEN = 15
MAX_LEN = 110


def log(msg: str) -> None:
    print(msg, flush=True)


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> int:
    week = iso_week(dt.datetime.now(TZ).date())
    force = "--force" in sys.argv

    if not force and OUT.exists():
        try:
            cur = json.loads(OUT.read_text(encoding="utf-8"))
            items = cur.get("items") or []
            fresh = bool(items) and bool(items[0].get("chunks"))   # 旧版没有 chunks，说明是旧算法生成的
            if (cur.get("week") == week and len(items) >= PER_TOPIC
                    and fresh and cur.get("gen") == GEN_VERSION):
                log(f"本周（{week}）语料池已是 v{GEN_VERSION}，{len(items)} 条，跳过刷新。")
                return 0
            log(f"本周语料池需要重建：week={cur.get('week')} 条数={len(items)} 含chunks={fresh} gen={cur.get('gen')}")
        except Exception:
            pass

    log(f"=== 生成本周（{week}）语料池 ===")
    # 先拉全部新闻，再按“正文里的完整句”筛，不够再放宽
    raw: dict[str, list[dict]] = {"生活": [], "汽车": []}
    ok_feeds = 0
    for _fid, name, url, topic in FEEDS:
        t = topic if topic in raw else "生活"
        xml = fetch(url)
        if not xml:
            continue
        items = parse_items(xml, name, t)
        if items:
            ok_feeds += 1
            log(f"  ✓ {name}: {len(items)} 条")
        raw[t].extend(items)

    buckets: dict[str, list[dict]] = {"生活": [], "汽车": []}
    for min_score in (45, 35, 25):
        buckets = {"生活": [], "汽车": []}
        seen: set[str] = set()
        for t in ("生活", "汽车"):
            for it in raw[t]:
                if len(buckets[t]) >= PER_TOPIC:
                    break
                body = pick_sentence(it, min_score)
                if not body:
                    continue
                key = norm(body)
                if not key or key in seen:
                    continue
                seen.add(key)
                buckets[t].append({"topic": t, "text": body, "chunks": auto_chunks(body), "source": it["feed"],
                                   "link": it.get("link", ""), "title": it.get("title", "")})
        if sum(len(v) for v in buckets.values()) >= PER_TOPIC:
            break

    items: list[dict] = []
    for t in ("生活", "汽车"):
        for i, x in enumerate(buckets[t][:PER_TOPIC]):
            items.append({"id": f"W-{week}-{t[0]}{i}", **x})

    n_life = sum(1 for x in items if x["topic"] == "生活")
    n_auto = sum(1 for x in items if x["topic"] == "汽车")

    if len(items) < 4:
        log(f"!! 只凑到 {len(items)} 条（生活 {n_life} / 汽车 {n_auto}），保留旧文件不动。")
        return 1

    payload = {
        "week": week,
        "gen": GEN_VERSION,
        "generatedAt": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        "feedsOk": ok_feeds,
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✓ 写入 {OUT}（{len(items)} 条：生活 {n_life} / 汽车 {n_auto}；{ok_feeds}/{len(FEEDS)} 个源可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
