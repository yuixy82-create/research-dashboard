#!/usr/bin/env python3
"""MP 머티리얼즈 트래커: 수집 + index.html 생성.

  manual.json  손으로 고치는 값 (분기 실적·추정치·타임라인). 봇은 건드리지 않음
  cache.json   자동 수집 누적 (NdPr 시세·환율·주가·내부자 거래)
  index.html   이 스크립트가 매번 새로 씀

자동 수집 4종
  ndpr   SMM(metal.com) Pr-Nd 산화물 일간 평균가, 위안/톤(부가세 13% 포함, 중국 도착도)
  fx     달러/위안 (frankfurter → 야후 CNY=X)
  px     MP 종가 (야후 → 나스닥)
  trades SEC EDGAR Form 4 (내부자 매수 P·매도 S). SEC가 막으면 오픈인사이더로 대신 받음
  dy_usd·tb_usd  SMM 달러 호가(부가세 제외), 최신 한 점씩
  earn   다음 분기 실적 보도자료가 올라왔는지 확인 → 화면 맨 위 알림

분기 실적·미중 타임라인·케파 표·추정치(컨센·바바리안)는 manual.json을 손으로 고침.
컨센 출처 마켓스크리너는 깃허브 러너를 막아 자동 수집 불가(26.11.18 실측).
수집 실패, 며칠째 갱신 안 됨, 새 실적 발표, 날짜 지난 일정은 화면 맨 위 빨간 줄로 표시됨

실행:  python mp-materials/tracker.py            수집 후 빌드
       python mp-materials/tracker.py --no-fetch  빌드만
"""
import gzip
import html as H
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "index.html")
CACHE = os.path.join(ROOT, "cache.json")
MANUAL = os.path.join(ROOT, "manual.json")
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
# SEC는 연락처를 밝히지 않은 자동 요청을 403으로 막음. 저장소 시크릿 SEC_UA("이름 이메일" 형식)가 있으면 그것을 씀
SEC_UA = os.environ.get("SEC_UA") or "research-dashboard mp-tracker (github.com/yuixy82-create/research-dashboard)"
CIK = "1801368"
SMM_URL = "https://www.metal.com/rare-earth-oxides/201102250162"
READER = "https://r.jina.ai/"
MON = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def today_kst():
    return datetime.now(KST).date()


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, sort_keys=True)


# ============================================================
# 1. 수집
# ============================================================
def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def get_or_reader(url, timeout=30):
    """직접 요청 → 막히면 r.jina.ai 리더 경유 (클라우드 러너 IP 차단 대비)."""
    errs = []
    try:
        t = http_get(url, {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8",
                           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}, timeout)
        if "Just a moment" not in t and "403 Forbidden" not in t[:2000]:
            return t
        errs.append("direct: blocked")
    except Exception as e:
        errs.append("direct: %s" % e)
    for hdr in ({"Accept": "text/plain"}, {"User-Agent": "curl/8.4.0", "Accept": "*/*"}):
        try:
            return http_get(READER + url, hdr, timeout + 30)
        except Exception as e:
            errs.append("reader: %s" % e)
    raise RuntimeError("; ".join(errs))


def _hist_from(t):
    """SMM 품목 페이지 서버 HTML에 박힌 initialProductHistory 블록을 읽음."""
    i = t.find("initialProductHistory")
    if i < 0:
        raise RuntimeError("가격 블록 없음")
    s_ = t[i:i + 80000].replace('\\"', '"')
    st = s_.find("{")
    depth = 0
    for k in range(st, len(s_)):
        if s_[k] == "{":
            depth += 1
        elif s_[k] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s_[st:k + 1])
    raise RuntimeError("가격 블록 파싱 실패")


def collect_smm(cache):
    """SMM Pr-Nd 산화물 평균가(위안/톤). 요약 문장과 가격 블록 두 군데서 읽어 서로 대조함.
    요약 문장: '... the average price of Pr-Nd oxide was 718,000 yuan/mt'
    가격 블록: 최신 행의 달러 호가(부가세 제외) × (1 + 부가세율) × 환율 → 500위안 단위 반올림"""
    t = get_or_reader(SMM_URL)
    cands = []
    i = t.find("collection_summary")
    seg = t[i:i + 900] if i >= 0 else t
    j = seg.find("initialProductHistory")
    if j > 0:
        seg = seg[:j]
    # 반드시 Pr-Nd oxide를 가리키는 문장만 (같은 페이지의 합금·다른 품목 문장 배제)
    m = (re.search(r"average price of Pr-Nd oxide[^\d]{0,20}([\d,]{6,10})\s*yuan/mt", seg)
         or re.search(r"Pr-Nd oxide[^.]{0,120}?average price[^\d]{0,30}([\d,]{6,10})\s*yuan/mt", seg))
    if m:
        v = int(m.group(1).replace(",", ""))
        d = None
        m2 = re.search(r'renew_date\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})', t)
        if m2:
            d = m2.group(1)
        else:
            m3 = re.search(r"On ([A-Z][a-z]+) (\d{1,2}), (\d{4})", seg[max(0, m.start() - 200):m.start()])
            if m3 and m3.group(1) in MON:
                d = "%s-%02d-%02d" % (m3.group(3), MON[m3.group(1)], int(m3.group(2)))
        if d and 200_000 <= v <= 2_500_000:
            cands.append((d, 1, v, "요약 문장"))
    try:
        o = _hist_from(t)
        row = o["prices"][0]
        rate = float(o.get("exchange_rate") or 0)
        if o.get("unit") == "USD/tonne" and rate:
            v = round(float(row["average"]) * (1 + float(row.get("vat_rate") or 0.13)) * rate / 500) * 500
            if 200_000 <= v <= 2_500_000:
                cands.append((row["renew_date"], 0, v, "가격 블록"))
    except Exception:
        pass
    if not cands:
        raise RuntimeError("평균가를 못 읽음 (페이지 구조 변경 가능)")
    d, _, v, src = max(cands)                 # 날짜가 늦은 쪽, 같으면 요약 문장
    note = ""
    other = [c for c in cands if c[3] != src and c[0] == d]
    if other and abs(other[0][2] - v) / v > 0.015:
        note = " (두 값 불일치: %s %s)" % (other[0][3], format(other[0][2], ","))
    cache["ndpr"][d] = v
    return "%s %s 위안/톤, %s%s%s" % (d, format(v, ","), src, ", 대조 일치" if other and not note else "", note)


SMM_AUX = {"dy_usd": ("201102250247", 50, 1500), "tb_usd": ("201102250488", 200, 5000)}


def smm_hist(pid):
    return _hist_from(get_or_reader("https://www.metal.com/rare-earth-oxides/%s" % pid))


def collect_smm_aux(cache):
    """Dy·Tb 산화물의 달러 호가(부가세 제외).
    로그인 없이 받는 과거 행은 값이 맞지 않아(26.11.18 확인) 최신 행 하나만 쌓음."""
    got, errs = [], []
    for key, (pid, lo, hi) in SMM_AUX.items():
        try:
            o = smm_hist(pid)
            if o.get("unit") != "USD/kg":
                raise RuntimeError("단위가 USD/kg이 아님: %s" % o.get("unit"))
            row = o["prices"][0]
            v = float(row["average"])
            if not lo <= v <= hi:
                raise RuntimeError("값 범위 이상: %s" % v)
            cache.setdefault(key, {})[row["renew_date"]] = round(v, 2)
            got.append("%s %s $%.1f" % (key, row["renew_date"][5:], v))
        except Exception as e:
            errs.append("%s: %s" % (key, str(e)[:80]))
    if not got:
        raise RuntimeError("; ".join(errs))
    return ", ".join(got) + ((" / 실패 " + "; ".join(errs)) if errs else "")


