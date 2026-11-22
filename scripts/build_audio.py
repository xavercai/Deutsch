#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预合成 Conrad 德语语音（免 Key）+ 按真实停顿切词块
================================================
用和电脑上完全相同的免费通道（edge-tts，微软 Edge「大声朗读」同源神经语音），
把「内置语料 + 当天新闻 + 本周新语料」的句子合成成 MP3，放进 news/audio/，
并写两份清单供网页使用：
  · news/audio.json  —— 文本 → 音频文件
  · news/chunks.json —— 整句 → 词块（按整句朗读里的**真实停顿**切出来的）

为什么按停顿切词块：合成时会返回每个词的起止时间（WordBoundary），词与词之间
停顿明显的地方，通常就是语法/意群的分界。比按词数硬切自然得多。

用法：
    python scripts/build_audio.py              # 增量
    python scripts/build_audio.py --limit 20   # 只做前 20 条（试跑）
    python scripts/build_audio.py --rebuild    # 全部重合成
依赖：pip install edge-tts
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

try:
    import miniaudio          # 可选：用于在音频里找真实停顿
    _HAS_MINIAUDIO = True
except ImportError:
    _HAS_MINIAUDIO = False

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS_FILE = HERE / "corpus.json"
DAILY_FILE = ROOT / "news" / "daily.json"
WEEKLY_FILE = ROOT / "news" / "weekly.json"
AUDIO_DIR = ROOT / "news" / "audio"
INDEX_FILE = ROOT / "news" / "audio.json"
CHUNKS_FILE = ROOT / "news" / "chunks.json"

VOICE = "de-DE-ConradNeural"
RATE = "+0%"
CONCURRENCY = 8
MIN_BYTES = 600
NEWS_KEEP_DAYS = 45
TZ = dt.timezone(dt.timedelta(hours=8))

CHUNK_GEN = 2          # 切块算法版本；改了就要重新取停顿
PAUSE_MS = 110         # 音频里静音 ≥ 该值（毫秒）算一次真实停顿
MAX_CHUNK_WORDS = 3    # 一个词块最多几个词
MAX_CHUNK_CHARS = 26   # 一个词块最多几个字符

WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß0-9]+(?:['’\-][A-Za-zÄÖÜäöüß]+)*")


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_unlink(p: pathlib.Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def sha16(text: str) -> str:
    return hashlib.sha1((VOICE + "|" + RATE + "|" + text).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 合成：同时收集逐词时间（用于按停顿切块）
# --------------------------------------------------------------------------
async def synth_text(text: str) -> tuple[bytes, list[tuple[int, int]]]:
    # boundary="WordBoundary" 是必需的：默认只给 SentenceBoundary，拿不到逐词时间
    comm = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    buf = bytearray()
    bounds: list[tuple[int, int]] = []
    async for chunk in comm.stream():
        kind = chunk.get("type")
        if kind == "audio":
            buf.extend(chunk["data"])
        elif kind == "WordBoundary":
            bounds.append((int(chunk.get("offset", 0)), int(chunk.get("duration", 0))))
    return bytes(buf), bounds


def detect_pauses(path: pathlib.Path, frame_ms: int = 10, min_sil_ms: int = PAUSE_MS,
                  rel_thr: float = 0.05) -> list[tuple[float, float]]:
    """在合成音频里找静音段（真实停顿）。返回 [(start_ms, end_ms)]。
    没有 miniaudio 或解码失败时返回空列表，调用方会退回语法规则切分。"""
    if not _HAS_MINIAUDIO:
        return []
    try:
        dec = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32)
    except Exception:
        return []
    sr, nch, samples = dec.sample_rate, dec.nchannels, dec.samples
    total = len(samples) // nch
    if total <= 0 or not sr:
        return []
    per = max(1, int(sr * frame_ms / 1000))
    rms: list[float] = []
    for i in range(0, total, per):
        hi = min(total, i + per)
        acc = 0.0
        for j in range(i, hi):
            v = samples[j * nch]
            acc += v * v
        rms.append((acc / max(1, hi - i)) ** 0.5)
    if not rms:
        return []
    thr = max((max(rms) or 1e-6) * rel_thr, 1e-4)
    out: list[tuple[float, float]] = []
    start = None
    for i, r in enumerate(rms):
        if r < thr and start is None:
            start = i
        elif r >= thr and start is not None:
            if (i - start) * frame_ms >= min_sil_ms:
                out.append((start * frame_ms, i * frame_ms))
            start = None
    if start is not None and (len(rms) - start) * frame_ms >= min_sil_ms:
        out.append((start * frame_ms, len(rms) * frame_ms))
    return out


def chunks_from_audio(sentence: str, bounds: list[tuple[int, int]],
                      pauses: list[tuple[float, float]]) -> list[str] | None:
    """按“整句朗读里的真实停顿”切词块；停顿落在哪两个词之间，就在那里断。
    再叠加长度上限，避免一个词块太长。切不出来返回 None（交给调用方兜底）。"""
    ms = list(WORD_RE.finditer(sentence or ""))
    if not sentence or not ms or len(ms) != len(bounds):
        return None
    # 每个词的起止（毫秒）
    times = [((o / 10000.0), (o + d) / 10000.0) for o, d in bounds]
    cuts: set[int] = set()
    for sa, sb in pauses:
        mid = (sa + sb) / 2
        if mid <= times[0][0] or mid >= times[-1][1]:
            continue                      # 开头/结尾的静音不算停顿
        k = None
        for i, (_s, e) in enumerate(times):
            if e <= mid:
                k = i
        if k is not None and k + 1 < len(ms):
            cuts.add(k)

    out: list[str] = []
    cur: list[int] = []
    for i in range(len(ms)):
        cur.append(i)
        last = i == len(ms) - 1
        span = ms[i].end() - ms[cur[0]].start()
        if (last or (i in cuts) or len(cur) >= MAX_CHUNK_WORDS or span >= MAX_CHUNK_CHARS):
            out.append(sentence[ms[cur[0]].start():ms[i].end()].strip())
            cur = []
    raw = [c for c in out if c]
    if not raw:
        return None
    # 避免“孤零零一个词”的词块（如单一个 gekommen / einig）：并回前一块（受长度限制）
    merged: list[str] = []
    for c in raw:
        if merged and len(WORD_RE.findall(c)) == 1 and len(merged[-1]) + len(c) + 1 <= MAX_CHUNK_CHARS + 10:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    if len(merged) >= 2 and len(WORD_RE.findall(merged[0])) == 1 and len(merged[1]) + len(merged[0]) + 1 <= MAX_CHUNK_CHARS + 10:
        merged[1] = merged[0] + " " + merged[1]
        merged.pop(0)
    return merged or None


async def synth_one(text: str, path: pathlib.Path, sem: asyncio.Semaphore, stats: dict,
                    capture: dict | None = None, force: bool = False) -> None:
    async with sem:
        if not force and not stats["rebuild"] and path.exists() and path.stat().st_size >= MIN_BYTES:
            stats["skip"] += 1
            return
        tmp = path.with_name(path.name + ".part")
        last: Exception | None = None
        for attempt in range(3):
            try:
                data, bounds = await synth_text(text)
                if len(data) < MIN_BYTES:
                    raise RuntimeError("服务端返回空音频")
                tmp.write_bytes(data)
                tmp.replace(path)
                if capture is not None:
                    capture[text] = bounds
                stats["new"] += 1
                return
            except Exception as exc:                       # noqa: BLE001
                last = exc
                safe_unlink(tmp)
                await asyncio.sleep(0.8 * (attempt + 1))
        stats["fail"] += 1
        stats.setdefault("errors", []).append(f"{type(last).__name__}: {text[:24]}")
        if stats["fail"] <= 5:
            log(f"  × {type(last).__name__}: {text[:30]}…")


async def run_jobs(jobs: list[dict], rebuild: bool) -> dict:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"new": 0, "skip": 0, "fail": 0, "rebuild": rebuild}
    await asyncio.gather(*(synth_one(j["text"], j["path"], sem, stats,
                                     j.get("capture"), j.get("force", False)) for j in jobs))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    today_str = today.isoformat()

    corpus_items = [str(x).strip() for x in (load_json(CORPUS_FILE, {}).get("items") or []) if str(x).strip()]
    daily = load_json(DAILY_FILE, {})
    weekly = load_json(WEEKLY_FILE, {})
    news_sents = [str(c.get("text", "")).strip() for c in (daily.get("cards") or []) if str(c.get("text", "")).strip()]
    weekly_sents = [str(x.get("text", "")).strip() for x in (weekly.get("items") or []) if str(x.get("text", "")).strip()]
    dyn_sents = list(dict.fromkeys(news_sents + weekly_sents))

    prev = load_json(CHUNKS_FILE, {})
    prev_gen = prev.get("gen") if isinstance(prev, dict) else None
    prev_map = (prev.get("map") or {}) if isinstance(prev, dict) else {}

    log(f"=== 预合成 Conrad 语音（{VOICE} {RATE}）===")
    log(f"  内置语料：{len(corpus_items)} 条   当日新闻：{len(news_sents)} 句   本周新语料：{len(weekly_sents)} 句")

    # ---- 阶段 1：语料（含人工词块）+ 新闻/周更的整句 ----
    index: dict[str, dict[str, str]] = {}
    jobs: list[dict] = []
    for t in corpus_items:
        fname = "c-" + sha16(t) + ".mp3"
        jobs.append({"text": t, "path": AUDIO_DIR / fname})
        index[t] = {"0": "news/audio/" + fname}

    bounds_capture: dict[str, list[tuple[int, int]]] = {}
    for t in dyn_sents:
        fname = f"n-{today_str}-" + sha16(t) + ".mp3"
        need_bounds = not (prev_gen == CHUNK_GEN and t in prev_map)
        jobs.append({"text": t, "path": AUDIO_DIR / fname, "capture": bounds_capture, "force": need_bounds})
        index[t] = {"0": "news/audio/" + fname}

    if args.limit:
        jobs = jobs[: args.limit]
        keep = {j["text"] for j in jobs}
        index = {k: v for k, v in index.items() if k in keep}

    if not jobs and not INDEX_FILE.exists():
        log("!! 没有可合成的内容，且没有旧索引，退出。")
        return 1

    st1 = asyncio.run(run_jobs(jobs, args.rebuild))
    log(f"  阶段1（语料+整句）：新增 {st1['new']} / 跳过 {st1['skip']} / 失败 {st1['fail']}")

    # ---- 按停顿切词块 ----
    chunks_map: dict[str, list[str]] = {}
    from_audio = from_sent = 0
    for t in dyn_sents:
        fname = f"n-{today_str}-" + sha16(t) + ".mp3"
        pauses = detect_pauses(AUDIO_DIR / fname)
        ch = chunks_from_audio(t, bounds_capture.get(t) or [], pauses)
        if ch:
            from_audio += 1
        elif prev_gen == CHUNK_GEN and prev_map.get(t):
            ch = prev_map[t]
            from_sent += 1
        chunks_map[t] = ch or []
    # 兜底：用 daily/weekly JSON 里已带的词块（fetch_*.py 按同规则切的）
    for bucket in (daily.get("cards") or [], weekly.get("items") or []):
        for x in bucket:
            t = str(x.get("text", "")).strip()
            if t and not chunks_map.get(t) and x.get("chunks"):
                chunks_map[t] = [str(c).strip() for c in x["chunks"] if str(c).strip()]
    log(f"  词块：按真实停顿切出 {from_audio} 句 / 沿用旧结果 {from_sent} 句（共 {len(dyn_sents)} 句）")

    # ---- 阶段 2：动态句的词块音频 ----
    jobs2: list[dict] = []
    for t in dyn_sents:
        for ch in chunks_map.get(t) or []:
            fname = f"n-{today_str}-" + sha16(ch) + ".mp3"
            jobs2.append({"text": ch, "path": AUDIO_DIR / fname})
            if ch not in index:
                index[ch] = {"0": "news/audio/" + fname}
    if jobs2:
        st2 = asyncio.run(run_jobs(jobs2, args.rebuild))
        log(f"  阶段2（词块）：新增 {st2['new']} / 跳过 {st2['skip']} / 失败 {st2['fail']}")

    # ---- 合并旧索引 + 清理过期/孤儿 ----
    old_index = load_json(INDEX_FILE, {})
    cutoff = today - dt.timedelta(days=NEWS_KEEP_DAYS)
    kept_old = removed = 0
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

    referenced = {v["0"] for v in index.values() if isinstance(v, dict) and v.get("0")}
    orphans = 0
    for f in AUDIO_DIR.glob("*.mp3"):
        if ("news/audio/" + f.name) not in referenced:
            f.unlink(missing_ok=True)
            orphans += 1

    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    CHUNKS_FILE.write_text(json.dumps({"gen": CHUNK_GEN, "map": chunks_map}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    files = list(AUDIO_DIR.glob("*.mp3"))
    total_mb = sum(f.stat().st_size for f in files) / 1048576
    log(f"  索引：{len(index)} 条（沿用旧 {kept_old}）  过期清理 {removed}  孤儿清理 {orphans}")
    log(f"  音频：{len(files)} 个文件，{total_mb:.1f} MB → news/audio.json + news/chunks.json")

    if len(index) == 0:
        log("!! 索引为空，判定为失败。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
