#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高雄市電影館 (KFA) + 內惟藝術中心 (NWAC) 場次爬蟲
輸出格式與 showtimes.json（atmovies 格式）相同。

安裝：
    pip install requests beautifulsoup4
    (可選) pip install lxml   # 解析較快，沒裝也能跑

使用：
    python kh_art_cinema_scraper.py                       # 兩館都抓，存成 kh_art_showtimes.json
    python kh_art_cinema_scraper.py -o out.json           # 指定輸出檔
    python kh_art_cinema_scraper.py --site kfa            # 只抓高雄市電影館
    python kh_art_cinema_scraper.py --merge showtimes.json
        # 合併進既有檔：先移除這兩館舊資料，再寫入新資料（會先備份 .bak）
    python kh_art_cinema_scraper.py --from 2026-09-17 --to 2026-09-30
    python kh_art_cinema_scraper.py --no-detail           # 不抓電影內頁（沒有片長）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("kh_scraper")

TZ = timezone(timedelta(hours=8))
REGION, REGION_ID = "高雄", "a07"
END_BUFFER_MIN = 12          # showtimes.json 的 end_estimated = start + 片長 + 12 分
MIDNIGHT_CUTOFF_HOUR = 5     # 05:00 前的場次視為隔日（與原始檔一致）