def collect_fx(cache):
    try:
        j = json.loads(http_get("https://api.frankfurter.app/2026-10-01..?from=USD&to=CNY"))
        for d, r in j["rates"].items():
            cache["fx"][d] = round(r["CNY"], 4)
        return "frankfurter %d일" % len(j["rates"])
    except Exception as e1:
        j = json.loads(http_get("https://query1.finance.yahoo.com/v8/finance/chart/CNY%3DX?range=1mo&interval=1d"))
        res = j["chart"]["result"][0]
        n = 0
        for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c:
                cache["fx"][datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")] = round(c, 4)
                n += 1
        return "야후 %d일 (frankfurter 실패: %s)" % (n, e1)


def _yahoo_px(text):
    res = json.loads(text)["chart"]["result"][0]
    out = {}
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            d = (datetime.fromtimestamp(ts, timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")
            out[d] = round(c, 2)
    meta = res.get("meta") or {}
    if meta.get("regularMarketPrice") and meta.get("regularMarketTime"):
        d = (datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")
        out.setdefault(d, round(meta["regularMarketPrice"], 2))
    return out


def collect_px(cache):
    errs = []
    yurl = "https://query1.finance.yahoo.com/v8/finance/chart/MP?range=3mo&interval=1d"
    for name, fn in (
        ("야후", lambda: _yahoo_px(http_get(yurl))),
        ("나스닥", _nasdaq_px),
        ("야후(리더)", lambda: _yahoo_px(re.search(r"\{\"chart\".*\}", http_get(READER + yurl, {"Accept": "text/plain"}, 60), re.S).group(0))),
    ):
        try:
            got = fn()
            if not got:
                raise RuntimeError("빈 응답")
            # 장중 실행이면 당일 값이 잠정치. 다음 실행에서 종가로 덮임
            cache["px"].update(got)
            return "%s %d일, 최신 %s" % (name, len(got), max(got))
        except Exception as e:
            errs.append("%s: %s" % (name, e))
    raise RuntimeError("; ".join(errs))


def _nasdaq_px():
    t1 = today_kst()
    t0 = t1 - timedelta(days=90)
    url = ("https://api.nasdaq.com/api/quote/MP/historical?assetclass=stocks&fromdate=%s&limit=200&todate=%s"
           % (t0.isoformat(), t1.isoformat()))
    j = json.loads(http_get(url, {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                                  "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}, 25))
    out = {}
    for r in j["data"]["tradesTable"]["rows"]:
        mm, dd, yy = r["date"].split("/")
        out["%s-%s-%s" % (yy, mm, dd)] = float(r["close"].replace("$", "").replace(",", ""))
    return out


def _txt(node, path):
    e = node.find(path)
    return (e.text or "").strip() if e is not None and e.text else ""


def norm(name):
    """이름 비교용: 대문자, 문장부호 제거 (EDGAR와 오픈인사이더 표기 차이 흡수)"""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z ]", "", name.upper())).strip()


def trade_key(t):
    return "|".join(str(x) for x in (t["date"], norm(t["name"]), t["code"], t["shares"], t["price"]))


def _days(a, b):
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def collect_openinsider(cache, since="2026-01-01"):
    """SEC가 막힐 때 쓰는 대체 경로. 같은 사람·같은 방향 거래가 이미 있으면(신고일 ±1일 또는 같은 거래일) 건너뜀.
    한 신고서의 여러 건을 합쳐 보여주는 사이트라 사전계획(10b5-1) 여부는 알 수 없음."""
    t = get_or_reader("http://openinsider.com/MP")
    parsed = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        idx = next((i for i, c in enumerate(cells) if re.match(r"^[PS] - (Purchase|Sale)", c)), None)
        if idx is None or idx < 2 or idx + 3 >= len(cells):
            continue
        dates = [c[:10] for c in cells[:idx] if re.match(r"^\d{4}-\d{2}-\d{2}", c)]
        if len(dates) < 2 or dates[0] < since:
            continue
        try:
            parsed.append({"filed": dates[0], "date": dates[1], "code": cells[idx][0],
                           "name": cells[idx - 2], "title": cells[idx - 1],
                           "shares": abs(int(cells[idx + 2].replace(",", "").replace("+", ""))),
                           "price": float(cells[idx + 1].replace("$", "").replace(",", "")),
                           "owned": int(cells[idx + 3].replace(",", "") or 0), "di": "", "plan": 0, "src": "oi"})
        except ValueError:
            continue
    if not parsed:
        raise RuntimeError("오픈인사이더 표를 못 읽음")
    dup = new = 0
    for t_ in parsed:
        if any(norm(x["name"]) == norm(t_["name"]) and x["code"] == t_["code"]
               and (_days(x["filed"], t_["filed"]) <= 1 or x["date"] == t_["date"]) for x in cache["trades"]):
            dup += 1
            continue
        cache["trades"].append(t_)
        new += 1
    return "오픈인사이더 %d행 (기존과 겹침 %d, 신규 %d)" % (len(parsed), dup, new)


def collect_form4(cache, since="2026-01-01"):
    hdr = {"User-Agent": SEC_UA, "Accept": "application/json, text/xml, */*", "Accept-Encoding": "gzip"}
    try:
        j = json.loads(http_get("https://data.sec.gov/submissions/CIK%s.json" % CIK.zfill(10), hdr))
    except Exception as e:
        raise RuntimeError("신고서 목록(data.sec.gov): %s" % e)
    rec = j["filings"]["recent"]
    seen = set(cache.get("f4_seen", []))
    keys = {trade_key(t) for t in cache["trades"]}
    new = 0
    for form, fdate, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
        if form not in ("4", "4/A") or fdate < since or acc in seen:
            continue
        url = "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (CIK, acc.replace("-", ""), doc.split("/")[-1])
        time.sleep(0.4)                       # SEC 권고: 초당 10건 이하
        root = ET.fromstring(http_get(url, hdr))
        name = _txt(root, ".//rptOwnerName")
        title = _txt(root, ".//officerTitle") or ("Director" if _txt(root, ".//isDirector") in ("1", "true") else "")
        plan = 1 if _txt(root, ".//aff10b5One") in ("1", "true") else 0
        for tx in root.iter("nonDerivativeTransaction"):
            code = _txt(tx, ".//transactionCode")
            if code not in ("P", "S"):
                continue
            try:
                t = {"filed": fdate, "date": _txt(tx, "./transactionDate/value"), "code": code,
                     "name": name, "title": title,
                     "shares": int(float(_txt(tx, ".//transactionShares/value"))),
                     "price": float(_txt(tx, ".//transactionPricePerShare/value")),
                     "owned": int(float(_txt(tx, ".//sharesOwnedFollowingTransaction/value") or 0)),
                     "di": _txt(tx, ".//directOrIndirectOwnership/value"), "plan": plan, "src": "edgar"}
            except ValueError:
                continue
            if trade_key(t) not in keys:
                # 같은 거래를 오픈인사이더에서 먼저 받아둔 게 있으면 공식 신고 내용으로 바꿈
                cache["trades"] = [x for x in cache["trades"] if not (
                    x.get("src") == "oi" and norm(x["name"]) == norm(t["name"]) and x["code"] == t["code"]
                    and (_days(x["filed"], t["filed"]) <= 1 or x["date"] == t["date"]))]
                cache["trades"].append(t)
                keys.add(trade_key(t))
                new += 1
        seen.add(acc)
    cache["f4_seen"] = sorted(seen)
    return "신규 거래 %d건, 확인한 신고서 %d건" % (new, len(seen))


QSLUG = {1: ("first-quarter-%d-results", "First Quarter %d"), 2: ("second-quarter-%d-results", "Second Quarter %d"),
         3: ("third-quarter-%d-results", "Third Quarter %d"),
         4: ("fourth-quarter-and-full-year-%d-results", "Fourth Quarter and Full Year %d")}


def _er(q):
    """'26.3Q' → (회사 보도자료 주소, 본문에 있어야 할 문구)"""
    y, n = 2000 + int(q[:2]), int(q[3])
    return "https://mpmaterials.com/news/mp-materials-reports-" + QSLUG[n][0] % y + "/", QSLUG[n][1] % y


def collect_earnings(cache, man):
    """다음 분기 실적 보도자료가 올라왔는지 확인. 올라오면 화면 맨 위에 갱신 필요 알림을 띄움.
    분기 숫자 자체는 manual.json을 손으로 고쳐야 함."""
    last = man["quarters"][-1]["q"]
    y, n = int(last[:2]), int(last[3])
    nxt = "%02d.%dQ" % ((y, n + 1) if n < 4 else (y + 1, 1))

    def has(q):
        url, phrase = _er(q)
        try:
            t = get_or_reader(url)
        except Exception:
            return False
        return phrase.lower() in t.lower() and "NdPr" in t

    if not has(last):
        raise RuntimeError("대조용 %s 보도자료를 못 읽어 새 실적 감지 불가" % last)
    al = cache.setdefault("alerts", {})
    if has(nxt):
        prev = al.get("earnings") or {}
        al["earnings"] = {"q": nxt, "url": _er(nxt)[0], "seen": prev.get("seen") if prev.get("q") == nxt else today_kst().isoformat()}
        return "%s 실적 발표 감지" % nxt
    al.pop("earnings", None)
    return "%s 확인됨(대조), %s 아직 없음" % (last, nxt)


def collect_trades(cache):
    try:
        return "SEC " + collect_form4(cache)
    except Exception as e:
        return collect_openinsider(cache) + " · SEC 실패: %s" % str(e)[:80]


def collect_all(cache, man):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for name, fn in (("ndpr", collect_smm), ("fx", collect_fx), ("px", collect_px), ("trades", collect_trades),
                     ("aux", collect_smm_aux), ("earn", lambda c: collect_earnings(c, man))):
        try:
            msg = fn(cache)
            cache["log"][name] = {"ok": True, "at": now, "msg": msg}
            print("  [ok]   %-7s %s" % (name, msg))
        except Exception as e:
            prev = cache["log"].get(name, {})
            cache["log"][name] = {"ok": False, "at": now, "msg": str(e)[:300], "lastOk": prev.get("at") if prev.get("ok") else prev.get("lastOk")}
            print("  [fail] %-7s %s" % (name, e))


# ============================================================
# 2. 차트 (인라인 SVG)
# ============================================================
T = {"primary": "#4A5228", "heading": "#5F6C2C", "vivid": "#A2B84A", "fill": "#E3E6D2",
     "teal": "#2E97A7", "red": "#B7332B", "gold": "#D9A62E", "blue": "#4587B2",
     "text": "#3A3A3A", "muted": "#6E6E6E", "faint": "#9B9B9B", "hair": "#D8D8D8", "gray": "#B5B5B5"}


def d2o(s):
    return date.fromisoformat(s).toordinal()


def kd(s):
    """2026-11-17 → 26.11.17"""
    return "%s.%s.%s" % (s[2:4], s[5:7], s[8:10])


def nice(lo, hi, n=5):
    """눈금이 딱 떨어지는 축 범위 (y0, y1, step)."""
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def svg_line(series, hlines=(), dots=(), w=760, h=310, yfmt=None, ymin=None, ymax=None, mr=205, tfmt=None):
    """series: [{label,color,points[(d,v)],dash,width,markers,end}]  hlines: [(v,label,color)]
    dots: [{d,v,r,color,title,ring}] — 마우스를 올리면 title이 뜸"""
    yfmt = yfmt or (lambda v: "%.0f" % v)
    series = [s for s in series if s.get("points")]
    if not series:
        return '<div class="note">데이터 수집 대기 중</div>'
    ml, mt, mb = 46, 14, 26
    pw, ph = w - ml - mr, h - mt - mb
    xs = [d2o(p[0]) for s in series for p in s["points"]]
    ys = [p[1] for s in series for p in s["points"]] + [x[0] for x in hlines] + [d["v"] for d in dots]
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.06 or 1
    y0, y1, step = nice(ymin if ymin is not None else lo - pad, ymax if ymax is not None else hi + pad)
    nt = int(round((y1 - y0) / step))
    X = lambda o: ml + (o - x0) / max(1, x1 - x0) * pw
    Y = lambda v: mt + (1 - (v - y0) / (y1 - y0)) * ph
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit">' % (w, h)]
    for i in range(nt + 1):
        v = y0 + step * i
        o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="%s"/>' % (ml, Y(v), w - mr, Y(v), T["hair"], 1 if i == 0 else 0.6))
        o.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10" fill="%s">%s</text>' % (ml - 6, Y(v) + 3.5, T["muted"], (tfmt or yfmt)(v)))
    span = x1 - x0
    n = 6
    for i in range(n):
        ov = x0 + span * i / (n - 1)
        o.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" fill="%s">%s</text>'
                 % (X(ov), h - 8, T["muted"], date.fromordinal(int(ov)).strftime("%y.%m" if span > 120 else "%m.%d")))
    ends = []
    for v, label, col in hlines:
        o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.3" stroke-dasharray="6,4"/>' % (ml, Y(v), w - mr, Y(v), col))
        ends.append([w - mr + 7, Y(v) + 3.5, col, label, ""])
    for s in series:
        pts = sorted(s["points"])
        co = [(X(d2o(p[0])), Y(p[1])) for p in pts]
        if len(co) > 1:
            o.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"%s stroke-linejoin="round"/>'
                     % (" ".join("%.1f,%.1f" % c for c in co), s["color"], s.get("width", 2.2),
                        ' stroke-dasharray="5,4"' if s.get("dash") else ""))
        if s.get("markers"):
            for (cx, cy), p in zip(co, pts):
                o.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s" stroke="#fff" stroke-width="1"><title>%s  %s</title></circle>'
                         % (cx, cy, s["color"], kd(p[0]), H.escape(yfmt(p[1]))))
        o.append('<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s" stroke="#fff" stroke-width="1.2"/>' % (co[-1][0], co[-1][1], s["color"]))
        if s.get("label"):
            sub = s.get("end", "")
            ends.append([w - mr + 7, co[-1][1] + 3.5, s["color"], "%s %s (%s)" % (s["label"], yfmt(pts[-1][1]), kd(pts[-1][0])), sub])
    for d in dots:
        cx, cy = X(d2o(d["d"])), Y(d["v"])
        if d.get("ring"):
            o.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#fff" stroke="%s" stroke-width="2"><title>%s</title></circle>' % (cx, cy, d.get("r", 4), d["color"], H.escape(d["title"])))
        else:
            o.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" fill-opacity="0.72" stroke="#fff" stroke-width="1"><title>%s</title></circle>' % (cx, cy, d.get("r", 4), d["color"], H.escape(d["title"])))
        if d.get("text"):
            o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9.5" font-weight="700" fill="%s">%s</text>' % (cx, cy - d.get("r", 4) - 4, d["color"], H.escape(d["text"])))
    ends.sort(key=lambda e: e[1])
    for i in range(1, len(ends)):
        gap = 25 if ends[i - 1][4] else 14
        if ends[i][1] - ends[i - 1][1] < gap:
            ends[i][1] = ends[i - 1][1] + gap
    for ex, ey, col, txt, sub in ends:
        o.append('<text x="%.1f" y="%.1f" font-size="10.5" font-weight="700" fill="%s">%s</text>' % (ex, ey, col, H.escape(txt)))
        if sub:
            o.append('<text x="%.1f" y="%.1f" font-size="9.5" fill="%s" opacity="0.8">%s</text>' % (ex, ey + 11.5, col, H.escape(sub)))
    o.append("</svg>")
    return "".join(o)


def svg_bars(cats, vals, color, fmt, hline=None, w=760, h=230, mr=205, ymax=None, tops=None):
    ml, mt, mb = 46, 20, 26
    pw, ph = w - ml - mr, h - mt - mb
    hi = ymax or max(vals + ([hline[0]] if hline else [])) * 1.15
    Y = lambda v: mt + (1 - v / hi) * ph
    bw = pw / len(cats) * 0.56
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit">' % (w, h)]
    o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>' % (ml, Y(0), w - mr, Y(0), T["hair"]))
    for i, (c, v) in enumerate(zip(cats, vals)):
        cx = ml + pw * (i + 0.5) / len(cats)
        last = i == len(cats) - 1
        o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="%s"/>' % (cx - bw / 2, Y(v), bw, max(0.5, Y(0) - Y(v)), color, 1 if last else 0.55))
        o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10.5" font-weight="700" fill="%s">%s</text>' % (cx, Y(v) - 5, T["text"], (tops[i] if tops else fmt(v))))
        o.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" fill="%s">%s</text>' % (cx, h - 8, T["muted"], c))
    if hline:
        o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.3" stroke-dasharray="6,4"/>' % (ml, Y(hline[0]), w - mr, Y(hline[0]), T["red"]))
        o.append('<text x="%d" y="%.1f" font-size="10.5" font-weight="700" fill="%s">%s</text>' % (w - mr + 7, Y(hline[0]) + 3.5, T["red"], hline[1]))
    o.append("</svg>")
    return "".join(o)


def svg_group(cats, a, b, la, lb, ca, cb, fmt, hline=None, w=760, h=250, mr=205):
    """분기별 막대 두 개씩. 오른쪽 여백에 범례."""
    ml, mt, mb = 46, 22, 26
    pw, ph = w - ml - mr, h - mt - mb
    hi = max(a + b + ([hline[0]] if hline else [])) * 1.14
    Y = lambda v: mt + (1 - v / hi) * ph
    slot = pw / len(cats)
    bw = slot * 0.36
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit">' % (w, h)]
    o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>' % (ml, Y(0), w - mr, Y(0), T["hair"]))
    for i, c in enumerate(cats):
        cx = ml + slot * (i + 0.5)
        for v, col, dx in ((a[i], ca, -bw / 2 - 1), (b[i], cb, bw / 2 + 1)):
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (cx + dx - bw / 2, Y(v), bw, max(0.5, Y(0) - Y(v)), col))
            o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9.5" font-weight="700" fill="%s">%s</text>' % (cx + dx, Y(v) - 4, T["text"], fmt(v)))
        o.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10" fill="%s">%s</text>' % (cx, h - 8, T["muted"], c))
    ly = mt + 10
    if hline:
        o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.3" stroke-dasharray="6,4"/>' % (ml, Y(hline[0]), w - mr, Y(hline[0]), T["red"]))
        o.append('<text x="%d" y="%.1f" font-size="10.5" font-weight="700" fill="%s">%s</text>' % (w - mr + 7, Y(hline[0]) + 3.5, T["red"], hline[1]))
        ly = Y(hline[0]) + 26
    for lab, col in ((la, ca), (lb, cb)):
        o.append('<rect x="%d" y="%.1f" width="10" height="10" fill="%s"/><text x="%d" y="%.1f" font-size="10.5" fill="%s">%s</text>' % (w - mr + 7, ly, col, w - mr + 22, ly + 9, T["text"], lab))
        ly += 17
    o.append("</svg>")
    return "".join(o)


EC = {"s1": "#2E4A61", "s2": "#5A84AA", "s3": "#C99F5E", "op": "#4CB5A0", "opt": "#2F9E88", "line": "#EE7B2E"}


def svg_earn(cats, stacks, total, op, margin, op_label, m_label, w=760, h=400):
    """기업 실적 그래프 스킬 디자인: 누적 매출 막대 + 반겹침 이익 막대 + 상단 이익률 선.
    stacks: [(이름, 색, [값])] 아래부터 쌓음"""
    ml, mr = 46, 46
    band0, band1 = 44, 96           # 이익률 띠
    top, bot = 124, h - 30          # 막대 영역
    pw = w - ml - mr
    hi = max(total) * 1.12
    lo = min(0, min(op)) * 1.5
    Y = lambda v: top + (hi - v) / (hi - lo) * (bot - top)
    slot = pw / len(cats)
    bw = slot * 0.46
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit">' % (w, h)]
    x = ml
    for name, col, _ in stacks:
        o.append('<rect x="%d" y="10" width="11" height="11" fill="%s"/><text x="%d" y="20" font-size="11" fill="%s">%s</text>' % (x, col, x + 15, T["text"], name))
        x += 26 + len(name) * 11
    o.append('<rect x="%d" y="10" width="11" height="11" fill="%s"/><text x="%d" y="20" font-size="11" fill="%s">%s</text>' % (x, EC["op"], x + 15, T["text"], op_label))
    x += 26 + len(op_label) * 11
    o.append('<line x1="%d" y1="16" x2="%d" y2="16" stroke="%s" stroke-width="2.4"/><circle cx="%d" cy="16" r="3.4" fill="%s"/><text x="%d" y="20" font-size="11" fill="%s">%s</text>' % (x, x + 20, EC["line"], x + 10, EC["line"], x + 25, T["text"], m_label))
    mlo, mhi = min(margin), max(margin)
    MY = lambda v: band1 - (v - mlo) / max(1e-9, mhi - mlo) * (band1 - band0 - 16)
    pts = []
    o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>' % (ml, Y(0), w - mr, Y(0), T["hair"]))
    for i, c in enumerate(cats):
        cx = ml + slot * (i + 0.5)
        base = 0
        for _, col, vals in stacks:
            v = vals[i]
            if v > 0:
                o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (cx - bw / 2, Y(base + v), bw, Y(base) - Y(base + v), col))
                base += v
        o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="12" font-weight="800" fill="%s">%.0f</text>' % (cx - bw * 0.12, Y(max(base, total[i])) - 6, T["text"], total[i]))
        ow = bw * 0.5
        ox = cx + bw * 0.22
        y0, y1 = sorted((Y(0), Y(op[i])))
        o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (ox, y0, ow, max(0.8, y1 - y0), EC["op"]))
        ty = (Y(op[i]) - 5) if op[i] >= 0 else (Y(op[i]) + 13)
        lab = "%.0f" % op[i]
        o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="14" rx="2" fill="#fff" opacity="0.9"/>' % (ox + ow / 2 - len(lab) * 3.6 - 2, ty - 11, len(lab) * 7.2 + 4))
        o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="800" fill="%s">%s</text>' % (ox + ow / 2, ty, EC["opt"], lab))
        o.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="11" font-weight="700" fill="%s">%s</text>' % (cx, h - 9, T["muted"], c))
        pts.append((cx, MY(margin[i])))
    o.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.2"/>' % (" ".join("%.1f,%.1f" % p_ for p_ in pts), EC["line"]))
    for (cx, cy), mv in zip(pts, margin):
        o.append('<circle cx="%.1f" cy="%.1f" r="3.6" fill="%s"/><text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="800" fill="%s">%.0f%%</text>' % (cx, cy, EC["line"], cx, cy - 8, EC["line"], mv))
    o.append("</svg>")
    return "".join(o)


