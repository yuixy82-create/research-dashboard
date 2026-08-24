#!/usr/bin/env python3
"""EIA API v2에서 가격·재고를 받아 data/auto.json을 갱신한다.

미완성 상태다. 활성화 전에 아래를 확정해야 한다.
  1. SERIES의 route/facet 값이 실제 응답을 반환하는지
  2. 단위 환산 (스팟 가격은 $/gal, 크랙은 $/bbl -> ×42)
  3. 계산된 크랙이 Reuters 공시값과 일치하는지

사용:
  EIA_API_KEY=xxxx python3 scripts/fetch_eia.py
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
OUT = Path(__file__).resolve().parent.parent / "data" / "auto.json"

# TODO: 각 항목의 route/series를 EIA API 브라우저에서 확정할 것.
#       https://www.eia.gov/opendata/browser/
SERIES = {
    "wti_spot":        {"route": "petroleum/pri/spt",  "series": "RWTC",  "freq": "daily"},
    "ulsd_spot":       {"route": "petroleum/pri/spt",  "series": None,    "freq": "daily"},   # 걸프 ULSD
    "rbob_spot":       {"route": "petroleum/pri/spt",  "series": None,    "freq": "daily"},   # 걸프 휘발유
    "us_distillate_stock": {"route": "petroleum/stoc/wstk", "series": "WDISTUS1", "freq": "weekly"},
}


def fetch(route, series, freq, length=5):
    if not KEY:
        raise SystemExit("EIA_API_KEY 미설정")
    if not series:
        return None
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
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.load(r)
    rows = payload.get("response", {}).get("data", [])
    return rows[0] if rows else None


def main():
    out = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "status": "ok", "indicators": {}, "errors": []}
    raw = {}
    for name, cfg in SERIES.items():
        try:
            raw[name] = fetch(cfg["route"], cfg["series"], cfg["freq"])
        except Exception as e:  # 실패해도 나머지는 계속
            raw[name] = None
            out["errors"].append(f"{name}: {e}")

    # 재고
    st = raw.get("us_distillate_stock")
    if st:
        out["indicators"]["us_distillate_stock"] = {
            "value": round(float(st["value"]) / 1000, 1),  # 천배럴 -> 백만배럴
            "unit": "mb", "asOf": st["period"], "source": "EIA Weekly", "mode": "auto",
        }

    # 크랙 (세 스팟이 모두 있을 때만)
    wti, ulsd, rbob = raw.get("wti_spot"), raw.get("ulsd_spot"), raw.get("rbob_spot")
    if wti and ulsd:
        w, d = float(wti["value"]), float(ulsd["value"]) * 42
        out["indicators"]["diesel_crack_1_1"] = {
            "value": round(d - w, 2), "unit": "$/bbl",
            "asOf": ulsd["period"], "source": "EIA 스팟 기반 산출", "mode": "auto",
        }
        if rbob:
            g = float(rbob["value"]) * 42
            out["indicators"]["crack_3_2_1"] = {
                "value": round((g * 2 + d - w * 3) / 3, 2), "unit": "$/bbl",
                "asOf": ulsd["period"], "source": "EIA 스팟 기반 산출", "mode": "auto",
            }

    if out["errors"]:
        out["status"] = "partial"
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not out["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
