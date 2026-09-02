#!/usr/bin/env python3
"""관세청 품목별 수출입실적에서 PX − 나프타 스프레드를 산출한다.

  https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList
  파라미터  serviceKey · strtYymm(YYYYMM) · endYymm · hsSgn
  응답 항목  year · statKor · hsCode · expDlr · expWgt · impDlr · impWgt (금액 달러, 중량 KG)

한국은 PX 순수출국, 나프타 순수입국이므로 각각 수출단가 · 수입단가를 씀.
수출은 FOB, 수입은 CIF 기준이라 스프레드에 운임 · 보험료가 섞임: 평가가격이 아닌 근사치임.

관세청은 매월 15일경 전월까지의 자료를 정정 반영하므로 매 실행마다 구간 전체를 다시 만든다.

사용:
  DATA_GO_KR_KEY=xxxx python3 refining/scripts/fetch_customs.py
  DATA_GO_KR_KEY=xxxx python3 refining/scripts/fetch_customs.py --probe   # 세번 탐색
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, date
from pathlib import Path

API = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
KEY = os.environ.get("DATA_GO_KR_KEY") or ""
# 포털이 보여주는 키는 URL 인코딩된 형태(%2B, %3D)일 수 있음. urlencode가 다시 감싸므로 먼저 풀어둠
if "%" in KEY:
    KEY = urllib.parse.unquote(KEY)
ROOT = Path(__file__).resolve().parent.parent
AUTO = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

KEEP_MONTHS = 60
MONTHS_BACK = 72

# 확정된 세번. --probe로 statKor를 확인해 고름
HS_PX = "290243"       # 파라자일렌
HS_NAPHTHA = "271012"  # 경질유 및 조제품(나프타 포함)
PROBE_CODES = ["290243", "271012", "271019", "271011"]


def call(hs, strt, end):
    if not KEY:
        raise RuntimeError("DATA_GO_KR_KEY 미설정: 저장소 Secrets 확인")
    q = {"serviceKey": KEY, "strtYymm": strt, "endYymm": end, "hsSgn": hs}
    url = API + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "research-dashboard/refining"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    code = root.findtext(".//resultCode")
    if code not in (None, "00", "0"):
        raise RuntimeError(f"{hs}: resultCode={code} {root.findtext('.//resultMsg')}")
    rows = []
    for it in root.iter("item"):
        g = lambda k: (it.findtext(k) or "").strip()
        rows.append({
            "year": g("year"), "hsCode": g("hsCode"), "statKor": g("statKor"),
            "expDlr": g("expDlr"), "expWgt": g("expWgt"),
            "impDlr": g("impDlr"), "impWgt": g("impWgt"),
        })
    if not rows:
        raise RuntimeError(f"{hs}: 빈 응답")
    return rows


def window():
    t = date.today()
    end = t.year * 100 + t.month
    y, m = t.year, t.month - MONTHS_BACK
    while m <= 0:
        m += 12; y -= 1
    return f"{y}{m:02d}", f"{end}"


def unit_price(dlr, wgt):
    """$/톤. 금액은 달러, 중량은 KG."""
    try:
        d, w = float(dlr), float(wgt)
    except (TypeError, ValueError):
        return None
    return round(d / (w / 1000.0), 1) if w > 0 else None


def monthly(rows, side):
    """{YYYYMM: 단가}. year가 'YYYYMM'인 월별 행만 취함(합계 행 제외)."""
    out = {}
    for r in rows:
        ym = r["year"]
        if not (len(ym) == 6 and ym.isdigit()):
            continue
        u = unit_price(r["expDlr"], r["expWgt"]) if side == "exp" else unit_price(r["impDlr"], r["impWgt"])
        if u:
            out[ym] = u
    return out


def probe():
    strt, end = window()
    recent = f"{end}"
    y, m = int(recent[:4]), int(recent[4:]) - 6
    while m <= 0:
        m += 12; y -= 1
    for hs in PROBE_CODES:
        print(f"\n===== hsSgn={hs} =====")
        try:
            rows = call(hs, f"{y}{m:02d}", end)
        except Exception as e:
            print("  ERR", e); continue
        for r in rows[:14]:
            print(f"  {r['year']:>8} {r['hsCode']:>12} {r['statKor'][:26]:<26} "
                  f"수출 {unit_price(r['expDlr'], r['expWgt'])} $/t   수입 {unit_price(r['impDlr'], r['impWgt'])} $/t")
    return 0


def write_series(key, label, unit, points):
    points = sorted(points, key=lambda p: p["d"])[-KEEP_MONTHS:]
    (SERIES_DIR / f"{key}.json").write_text(json.dumps({
        "key": key, "label": label, "unit": unit, "demo": False,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "points": points,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return points[-1] if points else None


def merge_auto(new_ind, errors):
    """EIA 결과를 지우지 않도록 기존 auto.json에 얹는다."""
    try:
        cur = json.loads(AUTO.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.setdefault("_comment", "GitHub Actions가 덮어쓰는 파일. 손으로 고치지 말 것. manual.json의 같은 key를 덮어씀.")
    cur.setdefault("indicators", {})
    cur["indicators"].update(new_ind)
    prior = [e for e in (cur.get("errors") or []) if not e.startswith("customs:")]
    cur["errors"] = prior + errors
    cur["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if new_ind or not cur.get("status"):
        cur["status"] = "partial" if cur["errors"] else "ok"
    AUTO.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cur


def main():
    if "--probe" in sys.argv:
        return probe()

    strt, end = window()
    ind, errors = {}, []
    try:
        px = monthly(call(HS_PX, strt, end), "exp")
        nap = monthly(call(HS_NAPHTHA, strt, end), "imp")
        months = sorted(set(px) & set(nap))
        if not months:
            raise RuntimeError("겹치는 월이 없음")
        pts = [{"d": f"{m[:4]}-{m[4:]}-01", "v": round(px[m] - nap[m], 1)} for m in months]
        last = write_series("px_naphtha_spread", "PX − 나프타 스프레드", "$/t", pts)
        if last:
            ind["px_naphtha_spread"] = {
                "value": last["v"], "unit": "$/t", "asOf": last["d"],
                "note": "관세청 수출입단가 기반 근사",
                "source": "관세청 품목별 수출입실적", "mode": "auto",
            }
    except Exception as e:
        errors.append(f"customs: px_naphtha_spread: {e}")

    out = merge_auto(ind, errors)
    print(json.dumps({"indicators": ind, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