# ============================================================
# 3. 계산
# ============================================================
def at_or_before(dmap, d):
    ks = sorted(dmap)
    prior = [k for k in ks if k <= d]
    return dmap[prior[-1]] if prior else dmap[ks[0]]


def bal_at(man, d):
    prior = [b for b in man["balance"] if b["asOf"] <= d]
    return prior[-1] if prior else man["balance"][0]


def ev_at(man, d, px):
    b = bal_at(man, d)
    mcap = px * b["shares"]
    return mcap, mcap + b["debt"] - b["cash"]


def group_trades(trades):
    g = {}
    for t in trades:
        k = (t["date"], norm(t["name"]), t["code"])
        x = g.setdefault(k, {"date": t["date"], "name": t["name"], "title": t["title"], "code": t["code"],
                             "shares": 0, "amt": 0.0, "owned": t["owned"], "plan": 0, "filed": t["filed"]})
        x["shares"] += t["shares"]
        x["amt"] += t["shares"] * t["price"]
        x["owned"] = max(x["owned"], t["owned"]) if t["code"] == "P" else min(x["owned"], t["owned"])
        x["plan"] = max(x["plan"], t.get("plan", 0))
        x["filed"] = max(x["filed"], t["filed"])
    return sorted(g.values(), key=lambda x: (x["date"], x["filed"]), reverse=True)


