#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日素材抓取：从德语新闻 RSS 里挑出 3–5 句适合跟读的句子，写成 news/daily.json。

给 GitHub Actions 定时跑（见 .github/workflows/daily.yml），也可以本地手动跑：
    python scripts/fetch_daily.py

只用 Python 标准库，不装任何依赖。
设计原则：宁可保留上一份，也不写一份空的 —— 全部源失败时以退出码 1 结束，
并保持已存在的 news/daily.json 不变，这样站点上永远有素材可用。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

# ---- 源列表：与工具内置的 09-news.js 保持一致 ---------------------------------
FEEDS = [
    ("tagesschau", "Tagesschau（德国电视一台）", "https://www.tagesschau.de/xml/rss2", "生活"),
    ("dw",         "Deutsche Welle 德语新闻",     "https://rss.dw.com/rdf/rss-de-all",  "生活"),
    ("ams",        "auto motor und sport",        "https://www.auto-motor-und-sport.de/rss/news/", "汽车"),
    ("heiseA",     "heise Autos",                 "https://www.heise.de/themen/auto.rss",     "汽车"),
    ("spiegelA",   "SPIEGEL Auto",                "https://www.spiegel.de/auto/index.rss",    "汽车"),
    ("zeitA",      "ZEIT Mobilität",              "https://newsfeed.zeit.de/mobilitaet/index", "汽车"),
]

MAX_CARDS = 5            # 最多几条
PER_FEED = 2             # 每个源最多取几条
TZ = dt.timezone(dt.timedelta(hours=8))   # 中文用户时区，决定"今天是哪天"
UA = "Mozilla/5.0 (compatible; DeutschNewsCoach/1.0; +https://github.com/)"

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "news" / "daily.json"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---- 选句：优先从正文挑“结构完整的陈述句”，而不是新闻标题 -------------------
# 标题（Tagesschau: „SPD streitet über Haushalt“）常用名词短语、省略动词、
# 冒号引语、全大写，对 A1 学习者既不像句子也含非标准表达。
# 这里改成：把正文拆句后逐句打分，只收“有主语 + 有常见动词 + 以句号结尾”的句子。
VERB_FORMS = {
    # sein / haben / werden
    "ist", "sind", "war", "waren", "bin", "bist", "seid", "gewesen",
    "hat", "habe", "haben", "habt", "hast", "hatte", "hatten",
    "wird", "werden", "werde", "wirst", "werdet", "wurde", "wurden", "geworden",
    # 情态动词
    "kann", "kannst", "können", "könnt", "muss", "musst", "müssen", "müsst",
    "darf", "darfst", "dürfen", "dürft", "will", "willst", "wollen", "wollt",
    "soll", "sollst", "sollen", "sollt", "mag", "mögen", "möchte", "möchten",
    # 高频实义动词
    "gibt", "geben", "geht", "gehen", "kommt", "kommen", "fährt", "fahren",
    "macht", "machen", "sagt", "sagen", "zeigt", "zeigen", "steigt", "steigen",
    "sinkt", "sinken", "bleibt", "bleiben", "braucht", "brauchen", "kostet", "kosten",
    "kauft", "kaufen", "verkauft", "verkaufen", "liegt", "liegen", "steht", "stehen",
    "nimmt", "nehmen", "bringt", "bringen", "hilft", "helfen", "sucht", "suchen",
    "findet", "finden", "denkt", "denken", "plant", "planen", "baut", "bauen",
    "arbeitet", "arbeiten", "lebt", "leben", "lernt", "lernen", "wohnt", "wohnen",
    "sieht", "sehen", "liest", "lesen", "schreibt", "schreiben", "hört", "hören",
    "spricht", "sprechen", "wartet", "warten", "beginnt", "beginnen", "endet", "enden",
    "dauert", "dauern", "wächst", "wachsen", "fällt", "fallen", "öffnet", "schließt",
    "fordert", "fordern", "erklärt", "erklären", "bietet", "bieten", "nutzt", "nutzen",
    "verursacht", "verursachen", "verbessert", "verbessern", "entscheidet", "entscheiden",
    "erwartet", "erwarten", "berichtet", "berichten", "testet", "testen",
}

