#!/usr/bin/env python3
"""수동 지표에 점 하나를 추가한다. 매주 iM증권 위클리를 옮겨 적을 때 쓴다.

  python3 refining/scripts/add_point.py sing_grm 2026-09-04 30.1 --note "전주 대비 −2.7"
  python3 refining/scripts/add_point.py saudi_osp_light 2026-10-01 -12.5 --note "10월분, 전월 −0.5"

하는 일
  1. data/series/<key>.json 에 {d, v}를 넣는다 (같은 날짜면 덮어씀, 날짜순 정렬)
  2. data/manual.json 의 같은 key에 value · asOf · note 를 갱신한다 (있을 때만)
  3. --base 를 주면 manual.json 의 baseDate 도 바꾼다

자동 수집 키(diesel_crack_1_1 · crack_3_2_1 · us_distillate_stock · px_naphtha_spread)는
auto.json이 덮으므로 여기로 넣지 말 것.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIES = ROOT / "data" / "series"
MANUAL = ROOT / "data" / "manual.json"
AUTO_KEYS = {"diesel_crack_1_1", "crack_3_2_1", "us_distillate_stock", "px_naphtha_spread", "group3_price", "group3_spread"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("value", type=float)
    ap.add_argument("--note", default=None)
    ap.add_argument("--base", default=None, help="manual.json baseDate (YYYY-MM-DD)")
    a = ap.parse_args()
    if a.key in AUTO_KEYS:
        print("자동 수집 키임. auto.json이 덮어씀:", a.key); return 1
    datetime.strptime(a.date, "%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    f = SERIES / f"{a.key}.json"
    s = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {
        "key": a.key, "label": a.key, "unit": "", "demo": False, "points": []}
    pts = [p for p in s.get("points", []) if p.get("d") != a.date]
    v = int(a.value) if a.value == int(a.value) else a.value
    pts.append({"d": a.date, "v": v})
    s["points"] = sorted(pts, key=lambda p: p["d"])
    s["updatedAt"] = now
    f.write_text(json.dumps(s, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"series/{a.key}.json: {len(s['points'])}점, 마지막 {a.date} {v}")

    m = json.loads(MANUAL.read_text(encoding="utf-8"))
    ind = m.get("indicators", {}).get(a.key)
    if ind is not None:
        ind["value"] = v
        ind["asOf"] = a.date
        if a.note is not None:
            ind["note"] = a.note
        print(f"manual.json {a.key}: value={v} asOf={a.date}")
    if a.base:
        m["baseDate"] = a.base
    MANUAL.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