def money(v):
    """달러 금액 → 만불/억불 표기"""
    if abs(v) >= 1e8:
        return "%.2f억불" % (v / 1e8)
    return "%s만불" % (format(round(v / 1e4), ",") if abs(v) >= 1e6 else "%.1f" % (v / 1e4))


# ============================================================
# 4. HTML
# ============================================================
CSS = """
:root{--primary:#4A5228;--heading:#5F6C2C;--teal:#2E97A7;--red:#B7332B;--gold:#D9A62E;--blue:#4587B2;
--text:#3A3A3A;--muted:#6E6E6E;--faint:#9B9B9B;--hair:#D8D8D8;--tint:#F3F4EA;--fill:#E3E6D2;--mark:#DCEBF7}
*{box-sizing:border-box}
body{font-family:"Pretendard","Noto Sans KR","Malgun Gothic","Apple SD Gothic Neo",Arial,sans-serif;color:var(--text);
margin:0;background:#F7F7F4;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 60px}
header.hero{background:var(--primary);color:#fff;padding:30px 0 22px;margin-bottom:20px}
.hero-in{max-width:1060px;margin:0 auto;padding:0 20px;display:flex;align-items:center;gap:18px;position:relative}
.back{position:absolute;top:-14px;left:20px;color:#fff;opacity:.72;font-size:11.5px;text-decoration:none;font-weight:600}
.back:hover{opacity:1;text-decoration:underline}
.stripes{display:inline-flex;gap:5px}
.stripes i{width:7px;height:38px;background:#fff;transform:skewX(-18deg);display:block;border-radius:1px}
.stripes i:nth-child(2){height:29px;margin-top:9px}.stripes i:nth-child(3){height:21px;margin-top:17px}
.hero h1{font-size:22px;font-weight:800;margin:0}
.hero .sub{font-size:11.5px;opacity:.85;margin:4px 0 0;font-weight:400;color:#fff}
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}
@media(max-width:760px){.kpi-row{grid-template-columns:repeat(2,1fr)}}
.kpi-card{background:#fff;border:1px solid var(--hair);border-radius:8px;padding:10px 12px}
.kpi-label{font-size:10.5px;font-weight:700;color:var(--muted)}
.kpi-num{font-size:21px;font-weight:800;color:var(--heading);margin:1px 0}
.kpi-unit{font-size:11px;color:var(--faint);font-weight:600;margin-left:2px}
.kpi-sub{font-size:10px;color:var(--faint)}
.alert{background:#FBEDEA;border-left:5px solid var(--red);padding:11px 16px;margin:0 0 14px;font-size:13px;line-height:1.7}
.alert a{color:var(--red)}
.thesis{background:var(--tint);border-left:5px solid var(--primary);padding:20px 24px;margin:0 0 26px}
.thesis-title{font-weight:800;color:var(--primary);font-size:21px;margin-bottom:12px;letter-spacing:-.02em}
.thesis ul{margin:0;padding-left:16px;list-style:disc}
.thesis li{color:var(--text);font-size:14.5px;line-height:1.7;margin-bottom:8px}
.thesis li:last-child{margin-bottom:0}
.banner{background:var(--primary);color:#fff;font-size:17px;font-weight:800;padding:9px 14px;margin:44px 0 14px;display:block;letter-spacing:-.01em}
.banner .u{font-weight:400;font-size:11px;opacity:.85;float:right;padding-top:6px}
.card-sec{background:#fff;border:1px solid var(--hair);border-radius:8px;padding:18px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.sub{font-size:15.5px;font-weight:800;color:var(--heading);margin:22px 0 8px}
.card-sec>.sub:first-child{margin-top:0}
.ana{font-size:13.5px;line-height:1.75;margin:0 0 12px}
.ana b{color:var(--heading)}
.mark{background:var(--mark);font-weight:700;padding:1px 3px}
.star{color:var(--blue);font-weight:800;margin-right:3px}
table.data{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0 4px}
table.data thead th{background:var(--primary);color:#fff;font-weight:700;padding:6px 9px;text-align:left;white-space:nowrap}
table.data td{padding:6px 9px;border-bottom:1px solid #EFEFEF;vertical-align:top;line-height:1.55}
table.data td.num,table.data th.num{text-align:right;white-space:nowrap}
table.data tr.hl td{background:var(--fill);font-weight:700}
table.data td a{color:inherit}
table.data.eq{table-layout:fixed}
table.data.eq thead th{white-space:normal;vertical-align:bottom}
table.data.eq th:first-child{width:9%}
.tw{overflow-x:auto}
.pos{color:var(--teal);font-weight:700}.neg{color:var(--red);font-weight:700}
.note{font-size:11px;color:var(--faint);margin-top:8px;line-height:1.6;border-top:1px solid #EFEFEF;padding-top:7px}
.note a,.lk a{color:var(--muted)}
.lk{font-size:11px;color:var(--faint);margin-top:3px}
.tl{border-left:2px solid var(--fill);margin:4px 0 0 4px;padding:0 0 0 16px}
.tl .it{margin-bottom:16px;position:relative}
.tl .it:before{content:"";position:absolute;left:-22px;top:6px;width:9px;height:9px;border-radius:50%;background:var(--primary)}
.tl .w{font-size:12px;font-weight:800;color:var(--heading)}
.tl .t{font-size:14px;font-weight:800;margin:1px 0 3px}
.tl .b{font-size:13px;line-height:1.7}
.tag{display:inline-block;font-size:10.5px;font-weight:800;padding:1px 7px;border-radius:3px;color:#fff}
.tag.buy{background:var(--teal)}.tag.sell{background:var(--red)}.tag.plan{background:var(--faint);margin-left:4px}
.legend{font-size:11px;color:var(--muted);margin:4px 0 0}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 10px;vertical-align:-1px}
details.more>summary{cursor:pointer;font-size:12.5px;font-weight:700;color:var(--heading);margin-top:8px}
footer{font-size:10.5px;color:var(--faint);line-height:1.7;border-top:1px solid var(--hair);padding-top:12px;margin-top:30px}
@media(max-width:640px){.banner{margin-top:30px}.thesis li{font-size:13.5px}}
"""


