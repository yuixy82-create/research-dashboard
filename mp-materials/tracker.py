#!/usr/bin/env python3
"""MP 머티리얼즈 트래커: 수집 + index.html 생성.

  manual.json  손으로 고치는 값 (분기 실적·추정치·타임라인). 봇은 건드리지 않음
  cache.json   자동 수집 누적 (NdPr 시세·환율·주가·내부자 거래)
  index.html   이 스크립트가 매번 새로 씀

자동 수집 4종
  ndpr   SMM(metal.com) Pr-Nd 산화물 일간 평균가, 위안/톤(부가세 13% 포함, 중국 도착도)
  fx     달러/위안 (frankfurter → 야후 CNY=X)
  px     MP 종가 (야후 → 나스닥)
  trades SEC EDGAR Form 4 (내부자 매수 P·매도 S)

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
SEC_UA = "research-dashboard yuixy82-create@users.noreply.github.com"
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


def collect_smm(cache):
    """SMM 페이지의 '... the average price of Pr-Nd oxide was 718,000 yuan/mt' 문장에서 평균가를 읽음."""
    t = get_or_reader(SMM_URL)
    m = re.search(r"average price[^\d]{0,60}?([\d,]{6,10})\s*yuan/mt", t)
    if not m:
        raise RuntimeError("평균가 문장을 못 찾음 (페이지 구조 변경 가능)")
    v = int(m.group(1).replace(",", ""))
    if not 200_000 <= v <= 2_500_000:
        raise RuntimeError("값 범위 이상: %s" % v)
    d = None
    m2 = re.search(r'renew_date\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})', t)
    if m2:
        d = m2.group(1)
    else:
        ctx = t[max(0, m.start() - 200):m.start()]
        m3 = re.search(r"On ([A-Z][a-z]+) (\d{1,2}), (\d{4})", ctx)
        if m3 and m3.group(1) in MON:
            d = "%s-%02d-%02d" % (m3.group(3), MON[m3.group(1)], int(m3.group(2)))
    if not d:
        raise RuntimeError("날짜를 못 찾음")
    cache["ndpr"][d] = v
    return "%s %s 위안/톤" % (d, format(v, ","))


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


def trade_key(t):
    return "|".join(str(t[k]) for k in ("date", "name", "code", "shares", "price"))


def collect_form4(cache, since="2026-01-01"):
    hdr = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}
    j = json.loads(http_get("https://data.sec.gov/submissions/CIK%s.json" % CIK.zfill(10), hdr))
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
                     "di": _txt(tx, ".//directOrIndirectOwnership/value"), "plan": plan}
            except ValueError:
                continue
            if trade_key(t) not in keys:
                cache["trades"].append(t)
                keys.add(trade_key(t))
                new += 1
        seen.add(acc)
    cache["f4_seen"] = sorted(seen)
    return "신규 거래 %d건, 확인한 신고서 %d건" % (new, len(seen))


def collect_all(cache):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for name, fn in (("ndpr", collect_smm), ("fx", collect_fx), ("px", collect_px), ("trades", collect_form4)):
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
        k = (t["date"], t["name"], t["code"])
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
    ld, lv = max(nd), nd[max(nd)]
    lin, lex = usd_in[-1][1], usd_ex[-1][1]
    qs = man["quarters"]
    for q in qs:
        q["real"] = q["ndpr_rev"] * 1000 / q["sales"]
    qdots = [{"d": q["end"], "v": q["real"], "r": 4.5, "color": T["gold"], "ring": True,
              "title": "%s MP 실현가 $%.1f/kg (NdPr 매출 ÷ 판매량)" % (q["q"], q["real"])} for q in qs]
    ch1 = svg_line(
        [{"label": "부가세 포함", "color": T["primary"], "points": usd_in, "markers": True,
          "end": "하한선 대비 %+.1f%%" % ((lin / floor - 1) * 100)},
         {"label": "부가세 제외", "color": T["teal"], "points": usd_ex, "dash": True, "width": 1.8,
          "end": "하한선 대비 %+.1f%%" % ((lex / floor - 1) * 100)}],
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
        qrows += ('<tr%s><td>%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%.1f</td><td class="num">$%.1f</td>'
                  '<td class="num">%s</td><td class="num">%.1f</td><td class="num %s">%.1f</td></tr>'
                  % (' class="hl"' if q is qs[-1] else "", q["q"], format(q["prod"], ","), format(q["sales"], ","), q["ndpr_rev"], q["real"],
                     ("%.1f" % q["ppa"]) if q["ppa"] else "0", q["rev"], "pos" if q["ebitda"] > 0 else "neg", q["ebitda"]))
    lq = qs[-1]
    ppa_first = next(q for q in qs if q["ppa"])

    # ---------- ③ 타임라인 ----------
    end = date.fromisoformat(man["truce_end"])
    dleft = (end - today).days
    ctl = "".join('<tr><td><b>%s</b></td><td>%s</td><td>%s<div class="lk">%s</div></td></tr>' % (c["name"], c["items"], c["status"], links(c["links"])) for c in man["controls"])
    up = "".join('<tr%s><td style="white-space:nowrap"><b>%s</b></td><td>%s</td><td>%s</td></tr>'
                 % (' class="hl"' if u["date"] == man["truce_end"] else "", u["when"], u["event"], u["check"])
                 for u in man["upcoming"] if u["date"] >= today.isoformat())
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
    nm = lambda n: people.get(n, n)
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
            x = per.setdefault(g["name"], {"P": 0.0, "S": 0.0, "Ps": 0, "Ss": 0, "S_noplan": 0.0})
            x[g["code"]] += g["amt"]
            x[g["code"] + "s"] += g["shares"]
            if g["code"] == "S" and not g["plan"]:
                x["S_noplan"] += g["amt"]
    prow = ""
    for n, x in sorted(per.items(), key=lambda kv: -(kv[1]["P"] + kv[1]["S"])):
        prow += ('<tr><td>%s</td><td class="num pos">%s</td><td class="num neg">%s</td><td class="num">%s</td></tr>'
                 % (nm(n), money(x["P"]) if x["P"] else "", money(x["S"]) if x["S"] else "",
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
    ceo = per.get("Litinsky James H.", {"S": 0, "Ss": 0, "P": 0})

    # ---------- ⑤ 밸류 ----------
    mcap, ev = ev_at(man, lpd, lpx)
    b = bal_at(man, lpd)
    ests = man["estimates"]
    years = ["2026", "2027", "2028"]
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
        pts = [(d, ev_at(man, d, p)[1] / e["ebitda"]["2027"]) for d, p in vline]
        vs.append({"label": e["name"].split(" (")[0], "color": T[e.get("color", "primary")], "points": pts, "width": 2.0})
    ch5 = svg_line(vs, hlines=[(r["x"], r["label"], T["faint"]) for r in man["multiple_refs"]],
                   yfmt=lambda v: "%.1f배" % v, tfmt=lambda v: "%.0f배" % v, ymin=10, mr=215)
    cons = next((e for e in ests if e["id"] == "consensus"), ests[-1])
    barb = next((e for e in ests if e["id"] == "barbarian"), ests[0])

    # ---------- 0. 헤더·KPI·지금 상태 ----------
    kp = "".join([
        kpi("NdPr 산화물 (SMM)", "%.1f" % (lv / 10000), "만 위안/톤", "%s · 부가세 포함" % kd(ld)),
        kpi("달러 환산 (부가세 포함)", "$%.1f" % lin, "/kg", "하한선 110불 대비 %+.1f%%" % ((lin / floor - 1) * 100)),
        kpi("달러 환산 (부가세 제외)", "$%.1f" % lex, "/kg", "하한선 110불 대비 %+.1f%%" % ((lex / floor - 1) * 100)),
        kpi("MP 분기 실현가", "$%.1f" % lq["real"], "/kg", "%s · NdPr 매출 ÷ 판매량" % lq["q"]),
        kpi("중국 규제 유예 만료", "D-%d" % dleft if dleft >= 0 else "만료", "", kd(man["truce_end"])),
        kpi("내부자 최근 30일", ("+%s" % money(buy30 - sell30)) if buy30 >= sell30 else ("−%s" % money(sell30 - buy30)), "", "순매수 (매수 − 매도)"),
        kpi("MP 주가", "$%.2f" % lpx, "", "%s · 시총 %.1f억불" % (kd(lpd), mcap / 100)),
        kpi("EV/EBITDA 27년", "%.1f" % (ev / cons["ebitda"]["2027"]), "배", "컨센 기준 · 바바리안 %.1f배" % (ev / barb["ebitda"]["2027"])),
    ])
    now = [
        "<b>NdPr 시세</b>: SMM %s 위안/톤(%s). 달러로 바꾸면 부가세 포함 %.1f불, 제외 %.1f불로 %s<br>MP는 110불 미만에서는 국방부 보전으로 110불을 받고, 110불을 넘으면 초과분을 전부 가져감 (10X 공장 풀가동 전까지)"
        % (format(lv, ","), kd(ld), lin, lex,
           "둘 다 국방부 하한선 %d불 아래" % floor if lin < floor else
           ("부가세 포함 기준으로는 하한선 %d불 위, 제외 기준으로는 아래" % floor if lex < floor else "둘 다 국방부 하한선 %d불 위" % floor)),
        "<b>분기 실현가</b>: %s 약 %.0f불. 국방부 보전금(PPA 수익)은 %s %.1f백만불에서 %s %.1f백만불로 감소"
        % (lq["q"], lq["real"], ppa_first["q"], ppa_first["ppa"], lq["q"], lq["ppa"]),
        "<b>미중 유예</b>: 중국의 25년 10월 추가 규제 유예가 %s 만료 (D-%d)" % (kd(man["truce_end"]), dleft),
        "<b>내부자</b>: 최근 30일 매수 %s, 매도 %s. %s년 누적으로는 CEO가 %s 매도"
        % (money(buy30) if buy30 else "없음", money(sell30) if sell30 else "없음", yr[2:], money(ceo["S"]) if ceo["S"] else "없음"),
        "<b>밸류</b>: 시총 %.1f억불, EV %.1f억불. 27년 추정 EBITDA 기준 EV/EBITDA는 컨센 %.1f배, 바바리안 추정 %.1f배"
        % (mcap / 100, ev / 100, ev / cons["ebitda"]["2027"], ev / barb["ebitda"]["2027"]),
    ]
    log = cache.get("log", {})
    lab = {"ndpr": "NdPr 시세", "fx": "환율", "px": "주가", "trades": "내부자 거래"}
    stat = " · ".join("%s %s" % (lab[k], ("정상 " + v["at"][5:]) if v.get("ok") else ("실패 " + v["at"][5:]))
                      for k, v in log.items() if k in lab) or "첫 자동 수집 전"

    html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MP 머티리얼즈 트래커</title><style>%(css)s</style></head><body>
<header class="hero"><div class="hero-in"><a class="back" href="../">← 리서치 대시보드</a>
<span class="stripes"><i></i><i></i><i></i></span>
<div><h1>MP 머티리얼즈 트래커</h1><div class="sub">미국 희토류(NYSE: MP) · NdPr 가격과 국방부 하한선, 미중 규제 유예, 내부자 매매, 밸류 · 기준 %(today)s · 매일 07:35 / 18:40 KST 자동 수집</div></div>
</div></header>
<div class="wrap">
<div class="kpi-row">%(kp)s</div>
<div class="thesis"><div class="thesis-title">지금 상태</div><ul>%(now)s</ul></div>

<span class="banner">① NdPr 가격 vs 국방부 하한선 110불<span class="u">달러/kg · 일간</span></span>
<div class="card-sec">
%(ch1)s
<div class="legend"><i style="background:#4A5228"></i>SMM 시세 달러 환산 (부가세 13%% 포함)<i style="background:#2E97A7"></i>부가세 제외<i style="background:#fff;border:2px solid #D9A62E"></i>MP 분기 실현가 (분기 말 위치)</div>
<div class="note">SMM Pr-Nd 산화물 평균가(위안/톤, 중국 도착도, 부가세 포함)를 그날 달러/위안 환율로 나눈 값. 계약서에 부가세 처리 문구가 없어 두 선을 함께 표시.
%(ld0)s 이전은 주요 시점의 SMM 공개 수치만 이은 선이고, 그 뒤로 매일 한 점씩 쌓임. 점에 마우스를 올리면 날짜와 값 표시.<br>
Source: <a href="%(smm)s" target="_blank" rel="noopener">SMM</a>, 야후 파이낸스(환율), MP 실적 발표자료</div>

<div class="sub">하한선 정산 방식</div>
<p class="ana"><span class="mark">국방부는 MP의 NdPr 가격이 kg당 110불에 못 미치면 차액을 분기마다 현금으로 보전 (25.10.01부터 10년)</span><br>
외부에 판 물량은 MP의 실제 판매가 기준, 재고로 쌓거나 자석 부문이 쓴 물량은 아시안메탈의 중국 EXW(공장 인도) NdPr 산화물 시세 기준으로 정산<br>
110불을 넘는 분기에는 초과분의 30%%를 MP가 국방부에 지급. 단 10X 공장(텍사스 제2 자석 공장, 28년 말 시운전)이 목표 케파에 도달한 뒤부터 적용</p>
<div class="tw"><table class="data"><thead><tr><th class="num">NdPr 가격</th><th class="num">MP 수취가<br>(10X 풀가동 전)</th><th class="num">110불 대비 연 매출 증가</th><th class="num">MP 수취가<br>(10X 풀가동 후)</th><th class="num">110불 대비 연 매출 증가</th></tr></thead><tbody>%(sens)s</tbody></table></div>
<div class="note">연 매출 증가 = (수취가 − 110불) × 마운틴패스 NdPr 목표 케파 %(cap)s톤. 계산치.<br>
Source: <a href="https://www.sec.gov/Archives/edgar/data/1801368/000119312525157310/d43796dex106.htm" target="_blank" rel="noopener">가격보전계약(PPA) 원문</a>, <a href="https://s25.q4cdn.com/570172628/files/doc_events/2025/Jul/10/FINAL-Transformational-Public-Private-Partnership-7-9-25-440pm.pdf" target="_blank" rel="noopener">국방부 파트너십 발표자료(25.07.10)</a></div>
</div>

<span class="banner">② 분기 실현가와 국방부 보전금<span class="u">분기 · 실적 발표 때 갱신</span></span>
<div class="card-sec">
<div class="sub">NdPr 실현가 <span style="font-weight:400;font-size:12px;color:#9B9B9B">달러/kg · NdPr 산화물·금속 매출 ÷ 판매량</span></div>
%(ch2a)s
<div class="sub">국방부 보전금 (PPA 수익) <span style="font-weight:400;font-size:12px;color:#9B9B9B">백만불</span></div>
%(ch2b)s
<p class="ana">%(qnote)s</p>
<div class="tw"><table class="data"><thead><tr><th>분기</th><th class="num">NdPr 생산(톤)</th><th class="num">NdPr 판매(톤)</th><th class="num">NdPr 매출</th><th class="num">실현가</th><th class="num">PPA 수익</th><th class="num">전체 매출</th><th class="num">조정 EBITDA</th></tr></thead><tbody>%(qrows)s</tbody></table></div>
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
<div class="tw"><table class="data"><thead><tr><th>이름</th><th class="num">매수</th><th class="num">매도</th><th class="num">순매수</th></tr></thead><tbody>%(prow)s</tbody></table></div>
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
<div class="sub">EV/EBITDA 추이 <span style="font-weight:400;font-size:12px;color:#9B9B9B">27년 추정 EBITDA 기준 · 최근 1년</span></div>
%(ch5)s
<div class="note">EV = 시총 + 차입금 − 현금·단기투자 = %(mcap)s + %(debt)s − %(cash)s = %(ev)s백만불 (재무제표 %(bal)s 기준). 시총은 발행주식 %(sh).1f백만주 기준으로, 국방부 전환우선주와 워런트는 미포함.<br>
추이 선은 추정치를 현재 값으로 고정하고 주가와 순현금만 날짜별로 반영. 추정치가 바뀌면 그날부터 새 값 적용. %(refsrc)s.<br>
Source: 야후 파이낸스(주가), MP 10-Q, 표의 각 출처</div>
</div>

<footer>자동 수집 상태: %(stat)s<br>
데이터: SMM(NdPr 시세) · 야후 파이낸스(주가·환율) · SEC EDGAR(Form 4·8-K·10-Q) · MP 실적 발표자료 · 중국 상무부·USTR·미 연방관보(규제)<br>
개인 리서치 기록이며 투자 권유가 아님</footer>
</div></body></html>""" % {
        "css": CSS, "today": kd(today.isoformat()), "kp": kp, "now": "".join("<li>%s</li>" % x for x in now),
        "ch1": ch1, "ld0": kd(man["ndpr_seed_until"]), "smm": SMM_URL, "sens": sens, "cap": format(man["capacity_ndpr_t"], ","),
        "ch2a": ch2a, "ch2b": ch2b, "qnote": man["quarter_note"], "qrows": qrows,
        "ctl": ctl, "up": up, "tl": tl, "tlv": man["timeline_view"], "tlvl": links(man["timeline_view_links"]),
        "ch4": ch4, "yr": yr[2:], "prow": prow, "thead": thead, "trs": trs,
        "more": ('<details class="more"><summary>이전 거래 %d건 더 보기</summary><div class="tw"><table class="data">%s<tbody>%s</tbody></table></div></details>' % (len(gt) - 12, thead, trs_more)) if trs_more else "",
        "lpd": kd(lpd), "lpx": lpx, "erow": erow, "crow": crow, "ch5": ch5,
        "mcap": format(mcap, ",.0f"), "debt": format(b["debt"], ",.0f"), "cash": format(b["cash"], ",.0f"), "ev": format(ev, ",.0f"),
        "bal": kd(b["asOf"]), "sh": b["shares"], "refsrc": man["multiple_refs_src"], "stat": stat,
    }
    return html


def main():
    cache = load(CACHE, {})
    for k, v in (("ndpr", {}), ("fx", {}), ("px", {}), ("trades", []), ("f4_seen", []), ("log", {})):
        cache.setdefault(k, v)
    man = load(MANUAL, None)
    if man is None:
        print("manual.json을 읽지 못함")
        return 1
    if "--no-fetch" not in sys.argv:
        print("수집 시작")
        collect_all(cache)
        cache["updatedAt"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        save(CACHE, cache)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_html(cache, man))
    print("index.html 생성 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
