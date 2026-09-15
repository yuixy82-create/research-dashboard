#!/usr/bin/env python3
"""NYMEX 선물 종가로 디젤 1:1 크랙과 3-2-1 크랙을 매 영업일 산출한다.

  디젤 1:1 크랙 = ULSD($/gal) x 42 - WTI($/bbl)
  3-2-1 크랙    = (RBOB x 42 x 2 + ULSD x 42 - WTI x 3) / 3

★ 세 다리는 반드시 같은 인도월 계약을 쓴다 (26.09.15 수정).
야후의 연결 심볼(HO=F 등)은 종목마다 롤오버 날짜가 달라서, 26.09.14에 난방유·휘발유만
11월물로 넘어가고 WTI는 10월물에 남는 바람에 크랙이 하루 만에 8달러 깎인 것처럼 찍혔다.
같은 착시가 26.09.01에도 있었다: RBOB 9월물(여름 규격) → 10월물(겨울 규격) 전환으로
3-2-1이 73 → 63으로 떨어진 듯 보였지만, 10월물끼리 다시 계산하면 62 → 63으로 오히려 올랐다.
그래서 이제 `CLV26.NYM` 처럼 인도월을 명시한 심볼만 쓴다.

인도월 결정: WTI 만기가 전월 20일경으로 셋 중 가장 이르므로 그 기준에 맞춘다.
  날짜의 일자 <= 19 → 다음 달물, 그 이후 → 다다음 달물
롤오버 단차는 남지만(겨울 규격 전환 같은 건 실제 시장 현상임) 세 다리가 같은 날 함께
넘어가므로 인위적인 단차는 없어진다. 각 점에 쓰인 월물 코드를 `m` 필드로 같이 남긴다.

하루 두 번 도는 것을 전제로 함.
  22:00 KST (09:00 ET)  장중이라 마지막 점이 잠정치. provisional=true로 표시함
  07:30 KST (18:30 ET)  전일 정산(14:30 ET)이 끝난 뒤라 같은 날짜가 확정치로 덮임
최근 LOOKBACK_DAYS 구간만 매번 다시 만들어 기존 계열에 덮으므로 확정 전환이 저절로 일어난다.

야후는 쿠키(A3)와 crumb 없이 부르면 가정용 IP에서도 429를 주는 경우가 있어(26.09.02 실측)
브라우저처럼 쿠키 → crumb → 차트 순서로 부른다. 스투크는 월물별 시세가 없어 폐기함.
"""
import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
AUTO = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

GAL_PER_BBL = 42.0
KEEP = 260
LOOKBACK_DAYS = 75          # 매 실행마다 다시 만드는 구간. 월물 3~4개면 덮임
ROLL_DAY = 19               # 이 날짜를 넘기면 한 달 더 뒤 월물로 넘어감 (WTI 만기 22일경)
SETTLE_HOUR = 14.5          # NYMEX 에너지 선물 정산 14:30 ET
ET_OFFSET = -4 * 3600       # 정산 판정용. 서머타임 폭은 30분 판정에 영향 없음
MONTH_CODE = "FGHJKMNQUVXZ"  # 1월~12월 NYMEX 월물 코드
ROOTS = ("cl", "ho", "rb")


def _open(url, opener=None, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    o = opener or urllib.request
    return o.urlopen(req, timeout=timeout) if opener is None else opener.open(req, timeout=timeout)


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


def from_yahoo(sym, rng="6mo"):
    global _YSESSION
    if _YSESSION is None:
        _YSESSION = _yahoo_session()
    op, crumb = _YSESSION
    last = None
    for attempt in range(3):
        throttled = False
        for host in ("query1", "query2"):
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(sym)}?range={rng}&interval=1d"
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
                    throttled = True
                    _YSESSION = _yahoo_session()
                    op, crumb = _YSESSION
        if not throttled:
            break                          # 만기가 지나 상장폐지된 월물은 바로 포기 (기다릴 이유 없음)
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(str(last))


def front_month(d):
    """그 날짜에 쓸 인도월 (년, 월). WTI 만기 기준으로 세 다리가 같이 넘어간다."""
    y, m = d.year, d.month + (1 if d.day <= ROLL_DAY else 2)
    while m > 12:
        m -= 12
        y += 1
    return y, m


def code(y, m):
    return f"{MONTH_CODE[m - 1]}{y % 100:02d}"


def symbol(root, y, m):
    return f"{root.upper()}{code(y, m)}.NYM"