def kpi(label, num, unit, sub):
    return ('<div class="kpi-card"><div class="kpi-label">%s</div><div class="kpi-num">%s<span class="kpi-unit">%s</span></div>'
            '<div class="kpi-sub">%s</div></div>' % (label, num, unit, sub))


def links(lst):
    return " · ".join('<a href="%s" target="_blank" rel="noopener">%s</a>' % (u, H.escape(n)) for n, u in lst)


def build_html(cache, man):
    today = today_kst()
    floor, vat = man["floor"], man["vat"]
    nd, fx, px = cache["ndpr"], cache["fx"], cache["px"]

    # ---------- ① NdPr 가격 ----------
    usd_in = [(d, v / at_or_before(fx, d) / 1000) for d, v in sorted(nd.items())]
    usd_ex = [(d, v / (1 + vat)) for d, v in usd_in]
    kmid = (1 + 1 / (1 + vat)) / 2                      # 부가세 포함가 대비 중간값 비율
    usd_mid = [(d, v * kmid) for d, v in usd_in]
    ld, lv = max(nd), nd[max(nd)]
    lin, lex, lmid = usd_in[-1][1], usd_ex[-1][1], usd_mid[-1][1]
    need = floor / kmid * at_or_before(fx, ld) * 1000   # 중간값이 하한선에 닿는 SMM 시세(위안/톤)
    qs = man["quarters"]
    for q in qs:
        q["real"] = q["ndpr_rev"] * 1000 / q["sales"]
    qdots = [{"d": q["end"], "v": q["real"], "r": 4.5, "color": T["gold"], "ring": True,
              "title": "%s MP 실현가 $%.1f/kg (NdPr 매출 ÷ 판매량)" % (q["q"], q["real"])} for q in qs]
    ch1 = svg_line(
        [{"label": "부가세 포함", "color": T["gray"], "points": usd_in, "width": 1.4},
         {"label": "중간값", "color": T["primary"], "points": usd_mid, "markers": True, "width": 2.6,
          "end": "하한선 대비 %+.1f%%" % ((lmid / floor - 1) * 100)},
         {"label": "부가세 제외", "color": T["teal"], "points": usd_ex, "dash": True, "width": 1.4}],
        hlines=[(floor, "국방부 하한선 $%d" % floor, T["red"])], dots=qdots,
        yfmt=lambda v: "$%.1f" % v, tfmt=lambda v: "$%.0f" % v, ymin=40)
    sens = ""
    for p in (100, 110, 120, 130, 150):
        a = max(floor, p)
        b = floor + 0.7 * (p - floor) if p > floor else floor
        ga = (a - floor) * man["capacity_ndpr_t"] / 1000
        gb = (b - floor) * man["capacity_ndpr_t"] / 1000
        sens += ('<tr><td class="num">$%d</td><td class="num">$%d</td><td class="num">%s</td><td class="num">$%.0f</td><td class="num">%s</td></tr>'
                 % (p, a, "+%.0f백만불" % ga if ga else "0", b, "+%.0f백만불" % gb if gb else "0"))

    # ---------- ② 분기 ----------
    qc = [q["q"] for q in qs]
    ch2a = svg_bars(qc, [q["real"] for q in qs], T["primary"], lambda v: "$%.1f" % v, hline=(floor, "국방부 하한선 $%d" % floor), ymax=130)
    ch2b = svg_bars(qc, [q["ppa"] for q in qs], T["teal"], lambda v: "%.1f" % v if v else "0", h=190)
    qrows = ""
    for q in reversed(qs):
        qrows += ('<tr%s><td>%s</td><td class="num">%s</td><td class="num">%.1f</td><td class="num">$%.1f</td><td class="num">%s</td></tr>'
                  % (' class="hl"' if q is qs[-1] else "", q["q"], format(q["sales"], ","), q["ndpr_rev"], q["real"],
                     ("%.1f" % q["ppa"]) if q["ppa"] else "0"))
    lq = qs[-1]
    ppa_first = next(q for q in qs if q["ppa"])

    # ---------- ③ 타임라인 ----------
    end = date.fromisoformat(man["truce_end"])
    dleft = (end - today).days
    ctl = "".join('<tr><td><b>%s</b></td><td>%s</td><td>%s<div class="lk">%s</div></td></tr>' % (c["name"], c["items"], c["status"], links(c["links"])) for c in man["controls"])
    overdue = [u for u in man["upcoming"] if u["date"] < today.isoformat()]
    up = "".join('<tr%s><td style="white-space:nowrap"><b>%s</b></td><td>%s%s</td><td>%s</td></tr>'
                 % (' class="hl"' if u["date"] == man["truce_end"] else "", u["when"], u["event"],
                    ' <span class="tag sell">지남: 결과 반영 필요</span>' if u in overdue else "", u["check"])
                 for u in man["upcoming"])
    tl = ""
    for e in man["timeline"]:
        title = ('<span class="star">★</span><span class="mark">%s</span>' % e["title"]) if e.get("star") else e["title"]
        tl += '<div class="it"><div class="w">%s</div><div class="t">%s</div><div class="b">%s</div><div class="lk">%s</div></div>' % (e["when"], title, e["body"], links(e["links"]))

    # ---------- ④ 내부자 ----------
    gt = group_trades(cache["trades"])
    pxs = sorted(px.items())
    t0 = min([g["date"] for g in gt] + [pxs[-1][0]])
    start = (date.fromisoformat(t0) - timedelta(days=20)).isoformat()
    line = [p for p in pxs if p[0] >= start]
    people = man["people"]
    nm = lambda n: people.get(norm(n), n)
    dots = []
    for g in sorted(gt, key=lambda g: -g["amt"]):
        avg = g["amt"] / g["shares"]
        dots.append({"d": g["date"], "v": avg, "r": max(3.6, min(15, 3 + math.sqrt(g["amt"] / 1e6) * 2.0)),
                     "color": T["teal"] if g["code"] == "P" else T["red"],
                     "title": "%s %s %s  %s주 × $%.2f = %s%s" % (kd(g["date"]), nm(g["name"]), "매수" if g["code"] == "P" else "매도",
                                                               format(g["shares"], ","), avg, money(g["amt"]), " (사전계획)" if g["plan"] else "")})
    lpd, lpx = pxs[-1]
    ch4 = svg_line([{"label": "MP 종가", "color": T["gray"], "points": line, "width": 1.6}], dots=dots,
                   yfmt=lambda v: "$%.2f" % v, tfmt=lambda v: "$%.0f" % v, ymin=25)
    d30 = (today - timedelta(days=30)).isoformat()
    buy30 = sum(g["amt"] for g in gt if g["code"] == "P" and g["date"] >= d30)
    sell30 = sum(g["amt"] for g in gt if g["code"] == "S" and g["date"] >= d30)
    yr = str(today.year)
    per = {}
    for g in gt:
        if g["date"][:4] == yr:
            x = per.setdefault(norm(g["name"]), {"P": 0.0, "S": 0.0, "Ps": 0, "Ss": 0, "S_noplan": 0.0, "name": g["name"]})
            x[g["code"]] += g["amt"]
            x[g["code"] + "s"] += g["shares"]
            if g["code"] == "S" and not g["plan"]:
                x["S_noplan"] += g["amt"]
    prow = ""
    for n, x in sorted(per.items(), key=lambda kv: -(kv[1]["P"] + kv[1]["S"])):
        prow += ('<tr><td>%s</td><td class="num pos">%s</td><td class="num neg">%s</td><td class="num">%s</td></tr>'
                 % (nm(x["name"]), money(x["P"]) if x["P"] else "", money(x["S"]) if x["S"] else "",
                    ("+" if x["P"] >= x["S"] else "−") + money(abs(x["P"] - x["S"]))))

    def trow(g):
        return ('<tr><td style="white-space:nowrap">%s</td><td>%s</td><td><span class="tag %s">%s</span>%s</td>'
                '<td class="num">%s</td><td class="num">$%.2f</td><td class="num">%s</td><td class="num">%s</td></tr>'
                % (kd(g["date"]), nm(g["name"]), "buy" if g["code"] == "P" else "sell", "매수" if g["code"] == "P" else "매도",
                   '<span class="tag plan">사전계획</span>' if g["plan"] else "", format(g["shares"], ","), g["amt"] / g["shares"],
                   money(g["amt"]), format(g["owned"], ",")))
    thead = '<thead><tr><th>거래일</th><th>이름</th><th>구분</th><th class="num">주식 수</th><th class="num">평균 단가</th><th class="num">금액</th><th class="num">거래 후 보유</th></tr></thead>'
    trs = "".join(trow(g) for g in gt[:12])
    trs_more = "".join(trow(g) for g in gt[12:])
    ceo = per.get("LITINSKY JAMES H", {"S": 0, "Ss": 0, "P": 0})

    # ---------- ⑤ 밸류 ----------
    mcap, ev = ev_at(man, lpd, lpx)
    b = bal_at(man, lpd)
    ests = [dict(e) for e in man["estimates"]]
    years = ["2026", "2027", "2028"]
    cons_note = ""
    for e in ests:
        ch_ = e.get("hist") or []          # 추정치가 바뀔 때마다 manual.json에 {d, rev, ebitda}를 한 줄씩 추가
        if ch_:
            e.update(rev=ch_[-1]["rev"], ebitda=ch_[-1]["ebitda"], asOf=ch_[-1]["d"])
            if len(ch_) >= 2 and (today - date.fromisoformat(ch_[-1]["d"])).days <= 45:
                a_, b_ = ch_[-2], ch_[-1]
                cons_note = "%s 변경 (%s): " % (e["name"].split(" (")[0], kd(b_["d"])) + ", ".join(
                    "%s년 EBITDA %.0f → %.0f" % (y[2:], a_["ebitda"][y], b_["ebitda"][y]) for y in years if abs(a_["ebitda"].get(y, 0) - b_["ebitda"][y]) >= 0.5)
    erow = ""
    for e in ests:
        nmx = '<a href="%s" target="_blank" rel="noopener">%s</a>' % (e["url"], e["name"]) if e.get("url") else e["name"]
        erow += ('<tr><td><b>%s</b></td><td>%s</td>' % (nmx, kd(e["asOf"]))
                 + "".join('<td class="num">%s</td>' % format(e["rev"][y], ",.0f") for y in years)
                 + "".join('<td class="num">%s</td>' % format(e["ebitda"][y], ",.0f") for y in years)
                 + "".join('<td class="num"%s>%.1f배</td>' % (' style="font-weight:800;color:#4587B2"' if y == "2027" else "", ev / e["ebitda"][y]) for y in years)
                 + "</tr>")
    crow = '<tr><td><b>회사 제시</b></td><td colspan="10">%s</td></tr>' % "<br>".join(man["company_view"])
    vline = [p for p in pxs if p[0] >= (today - timedelta(days=365)).isoformat()]
    vs = []
    for e in ests:
        hs = e.get("hist")

        def eb27(d, e=e, hs=hs):
            if not hs:
                return e["ebitda"]["2027"]
            prior = [h for h in hs if h["d"] <= d]
            return (prior[-1] if prior else hs[0])["ebitda"]["2027"]
        pts = [(d, ev_at(man, d, p)[1] / eb27(d)) for d, p in vline]
        vs.append({"label": e["name"].split(" (")[0], "color": T[e.get("color", "primary")], "points": pts, "width": 2.0})
    ch5 = svg_line(vs, hlines=[(r["x"], r["label"], T["faint"]) for r in man["multiple_refs"]],
                   yfmt=lambda v: "%.1f배" % v, tfmt=lambda v: "%.0f배" % v, ymin=10, mr=215)
    cons = next((e for e in ests if e["id"] == "consensus"), ests[-1])
    barb = next((e for e in ests if e["id"] == "barbarian"), ests[0])

    # ---------- ⑥ 생산·판매량 ----------
    capq = man["capacity_ndpr_t"] / 4
    ch6 = svg_group(qc, [q["prod"] for q in qs], [q["sales"] for q in qs], "NdPr 생산 (톤)", "NdPr 판매 (톤)",
                    T["primary"], T["vivid"], lambda v: format(v, ","), hline=(capq, "목표 케파 분기 환산 %s톤" % format(round(capq), ",")))
    util = lq["prod"] / capq * 100

    # ---------- ⑦ 부문 실적 ----------
    tot = [q["rev"] + q["ppa"] for q in qs]
    marg = [q["ebitda"] / t_ * 100 for q, t_ in zip(qs, tot)]
    ch7 = svg_earn(qc, [("소재 부문 매출", EC["s1"], [q["mat"] for q in qs]), ("자석 부문 매출", EC["s2"], [q["mag"] for q in qs]),
                        ("국방부 보전금", EC["s3"], [q["ppa"] for q in qs])], tot, [q["ebitda"] for q in qs], marg, "조정 EBITDA", "조정 EBITDA 이익률")
    srow = ""
    for q, t_, m_ in reversed(list(zip(qs, tot, marg))):
        srow += ('<tr%s><td>%s</td><td class="num">%.1f</td><td class="num">%.1f</td><td class="num">%s</td><td class="num">%.1f</td>'
                 '<td class="num %s">%.1f</td><td class="num %s">%.1f</td><td class="num %s">%.1f</td><td class="num">%.0f%%</td></tr>'
                 % (' class="hl"' if q is qs[-1] else "", q["q"], q["mat"], q["mag"], ("%.1f" % q["ppa"]) if q["ppa"] else "0", t_,
                    "pos" if q["mat_e"] > 0 else "neg", q["mat_e"], "pos" if q["mag_e"] > 0 else "neg", q["mag_e"],
                    "pos" if q["ebitda"] > 0 else "neg", q["ebitda"], m_))

    # ---------- ⑧ 현금·차입금·주식 수 ----------
    ct = man["cash_table"]
    ch8 = svg_group([c["p"] for c in ct], [c["cash"] for c in ct], [c["debt"] for c in ct], "현금·단기투자 (백만불)", "차입금 (백만불)",
                    T["primary"], T["gray"], lambda v: format(v, ",.0f"), h=230)
    crow8 = ""
    for c in reversed(ct):
        crow8 += ('<tr%s><td>%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%.1f</td><td class="num">%s</td></tr>'
                 % (' class="hl"' if c is ct[-1] else "", c["p"], format(c["cash"], ",.1f"), format(c["debt"], ",.1f"),
                    format(c["cash"] - c["debt"], "+,.1f"), c["shares"], c["capex"]))

    # ---------- ⑨ Dy·Tb ----------
    dyp, tbp = sorted((cache.get("dy_usd") or {}).items()), sorted((cache.get("tb_usd") or {}).items())
    ch9a = svg_line([{"label": "디스프로슘 산화물", "color": T["primary"], "points": dyp, "markers": True}], h=230,
                    yfmt=lambda v: "$%.1f" % v, tfmt=lambda v: "$%.0f" % v) if dyp else ""
    ch9b = svg_line([{"label": "터븀 산화물", "color": T["teal"], "points": tbp, "markers": True}], h=230,
                    yfmt=lambda v: "$%.1f" % v, tfmt=lambda v: "$%.0f" % v) if tbp else ""

    # ---------- ⑩ 케파 ----------
    caprow = "".join('<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>' % (c["name"], c["cap"], c["now"], c["next"]) for c in man["capacity"])

    # ---------- 0. 헤더·KPI·지금 상태 ----------
    kp = "".join([
        kpi("NdPr 산화물 (SMM)", "%.1f" % (lv / 10000), "만 위안/톤", "%s · 부가세 포함" % kd(ld)),
        kpi("달러 환산 중간값", "$%.1f" % lmid, "/kg", "부가세 포함 %.1f · 제외 %.1f · 하한선 대비 %+.1f%%" % (lin, lex, (lmid / floor - 1) * 100)),
        kpi("110불에 닿는 SMM 시세", "%.1f" % (need / 10000), "만 위안/톤", "지금보다 %+.1f%% · 중간값 기준" % ((need / lv - 1) * 100)),
        kpi("MP 분기 실현가", "$%.1f" % lq["real"], "/kg", "%s · NdPr 매출 ÷ 판매량" % lq["q"]),
        kpi("중국 규제 유예 만료", "D-%d" % dleft if dleft >= 0 else "만료", "", kd(man["truce_end"])),
        kpi("내부자 최근 30일", ("+%s" % money(buy30 - sell30)) if buy30 >= sell30 else ("−%s" % money(sell30 - buy30)), "", "순매수 (매수 − 매도)"),
        kpi("MP 주가", "$%.2f" % lpx, "", "%s · 시총 %.1f억불" % (kd(lpd), mcap / 100)),
        kpi("EV/EBITDA 27년", "%.1f" % (ev / cons["ebitda"]["2027"]), "배", "컨센 기준 · 바바리안 %.1f배" % (ev / barb["ebitda"]["2027"])),
    ])
    now = [
        "<b>NdPr 시세</b>: SMM %s 위안/톤(%s). 달러로 바꾸면 부가세 포함 %.1f불, 제외 %.1f불, 중간값 %.1f불<br>MP 실제 판매가는 중간값 근처에서 형성되므로, MP가 110불 넘게 받으려면 SMM 시세가 약 %s 위안/톤(지금보다 %+.1f%%)까지 올라야 함<br>그 전까지는 국방부 보전으로 110불을 받고, 넘으면 초과분을 전부 가져감 (10X 공장 풀가동 전까지)"
        % (format(lv, ","), kd(ld), lin, lex, lmid, format(round(need, -3), ",.0f"), (need / lv - 1) * 100),
        "<b>분기 실현가</b>: %s 약 %.0f불. 국방부 보전금(PPA 수익)은 %s %.1f백만불에서 %s %.1f백만불로 감소"
        % (lq["q"], lq["real"], ppa_first["q"], ppa_first["ppa"], lq["q"], lq["ppa"]),
        "<b>미중 유예</b>: 중국의 25년 10월 추가 규제 유예가 %s 만료 (D-%d)" % (kd(man["truce_end"]), dleft),
        "<b>내부자</b>: 최근 30일 매수 %s, 매도 %s. %s년 누적으로는 CEO가 %s 매도"
        % (money(buy30) if buy30 else "없음", money(sell30) if sell30 else "없음", yr[2:], money(ceo["S"]) if ceo["S"] else "없음"),
        "<b>밸류</b>: 시총 %.1f억불, EV %.1f억불. 27년 추정 EBITDA 기준 EV/EBITDA는 컨센 %.1f배, 바바리안 추정 %.1f배"
        % (mcap / 100, ev / 100, ev / cons["ebitda"]["2027"], ev / barb["ebitda"]["2027"]),
    ]
    log = cache.get("log", {})
    lab = {"ndpr": "NdPr 시세", "fx": "환율", "px": "주가", "trades": "내부자 거래", "aux": "Dy·Tb", "earn": "실적 발표 감지"}
    warn = []
    er = (cache.get("alerts") or {}).get("earnings")
    if er and er["q"] not in [q["q"] for q in qs]:
        warn.append('<b>%s 실적 발표됨</b> (%s 확인): ②④⑤⑧ 표는 아직 %s 기준 <a href="%s" target="_blank" rel="noopener">보도자료</a>' % (er["q"], kd(er["seen"]), lq["q"], er["url"]))
    if overdue:
        warn.append("③ 추후 일정 중 날짜가 지난 것 %d건: 결과 반영 필요" % len(overdue))
    for k, v in log.items():
        if k in lab and not v.get("ok"):
            warn.append("%s 자동 수집 실패 (%s)" % (lab[k], v["at"][5:]))
    for name, dm, lim in (("NdPr 시세", nd, 6), ("주가", px, 5)):
        if (today - date.fromisoformat(max(dm))).days > lim:
            warn.append("%s가 %s 이후 갱신되지 않음" % (name, kd(max(dm))))
    for e in ests:
        if e["id"] == "consensus" and (today - date.fromisoformat(e["asOf"])).days > 45:
            warn.append("⑥ 컨센 추정치 기준일이 %s: 45일 넘게 갱신되지 않음" % kd(e["asOf"]))
    warnbox = ('<div class="alert">%s</div>' % "<br>".join(warn)) if warn else ""
    stat = " · ".join("%s %s" % (lab[k], ("정상 " + v["at"][5:]) if v.get("ok") else ("실패 " + v["at"][5:]))
                      for k, v in log.items() if k in lab) or "첫 자동 수집 전"

    html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MP 머티리얼즈 트래커</title><style>%(css)s</style></head><body>