SITES = {
    "kfa": {
        "cinema": "高雄市電影館",
        "theater_id": "t07726",
        "url": "https://kfa.kcg.gov.tw/tw/calendar",
        "link_position": "before",   # 連結在場次資訊「前面」
    },
    "nwac": {
        "cinema": "內惟藝術中心",
        "theater_id": "t07731",
        "url": "https://www.nwac.org.tw/tw/movie-showings",
        "link_position": "after",    # 連結在場次資訊「後面」
    },
}

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
RE_DAY = re.compile(r"^(\d{1,2})\s*(MON|TUE|WED|THU|FRI|SAT|SUN)\.?$", re.I)
RE_DAY_NUM = re.compile(r"^\d{1,2}$")
RE_WEEKDAY = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT|SUN)\.?$", re.I)
RE_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*(.*)$")
RE_YM = re.compile(r"^(20\d{2})[./-](\d{1,2})$")
RE_MONTH_EN = re.compile(r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\.?$", re.I)
MOVIE_LINK = "/movies-content/"


# ─────────────────────────── HTTP ───────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    retry = Retry(total=3, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def fetch_html(session: requests.Session, url: str, delay: float = 0.5) -> str:
    log.info("GET %s", url)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    time.sleep(delay)
    return r.text


# lxml 較快，但沒裝時自動改用 Python 內建的 html.parser
try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"


# ─────────────────────── Token 串流 ───────────────────────
# 兩個網站都是同一家廠商做的、class 名稱可能改版，
# 所以不依賴 CSS class，而是把頁面「依文件順序」攤平成 token：
#   ("text", 字串) / ("link", 網址) / ("day", 日, 星期) / ("ym", 年, 月)
def tokenize(html: str, base_url: str) -> list[tuple]:
    soup = BeautifulSoup(html, PARSER)
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    body = soup.body or soup

    raw: list[tuple] = []
    for node in body.descendants:
        if isinstance(node, Tag) and node.name == "a":
            href = node.get("href") or ""
            if MOVIE_LINK in href:
                raw.append(("link", urljoin(base_url, href)))
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            txt = " ".join(str(node).split())
            if txt:
                raw.append(("text", txt))

    # 合併被拆開的「30」「SUN.」
    out: list[tuple] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok[0] == "text":
            t = tok[1]
            m = RE_DAY.match(t)
            if m:
                out.append(("day", int(m.group(1)), WEEKDAYS[m.group(2).upper()]))
                i += 1
                continue
            if (RE_DAY_NUM.match(t) and i + 1 < len(raw) and raw[i + 1][0] == "text"
                    and RE_WEEKDAY.match(raw[i + 1][1])):
                wd = RE_WEEKDAY.match(raw[i + 1][1]).group(1).upper()
                out.append(("day", int(t), WEEKDAYS[wd]))
                i += 2
                continue
            m = RE_YM.match(t)
            if m:
                out.append(("ym", int(m.group(1)), int(m.group(2))))
                i += 1
                continue
        out.append(tok)
        i += 1
    return out


# ─────────────────────── 日期推算 ───────────────────────
# 月曆格子只有「日 + 星期」，且會混入上/下月的日期。
# 以「前一格日期」為錨點，找 ±45 天內 日、星期 都吻合且最接近的日期。
def _candidates(day: int, wd: int, around: date, span: int) -> list[date]:
    res = []
    for off in range(-span, span + 1):
        d = around + timedelta(days=off)
        if d.day == day and d.weekday() == wd:
            res.append(d)
    return res


def resolve_dates(labels: list[tuple[int, int]], anchor: date) -> list[date | None]:
    """labels: [(day, weekday), ...]  依頁面順序。回傳對應日期。"""
    if not labels:
        return []

    def decode(first: date) -> tuple[list[date | None], int]:
        out, miss, prev = [first], 0, first
        for day, wd in labels[1:]:
            cands = _candidates(day, wd, prev + timedelta(days=1), 45)
            if not cands:
                out.append(None)
                miss += 1
                continue
            best = min(cands, key=lambda d: abs((d - prev - timedelta(days=1)).days))
            out.append(best)
            prev = best
        return out, miss

    d0, w0 = labels[0]
    firsts = _candidates(d0, w0, anchor, 200)
    if not firsts:
        return [None] * len(labels)
    # 解碼失敗數最少者優先，其次離錨點最近
    best = min(((decode(f), f) for f in firsts),
               key=lambda x: (x[0][1], abs((x[1] - anchor).days)))
    return best[0][0]


# ─────────────────────── 解析 ───────────────────────
@dataclass
class RawShow:
    day_idx: int
    url: str
    texts: list[str] = field(default_factory=list)


def parse_page(tokens: list[tuple], link_position: str, anchor: date) -> list[tuple[date, dict]]:
    labels: list[tuple[int, int]] = []
    shows: list[RawShow] = []
    buf: list[str] = []
    current: RawShow | None = None
    ym_locked = False

    for tok in tokens:
        kind = tok[0]
        if kind == "ym":
            # KFA 頁首有「2026.09」→ 當作錨點（取第一個出現的）
            if not labels and not ym_locked:
                anchor = date(tok[1], tok[2], 1)
                ym_locked = True
            continue
        if kind == "day":
            labels.append((tok[1], tok[2]))
            buf, current = [], None
            continue
        if not labels:
            continue
        if kind == "link":
            if link_position == "before":
                current = RawShow(len(labels) - 1, tok[1])
                shows.append(current)
            else:
                shows.append(RawShow(len(labels) - 1, tok[1], buf))
                buf = []
            continue
        # text
        if link_position == "before":
            if current is not None:
                current.texts.append(tok[1])
        else:
            buf.append(tok[1])

    dates = resolve_dates(labels, anchor)
    result = []
    for s in shows:
        d = dates[s.day_idx]
        info = interpret_texts(s.texts)
        if d is None or info is None:
            log.debug("略過無法解析的項目: %s %s", s.url, s.texts)
            continue
        info["url"] = s.url
        result.append((d, info))
    return result


def interpret_texts(texts: list[str]) -> dict | None:
    """從一格的文字找出 片名 / 時間 / 廳別 / 標籤。"""
    ti = next((i for i, t in enumerate(texts) if RE_TIME.match(t)), None)
    if ti is None:
        return None
    m = RE_TIME.match(texts[ti])
    hh, mm, rest = int(m.group(1)), int(m.group(2)), m.group(3).strip()

    title = next((t for t in texts[:ti] if not t.startswith("#")), None)
    if not title:
        return None

    hall = rest or next((t for t in texts[ti + 1:] if not t.startswith("#")), "")
    tags = [t.lstrip("#").strip() for t in texts if t.startswith("#")]
    return {"title": title, "hh": hh, "mm": mm, "hall": hall, "tags": tags}


# ─────────────────────── 電影內頁（片長） ───────────────────────
RE_RUNTIME = [
    re.compile(r"片\s*長\s*[：:／/]?\s*(\d{2,3})"),
    re.compile(r"(\d)\s*(?:小時|hrs?|h)\s*(\d{1,2})\s*(?:分|min|m)", re.I),
    re.compile(r"(\d{2,3})\s*(?:分鐘|分|mins?\.?|minutes)", re.I),
]


def fetch_runtime(session, url: str, cache: dict, delay: float) -> int | None:
    if url in cache:
        return cache[url]
    runtime = None
    try:
        soup = BeautifulSoup(fetch_html(session, url, delay), PARSER)
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        text = " ".join(soup.get_text(" ").split())
        for rx in RE_RUNTIME:
            m = rx.search(text)
            if m:
                runtime = (int(m.group(1)) * 60 + int(m.group(2))
                           if m.lastindex == 2 else int(m.group(1)))
                if 20 <= runtime <= 600:
                    break
                runtime = None
    except requests.RequestException as e:
        log.warning("內頁抓取失敗 %s: %s", url, e)
    cache[url] = runtime
    return runtime


# ─────────────────────── 轉成 showtimes.json 格式 ───────────────────────
def normalize_hall(hall: str) -> str:
    # 「Reel one 1 廳」→「1廳」；「高雄市電影館三樓」→「三樓」
    m = re.search(r"(\d+)\s*廳", hall)
    if m:
        return f"{m.group(1)}廳"
    m = re.search(r"([一二三四五六七八九十\d]+樓)", hall)
    return m.group(1) if m else hall.strip()


def build_record(site_key: str, d: date, info: dict, runtime: int | None) -> dict:
    site = SITES[site_key]
    movie_id = f"{site_key}-{info['url'].rstrip('/').rsplit('/', 1)[-1]}"
    hh, mm = info["hh"], info["mm"]

    crosses = hh < MIDNIGHT_CUTOFF_HOUR
    actual = d + timedelta(days=1) if crosses else d
    start = datetime(actual.year, actual.month, actual.day, hh, mm, tzinfo=TZ)
    end = (start + timedelta(minutes=runtime + END_BUFFER_MIN)) if runtime else None

    events = [t for t in info["tags"] if t and t != "藝術院線"]
    if re.search(r"(4K|數位)?修復", info["title"]) and "經典重映" not in events:
        events.append("經典重映")
    hall = normalize_hall(info["hall"]) if info["hall"] else ""

    return {
        "date": d.isoformat(),
        "region": REGION,
        "region_id": REGION_ID,
        "cinema": site["cinema"],
        "theater_id": site["theater_id"],
        "movie": info["title"],
        "movie_id": movie_id,
        "runtime_minutes": runtime,
        "version": "一般",
        "time": f"{hh:02d}:{mm:02d}",
        "movie_url": info["url"],
        "version_raw": "一般",
        "languages": [],
        "formats": [],
        "halls": [hall] if hall else [],
        "events": events,
        "version_unmapped": False,
        "start": start.isoformat(),
        "end_estimated": end.isoformat() if end else None,
        "crosses_midnight": crosses,
        "actual_date": actual.isoformat(),
    }


# ─────────────────────── 主流程 ───────────────────────
def scrape_site(session, site_key: str, *, today: date, with_detail: bool,
                include_placeholders: bool, delay: float,
                extra_urls: Iterable[str] = ()) -> list[dict]:
    site = SITES[site_key]
    urls = [site["url"], *extra_urls]
    raw: list[tuple[date, dict]] = []
    for url in urls:
        tokens = tokenize(fetch_html(session, url, delay), url)
        raw.extend(parse_page(tokens, site["link_position"], today))

    cache: dict[str, int | None] = {}
    records, seen = [], set()
    for d, info in raw:
        # NWAC 影展以 00:00 佔位（非真實場次），預設略過
        if info["hh"] == 0 and info["mm"] == 0 and not include_placeholders:
            continue
        key = (d, info["hh"], info["mm"], info["url"], info["hall"])
        if key in seen:          # NWAC 頁面同月份會重複輸出兩次
            continue
        seen.add(key)
        runtime = fetch_runtime(session, info["url"], cache, delay) if with_detail else None
        records.append(build_record(site_key, d, info, runtime))

    records.sort(key=lambda r: (r["start"], r["halls"], r["movie"]))
    log.info("%s：%d 筆", site["cinema"], len(records))
    return records


def save_json(records: list[dict], path: str | Path, indent: int = 2) -> Path:
    """儲存成 JSON（UTF-8、保留中文）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(records, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    tmp.replace(path)          # 原子寫入，避免中途失敗留下壞檔
    return path


def merge_into(existing_path: str | Path, new_records: list[dict]) -> list[dict]:
    """讀入既有 showtimes.json，移除這兩館舊資料後加入新資料。"""
    p = Path(existing_path)
    old = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else []
    ids = {r["theater_id"] for r in new_records}
    kept = [r for r in old if r.get("theater_id") not in ids]
    return kept + new_records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="高雄市電影館 / 內惟藝術中心 場次爬蟲")
    ap.add_argument("-o", "--output", default="kh_art_showtimes.json", help="輸出 JSON 路徑")
    ap.add_argument("--merge", metavar="JSON", help="合併進既有 showtimes.json（直接覆寫該檔）")
    ap.add_argument("--site", choices=["kfa", "nwac", "all"], default="all")
    ap.add_argument("--from", dest="date_from", help="只保留此日（含）之後，YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="只保留此日（含）之前，YYYY-MM-DD")
    ap.add_argument("--no-detail", action="store_true", help="不抓電影內頁（片長為 null）")
    ap.add_argument("--include-placeholders", action="store_true",
                    help="保留 00:00 的佔位場次（如影展期間）")
    ap.add_argument("--kfa-extra-url", action="append", default=[],
                    help="額外的 KFA 月曆網址（例如下個月的頁面），可重複")
    ap.add_argument("--delay", type=float, default=0.5, help="每次請求間隔秒數")
    ap.add_argument("--indent", type=int, default=2)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    today = datetime.now(TZ).date()
    session = make_session()
    keys = ["kfa", "nwac"] if a.site == "all" else [a.site]

    records: list[dict] = []
    for k in keys:
        try:
            records += scrape_site(
                session, k, today=today, with_detail=not a.no_detail,
                include_placeholders=a.include_placeholders, delay=a.delay,
                extra_urls=a.kfa_extra_url if k == "kfa" else (),
            )
        except requests.RequestException as e:
            log.error("%s 抓取失敗：%s", SITES[k]["cinema"], e)

    if a.date_from:
        records = [r for r in records if r["date"] >= a.date_from]
    if a.date_to:
        records = [r for r in records if r["date"] <= a.date_to]

    if not records:
        log.error("沒有抓到任何場次，未寫檔。")
        return 1

    if a.merge:
        target = Path(a.merge)
        if target.exists():
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
        merged = merge_into(target, records)
        out = save_json(merged, target, a.indent)
        log.info("合併完成：總計 %d 筆（本次新增 %d 筆）", len(merged), len(records))
    else:
        out = save_json(records, a.output, a.indent)
    log.info("已儲存 %d 筆 → %s", len(records), out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
