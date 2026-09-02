#!/usr/bin/env python3
"""NYMEX 선물 종가로 디젤 1:1 크랙과 3-2-1 크랙을 매일 산출한다.

  ULSD 근월물 ($/gal) · RBOB 근월물 ($/gal) · WTI 근월물 ($/bbl)

  디젤 1:1 크랙 = ULSD x 42 - WTI
  3-2-1 크랙    = (RBOB x 42 x 2 + ULSD x 42 - WTI x 3) / 3

EIA 스팟을 쓰지 않는 이유: EIA는 주 1회(수) 배치로만 공표해 평균 4일이 밀림.
선물은 매 영업일 정산되므로 블룸버그 등이 인용하는 값과 같은 주기로 따라감.
근월물 연결 계열이라 롤오버 시점에 작은 단차가 생김: 수준보다 방향을 볼 것.

하루 두 번 도는 것을 전제로 함.
  22:00 KST (09:00 ET)  장중이라 마지막 점이 잠정치. provisional=true로 표시함
  07:30 KST (18:30 ET)  전일 정산(14:30 ET)이 끝난 뒤라 같은 날짜가 확정치로 덮임
매 실행마다 원본에서 계열 전체를 다시 만들기 때문에 확정 전환이 저절로 일어남.

소스는 두 곳을 순서대로 시도한다. 야후는 쿠키(A3)와 crumb 없이 부르면 데이터센터
IP뿐 아니라 가정용 IP에서도 429를 주는 경우가 있어(26.09.02 실측) 브라우저처럼
쿠키 → crumb → 차트 순서로 부른다. 야후가 막히면 스투크로 넘어간다.
"""
import csv
import http.cookiejar
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
AUTO = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

GAL_PER_BBL = 42.0
KEEP = 260
SETTLE_HOUR = 14.5          # NYMEX 에너지 선물 정산 14:30 ET
ET_OFFSET = -4 * 3600       # 정산 판정용. 서머타임 폭은 30분 판정에 영향 없음

SYMBOLS = {                 # 이름: (스투크, 야후)
    "ho": ("ho.f", "HO=F"),
    "rb": ("rb.f", "RB=F"),
    "cl": ("cl.f", "CL=F"),
}