def load_window(days_back=LOOKBACK_DAYS):
    """구간에 필요한 인도월들을 월물별 심볼로 받아온다. {(root,(y,m)): {날짜: 종가}}"""
    today = date.today()
    months, seen = [], set()
    for k in range(days_back, -1, -1):
        ym = front_month(today - timedelta(days=k))
        if ym not in seen:
            seen.add(ym)
            months.append(ym)
    px, errs = {}, []
    for (y, m) in months:
        for root in ROOTS:
            sym = symbol(root, y, m)
            try:
                px[(root, (y, m))] = from_yahoo(sym)
            except Exception as e:
                errs.append(f"{sym}: {e}")
            time.sleep(1)
    return px, months, errs


def build(px, days_back=LOOKBACK_DAYS):
    """날짜마다 그 날의 인도월 하나로 세 다리를 맞춰 크랙을 계산한다."""
    today = date.today()
    diesel, c321 = [], []
    for k in range(days_back, -1, -1):
        d = today - timedelta(days=k)
        ym = front_month(d)
        key = d.strftime("%Y-%m-%d")
        cl = (px.get(("cl", ym)) or {}).get(key)
        ho = (px.get(("ho", ym)) or {}).get(key)
        rb = (px.get(("rb", ym)) or {}).get(key)
        if cl is None or ho is None:
            continue
        diesel.append({"d": key, "v": round(ho * GAL_PER_BBL - cl, 2), "m": code(*ym)})
        if rb is not None:
            c321.append({"d": key,
                         "v": round((rb * GAL_PER_BBL * 2 + ho * GAL_PER_BBL - cl * 3) / 3, 2),
                         "m": code(*ym)})
    return diesel, c321


def is_provisional(last_day):
    now = datetime.now(timezone.utc) + timedelta(seconds=ET_OFFSET)
    if last_day != now.strftime("%Y-%m-%d"):
        return False
    return now.hour + now.minute / 60 < SETTLE_HOUR


def merge_series(key, label, fresh, provisional):
    """기존 계열에 최근 구간만 덮어쓴다. 옛 점은 그대로 둔다."""
    path = SERIES_DIR / f"{key}.json"
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    pts = {p["d"]: p for p in (cur.get("points") or [])}
    pts.update({p["d"]: p for p in fresh})
    points = sorted(pts.values(), key=lambda p: p["d"])[-KEEP:]
    path.write_text(json.dumps({
        "key": key, "label": label, "unit": "$/bbl", "demo": False,
        "provisional": provisional, "src": "yahoo",
        "note": "세 다리 모두 같은 인도월 계약. m은 쓰인 월물 코드. 26.08.20 이전 점은 옛 연결 방식이라 원유 만기 직후 약 8영업일 구간이 실제보다 높게 찍혀 있음",
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
        px, months, errs = load_window()
    except Exception as e:
        merge_auto({}, [f"futures: {e}"])
        print(f"실패: {e}")
        return 1
    print("월물:", " ".join(code(*ym) for ym in months))

    diesel, c321 = build(px)
    if not diesel:
        merge_auto({}, [f"futures: 계산된 점 없음 ({'; '.join(errs[:3]) or '원인 미상'})"])
        print("실패: 점 없음", errs[:3])
        return 1
    # 구간 안에 만기 지난 월물이 섞여 일부 심볼이 비는 건 정상이므로, 점이 나왔으면 에러로 올리지 않는다

    prov = is_provisional(diesel[-1]["d"])
    last = merge_series("diesel_crack_1_1", "디젤 1:1 크랙", diesel, prov)
    if last:
        ind["diesel_crack_1_1"] = {
            "value": last["v"], "unit": "$/bbl", "asOf": last["d"],
            "note": "장중 잠정치" if prov else None,
            "source": f"NYMEX ULSD · WTI {last.get('m', '')}물 {'장중' if prov else '종가'} 기반 산출",
            "mode": "auto"}

    prov3 = is_provisional(c321[-1]["d"]) if c321 else False
    last3 = merge_series("crack_3_2_1", "3-2-1 크랙", c321, prov3)
    if last3:
        ind["crack_3_2_1"] = {
            "value": last3["v"], "unit": "$/bbl", "asOf": last3["d"],
            "note": "장중 잠정치" if prov3 else None,
            "source": f"NYMEX {last3.get('m', '')}물 {'장중' if prov3 else '종가'} 기반 산출",
            "mode": "auto"}

    merge_auto(ind, errors)
    print(json.dumps({"indicators": ind, "누락 심볼": errs[:5]}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
