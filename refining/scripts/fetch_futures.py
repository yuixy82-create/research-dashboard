#!/usr/bin/env python3
"""NYMEX 선물 종가로 디젤 1:1 크랙과 3-2-1 크랙을 매일 산출한다.

  HO=F  NY Harbor ULSD 근월물 ($/gal)
  RB=F  RBOB 휘발유 근월물 ($/gal)
  CL=F  WTI 근월물 ($/bbl)

  디젤 1:1 크랙 = HO x 42 - CL
  3-2-1 크랙    = (RB x 42 x 2 + HO x 42 - CL x 3) / 3

EIA 스팟을 쓰지 않는 이유: EIA는 주 1회(수) 배치로만 공표해 평균 4일이 밀림.
선물은 매 영업일 정산되므로 블룸버그 등이 인용하는 값과 같은 주기로 따라감.
근월물 연결 계열이라 롤오버 시점에 작은 단차가 생김: 수준보다 방향을 볼 것.

하루 두 번 도는 것을 전제로 함.
  22:00 KST (09:00 ET)  장중이라 마지막 점이 잠정치. provisional=true로 표시함
  07:30 KST (18:30 ET)  전일 정산(14:30 ET)이 끝난 뒤라 같은 날짜가 확정치로 덮임
매 실행마다 원본에서 계열 전체를 다시 만들기 때문에 확정 전환이 저절로 일어남.

사용:
  python3 refining/scripts/fetch_futures.py
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
AUTO = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

GAL_PER_BBL = 42.0
KEEP = 260
SYMBOLS = {"ho": "HO=F", "rb": "RB=F", "cl": "CL=F"}
SETTLE_HOUR = 14.5   # NYMEX 에너지 선물 정산 14:30 ET


def is_provisional(last_day, gmtoffset):
    """마지막 점이 아직 정산 전인지. 장중이면 잠정치임."""
    now = datetime.now(timezone.utc) + timedelta(seconds=gmtoffset)
    if last_day != now.strftime("%Y-%m-%d"):
        return False
    return now.hour + now.minute / 60 < SETTLE_HOUR


def series(sym):
    """{'YYYY-MM-DD': 종가}. 거래소 현지 날짜로 맞춘다."""
    req = urllib.request.Request(CHART.format(sym=sym.replace("=", "%3D")),
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.load(r)
    res = (payload.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"{sym}: 빈 응답")
    res = res[0]
    off = res.get("meta", {}).get("gmtoffset") or 0
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = float(c)
    if not out:
        raise RuntimeError(f"{sym}: 종가가 전부 결측")
    return out, off


def write_series(key, label, points, provisional):
    points = sorted(points, key=lambda p: p["d"])[-KEEP:]
    (SERIES_DIR / f"{key}.json").write_text(json.dumps({
        "key": key, "label": label, "unit": "$/bbl", "demo": False,
        "provisional": provisional,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "points": points,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return points[-1] if points else None


def merge_auto(new_ind, errors):
    """자기 key만 얹는다. EIA · 관세청 수집분을 지우지 않기 위함."""
    try:
        cur = json.loads(AUTO.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.setdefault("_comment", "GitHub Actions가 덮어쓰는 파일. 손으로 고치지 말 것. manual.json의 같은 key를 덮어씀.")
    cur.setdefault("indicators", {})
    cur["indicators"].update(new_ind)
    prior = [e for e in (cur.get("errors") or []) if not e.startswith("futures:")]
    cur["errors"] = prior + errors
    cur["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur["status"] = "ok" if not cur["errors"] else ("partial" if cur["indicators"] else "error")
    AUTO.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    px, errors, ind, offset = {}, [], {}, 0
    for name, sym in SYMBOLS.items():
        try:
            px[name], offset = series(sym)
        except Exception as e:
            errors.append(f"futures: {sym}: {e}")

    if {"ho", "cl"} <= px.keys():
        days = sorted(set(px["ho"]) & set(px["cl"]))
        pts = [{"d": d, "v": round(px["ho"][d] * GAL_PER_BBL - px["cl"][d], 2)} for d in days]
        prov = is_provisional(pts[-1]["d"], offset) if pts else False
        last = write_series("diesel_crack_1_1", "디젤 1:1 크랙", pts, prov)
        if last:
            ind["diesel_crack_1_1"] = {
                "value": last["v"], "unit": "$/bbl", "asOf": last["d"],
                "note": "장중 잠정치" if prov else None,
                "source": "NYMEX ULSD · WTI 선물 " + ("장중" if prov else "종가") + " 기반 산출",
                "mode": "auto"}

        if "rb" in px:
            d3 = sorted(set(days) & set(px["rb"]))
            pts3 = []
            for d in d3:
                h = px["ho"][d] * GAL_PER_BBL
                g = px["rb"][d] * GAL_PER_BBL
                c = px["cl"][d]
                pts3.append({"d": d, "v": round((g * 2 + h - c * 3) / 3, 2)})
            prov3 = is_provisional(pts3[-1]["d"], offset) if pts3 else False
            last3 = write_series("crack_3_2_1", "3-2-1 크랙", pts3, prov3)
            if last3:
                ind["crack_3_2_1"] = {
                    "value": last3["v"], "unit": "$/bbl", "asOf": last3["d"],
                    "note": "장중 잠정치" if prov3 else None,
                    "source": "NYMEX 선물 " + ("장중" if prov3 else "종가") + " 기반 산출",
                    "mode": "auto"}
    else:
        errors.append("futures: HO · CL 중 하나가 없어 크랙을 산출하지 못함")

    merge_auto(ind, errors)
    print(json.dumps({"indicators": ind, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