def _open(url, opener=None, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/csv,application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    o = opener or urllib.request
    return o.urlopen(req, timeout=timeout) if opener is None else opener.open(req, timeout=timeout)


def from_stooq(sym):
    """스투크 일별 CSV. Date,Open,High,Low,Close,Volume"""
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    with _open(url) as r:
        text = r.read().decode("utf-8", "replace").strip()
    if not text or "Date" not in text.split("\n")[0]:
        raise RuntimeError("빈 응답 또는 헤더 없음")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        c = row.get("Close")
        if c in (None, "", "N/A"):
            continue
        try:
            out[row["Date"]] = float(c)
        except ValueError:
            continue
    if not out:
        raise RuntimeError("종가가 전부 결측")
    return out


def _yahoo_session():
    """브라우저와 같은 순서: fc.yahoo.com에서 A3 쿠키 → getcrumb → 차트."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        _open("https://fc.yahoo.com", opener=op, timeout=10).read()
    except Exception:
        pass                              # 404를 주지만 쿠키는 붙는다
    crumb = ""
    for host in ("query2", "query1"):
        try:
            with _open(f"https://{host}.finance.yahoo.com/v1/test/getcrumb", opener=op, timeout=10) as r:
                crumb = r.read().decode("utf-8", "replace").strip()
            if crumb and "<" not in crumb:
                break
            crumb = ""
        except Exception:
            crumb = ""
    return op, crumb


_YSESSION = None


def from_yahoo(sym):
    global _YSESSION
    if _YSESSION is None:
        _YSESSION = _yahoo_session()
    op, crumb = _YSESSION
    last = None
    for attempt in range(3):
        for host in ("query1", "query2"):
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                   f"{sym.replace('=', '%3D')}?range=2y&interval=1d"
                   + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""))
            try:
                with _open(url, opener=op) as r:
                    payload = json.load(r)
                res = (payload.get("chart") or {}).get("result") or []
                if not res:
                    raise RuntimeError("빈 응답")
                res = res[0]
                off = res.get("meta", {}).get("gmtoffset") or 0
                closes = res["indicators"]["quote"][0]["close"]
                out = {}
                for t, c in zip(res["timestamp"], closes):
                    if c is None:
                        continue
                    out[datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")] = float(c)
                if out:
                    return out
                raise RuntimeError("종가가 전부 결측")
            except Exception as e:
                last = e
                if "429" in str(e):        # 세션을 새로 열고 한참 쉰다
                    _YSESSION = _yahoo_session()
                    op, crumb = _YSESSION
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(str(last))


SOURCES = [("yahoo", from_yahoo, 1), ("stooq", from_stooq, 0)]


def load_all():
    """모든 종목을 한 소스에서 가져온다. 소스가 섞이면 계열이 어긋나므로."""
    notes = []
    for label, fn, idx in SOURCES:
        got, err = {}, None
        for name, syms in SYMBOLS.items():
            try:
                got[name] = fn(syms[idx])
            except Exception as e:
                err = f"{label}:{syms[idx]}: {e}"
                notes.append(err)
                break
        if len(got) == len(SYMBOLS):
            return got, label, notes
    raise RuntimeError(" | ".join(notes) or "모든 소스 실패")


def is_provisional(last_day):
    now = datetime.now(timezone.utc) + timedelta(seconds=ET_OFFSET)
    if last_day != now.strftime("%Y-%m-%d"):
        return False
    return now.hour + now.minute / 60 < SETTLE_HOUR


def write_series(key, label, points, provisional, source):
    points = sorted(points, key=lambda p: p["d"])[-KEEP:]
    (SERIES_DIR / f"{key}.json").write_text(json.dumps({
        "key": key, "label": label, "unit": "$/bbl", "demo": False,
        "provisional": provisional, "src": source,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "points": points,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return points[-1] if points else None


def merge_auto(new_ind, errors):
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
    ind, errors = {}, []
    try:
        px, src, notes = load_all()
        print(f"소스: {src}" + (f" (앞선 시도 실패: {'; '.join(notes)})" if notes else ""))
    except Exception as e:
        merge_auto({}, [f"futures: {e}"])
        print(f"실패: {e}")
        return 1

    days = sorted(set(px["ho"]) & set(px["cl"]))
    pts = [{"d": d, "v": round(px["ho"][d] * GAL_PER_BBL - px["cl"][d], 2)} for d in days]
    prov = is_provisional(pts[-1]["d"]) if pts else False
    last = write_series("diesel_crack_1_1", "디젤 1:1 크랙", pts, prov, src)
    if last:
        ind["diesel_crack_1_1"] = {
            "value": last["v"], "unit": "$/bbl", "asOf": last["d"],
            "note": "장중 잠정치" if prov else None,
            "source": f"NYMEX ULSD · WTI 선물 {'장중' if prov else '종가'} 기반 산출",
            "mode": "auto"}

    d3 = sorted(set(days) & set(px["rb"]))
    pts3 = []
    for d in d3:
        h, g, c = px["ho"][d] * GAL_PER_BBL, px["rb"][d] * GAL_PER_BBL, px["cl"][d]
        pts3.append({"d": d, "v": round((g * 2 + h - c * 3) / 3, 2)})
    prov3 = is_provisional(pts3[-1]["d"]) if pts3 else False
    last3 = write_series("crack_3_2_1", "3-2-1 크랙", pts3, prov3, src)
    if last3:
        ind["crack_3_2_1"] = {
            "value": last3["v"], "unit": "$/bbl", "asOf": last3["d"],
            "note": "장중 잠정치" if prov3 else None,
            "source": f"NYMEX 선물 {'장중' if prov3 else '종가'} 기반 산출", "mode": "auto"}

    merge_auto(ind, errors)
    print(json.dumps({"source": src, "indicators": ind}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