<header class="hero"><div class="hero-in"><a class="back" href="../">← 리서치 대시보드</a>
<span class="stripes"><i></i><i></i><i></i></span>
<div><h1>MP 머티리얼즈 트래커</h1><div class="sub">미국 희토류(NYSE: MP) · NdPr 가격과 국방부 하한선, 미중 규제 유예, 내부자 매매, 밸류 · 케파·실적 · 기준 %(today)s · 매일 07:35 / 18:40 KST 자동 수집</div></div>
</div></header>
<div class="wrap">
%(warnbox)s<div class="kpi-row">%(kp)s</div>
<div class="thesis"><div class="thesis-title">지금 상태</div><ul>%(now)s</ul></div>

<span class="banner">① NdPr 가격 vs 국방부 하한선 110불<span class="u">달러/kg · 일간</span></span>
<div class="card-sec">
%(ch1)s
<div class="legend"><i style="background:#4A5228"></i>중간값 (MP 판매가 근사)<i style="background:#B5B5B5"></i>SMM 시세 달러 환산 (부가세 13%% 포함)<i style="background:#2E97A7"></i>부가세 제외<i style="background:#fff;border:2px solid #D9A62E"></i>MP 분기 실현가 (분기 말 위치)</div>
<p class="ana" style="margin-top:12px"><span class="star">★</span><span class="mark">110불과 비교할 선은 중간값: SMM 시세가 약 %(need)s 위안/톤을 넘어야 MP가 110불보다 많이 받음</span></p>
<div class="sub">%(rtitle)s</div>
<p class="ana">%(rlines)s</p>
<div class="sub">%(iotitle)s</div>
<p class="ana">%(iolines)s</p>
<div class="note">SMM Pr-Nd 산화물 평균가(위안/톤, 중국 도착도, 부가세 포함)를 그날 달러/위안 환율로 나눈 값. 중간값은 부가세 포함가와 제외가의 평균.
%(ld0)s 이전은 주요 시점의 SMM 공개 수치만 이은 선이고, 그 뒤로 매일 한 점씩 쌓임. 점에 마우스를 올리면 날짜와 값 표시.<br>
Source: <a href="%(smm)s" target="_blank" rel="noopener">SMM</a>, 야후 파이낸스(환율), MP 실적 발표자료 · %(psrc)s</div>

