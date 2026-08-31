#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CNSY (Cerenome, Inc. / 구 Plus Therapeutics) 추적기 · 수집 스크립트
표준 라이브러리만 사용. 부분 실패를 허용하고 성공한 소스만 갱신한다.

출력: data/cache.json
  meta      수집 시각 · 소스별 성공/실패
  quote     주가 · 시총
  shares    발행주식수 시계열 (SEC XBRL) — ATM 희석 추적의 1차 출처
  filings   SEC 제출물 최근분
  news      IR 보도자료 · SEC 피드
  trials    respect-trials.com 환자 등록 카운터
  ctgov     ClinicalTrials.gov 등록자수 · 상태
  cnside    cnside-dx.com 글
  reddit    레딧 신규 글 (미확인 신호)
  cms       CY2027 CLFS 가격결정 파일 등장 감지
  log       변화 로그 (최신이 위, 등급 부여)
"""

import json, os, re, sys, ssl, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache.json")
SEED = os.path.join(DATA, "seed.json")

CIK = "0001095981"          # Cerenome, Inc. (구 Plus Therapeutics) — 사명·티커 변경과 무관하게 불변
TICKER = "CNSY"
UA_SEC = "CNSY Tracker (personal research) yuixy82@gmail.com"
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

LOG_MAX = 300
STATUS = {}
_ERR = []


# ────────────────────────────── 공통 ──────────────────────────────

def get(url, ua=UA_WEB, timeout=30, retries=2, accept="*/*", plain=False):
    """plain=True면 위장 헤더 없이 최소 헤더로 보낸다. 일부 API는 이쪽을 더 잘 받는다."""
    last = None
    for i in range(retries + 1):
        try:
            h = {"User-Agent": ua, "Accept": accept, "Accept-Encoding": "identity"}
            if not plain:
                h.update({
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                })
            req = urllib.request.Request(url, headers=h)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def get_json(url, ua=UA_WEB, timeout=30, plain=False):
    return json.loads(get(url, ua=ua, timeout=timeout,
                          accept="application/json", plain=plain))


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                # noqa: BLE001
        return default


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def today_kst():
    return datetime.now(KST).strftime("%Y-%m-%d")


_SEED = None


def section(name, fn, prev):
    """소스 하나를 수집한다. 실패하면 직전 값을, 직전 값도 없으면 seed 값을 물려준다."""
    global _SEED
    try:
        out = fn()
        STATUS[name] = "ok"
        return out
    except Exception as e:                           # noqa: BLE001
        STATUS[name] = "fail: %s" % str(e)[:140]
        print("  [fail] %s : %s" % (name, e), file=sys.stderr)
        if prev:
            return prev
        if _SEED is None:
            _SEED = load(SEED, {})
        return _SEED.get(name)


def strip_tags(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#039;", "'").replace("&#8217;", "'")
          .replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


def parse_rss(xml, limit=25):
    items = []
    for blk in re.findall(r"<item[ >].*?</item>|<entry[ >].*?</entry>", xml, flags=re.S)[:limit]:
        def pick(tag):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), blk, flags=re.S)
            return strip_tags(m.group(1)) if m else ""
        link = pick("link")
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', blk)
            link = m.group(1) if m else ""
        items.append({
            "title": pick("title"),
            "link": link,
            "date": (pick("pubDate") or pick("updated") or pick("published"))[:31],
        })
    return items


# ────────────────────────────── 소스별 수집 ──────────────────────────────

def f_filings():
    j = get_json("https://data.sec.gov/submissions/CIK%s.json" % CIK, ua=UA_SEC)
    r = j["filings"]["recent"]
    out = []
    for i in range(min(40, len(r["accessionNumber"]))):
        acc = r["accessionNumber"][i].replace("-", "")
        out.append({
            "form": r["form"][i],
            "filed": r["filingDate"][i],
            "items": (r.get("items") or [])[i] if i < len(r.get("items") or []) else "",
            "acc": r["accessionNumber"][i],
            "url": "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (
                int(CIK), acc, r["primaryDocument"][i]),
        })
    return {"entity": j.get("name", ""), "list": out}


def f_shares():
    j = get_json(
        "https://data.sec.gov/api/xbrl/companyconcept/CIK%s/dei/"
        "EntityCommonStockSharesOutstanding.json" % CIK, ua=UA_SEC)
    pts = {}
    for u in j.get("units", {}).get("shares", []):
        d = u.get("end") or u.get("filed")
        if d:
            pts[d] = int(u["val"])
    series = [{"date": d, "shares": pts[d]} for d in sorted(pts)]
    return {"series": series[-40:], "latest": series[-1] if series else None}


def f_quote():
    j = get_json("https://query1.finance.yahoo.com/v8/finance/chart/%s"
                 "?range=6mo&interval=1d" % TICKER)
    m = j["chart"]["result"][0]["meta"]
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    cl = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    hist = [{"date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
             "close": round(c, 3)}
            for t, c in zip(ts, cl) if c is not None]
    return {
        "price": m.get("regularMarketPrice"),
        "prev": m.get("chartPreviousClose") or m.get("previousClose"),
        "high52": m.get("fiftyTwoWeekHigh"),
        "low52": m.get("fiftyTwoWeekLow"),
        "hist": hist[-130:],
    }


def _dig(html):
    """Divi 숫자 카운터에서 제목과 값을 뽑는다. 숏코드형과 렌더형 둘 다 지원."""
    nums = {}
    # ① WP API가 돌려주는 숏코드형
    for m in re.finditer(r'et_pb_number_counter([^\]]*)\]', html):
        blk = m.group(1)
        t = re.search(r'title="([^"]*)"', blk)
        n = re.search(r'number="(\d+)"', blk)
        if t and n:
            nums[t.group(1).strip()] = int(n.group(1))
    if nums:
        return nums
    # ② 렌더된 HTML형: class="... et_pb_number_counter ..." data-number-value="37"
    for m in re.finditer(r'et_pb_number_counter[^>]*data-number-value="(\d+)"', html):
        val = int(m.group(1))
        tail = html[m.end():m.end() + 800]
        t = re.search(r'<span[^>]*id="[^"]*"[^>]*>([^<]{3,80})</span>', tail)
        key = t.group(1).strip() if t else "Patients Treated to Date"
        nums[key] = val
    return nums


def _modified(html):
    m = re.search(r'property="article:modified_time"\s+content="([^"]+)"', html)
    if not m:
        m = re.search(r'"dateModified"\s*:\s*"([^"]+)"', html)
    return m.group(1)[:19] if m else ""


def _counters(host, slug):
    """워드프레스 Divi number_counter 위젯에서 숫자를 뽑는다. API가 막히면 페이지 원문으로 폴백."""
    try:
        j = get_json("%s/wp-json/wp/v2/pages?slug=%s&_fields=slug,modified,content" % (host, slug))
        if j:
            html = j[0].get("content", {}).get("rendered", "") or ""
            nums = _dig(html)
            if nums:
                return {"modified": j[0].get("modified", ""), "counters": nums}
    except Exception as e:                               # noqa: BLE001
        _ERR.append("wp %s: %s" % (slug, e))
    html = get("%s/%s/" % (host, slug))
    return {"modified": _modified(html), "counters": _dig(html)}




def f_trials():
    del _ERR[:]
    out = {}
    for host in ("https://www.respect-trials.com", "https://respect-trials.com"):
        for slug in ("lm", "gbm", "pbc", "pediatric-brain-cancer"):
            if slug in out:
                continue
            try:
                r = _counters(host, slug)
                if r and r["counters"]:
                    out[slug] = r
            except Exception as e:                   # noqa: BLE001
                _ERR.append("%s: %s" % (slug, e))
        if out:
            break
    if not out:
        raise RuntimeError("카운터 없음 · " + " | ".join(_ERR[:4]))
    return out


def f_ctgov():
    del _ERR[:]
    out, seen = [], set()
    for spon in ("Cerenome", "Plus Therapeutics"):
        url = ("https://clinicaltrials.gov/api/v2/studies"
               "?query.spons=%s&pageSize=40&countTotal=true"
               "&fields=NCTId,BriefTitle,OverallStatus,LastUpdatePostDate,"
               "EnrollmentCount,EnrollmentType,PrimaryCompletionDate"
               % urllib.parse.quote(spon))
        j = None
        for plain in (True, False):
            try:
                j = get_json(url, ua="python-urllib/3 CNSY Tracker", plain=plain) if plain \
                    else get_json(url)
                break
            except Exception as e:                   # noqa: BLE001
                _ERR.append("%s(plain=%s): %s" % (spon, plain, e))
        if j is None:
            continue
        for s in j.get("studies", []):
            p = s.get("protocolSection", {})
            nct = p.get("identificationModule", {}).get("nctId", "")
            if not nct or nct in seen:
                continue
            seen.add(nct)
            st = p.get("statusModule", {})
            en = p.get("designModule", {}).get("enrollmentInfo", {})
            out.append({
                "nct": nct,
                "title": p.get("identificationModule", {}).get("briefTitle", ""),
                "status": st.get("overallStatus", ""),
                "updated": st.get("lastUpdatePostDateStruct", {}).get("date", ""),
                "enroll": en.get("count"),
                "enrollType": en.get("type", ""),
                "primaryCompletion": st.get("primaryCompletionDateStruct", {}).get("date", ""),
            })
    if not out:
        raise RuntimeError("임상 없음 · " + " | ".join(_ERR[:3]))
    return sorted(out, key=lambda x: x["nct"])


def f_news():
    out = []
    for u in ("https://ir.cerenome.com/rss/news-releases.xml",
              "https://ir.cerenome.com/rss/sec-filings.xml"):
        try:
            out += parse_rss(get(u), limit=20)
        except Exception:                            # noqa: BLE001
            continue
    if not out:
        raise RuntimeError("no feed")
    ded, seen = [], set()
    for it in out:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        ded.append(it)
    return ded[:30]


def f_cnside():
    j = get_json("https://cnside-dx.com/wp-json/wp/v2/posts"
                 "?per_page=15&_fields=id,date,modified,title,link")
    return [{"title": strip_tags(p["title"]["rendered"]),
             "link": p["link"], "date": p["date"][:10],
             "modified": p["modified"][:10]} for p in j]


def f_reddit():
    out = []
    urls = [
        "https://www.reddit.com/r/Plus_Therapeutics/new.json?limit=20",
        "https://www.reddit.com/search.json?q=CNSY+OR+Cerenome&sort=new&limit=20&t=month",
    ]
    for u in urls:
        try:
            j = get_json(u)
        except Exception:                            # noqa: BLE001
            continue
        for c in j.get("data", {}).get("children", []):
            d = c.get("data", {})
            out.append({
                "title": d.get("title", "")[:180],
                "sub": d.get("subreddit", ""),
                "author": d.get("author", ""),
                "score": d.get("score", 0),
                "link": "https://www.reddit.com" + d.get("permalink", ""),
                "date": datetime.fromtimestamp(
                    d.get("created_utc", 0), KST).strftime("%Y-%m-%d"),
            })
    ded, seen = [], set()
    for it in sorted(out, key=lambda x: x["date"], reverse=True):
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        ded.append(it)
    return ded[:20]


def f_coverage(news):
    """보도자료 제목에서 커버드 라이프 수치를 뽑아 시계열로 만든다."""
    pat = re.compile(r"([\d]{1,3}(?:\.\d+)?)\s*million\s+(?:contracted\s+)?(?:covered\s+)?lives", re.I)
    out = []
    for n in news or []:
        m = pat.search(n.get("title", ""))
        if not m:
            continue
        out.append({"date": n.get("date", "")[:16], "lives_m": float(m.group(1)),
                    "title": n.get("title", ""), "link": n.get("link", "")})
    return out


def f_cms():
    """CY2027 CLFS 신규코드 가격결정 파일이 올라오는 순간을 잡는다."""
    html = get("https://www.cms.gov/medicare/payment/fee-schedules/"
               "clinical-laboratory-fee-schedule-clfs/annual-public-meetings")
    files = sorted(set(re.findall(r'href="([^"]*cy-?20 ?27[^"]*\.zip)"', html, flags=re.I)))
    files = [("https://www.cms.gov" + f) if f.startswith("/") else f for f in files]
    return {"cy2027_files": files, "checked": today_kst()}


# ────────────────────────────── 변화 판정 ──────────────────────────────

FORM_RED = ("S-1", "S-3", "424B", "S-1MEF")
NEWS_RED = ("topline", "top-line", "overall survival", "median os", "phase 3",
            "pivotal", "offering", "pricing of", "reverse split", "nasdaq",
            "delisting", "going concern")
NEWS_ORANGE = ("coverage", "medicare", "reimbursement", "payer", "cms", "fda",
               "end-of-phase", "enrollment", "data", "assay", "launch",
               "revenue", "partnership", "designation")


def grade_form(form):
    f = form.upper()
    if any(f.startswith(x) for x in FORM_RED):
        return "red", "자본조달"
    if f.startswith("8-K"):
        return "orange", "공시"
    if f.startswith(("SC 13", "13D", "13G")):
        return "orange", "지분"
    if f in ("4", "3", "5"):
        return "yellow", "내부자"
    return "yellow", "공시"


def grade_news(title):
    t = title.lower()
    if any(k in t for k in NEWS_RED):
        return "red"
    if any(k in t for k in NEWS_ORANGE):
        return "orange"
    return "yellow"


def diff(prev, cur):
    """직전 스냅샷과 비교해 변화만 뽑는다."""
    ev = []
    d = today_kst()

    def add(level, axis, text, link=""):
        ev.append({"date": d, "level": level, "axis": axis, "text": text, "link": link})

    # 1. SEC 제출물
    old = {f["acc"] for f in (prev.get("filings") or {}).get("list", [])}
    for f in (cur.get("filings") or {}).get("list", []):
        if old and f["acc"] not in old:
            lv, kind = grade_form(f["form"])
            it = (" · Item " + f["items"]) if f.get("items") else ""
            add(lv, "자본", "SEC %s 제출 (%s)%s" % (f["form"], f["filed"], it), f["url"])

    # 2. 발행주식수 — ATM 희석
    po = ((prev.get("shares") or {}).get("latest") or {})
    pn = ((cur.get("shares") or {}).get("latest") or {})
    if po.get("shares") and pn.get("shares") and pn["shares"] != po["shares"]:
        delta = pn["shares"] - po["shares"]
        pct = delta / po["shares"] * 100
        lv = "red" if abs(pct) >= 5 else ("orange" if abs(pct) >= 2 else "yellow")
        add(lv, "희석", "발행주식수 %s → %s (%+.2f%%)" % (
            format(po["shares"], ","), format(pn["shares"], ","), pct))

    # 3. 임상 환자 등록 카운터
    for slug, v in (cur.get("trials") or {}).items():
        pv = (prev.get("trials") or {}).get(slug, {}).get("counters", {})
        for k, n in v.get("counters", {}).items():
            if k in pv and pv[k] != n:
                add("orange", "임상", "%s %s: %d → %d명" % (slug.upper(), k, pv[k], n),
                    "https://www.respect-trials.com/%s/" % slug)

    # 4. ClinicalTrials.gov
    pm = {s["nct"]: s for s in (prev.get("ctgov") or [])}
    for s in (cur.get("ctgov") or []):
        o = pm.get(s["nct"])
        if not o:
            continue
        if o.get("status") != s.get("status"):
            add("orange", "임상", "%s 상태 %s → %s" % (s["nct"], o["status"], s["status"]),
                "https://clinicaltrials.gov/study/" + s["nct"])
        if o.get("enroll") != s.get("enroll"):
            add("orange", "임상", "%s 등록 %s → %s명" % (s["nct"], o.get("enroll"), s.get("enroll")),
                "https://clinicaltrials.gov/study/" + s["nct"])

    # 5. IR 보도자료
    oldn = {n["link"] for n in (prev.get("news") or [])}
    for n in (cur.get("news") or []):
        if oldn and n["link"] not in oldn:
            add(grade_news(n["title"]), "뉴스", n["title"], n["link"])

    # 6. CNSide 사이트
    oldc = {c["link"] for c in (prev.get("cnside") or [])}
    for c in (cur.get("cnside") or []):
        if oldc and c["link"] not in oldc:
            add("yellow", "진단", "CNSide: " + c["title"], c["link"])

    # 7. CMS CY2027 가격결정 — 0640U 단가가 처음 숫자로 나오는 자리
    of = set((prev.get("cms") or {}).get("cy2027_files", []))
    for f in (cur.get("cms") or {}).get("cy2027_files", []):
        if f not in of:
            add("red", "수가", "CMS CY2027 CLFS 가격결정 파일 게시 — 0640U 단가 확인", f)

    # 8. 레딧 — 언제나 미확인
    oldr = {r["link"] for r in (prev.get("reddit") or [])}
    for r in (cur.get("reddit") or []):
        if oldr and r["link"] not in oldr and r["score"] >= 5:
            add("yellow", "미확인", "[r/%s] %s" % (r["sub"], r["title"]), r["link"])

    return ev


# ────────────────────────────── 실행 ──────────────────────────────

def main():
    prev = load(CACHE, {})
    cur = {}

    print("collecting ...")
    cur["filings"] = section("filings", f_filings, prev.get("filings"))
    cur["shares"] = section("shares", f_shares, prev.get("shares"))
    cur["quote"] = section("quote", f_quote, prev.get("quote"))
    cur["trials"] = section("trials", f_trials, prev.get("trials"))
    cur["ctgov"] = section("ctgov", f_ctgov, prev.get("ctgov"))
    cur["news"] = section("news", f_news, prev.get("news"))
    cur["cnside"] = section("cnside", f_cnside, prev.get("cnside"))
    cur["reddit"] = section("reddit", f_reddit, prev.get("reddit"))
    cur["cms"] = section("cms", f_cms, prev.get("cms"))

    # 임상 카운터 시계열 누적: 하루 한 점, 값이 바뀌면 갱신
    hist = list(prev.get("trials_hist") or (load(SEED, {}).get("trials_hist") or []))
    t = cur.get("trials") or {}

    def cnt(k):
        v = (t.get(k) or {}).get("counters") or {}
        for key in v:
            if "Patients Treated" in key:
                return v[key]
        return None

    lm, gbm = cnt("lm"), cnt("gbm")
    if lm is not None or gbm is not None:
        d = today_kst()
        hist = [h for h in hist if h.get("date") != d]
        last = hist[-1] if hist else {}
        if last.get("lm") != lm or last.get("gbm") != gbm or not hist:
            hist.append({"date": d, "lm": lm, "gbm": gbm})
        else:
            hist[-1] = {"date": d, "lm": lm, "gbm": gbm}
    cur["trials_hist"] = sorted(hist, key=lambda h: h["date"])[-200:]

    cur["coverage"] = section("coverage", lambda: f_coverage(cur.get("news")),
                              prev.get("coverage"))

    new_events = diff(prev, cur)
    base = prev.get("log") or (load(SEED, {}).get("log") or [])   # 첫 실행이면 수기 기록에서 이어붙임
    seen, log = set(), []
    for e in new_events + base:
        k = (e.get("date"), e.get("text"))
        if k in seen:
            continue
        seen.add(k)
        log.append(e)
    cur["log"] = log[:LOG_MAX]

    cur["meta"] = {
        "updated": now_kst(),
        "ticker": TICKER,
        "cik": CIK,
        "status": STATUS,
        "new_events": len(new_events),
    }

    os.makedirs(DATA, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)

    print("done. new events: %d" % len(new_events))
    for e in new_events:
        print("  [%s] %s — %s" % (e["level"], e["axis"], e["text"]))
    fails = [k for k, v in STATUS.items() if v != "ok"]
    if fails:
        print("failed sources: %s" % ", ".join(fails), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
