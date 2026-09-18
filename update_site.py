#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把爬蟲產生的 JSON 塞進排片網站（kh_art_planner.html）。

用法：
    py update_site.py                                   # 預設讀 kh_art_showtimes.json
    py update_site.py 我的資料.json                      # 指定 JSON
    py update_site.py 我的資料.json --html kh_art_planner.html
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DATA_RE = re.compile(r"/\*DATA_START\*/.*?/\*DATA_END\*/", re.S)
GEN_RE = re.compile(r"/\*GEN_START\*/.*?/\*GEN_END\*/", re.S)


def main() -> int:
    ap = argparse.ArgumentParser(description="更新排片網站內嵌的場次資料")
    ap.add_argument("json", nargs="?", default="kh_art_showtimes.json")
    ap.add_argument("--html", default="kh_art_planner.html")
    a = ap.parse_args()

    jp, hp = Path(a.json), Path(a.html)
    if not jp.exists():
        print(f"找不到 {jp}，請先執行 kh_art_cinema_scraper.py")
        return 1
    if not hp.exists():
        print(f"找不到 {hp}，請把 update_site.py 和網站檔放在同一個資料夾")
        return 1

    data = json.loads(jp.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or not data:
        print(f"{jp} 裡沒有場次資料")
        return 1

    html = hp.read_text(encoding="utf-8")
    if not DATA_RE.search(html):
        print(f"{hp} 裡找不到資料標記，請使用新版網站檔")
        return 1

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    stamp = datetime.now().strftime("%-m/%-d %H:%M") if sys.platform != "win32" \
        else datetime.now().strftime("%#m/%#d %H:%M")
    html = DATA_RE.sub(lambda _: f"/*DATA_START*/{payload}/*DATA_END*/", html, count=1)
    html = GEN_RE.sub(lambda _: f"/*GEN_START*/{json.dumps(stamp)}/*GEN_END*/", html, count=1)
    hp.write_text(html, encoding="utf-8")

    dates = sorted({d.get("actual_date") or d.get("date") for d in data})
    print(f"已更新 {hp}：{len(data)} 場，{dates[0]} ～ {dates[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
