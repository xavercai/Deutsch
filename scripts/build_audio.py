#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预合成 Conrad 德语语音（免 Key）
================================================
用和电脑上完全相同的免费通道（edge-tts，即微软 Edge「大声朗读」同源神经语音），
把「内置语料 + 当天新闻」的句子预先合成成 MP3，放进 news/audio/，并写一份
news/audio.json 供网页直接引用。这样手机端不需要任何 Key、也不需要电脑。

给 GitHub Actions 每天跑（见 .github/workflows/daily.yml），也可本地手动跑：
    python scripts/build_audio.py            # 增量：已存在的跳过
    python scripts/build_audio.py --limit 20 # 只合成前 20 条，用于试跑
    python scripts/build_audio.py --rebuild  # 忽略已有文件，全部重合成

依赖：pip install edge-tts
注意：这条通道不是微软对外承诺的正式 API，云端 IP 偶尔会被临时拒绝；
      真被拒时本脚本会打印失败并保留旧结果，不影响站点原有素材。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

try:
    import edge_tts
except ImportError:
    print("缺少依赖 edge-tts。请先执行：python -m pip install edge-tts")
    raise SystemExit(1)

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS_FILE = HERE / "corpus.json"
DAILY_FILE = ROOT / "news" / "daily.json"
AUDIO_DIR = ROOT / "news" / "audio"
INDEX_FILE = ROOT / "news" / "audio.json"

VOICE = "de-DE-ConradNeural"
RATE = "+0%"                 # 原始语速；语速梯度由播放端以 playbackRate 实现
CONCURRENCY = 8              # 并发合成数
MIN_BYTES = 600
NEWS_KEEP_DAYS = 45          # 新闻音频保留天数（语料音频永久保留）
TZ = dt.timezone(dt.timedelta(hours=8))


def log(msg: str) -> None:
    print(msg, flush=True)


def sha16(text: str) -> str:
    return hashlib.sha1((VOICE + "|" + RATE + "|" + text).encode("utf-8")).hexdigest()[:16]


async def synth_one(text: str, path: pathlib.Path, sem: asyncio.Semaphore, stats: dict) -> None:
    async with sem:
        if stats["rebuild"] is False and path.exists() and path.stat().st_size >= MIN_BYTES:
            stats["skip"] += 1
            return
        tmp = path.with_name(path.name + ".part")
        try:
            comm = edge_tts.Communicate(text, VOICE, rate=RATE)
            total = 0
            with open(tmp, "wb") as fh:
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio":
                        fh.write(chunk["data"])
                        total += len(chunk["data"])
            if total < MIN_BYTES:
                tmp.unlink(missing_ok=True)
                stats["fail"] += 1
                log(f"  × 空音频：{text[:40]}")
                return
            tmp.replace(path)
            stats["new"] += 1
        except Exception as exc:                       # noqa: BLE001
            tmp.unlink(missing_ok=True)
            stats["fail"] += 1
            log(f"  × {type(exc).__name__}: {text[:30]}…")


async def synth_all(jobs: list[tuple[str, pathlib.Path]], rebuild: bool) -> dict:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"new": 0, "skip": 0, "fail": 0, "rebuild": rebuild}
    await asyncio.gather(*(synth_one(t, p, sem, stats) for t, p in jobs))
    return stats


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（试跑用）")
    ap.add_argument("--rebuild", action="store_true", help="忽略已有文件，全部重合成")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    today_str = today.isoformat()

    corpus = load_json(CORPUS_FILE, {})
    corpus_items = [str(x).strip() for x in (corpus.get("items") or []) if str(x).strip()]
    daily = load_json(DAILY_FILE, {})
    news_items = [str(c.get("text", "")).strip() for c in (daily.get("cards") or []) if str(c.get("text", "")).strip()]

    log(f"=== 预合成 Conrad 语音（{VOICE} {RATE}）===")
    log(f"  内置语料：{len(corpus_items)} 条   当日新闻：{len(news_items)} 条")

    # ---- 组装任务 ----
    jobs: list[tuple[str, pathlib.Path]] = []
    index: dict[str, dict[str, str]] = {}

    # 语料：永久保留
    for text in corpus_items:
        fname = "c-" + sha16(text) + ".mp3"
        jobs.append((text, AUDIO_DIR / fname))
        index[text] = {"0": "news/audio/" + fname}

    # 新闻：按日期命名，便于过期清理
    for text in news_items:
        fname = f"n-{today_str}-" + sha16(text) + ".mp3"
        jobs.append((text, AUDIO_DIR / fname))
        index[text] = {"0": "news/audio/" + fname}

    if args.limit:
        jobs = jobs[: args.limit]
        keep = {t for t, _ in jobs}
        index = {k: v for k, v in index.items() if k in keep}

    if not jobs and not INDEX_FILE.exists():
        log("!! 没有可合成的内容，且没有旧索引，退出。")
        return 1

    stats = asyncio.run(synth_all(jobs, args.rebuild))
    log(f"  合成：新增 {stats['new']} / 跳过 {stats['skip']} / 失败 {stats['fail']}")

    # ---- 合并旧索引（保留仍在保留期内的新闻条目），并清理过期新闻音频 ----
    old_index = load_json(INDEX_FILE, {})
    cutoff = today - dt.timedelta(days=NEWS_KEEP_DAYS)
    kept_old = 0
    removed = 0
    for text, rec in (old_index or {}).items():
        if not isinstance(rec, dict):
            continue
        uri = rec.get("0", "")
        m = re.match(r"news/audio/n-(\d{4}-\d{2}-\d{2})-", uri)
        if m:
            try:
                d = dt.date.fromisoformat(m.group(1))
            except ValueError:
                d = today
            if d < cutoff:
                (ROOT / uri).unlink(missing_ok=True)
                removed += 1
                continue
        if text not in index:
            index[text] = rec
            kept_old += 1

    # 清理「孤儿」：有文件但索引里已没有
    referenced = {v["0"] for v in index.values() if isinstance(v, dict) and v.get("0")}
    orphans = 0
    for f in AUDIO_DIR.glob("*.mp3"):
        rel = "news/audio/" + f.name
        if rel not in referenced:
            f.unlink(missing_ok=True)
            orphans += 1

    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    files = len(list(AUDIO_DIR.glob("*.mp3")))
    total_mb = sum(f.stat().st_size for f in AUDIO_DIR.glob("*.mp3")) / 1048576
    log(f"  索引：{len(index)} 条（沿用旧 {kept_old}）  过期清理 {removed}  孤儿清理 {orphans}")
    log(f"  音频：{files} 个文件，{total_mb:.1f} MB → news/audio.json")

    if len(index) == 0:
        log("!! 索引为空，判定为失败。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
