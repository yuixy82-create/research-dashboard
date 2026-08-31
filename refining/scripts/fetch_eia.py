#!/usr/bin/env python3
"""EIA API v2에서 코어 3종을 받아 refining/data를 갱신한다.

산출물
  data/auto.json                 KPI 타일용 최신값
  data/series/diesel_crack_1_1.json
  data/series/crack_3_2_1.json
  data/series/us_distillate_stock.json

크랙은 EIA가 직접 주지 않으므로 스팟 3종에서 산출한다.
  디젤 1:1 크랙 = ULSD($/gal) x 42 - WTI($/bbl)
  3-2-1 크랙    = (RBOB x 42 x 2 + ULSD x 42 x 1 - WTI x 3) / 3

세 스팟의 최신 날짜가 서로 다를 수 있으므로 날짜 교집합에서만 계산한다.
매 실행마다 EIA 원본에서 전체 구간을 다시 만든다: 값이 사후 수정돼도 따라감.

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

GAL_PER_BBL = 42.0
KEEP_DAILY = 260      # 크랙 계열 보존 포인트 (약 1년치 영업일)
KEEP_WEEKLY = 156     # 재고 계열 보존 포인트 (약 3년치)

SPOT = {
    # EIA 스팟 가격. 전부 route=petroleum/pri/spt, frequency=daily
    "wti":  {"series": "RWTC",                        "label": "WTI 쿠싱 현물"},
    "ulsd": {"series": "EER_EPD2DXL0_PF4_RGC_DPG",    "label": "걸프 ULSD 현물"},
    "rbob": {"series": "EER_EPMRU_PF4_RGC_DPG",       "label": "걸프 휘발유 현물"},
}
STOCK_SERIES = "WDISTUS1"   # 미국 중간유분 주간 재고, 천배럴


def call(route, series, freq, length):
    if not KEY:
        raise RuntimeError("EIA_API_KEY 미설정: 저장소 Secrets 확인")
    q = {
        "api_key": KEY,
        "frequency": freq,
        "data[0]": "value",
        "facets[series][]": series,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": str(length),
    }
    url = f"{API}/{route}/data/?" + urllib.parse.urlencode(q, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "research-dashboard/refining"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.load(r)
    resp = payload.get("response") or {}
    rows = resp.get("data") or []
    if not rows:
        raise RuntimeError(f"{series}: 빈 응답")
    unit = str(rows[0].get("units") or "")
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
    return out, unit


def to_bbl(value, unit):
    """스팟 단위가 $/gal이면 배럴로 환산한다."""
    return value * GAL_PER_BBL if "GAL" in unit.upper() else value


def write_series(key, label, unit, points, keep):
    points = sorted(points, key=lambda p: p["d"])[-keep:]
    body = {
        "key": key, "label": label, "unit": unit,
        "demo": False,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "points": points,
    }
    (SERIES_DIR / f"{key}.json").write_text(
        json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
    return points[-1] if points else None


def main():
    out = {
        "_comment": "GitHub Actions가 덮어쓰는 파일. 손으로 고치지 말 것. manual.json의 같은 key를 덮어씀.",
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok", "indicators": {}, "errors": [],
    }

    # --- 스팟 3종 ---
    spot, units = {}, {}
    for name, cfg in SPOT.items():
        try:
            spot[name], units[name] = call("petroleum/pri/spt", cfg["series"], "daily", 400)
        except Exception as e:
            out["errors"].append(f"{name}({cfg['series']}): {e}")

    if {"wti", "ulsd"} <= spot.keys():
        dates = sorted(set(spot["wti"]) & set(spot["ulsd"]))
        pts = [{"d": d,
                "v": round(to_bbl(spot["ulsd"][d], units["ulsd"]) - to_bbl(spot["wti"][d], units["wti"]), 2)}
               for d in dates]
        last = write_series("diesel_crack_1_1", "디젤 1:1 크랙", "$/bbl", pts, KEEP_DAILY)
        if last:
            out["indicators"]["diesel_crack_1_1"] = {
                "value": last["v"], "unit": "$/bbl", "asOf": last["d"],
                "source": "EIA 스팟 기반 산출", "mode": "auto"}

        if "rbob" in spot:
            d3 = sorted(set(dates) & set(spot["rbob"]))
            pts3 = []
            for d in d3:
                w = to_bbl(spot["wti"][d], units["wti"])
                g = to_bbl(spot["rbob"][d], units["rbob"])
                o = to_bbl(spot["ulsd"][d], units["ulsd"])
                pts3.append({"d": d, "v": round((g * 2 + o - w * 3) / 3, 2)})
            last3 = write_series("crack_3_2_1", "3-2-1 크랙", "$/bbl", pts3, KEEP_DAILY)
            if last3:
                out["indicators"]["crack_3_2_1"] = {
                    "value": last3["v"], "unit": "$/bbl", "asOf": last3["d"],
                    "source": "EIA 스팟 기반 산출", "mode": "auto"}

    # --- 주간 재고 ---
    try:
        stock, unit = call("petroleum/stoc/wstk", STOCK_SERIES, "weekly", 220)
        div = 1000.0 if "THOUSAND" in unit.upper() else 1.0   # 천배럴 -> 백만배럴
        pts = [{"d": d, "v": round(v / div, 1)} for d, v in stock.items()]
        last = write_series("us_distillate_stock", "미국 중간유분 재고", "mb", pts, KEEP_WEEKLY)
        if last:
            note = None
            tail = sorted(pts, key=lambda p: p["d"])[-5:]
            if len(tail) == 5 and all(tail[i]["v"] < tail[i + 1]["v"] for i in range(4)):
                note = "4주 연속 증가"
            out["indicators"]["us_distillate_stock"] = {
                "value": last["v"], "unit": "mb", "asOf": last["d"],
                "note": note, "source": "EIA Weekly", "mode": "auto"}
    except Exception as e:
        out["errors"].append(f"us_distillate_stock({STOCK_SERIES}): {e}")

    if out["errors"]:
        out["status"] = "partial" if out["indicators"] else "error"
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["indicators"] else 1


if __name__ == "__main__":
    sys.exit(main())