# 标题腔 / 新闻腔特征：命中就扣分
BAD_PATTERNS = [
    (re.compile(r"[:：]"), 26),                  # 标题常用冒号引出内容
    (re.compile(r"[„“”\"]"), 20),               # 引语
    (re.compile(r"[–—|]"), 18),                  # 破折号 / 竖线
    (re.compile(r"\b[A-ZÄÖÜ]{5,}\b"), 30),      # 全大写长词（SO TESTET / VORSCHRIFTEN）
    (re.compile(r"\b(sei|seien|habe|hätten|wolle|wollen|könne|müsse|werde|sollten)\b"), 14),  # Konjunktiv I 转述
    (re.compile(r"\?"), 30),                     # 问句多依赖上下文，不适合单句跟读
    (re.compile(r"\b(laut|demnach|zuvor|dpa|afp|rtr|epd|Reuters)\b", re.I), 14),
    (re.compile(r"\.\.\.|…"), 15),
    # 开头就是转折/递进连词：后面那个“对比对象”在文章前文里，单句读会莫名
    (re.compile(r"^(Aber|Doch|Zudem|Ferner|Jedoch|Allerdings|Vielmehr)\b"), 12),
    # 指向“本文/播客/视频”的元话语：脱离上下文读起来莫名其妙
    (re.compile(r"\b(im Podcast|in dieser Folge|in diesem Artikel|diesen Artikel|das Video|die Bilder|mehr dazu|zum Artikel|hier|oben|unten)\b", re.I), 28),
    # 以指示代词开头：Das/Dieser… 都在指前面说过的东西
    (re.compile(r"^(Das|Dies|Dieser|Diese|Dieses|Dabei|Dafür|Dagegen|Darüber|Er|Es|Ihm|Ihn|Dann|Danach|Dort)\b"), 16),
]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def score_sentence(s: str) -> int:
    """给一个候选句打分；完全不合格直接返回 -99。"""
    if not s or not s[0].isupper():
        return -99
    if not re.search(r"[.!?]$", s):        # 必须是完整句子（标题常常没有句末标点）
        return -99
    if re.search(r"\b[A-ZÄÖÜ]{5,}\b", s):  # 全大写长词＝标题加粗腔，直接不要（EU/BMW 等缩写不受影响）
        return -99
    n = len(s)
    if not (20 <= n <= 100):
        return -99
    score = 0
    if 28 <= n <= 80:
        score += 20
    words = s.split()
    if 5 <= len(words) <= 14:
        score += 18
    if re.search(r"\b(ich|du|er|sie|es|wir|ihr|man|Sie)\b", s):
        score += 16
    if re.search(r"\b(der|die|das|den|dem|ein|eine|und|aber|oder|weil|dass|wenn|nicht|auch|sehr|viele|immer|oft)\b", s.lower()):
        score += 12
    hits = [w for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", s) if w.lower() in VERB_FORMS]
    score += 22 if hits else -18            # 没有常见动词形态的基本不是完整句
    if s.count(",") == 0:
        score += 8
    elif s.count(",") >= 3:
        score -= 12
    if n > 85:
        score -= 8
    # 从句多的句子对 A1 偏难；有更简单的主谓句时优先选简单的
    if re.search(r"\b(dass|weil|obwohl|damit|während|sofern|falls)\b", s.lower()):
        score -= 10
    for rx, pen in BAD_PATTERNS:
        if rx.search(s):
            score -= pen
    return score


def pick_sentence(item: dict, min_score: int = 40) -> str:
    """从正文里挑一句最适合初学的完整句；不行再考虑标题（标题本身是完整句时）。
    挑不到就返回空串，宁可这条不用，也不要给一句标题腔。
    正文的**第一句**天然自包含（没有前指），给额外加分；越靠后的句子越可能
    出现“der/er/das”这类指代前文的表达，分数递减。"""
    best, best_s = "", -999
    cands = _split_sentences(item.get("desc", ""))
    for pos, cand in enumerate(cands):
        sc = score_sentence(cand)
        sc += 18 if pos == 0 else (6 if pos == 1 else 0)      # 首句优先
        if sc > best_s:
            best_s, best = sc, cand
    for cand in _split_sentences(item.get("title", "")):
        sc = score_sentence(cand)
        if sc > best_s:
            best_s, best = sc, cand
    return best if best_s >= min_score else ""


def fetch(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return None
        return raw.decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001 - 单源失败不应中断整体
        log(f"  × {url} -> {type(exc).__name__}: {exc}")
        return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def text_of(el: ET.Element) -> str:
    return "".join(el.itertext()).strip()


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]*>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_items(xml_text: str, feed_name: str, topic: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log(f"  × {feed_name}: XML 解析失败 {exc}")
        return []
    items = []
    for el in root.iter():
        if local_name(el.tag) not in ("item", "entry"):
            continue
        title = desc = link = date = ""
        for child in el:
            name = local_name(child.tag)
            if name == "title":
                title = text_of(child)
            elif name in ("description", "summary", "content"):
                if not desc:
                    desc = text_of(child)
            elif name == "link":
                link = (child.get("href") or text_of(child) or "").strip()
            elif name in ("pubdate", "updated", "date", "published"):
                if not date:
                    date = text_of(child)
        title = strip_html(title)
        desc = strip_html(desc)
        if title:
            items.append({"feed": feed_name, "topic": topic, "title": title,
                          "desc": desc, "link": link, "date": date})
    return items


def sentence_from(item: dict) -> str:
    """从标题/摘要里取一句适合跟读的德语。"""
    title = re.sub(r"^(dpa|afp|rtr|reuters|epd)[\s/\-]*", "", item["title"], flags=re.I).strip()
    if 18 <= len(title) <= 120:
        return title
    desc = re.sub(r"^(dpa|afp|rtr|reuters|epd)[\s/\-]*", "", item["desc"], flags=re.I).strip()
    m = re.search(r"[^.!?]{15,120}[.!?]", desc)
    if m:
        return m.group(0).strip()
    return (desc[:110] or title).strip()


def score(item: dict) -> float:
    s = 0.0
    n = len(item["title"])
    if 25 <= n <= 95:
        s += 40
    elif n < 120:
        s += 15
    if re.search(r"auto|elektro|laden|verkehr|motor|fahrzeug|suv", item["title"], re.I):
        s += 8
    return s


def norm(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


# ---- 词块切分：必须与前端 04-nlp.js 的 autoChunks() 完全一致 --------------------
# 前端对「新闻 / 每周新语料」会在浏览器里现切词块；如果服务端不按同一规则
# 把词块也合成好，那些词块就会没有音频、回退到系统合成音（听起来像机器人）。
STOP_PRE = re.compile(
    r"^(in|im|an|am|auf|aus|bei|beim|mit|nach|von|vom|zu|zum|zur|für|über|unter|vor|"
    r"seit|gegen|ohne|bis|durch|zwischen|um|der|die|das|den|dem|ein|eine|einen|einem|"
    r"einer|kein|keine|mein|meine|unser|unsere|dieser|diese|dieses|sein|seine|ihr|ihre)$",
    re.I)
_CHUNK_PUNCT = re.compile("[.,!?;:„“”'()]")
_CHUNK_NONWORD = re.compile(r"[^\wäöüß\- ]", re.ASCII)
_END_PUNCT = re.compile(r"[.,!?;:]$")


def auto_chunks(sentence: str) -> list[str]:
    """把一句德语切成词块（与前端 autoChunks 同规则）。"""
    if not sentence:
        return []
    words = re.split(r"\s+", sentence)
    out: list[str] = []
    cur: list[str] = []
    for i, raw in enumerate(words):
        cur.append(raw)
        nxt = _CHUNK_PUNCT.sub("", words[i + 1]) if i + 1 < len(words) else None
        end_punct = bool(_END_PUNCT.search(raw))
        length = len(_CHUNK_NONWORD.sub("", " ".join(cur)))
        if end_punct or length > 22 or len(cur) >= 3 or (nxt and STOP_PRE.match(nxt) and length > 8):
            out.append(" ".join(cur))
            cur = []
        if i == len(words) - 1 and cur:
            out.append(" ".join(cur))
    return out


def main() -> int:
    day = dt.datetime.now(TZ).date().isoformat()
    log(f"=== 抓取 {day} 的德语新闻素材 ===")

    collected: list[dict] = []
    ok_feeds = 0
    for fid, name, url, topic in FEEDS:
        xml = fetch(url)
        if not xml:
            continue
        items = parse_items(xml, name, topic)
        if items:
            ok_feeds += 1
            log(f"  ✓ {name}: {len(items)} 条")
        collected.extend(items)

    if not collected:
        log("!! 所有源都失败，保留现有 daily.json 不动。")
        return 1

    collected.sort(key=score, reverse=True)

    def build(min_score: int) -> list[dict]:
        out: list[dict] = []
        per_feed: dict[str, int] = {}
        seen: set[str] = set()
        for it in collected:
            if per_feed.get(it["feed"], 0) >= PER_FEED:
                continue
            body = pick_sentence(it, min_score)
            if not body:
                continue
            k = norm(body)
            if k in seen:
                continue
            seen.add(k)
            per_feed[it["feed"]] = per_feed.get(it["feed"], 0) + 1
            out.append({**it, "text": body})
            if len(out) >= MAX_CARDS:
                break
        return out

    # 先用严格标准（结构清晰、无标题腔）；实在凑不够再逐步放宽
    picked: list[dict] = []
    for ms in (45, 35, 25):
        picked = build(ms)
        if len(picked) >= 3:
            break

    if not picked:
        log("!! 正文里没找到结构完整的句子，保留现有 daily.json 不动。")
        return 1

    cards = [{
        "id": f"N-{day}-{i}",
        "topic": it["topic"],
        "source": it["feed"],
        "title": it["title"],
        "text": it["text"],
        "chunks": auto_chunks(it["text"]),   # 与前端同规则，供预合成音频使用
        "link": it["link"],
        "date": it["date"],
        "isNews": True,
    } for i, it in enumerate(picked)]

    payload = {
        "date": day,
        "generatedAt": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        "source": "live",
        "feedsOk": ok_feeds,
        "cards": cards,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✓ 写入 {OUT}（{len(cards)} 条，{ok_feeds}/{len(FEEDS)} 个源可用）")
    for c in cards:
        log(f"    · [{c['topic']}] {c['text'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
