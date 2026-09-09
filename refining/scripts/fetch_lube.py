#!/usr/bin/env python3
"""Group III 기유 가격과 스프레드. Lubes'n'Greases 주간 아시아 리포트에서 뽑는다.

  Group III 4cSt FOB Asia 평가 범위(예: $3,550/t-$3,600/t)의 중간값  → group3_price  ($/t)
  중간값 − Brent 근월물 종가($/bbl) × 7.33 bbl/t                      → group3_spread ($/t)

리포트는 주 1회(월·화)라 매일 돌려도 새 점은 주 1회 붙는다. 무료로 매일 나오는 Group III 가격은
없음(26.09.03 조사: 아거스·플랫츠·ICIS는 유료, 중국 일간 사이트는 I·II군만). 관세청 수출단가는
월간·2개월 지연이라 이 주간 계열을 코어로 쓴다.

수집 방법: 사이트맵(bor-asia-sitemap.xml)에서 새 글 URL을 찾고, 본문 HTML의 마지막 "Group III … 4 cSt …
$a/t-$b/t" 구절을 읽는다. 본문 텍스트는 서버 HTML에 그대로 들어 있음(브라우저에선 JS가 가림). 러너 IP는 클라우드플레어에
막혀서(26.09.09 실측, curl.exe도 403) r.jina.ai 리더 경유로 텍스트를 받는다. 리더는 무료·무키, 분당 20회.
이미 읽은 글은 series 파일의 seen 목록으로 건너뛰므로 평소엔 요청 1~2건, 첫 실행만 1년치(약 50건, 5초 간격).

Brent는 fetch_futures의 야후 경로를 그대로 씀. 스프레드는 리포트 날짜 이하 가장 가까운 종가로 계산.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_futures import from_yahoo, UA  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUTO = ROOT / "data" / "auto.json"
SERIES_DIR = ROOT / "data" / "series"

SITEMAP = "https://www.lubesngreases.com/bor-asia-sitemap.xml"
BBL_PER_T = 7.33          # 원유 톤당 배럴(업계 관행치). Brent $/bbl → $/t 환산
KEEP = 160                # 약 3년치 주간 점
BACKFILL = 52             # 첫 실행 때 읽을 글 수
DELAY = 5                 # robots.txt Crawl-delay: 10 → 첫 실행만 여러 건이라 5초로 완만하게


HEADERS = {   # 브라우저와 같은 헤더 묶음. 그래도 러너 IP는 클라우드플레어가 403을 주므로 get()이 리더로 넘어감
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1", "Connection": "keep-alive",
}


READER = "https://r.jina.ai/"   # 서버 쪽 헤드리스 브라우저로 렌더한 본문을 텍스트로 돌려주는 공개 리더


def _blocked(text):
    return "__CF$cv$params" in text or "<title>403 Forbidden</title>" in text or "Just a moment" in text


def get(url, timeout=30):
    """직접 요청 → 클라우드플레어에 막히면 리더 경유. 26.09.09 실측: 러너 IP는 curl.exe로도 403"""
    import gzip
    import urllib.error
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
        if not _blocked(text):
            return text
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429, 503):
            raise
    req = urllib.request.Request(READER + url, headers={"User-Agent": UA, "Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout + 30) as r:
        return r.read().decode("utf-8", "replace")


def sitemap():
    x = get(SITEMAP)
    items = re.findall(r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", x)
    if not items:                      # 리더 경유면 마크다운: [url](url) 줄 다음 줄에 lastmod
        items = re.findall(r"\[(https://[^\]\s]+)\]\([^)]*\)\s*(\d{4}-\d{2}-\d{2}T\S+)", x)
    if not items:                      # WAF 차단 페이지가 200으로 올 수 있음. 조용히 0건으로 끝내지 않는다
        head = re.sub(r"\s+", " ", x[:300])
        raise RuntimeError(f"사이트맵 항목 0건 (len={len(x)}): {head}")
    items.sort(key=lambda t: t[1], reverse=True)
    return items


def parse(html):
    """본문 마지막 'Group III' 구절에서 4·6·8 cSt FOB Asia 범위를 읽는다."""
    txt = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")
    txt = re.sub(r"\s+", " ", txt)
    gi = txt.rfind("Group III")
    if gi < 0:
        return None
    seg = txt[gi:gi + 400]

    def pair(label):
        j = seg.find(label)
        if j < 0:
            return None
        m = re.search(r"\$([\d,]+)/t-\$([\d,]+)/t", seg[j:])
        return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else None

    m = re.search(r'"datePublished":"([^"]+)"', html) or re.search(r'article:published_time" content="([^"]+)"', html)
    date = m.group(1)[:10] if m else None
    if not date:                        # 리더 경유: "Published Time: Mon, 07 Sep 2026 10:03:14 GMT"
        m = re.search(r"Published Time:\s*([^\n]+)", html)
        if m:
            from email.utils import parsedate_to_datetime
            try:
                date = parsedate_to_datetime(m.group(1).strip()).strftime("%Y-%m-%d")
            except Exception:
                date = None
    g4 = pair("4 cSt")
    if not (date and g4):
        return None
    return {"d": date, "lo": g4[0], "hi": g4[1], "g6": pair("6 cSt"), "g8": pair("8 cSt")}


def load_series(key):
    p = SERIES_DIR / f"{key}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"key": key, "points": []}


def save_series(key, label, unit, points, extra):
    points = sorted(points, key=lambda p: p["d"])[-KEEP:]
    d = {"key": key, "label": label, "unit": unit, "demo": False,
         "src": "Lubes'n'Greases Weekly Asia Base Oil Price Report · FOB Asia",
         "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "points": points}
    d.update(extra)
    (SERIES_DIR / f"{key}.json").write_text(json.dumps(d, ensure_ascii=False) + "\n", encoding="utf-8")
    return points[-1] if points else None


def merge_auto(new_ind, errors):
    try:
        cur = json.loads(AUTO.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.setdefault("_comment", "GitHub Actions가 덮어쓰는 파일. 손으로 고치지 말 것. manual.json의 같은 key를 덮어씀.")
    cur.setdefault("indicators", {})
    cur["indicators"].update(new_ind)
    prior = [e for e in (cur.get("errors") or []) if not e.startswith("lube:")]
    cur["errors"] = prior + errors
    cur["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if new_ind or not cur.get("status"):
        cur["status"] = "partial" if cur["errors"] else "ok"
    AUTO.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    price = load_series("group3_price")
    seen = set(price.get("seen") or [])
    raw = {p["d"]: p for p in price.get("points", []) if "lo" in p}   # d → {d, v, lo, hi}
    errors = []

    try:
        items = sitemap()
    except Exception as e:
        items = []                       # 사이트가 막혀도 기존 점으로 스프레드는 갱신한다
        errors.append(f"lube: sitemap: {e}")
    todo = [u for u, _ in items if u not in seen][: (BACKFILL if not seen else 8)]
    for i, u in enumerate(todo):
        try:
            html = get(u)
            r = parse(html)
            if r:
                raw[r["d"]] = {"d": r["d"], "v": round((r["lo"] + r["hi"]) / 2), "lo": r["lo"], "hi": r["hi"]}
                seen.add(u)
            elif "Group III" not in html:
                raise RuntimeError(f"본문 없음 (len={len(html)}): " + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[:400]))[:120])
            else:
                seen.add(u)                # 가격 구절이 없는 글(공지 등)은 건너뜀
        except Exception as e:
            errors.append(f"lube: {u.rsplit('/', 2)[-2]}: {e}")
        if i < len(todo) - 1:
            time.sleep(DELAY)
    print(f"리포트 {len(todo)}건 읽음, 점 {len(raw)}개")

    pts = sorted(raw.values(), key=lambda p: p["d"])
    last = save_series("group3_price", "Group III 4cSt FOB Asia", "$/t", pts,
                       {"seen": sorted(seen)[-400:], "note": "주간 평가 범위의 중간값. lo·hi는 범위"})
    ind = {}
    if last:
        ind["group3_price"] = {"value": last["v"], "unit": "$/t", "asOf": last["d"],
                               "note": f"4cSt FOB Asia 범위 {last['lo']:,}~{last['hi']:,}",
                               "source": "Lubes'n'Greases 주간 리포트", "mode": "auto"}

    # 스프레드: 리포트 날짜 이하 가장 최근 Brent 종가
    try:
        bz = from_yahoo("BZ=F")
        days = sorted(bz)
        sp = []
        for p in pts:
            prior = [d for d in days if d <= p["d"]]
            if not prior:
                continue
            crude_t = bz[prior[-1]] * BBL_PER_T
            sp.append({"d": p["d"], "v": round(p["v"] - crude_t), "brent": round(bz[prior[-1]], 2)})
        last_sp = save_series("group3_spread", "Group III 기유 스프레드", "$/t", sp,
                              {"delta": "abs", "note": "4cSt FOB Asia 중간값 − Brent 근월물 × 7.33 bbl/t"})
        if last_sp:
            ind["group3_spread"] = {"value": last_sp["v"], "unit": "$/t", "asOf": last_sp["d"],
                                    "note": f"Group III {last['v']:,} − Brent {last_sp['brent']} × 7.33",
                                    "source": "Lubes'n'Greases · NYMEX Brent 선물", "mode": "auto"}
    except Exception as e:
        errors.append(f"lube: brent: {e}")

    merge_auto(ind, errors)
    print(json.dumps({"indicators": ind, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if ind else 1


if __name__ == "__main__":
    sys.exit(main())
