#!/usr/bin/env python3
"""EIA API v2에서 미국 중간유분 주간 재고를 받아 data를 갱신한다.

크랙은 여기서 다루지 않음: EIA 스팟은 주 1회(수) 배치 공표라 평균 4일이 밀림.
매 영업일 갱신되는 선물 종가로 옮겼고 fetch_futures.py가 담당함.
주간 재고는 EIA 말고 대체 소스가 없어 그대로 둠.

사용:
  EIA_API_KEY=xxxx python3 refining/scripts/fetch_eia.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.eia.gov/v2"
KEY = os.environ.get("EIA_API_KEY")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

KEEP_WEEKLY = 156
STOCK_SERIES = "WDISTUS1"   # 미국 중간유분 주간 재고


def call(route, series, freq, length):
    if not KEY:
        raise RuntimeError("EIA_API_KEY 미설정: 저장소 Secrets 확인")
    q = {
        "api_key": KEY, "frequency": freq, "data[0]": "value",
        "facets[series][]": series,
        "sort[0][column]": "period", "sort[0][direction]": "desc",
        "length": str(length),
    }
    url = f"{API}/{route}/data/?" + urllib.parse.urlencode(q, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "research-dashboard/refining"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.load(r)
    rows = (payload.get("response") or {}).get("data") or []
    if not rows:
        raise RuntimeError(f"{series}: 빈 응답")
    out = {}
    for row in rows:
        v = row.get("value")
        if v is None:
            continue
        try:
            out[row["period"]] = float(v)
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"{series}: 값이 전부 결측")
    return out, str(rows[0].get("units") or "")


def merge_auto(new_ind, errors):
    """자기 key만 얹는다. 선물 · 관세청 수집분을 지우지 않기 위함."""
    try:
        cur = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.setdefault("_comment", "GitHub Actions가 덮어쓰는 파일. 손으로 고치지 말 것. manual.json의 같은 key를 덮어씀.")
    cur.setdefault("indicators", {})
    cur["indicators"].update(new_ind)
    prior = [e for e in (cur.get("errors") or []) if not e.startswith("eia:")]
    cur["errors"] = prior + errors
    cur["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur["status"] = "ok" if not cur["errors"] else ("partial" if cur["indicators"] else "error")
    OUT.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ind, errors = {}, []
    try:
        stock, unit = call("petroleum/stoc/wstk", STOCK_SERIES, "weekly", 220)
        # EIA는 이 계열의 단위를 "MBBL"(천배럴)로 적음. 문자열 대신 값 크기로 판정함
        div = 1000.0 if max(stock.values()) > 1000 else 1.0
        pts = sorted(({"d": d, "v": round(v / div, 1)} for d, v in stock.items()),
                     key=lambda p: p["d"])[-KEEP_WEEKLY:]
        (SERIES_DIR / "us_distillate_stock.json").write_text(json.dumps({
            "key": "us_distillate_stock", "label": "미국 중간유분 재고", "unit": "mb",
            "demo": False,
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "points": pts,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        note = None
        tail = pts[-5:]
        if len(tail) == 5 and all(tail[i]["v"] < tail[i + 1]["v"] for i in range(4)):
            note = "4주 연속 증가"
        ind["us_distillate_stock"] = {
            "value": pts[-1]["v"], "unit": "mb", "asOf": pts[-1]["d"],
            "note": note, "source": "EIA Weekly", "mode": "auto"}
    except Exception as e:
        errors.append(f"eia: {STOCK_SERIES}: {e}")

    merge_auto(ind, errors)
    print(json.dumps({"indicators": ind, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