<div class="sub">하한선 정산 방식</div>
<p class="ana"><span class="mark">국방부는 MP의 NdPr 가격이 kg당 110불에 못 미치면 차액을 분기마다 현금으로 보전 (25.10.01부터 10년)</span><br>
110불을 넘는 분기에는 초과분의 30%%를 MP가 국방부에 지급. 단 10X 공장(텍사스 제2 자석 공장, 28년 말 시운전)이 목표 케파에 도달한 뒤부터 적용</p>
<div class="note">Source: <a href="https://www.sec.gov/Archives/edgar/data/1801368/000119312525157310/d43796dex106.htm" target="_blank" rel="noopener">가격보전계약(PPA) 원문</a>, <a href="https://s25.q4cdn.com/570172628/files/doc_events/2025/Jul/10/FINAL-Transformational-Public-Private-Partnership-7-9-25-440pm.pdf" target="_blank" rel="noopener">국방부 파트너십 발표자료(25.07.10)</a></div>
</div>

<span class="banner">② 분기 실현가와 국방부 보전금<span class="u">분기 · 실적 발표 때 갱신</span></span>
<div class="card-sec">
<div class="sub">NdPr 실현가 <span style="font-weight:400;font-size:12px;color:#9B9B9B">달러/kg · NdPr 산화물·금속 매출 ÷ 판매량</span></div>
%(ch2a)s
<div class="sub">국방부 보전금 (PPA 수익) <span style="font-weight:400;font-size:12px;color:#9B9B9B">백만불</span></div>
%(ch2b)s
<p class="ana">%(qnote)s</p>
<div class="tw"><table class="data eq" style="max-width:640px"><thead><tr><th style="width:16%%">분기</th><th class="num">NdPr 판매(톤)</th><th class="num">NdPr 매출</th><th class="num">실현가</th><th class="num">PPA 수익</th></tr></thead><tbody>%(qrows)s</tbody></table></div>
<div class="note">금액 단위 백만불. 실현가는 금속 가공분과 자석 부문 내부 판매를 포함한 매출을 판매량으로 나눈 근사치.
PPA 수익은 25.4Q~26.1Q에 재고로 쌓은 정광 속 NdPr 몫까지 포함돼 컸고, 26.2Q부터는 대부분 외부 판매분.<br>
Source: MP 분기 실적 발표자료(8-K), <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001801368&type=8-K" target="_blank" rel="noopener">SEC EDGAR</a></div>
</div>

<span class="banner">③ 미중 희토류 규제 유예<span class="u">이벤트 발생 시 갱신</span></span>
<div class="card-sec">
<div class="sub">통제의 두 층위</div>
<div class="tw"><table class="data"><thead><tr><th>구분</th><th>대상 품목</th><th>현재 상태</th></tr></thead><tbody>%(ctl)s</tbody></table></div>
<div class="sub">추후 일정</div>
<div class="tw"><table class="data"><thead><tr><th>시점</th><th>이벤트</th><th>확인할 것</th></tr></thead><tbody>%(up)s</tbody></table></div>
<div class="sub">진행 경과 <span style="font-weight:400;font-size:12px;color:#9B9B9B">최신이 위</span></div>
<div class="tl">%(tl)s</div>
<div class="sub">짚어둘 점</div>
<p class="ana">%(tlv)s</p><div class="lk">%(tlvl)s</div>
</div>

<span class="banner">④ 내부자 매매<span class="u">SEC Form 4 · 매일 확인</span></span>
<div class="card-sec">
%(ch4)s
<div class="legend"><i style="background:#2E97A7"></i>매수<i style="background:#B7332B"></i>매도 · 원 크기는 거래 금액, 높이는 평균 단가. 원에 마우스를 올리면 상세 표시</div>
<div class="sub">%(yr)s년 누적</div>
<div class="tw"><table class="data eq" style="max-width:640px"><thead><tr><th style="width:40%%">이름</th><th class="num">매수</th><th class="num">매도</th><th class="num">순매수</th></tr></thead><tbody>%(prow)s</tbody></table></div>
<div class="sub">거래 내역</div>
<div class="tw"><table class="data">%(thead)s<tbody>%(trs)s</tbody></table></div>
%(more)s
<div class="note">공개시장 매수(P)·매도(S)만 집계하고 주식 보상·옵션 행사·세금 납부용 처분은 제외. 같은 날 같은 사람의 거래는 한 줄로 합침.
사전계획 표시는 신고서에 사전 매매계획(10b5-1) 거래라고 체크된 건.<br>
Source: <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001801368&type=4&dateb=&owner=include&count=40" target="_blank" rel="noopener">SEC EDGAR Form 4</a></div>
</div>

<span class="banner">⑤ 밸류에이션<span class="u">EV/EBITDA · 주가는 매일, 추정치는 바뀔 때 갱신</span></span>
<div class="card-sec">
<div class="sub">추정치와 현재 배수 <span style="font-weight:400;font-size:12px;color:#9B9B9B">백만불 · %(lpd)s 종가 $%(lpx).2f 기준</span></div>
<div class="tw"><table class="data"><thead><tr><th rowspan="2">출처</th><th rowspan="2">기준일</th><th colspan="3" class="num" style="text-align:center">매출</th><th colspan="3" class="num" style="text-align:center">조정 EBITDA</th><th colspan="3" class="num" style="text-align:center">EV/EBITDA</th></tr>
<tr><th class="num">26E</th><th class="num">27E</th><th class="num">28E</th><th class="num">26E</th><th class="num">27E</th><th class="num">28E</th><th class="num">26E</th><th class="num">27E</th><th class="num">28E</th></tr></thead><tbody>%(erow)s%(crow)s</tbody></table></div>
%(consnote)s<div class="sub">EV/EBITDA 추이 <span style="font-weight:400;font-size:12px;color:#9B9B9B">27년 추정 EBITDA 기준 · 최근 1년</span></div>
%(ch5)s
<div class="note">EV = 시총 + 차입금 − 현금·단기투자 = %(mcap)s + %(debt)s − %(cash)s = %(ev)s백만불 (재무제표 %(bal)s 기준). 시총은 발행주식 %(sh).1f백만주 기준으로, 국방부 전환우선주와 워런트는 미포함.<br>
추이 선은 추정치를 현재 값으로 고정하고 주가와 순현금만 날짜별로 반영. 추정치가 바뀌면 그날부터 새 값 적용. %(refsrc)s.<br>
Source: 야후 파이낸스(주가), MP 10-Q, 표의 각 출처</div>
</div>

<span class="banner">⑥ NdPr 생산·판매량<span class="u">분기 · 실적 발표 때 갱신</span></span>
<div class="card-sec">
%(ch6)s
<p class="ana">%(lq)s 생산량은 목표 케파의 %(util).0f%% 수준<br>%(vnote)s</p>
<div class="note">목표 케파는 마운틴패스 NdPr 산화물 연 %(cap)s톤. 판매량은 자석 부문 내부 판매를 포함하고, 금속으로 판 물량은 산화물 환산.<br>
Source: MP 분기 실적 발표자료(8-K)</div>
</div>

<span class="banner">⑦ 부문별 매출과 조정 EBITDA<span class="u">백만불 · 분기</span></span>
<div class="card-sec">
%(ch7)s
<div class="tw"><table class="data eq"><thead><tr><th>분기</th><th class="num">소재 매출</th><th class="num">자석 매출</th><th class="num">국방부 보전금</th><th class="num">매출 + 보전금</th><th class="num">소재 EBITDA</th><th class="num">자석 EBITDA</th><th class="num">전사 조정 EBITDA</th><th class="num">이익률</th></tr></thead><tbody>%(srow)s</tbody></table></div>
<div class="note">막대 위 숫자는 회사가 발표하는 매출 + 국방부 보전금(PPA 수익) 합계로, 부문 간 내부거래를 뺀 값. 이익률은 전사 조정 EBITDA ÷ 그 합계.
소재 부문은 마운틴패스(광산·분리정제), 자석 부문은 인디펜던스 공장. 전사 조정 EBITDA는 본사 비용 차감 후. 25.3Q부터 중국향 정광 판매 중단.<br>
Source: MP 분기 실적 발표자료(8-K)</div>
</div>

<span class="banner">⑧ 현금·차입금·주식 수<span class="u">백만불 · 분기 말</span></span>
<div class="card-sec">
%(ch8)s
<div class="tw"><table class="data eq"><thead><tr><th>기준</th><th class="num">현금·단기투자</th><th class="num">차입금</th><th class="num">순현금</th><th class="num">발행주식 (백만주)</th><th class="num">설비투자</th></tr></thead><tbody>%(crow8)s</tbody></table></div>
<p class="ana" style="margin-top:10px">%(cnote)s</p>
<div class="note">차입금은 장부가. 분기 설비투자는 누계 차이로 계산.<br>Source: MP 실적 발표자료(8-K)의 재무상태표·현금흐름표</div>
</div>

<span class="banner">⑨ 중희토류 가격: 디스프로슘·터븀<span class="u">달러/kg · 부가세 제외 · 일간</span></span>
<div class="card-sec">
<div class="sub">디스프로슘 산화물</div>
%(ch9a)s
<div class="sub">터븀 산화물</div>
%(ch9b)s
<div class="note">MP는 26.4Q부터 마운틴패스에서 Dy·Tb를 상업 생산. 중국의 25년 4월 수출허가 규제 대상 품목.
%(ld0)s 이전은 주요 시점의 SMM 위안화 수치를 부가세 제외 달러로 바꾼 값이고, 그 뒤로는 SMM 달러 호가를 매일 한 점씩 쌓음.<br>
Source: <a href="https://www.metal.com/rare-earth-oxides/201102250247" target="_blank" rel="noopener">SMM 디스프로슘 산화물</a>, <a href="https://www.metal.com/rare-earth-oxides/201102250488" target="_blank" rel="noopener">SMM 터븀 산화물</a></div>
</div>

<span class="banner">⑩ 케파와 가동 일정<span class="u">이벤트 발생 시 갱신</span></span>
<div class="card-sec">
<div class="tw"><table class="data"><thead><tr><th>설비</th><th>케파</th><th>현재</th><th>다음 확인</th></tr></thead><tbody>%(caprow)s</tbody></table></div>
<div class="note">Source: %(capsrc)s</div>
</div>

<footer>자동 수집 상태: %(stat)s<br>
데이터: SMM(NdPr 시세) · 야후 파이낸스(주가·환율) · SEC EDGAR(Form 4·8-K·10-Q) · MP 실적 발표자료 · 중국 상무부·USTR·미 연방관보(규제)<br>
개인 리서치 기록이며 투자 권유가 아님</footer>
</div></body></html>""" % {
        "css": CSS, "today": kd(today.isoformat()), "warnbox": warnbox, "kp": kp, "now": "".join("<li>%s</li>" % x for x in now),
        "need": format(round(need, -3), ",.0f"), "rtitle": man["realization"]["title"], "rlines": "<br>\n".join(man["realization"]["lines"]),
        "iotitle": man["inout"]["title"], "iolines": "<br>\n".join(man["inout"]["lines"]), "psrc": links(man["price_src"]),
        "ch1": ch1, "ld0": kd(man["ndpr_seed_until"]), "smm": SMM_URL, "sens": sens, "cap": format(man["capacity_ndpr_t"], ","),
        "ch2a": ch2a, "ch2b": ch2b, "qnote": man["quarter_note"], "qrows": qrows,
        "ctl": ctl, "up": up, "tl": tl, "tlv": man["timeline_view"], "tlvl": links(man["timeline_view_links"]),
        "ch4": ch4, "yr": yr[2:], "prow": prow, "thead": thead, "trs": trs,
        "more": ('<details class="more"><summary>이전 거래 %d건 더 보기</summary><div class="tw"><table class="data">%s<tbody>%s</tbody></table></div></details>' % (len(gt) - 12, thead, trs_more)) if trs_more else "",
        "lpd": kd(lpd), "lpx": lpx, "erow": erow, "crow": crow, "ch5": ch5,
        "consnote": ('<p class="ana" style="margin-top:8px"><span class="mark">%s</span></p>' % cons_note) if cons_note and not cons_note.endswith(": ") else "",
        "mcap": format(mcap, ",.0f"), "debt": format(b["debt"], ",.0f"), "cash": format(b["cash"], ",.0f"), "ev": format(ev, ",.0f"),
        "ch6": ch6, "lq": lq["q"], "util": util, "vnote": man["volume_note"], "ch7": ch7, "srow": srow,
        "ch8": ch8, "crow8": crow8, "cnote": man["cash_note"], "ch9a": ch9a, "ch9b": ch9b, "caprow": caprow,
        "capsrc": links(man["capacity_src"]),
        "bal": kd(b["asOf"]), "sh": b["shares"], "refsrc": man["multiple_refs_src"], "stat": stat,
    }
    # 섹션 순서는 manual.json의 section_order(템플릿에 적힌 순서 기준 인덱스)로 바꾸고 번호를 다시 매김
    order = man.get("section_order")
    if order:
        head, rest = html.split('<span class="banner">', 1)
        body, foot = ('<span class="banner">' + rest).split("<footer>", 1)
        secs = [x for x in re.split(r'(?=<span class="banner">)', body) if x.strip()]
        if sorted(order) == list(range(len(secs))):
            nums = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
            body = "".join(re.sub(r'(<span class="banner">)[①-⑳]', lambda m_, n=n: m_.group(1) + nums[n], secs[i], count=1)
                           for n, i in enumerate(order))
            html = head + body + "<footer>" + foot
    return html


def main():
    cache = load(CACHE, {})
    for k, v in (("ndpr", {}), ("fx", {}), ("px", {}), ("trades", []), ("f4_seen", []), ("log", {}), ("alerts", {})):
        cache.setdefault(k, v)
    man = load(MANUAL, None)
    if man is None:
        print("manual.json을 읽지 못함")
        return 1
    if "--no-fetch" not in sys.argv:
        print("수집 시작")
        collect_all(cache, man)
        cache["log"] = {k: v for k, v in cache["log"].items() if k in ("ndpr", "fx", "px", "trades", "aux", "earn")}
        cache["updatedAt"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        save(CACHE, cache)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_html(cache, man))
    print("index.html 생성 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
