# -*- coding: utf-8 -*-


"""AI 클라우드 임대가격 트래커 — 수집부터 화면 생성까지 이 파일 하나가 전부 한다.

실행:  python gpu-cloud/tracker.py
결과:  gpu-cloud/index.html (대시보드) + gpu-cloud/cache.json (수집 기록 누적, 자동 생성)
외부 라이브러리 설치 불필요 — 파이썬 기본 기능만 사용.

[사람이 손대는 곳은 아래 두 상수뿐]
  SEED     : 블룸버그 지수처럼 자동 수집이 안 되는 값을 손으로 적어두는 곳
  ANALYSIS : 대시보드 맨 아래 '해석' 문단
"""


import csv
import html as htmlmod
import io
import json
import os
import re
import time
import urllib.request
from datetime import datetime, date, timezone, timedelta
from html.parser import HTMLParser


ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "index.html")
CACHE_FILE = os.path.join(ROOT, "cache.json")


# ============================================================
# 1. 수동 기록 데이터 — Bloomberg 등 자동 수집 불가 소스
#    PDF를 주시면 Claude가 points 에 [날짜, 값] 을 추가한다
# ============================================================


SEED = {
    "manual_sdh100rt": {
        "label": "SDH100RT (Silicon Data H100 임대 지수, $/GPU-시간)",
        "source": "Bloomberg, 사용자 제공 PDF (26.08.18)",
        "points": [
            [
                "2025-12-26",
                1.96
            ],
            [
                "2026-08-10",
                2.8
            ],
            [
                "2026-08-17",
                2.76
            ]
        ],
        "note": "25.12.26 저점 1.96 → 26.08.10 고점 2.80 (차트 범례 판독값, 잠정)"
    },
    "manual_blackwell": {
        "label": "Blackwell 임대 지수 ($/GPU-시간)",
        "source": "Bloomberg, 사용자 제공 PDF (26.08.18)",
        "points": [
            [
                "2026-08-17",
                5.2565
            ]
        ],
        "note": "잠정 판독값"
    },
    "manual_cds": {
        "label": "CoreWeave CDS 5Y (bp)",
        "source": "Bloomberg, 사용자 제공 PDF (26.08.18)",
        "points": [
            [
                "2026-08-17",
                738.8
            ]
        ],
        "note": "26.07월 말 ~970bp까지 급등 후 반락 (차트 판독, 잠정)"
    }
}


ANALYSIS = {
    "updated": "2026-08-18",
    "thesis": "클라우드 임대 가격이 유지되는 한 자금은 계속 들어온다 — 무너지면 담보 구조 전체가 흔들린다",
    "paragraphs": [
        "이번 사이클의 AI 인프라 자금 조달은 데이터센터가 만들어낼 <b>클라우드 임대 현금흐름을 담보로 한 대출</b>이 핵심이다: GPU 수명이 짧아 실물 담보 가치는 제한적이고, 결국 <b>임대 가격 × 가동률 × 고객 신용도</b>가 대출의 전부를 결정함",
        "H100 임대가는 25.12월 저점($1.96) 이후 <b>26.08월 $2.76까지 약 +40% 회복</b>: 임대가 상승 구간에서는 담보가치가 유지되어 보험·연금발 자금 유입이 지속될 수 있는 환경임",
        "코어위브 CDS(신용부도스와프, 부도 위험 보험료)는 26.07월 말 <b>~970bp까지 급등 후 ~740bp로 반락</b>: 임대가가 견조한데도 CDS가 급등했던 것은 개별 기업 레버리지 이슈 — 임대가 하락과 CDS 급등이 <b>동시에</b> 나타나면 시스템 리스크 신호로 해석해야 함",
        "★ 체크포인트: ① H100 임대가가 $2.5 아래로 꺾이는지 ② Blackwell/GB300 임대가가 신규 공급에도 $5 이상을 유지하는지 ③ CDS 재차 900bp 돌파 여부"
    ]
}


# ============================================================
# 2. 공용 유틸
# ============================================================


KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def http_get(url: str, retries: int = 2, timeout: int = 20, headers: dict | None = None) -> str | None:
    """GET → 본문 텍스트. 실패 시 None (부분 실패 허용 원칙)."""
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [warn] GET {url} 시도 {i+1}/{retries} 실패: {e}")
            time.sleep(2 * (i + 1))
    return None


def http_post_json(url: str, payload: dict, retries: int = 2, timeout: int = 20) -> dict | None:
    body = json.dumps(payload).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"User-Agent": UA, "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print(f"  [warn] POST {url} 시도 {i+1}/{retries} 실패: {e}")
            time.sleep(2 * (i + 1))
    return None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    m = n // 2
    return round(xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2, 4)


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


# ============================================================
# 3-A. 수집 — getdeploying.com (75개 클라우드 제공업체 가격 집계)
# ============================================================


BASE = "https://getdeploying.com"
# 추적 대상 모델: 표시명 → 상세 페이지 slug
MODELS = {
    "H100": "nvidia-h100",
    "H200": "nvidia-h200",
    "B200": "nvidia-b200",
    "B300": "nvidia-b300",
    "GB200": "nvidia-gb200",
    "GB300": "nvidia-gb300",
}


class TableExtractor(HTMLParser):
    """HTML 내 모든 <table>을 [ [행[셀텍스트,...]], ... ] 로 뽑는다."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._t = None   # current table rows
        self._r = None   # current row cells
        self._c = None   # current cell text parts
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._t = []
        elif self._depth >= 1 and tag == "tr":
            self._r = []
        elif self._depth >= 1 and tag in ("td", "th"):
            self._c = []

    def handle_endtag(self, tag):
        if tag == "table" and self._depth >= 1:
            self._depth -= 1
            if self._depth == 0 and self._t is not None:
                self.tables.append(self._t)
                self._t = None
        elif tag in ("td", "th") and self._c is not None and self._r is not None:
            self._r.append(re.sub(r"\s+", " ", " ".join(self._c)).strip())
            self._c = None
        elif tag == "tr" and self._r is not None and self._t is not None:
            if self._r:
                self._t.append(self._r)
            self._r = None

    def handle_data(self, data):
        if self._c is not None:
            self._c.append(data)


def parse_tables(html: str):
    p = TableExtractor()
    p.feed(html)
    return p.tables


def money(cell: str):
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", cell)
    return float(m.group(1).replace(",", "")) if m else None


def find_col(header, *keywords):
    for i, h in enumerate(header):
        hl = h.lower()
        if all(k.lower() in hl for k in keywords):
            return i
    return None


def collect_overview():
    """모델별 중위가 개요 (/gpus)."""
    html = http_get(f"{BASE}/gpus")
    if not html:
        return {}
    out = {}
    for table in parse_tables(html):
        if not table or len(table) < 2:
            continue
        header = table[0]
        med_i = find_col(header, "median")
        if med_i is None:
            continue
        for row in table[1:]:
            if len(row) <= med_i:
                continue
            name = row[0]
            price = money(row[med_i])
            if price is None:
                continue
            for model in MODELS:
                # "Nvidia H100" 매칭 (H100 NVL 등 변형은 별도 행이므로 정확 일치 지향)
                if re.search(rf"\b{model}\b", name, re.I) and "nvl" not in name.lower():
                    out.setdefault(model, price)
    return out


def collect_model_detail(model: str, slug: str):
    """모델 상세 페이지 → 온디맨드 제공업체별 $/GPU/h."""
    html = http_get(f"{BASE}/gpus/{slug}")
    if not html:
        return None
    best = None
    for table in parse_tables(html):
        if not table or len(table) < 2:
            continue
        header = table[0]
        price_i = find_col(header, "/gpu")  # "$/GPU/h" 컬럼
        if price_i is None:
            continue
        prov_i = 0
        bill_i = find_col(header, "billing")
        rows = []
        for row in table[1:]:
            if len(row) <= price_i:
                continue
            price = money(row[price_i])
            if price is None or price <= 0:
                continue
            billing = row[bill_i] if bill_i is not None and len(row) > bill_i else ""
            rows.append({"provider": row[prov_i][:40], "billing": billing, "usd_hr": price})
        if rows and (best is None or len(rows) > len(best)):
            best = rows
    if not best:
        return None
    ondemand = [r["usd_hr"] for r in best
                if not re.search(r"spot|reserved|month", r["billing"], re.I)]
    prices = ondemand or [r["usd_hr"] for r in best]
    return {
        "min": min(prices),
        "median": median(prices),
        "n_offers": len(best),
        "providers": sorted(best, key=lambda r: r["usd_hr"])[:12],
    }


def collect_getdeploying():
    result = {"models": {}, "overview_median": collect_overview()}
    for model, slug in MODELS.items():
        d = collect_model_detail(model, slug)
        if d:
            result["models"][model] = d
            print(f"  getdeploying {model}: min ${d['min']}, median ${d['median']} ({d['n_offers']} offers)")
        else:
            print(f"  [warn] getdeploying {model}: 수집 실패")
    return result


# ============================================================
# 3-B. 수집 — Vast.ai (GPU 개인간 장터 공개 API)
# ============================================================


VAST_API = "https://console.vast.ai/api/v0/bundles/"

# 표시명 → vast.ai gpu_name 후보들
GPU_NAMES = {
    "H100": ["H100 SXM", "H100 NVL", "H100 PCIE"],
    "H200": ["H200", "H200 NVL"],
    "B200": ["B200"],
}


def query(gpu_name: str):
    payload = {
        "gpu_name": {"eq": gpu_name},
        "rentable": {"eq": True},
        "external": {"eq": False},
        "verified": {"eq": True},
        "type": "ask",
        "limit": 300,
        "order": [["dph_total", "asc"]],
    }
    res = http_post_json(VAST_API, payload)
    if not res or "offers" not in res:
        return []
    out = []
    for o in res["offers"]:
        try:
            n = max(int(o.get("num_gpus") or 1), 1)
            dph = float(o.get("dph_total"))
            if dph > 0:
                out.append(round(dph / n, 4))
        except (TypeError, ValueError):
            continue
    return out


def collect_vastai():
    result = {}
    for model, names in GPU_NAMES.items():
        prices = []
        for name in names:
            prices += query(name)
        if prices:
            result[model] = {"median": median(prices), "min": min(prices), "n": len(prices)}
            print(f"  vast.ai {model}: median ${result[model]['median']} (n={len(prices)})")
        else:
            print(f"  [warn] vast.ai {model}: 매물 없음/수집 실패")
    return result


# ============================================================
# 3-C. 수집 — stooq.com (네오클라우드 주가: 신용 스트레스 프록시)
# ============================================================


# 티커 → 표시명
TICKERS = {
    "crwv.us": "CoreWeave(코어위브)",
    "nbis.us": "Nebius(네비우스)",
    "iren.us": "IREN(아이렌)",
    "apld.us": "Applied Digital(어플라이드 디지털)",
}


def collect_stocks():
    out = {}
    for tk, name in TICKERS.items():
        url = f"https://stooq.com/q/l/?s={tk}&f=sd2t2ohlcv&h&e=csv"
        text = http_get(url)
        if not text:
            print(f"  [warn] stooq {tk}: 실패")
            continue
        try:
            row = next(csv.DictReader(io.StringIO(text)))
            close = float(row["Close"])
            out[tk.split(".")[0].upper()] = {
                "name": name, "close": close, "date": row["Date"],
            }
            print(f"  {tk}: {close} ({row['Date']})")
        except Exception as e:
            print(f"  [warn] stooq {tk} 파싱 실패: {e}")
    return out


# ============================================================
# 4. 대시보드 HTML 생성
# ============================================================




T = {  # design-skill tokens.json 발췌 — 임의 색 발명 금지
    "primary": "#4A5228", "heading": "#5F6C2C", "bright": "#819434",
    "vivid": "#A2B84A", "fillLight": "#E3E6D2", "bgTint": "#F3F4EA",
    "teal": "#2E97A7", "tealLight": "#81C0CB", "tealBg": "#E6F2F4",
    "red": "#B7332B", "redBg": "#FBEDEA", "gold": "#D9A62E",
    "text": "#3A3A3A", "muted": "#6E6E6E", "faint": "#9B9B9B",
    "hair": "#D8D8D8", "chartGray": "#C9C9C9", "peer": "#ABABAB",
}




def d2o(s):
    return date.fromisoformat(s).toordinal()


def fmt_date_kr(s):
    y, m, dd = s.split("-")
    return f"{y[2:]}.{m}.{dd}"


# ---------------- SVG 차트 ----------------
def svg_chart(series, w=640, h=280, yfmt=None, title_unit=""):
    """series: [{label,color,points:[(date,val)],dash,width}] — 주인공만 색, 직접 라벨."""
    yfmt = yfmt or (lambda v: f"${v:,.2f}")
    series = [s for s in series if s.get("points")]
    if not series:
        return f'<div style="color:{T["faint"]};font-size:12px;padding:30px">데이터 수집 대기 중 — 첫 자동 수집 후 차트가 그려집니다</div>'
    ml, mr, mt, mb = 52, 118, 14, 26
    pw, ph = w - ml - mr, h - mt - mb
    xs = [d2o(p[0]) for s in series for p in s["points"]]
    ys = [p[1] for s in series for p in s["points"]]
    x0, x1 = min(xs), max(xs)
    if x0 == x1:
        x0, x1 = x0 - 1, x1 + 1
    pad = (max(ys) - min(ys)) * 0.15 or max(ys) * 0.1 or 1
    y0, y1 = min(ys) - pad, max(ys) + pad
    if y0 > 0 and y0 < (max(ys) - min(ys)):
        y0 = 0  # 낮은 저점이면 0부터

    def X(o):
        return ml + (o - x0) / (x1 - x0) * pw

    def Y(v):
        return mt + (1 - (v - y0) / (y1 - y0)) * ph

    out = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
           f'style="width:100%;height:auto;font-family:inherit">']
    # 수평 기준선 3개 + y 라벨 (hairline, muted 9px)
    for i in range(4):
        v = y0 + (y1 - y0) * i / 3
        yy = Y(v)
        out.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{w-mr}" y2="{yy:.1f}" '
                   f'stroke="{T["hair"]}" stroke-width="{1 if i==0 else 0.6}"/>')
        out.append(f'<text x="{ml-6}" y="{yy+3:.1f}" text-anchor="end" '
                   f'font-size="9.5" fill="{T["muted"]}">{yfmt(v)}</text>')
    # x 라벨 4개 (기간이 짧으면 일 단위 표기)
    span = x1 - x0
    xfmt = "%y.%m" if span > 70 else "%m.%d"
    n_lab = min(5, max(3, span // 30 + 2))
    for i in range(int(n_lab)):
        o = x0 + span * i / (n_lab - 1 if n_lab > 1 else 1)
        dt = date.fromordinal(int(o))
        out.append(f'<text x="{X(o):.1f}" y="{h-8}" text-anchor="middle" '
                   f'font-size="9.5" fill="{T["muted"]}">{dt.strftime(xfmt)}</text>')
    # 시리즈
    end_labels = []
    for s in series:
        pts = sorted(s["points"], key=lambda p: p[0])
        coords = [(X(d2o(p[0])), Y(p[1])) for p in pts]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        dash = ' stroke-dasharray="5,4"' if s.get("dash") else ""
        wd = s.get("width", 2.2)
        if len(coords) > 1:
            out.append(f'<polyline points="{path}" fill="none" stroke="{s["color"]}" '
                       f'stroke-width="{wd}"{dash} stroke-linejoin="round"/>')
        if len(coords) <= 8 or s.get("markers"):
            for x, y in coords:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{s["color"]}" '
                           f'stroke="#fff" stroke-width="1.2"/>')
        # 직접 라벨: 선 끝에 시리즈명 + 최종값 (겹침은 아래에서 조정)
        lx, ly = coords[-1]
        end_labels.append([lx + 7, ly + 3, s["color"],
                           f'{s["label"]} {yfmt(pts[-1][1])}'])
    # 끝 라벨 겹침 방지: y 기준 정렬 후 최소 13px 간격 확보
    end_labels.sort(key=lambda e: e[1])
    for i in range(1, len(end_labels)):
        if end_labels[i][1] - end_labels[i - 1][1] < 13:
            end_labels[i][1] = end_labels[i - 1][1] + 13
    for lx, ly, color, text in end_labels:
        out.append(f'<text x="{lx:.1f}" y="{min(ly, h-16):.1f}" font-size="10" '
                   f'font-weight="700" fill="{color}">{htmlmod.escape(text)}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------- 데이터 조립 ----------------
def series_from_cache(cache, path_fn):
    """daily 스냅샷에서 (date, value) 시계열 추출. path_fn(snap)->float|None"""
    pts = []
    for day in sorted(cache.get("daily", {})):
        try:
            v = path_fn(cache["daily"][day])
            if v is not None:
                pts.append((day, round(float(v), 4)))
        except Exception:
            continue
    return pts


def latest(cache, path_fn):
    pts = series_from_cache(cache, path_fn)
    if not pts:
        return None, None, None
    last = pts[-1]
    prev = pts[-2] if len(pts) > 1 else None
    delta = (last[1] - prev[1]) if prev else None
    return last[1], last[0], delta


def kpi(label, value, sub, delta=None, unit=""):
    dhtml = ""
    if delta is not None and delta != 0:
        cls = "pos" if delta > 0 else "neg"
        arrow = "▲" if delta > 0 else "▼"
        dhtml = f'<span class="{cls}" style="font-size:12px"> {arrow}{abs(delta):,.2f}</span>'
    vs = f"{value:,.2f}" if isinstance(value, (int, float)) else (value or "—")
    return (f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-num">{vs}<span class="kpi-unit">{unit}</span>{dhtml}</div>'
            f'<div class="kpi-sub">{sub}</div></div>')


def build_html():
    cache = load_json(CACHE_FILE, {"daily": {}})
    seed = SEED
    ana = ANALYSIS
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    days = sorted(cache.get("daily", {}).keys())
    n_days = len(days)

    def gd_med(model):
        return lambda s: (s.get("getdeploying", {}).get("models", {}).get(model, {}) or {}).get("median")

    def gd_min(model):
        return lambda s: (s.get("getdeploying", {}).get("models", {}).get(model, {}) or {}).get("min")

    def va_med(model):
        return lambda s: (s.get("vastai", {}).get(model, {}) or {}).get("median")

    def stock(tk):
        return lambda s: (s.get("stocks", {}).get(tk, {}) or {}).get("close")

    sd_pts = [(p[0], p[1]) for p in seed.get("manual_sdh100rt", {}).get("points", [])]
    bw_pts = [(p[0], p[1]) for p in seed.get("manual_blackwell", {}).get("points", [])]
    cds_pts = [(p[0], p[1]) for p in seed.get("manual_cds", {}).get("points", [])]

    # ---- KPI ----
    h100_v, h100_d, h100_del = latest(cache, gd_med("H100"))
    gb300_v, gb300_d, gb300_del = latest(cache, gd_med("GB300"))
    b200_v, b200_d, b200_del = latest(cache, gd_med("B200"))
    crwv_v, crwv_d, crwv_del = latest(cache, stock("CRWV"))
    sd_last = sd_pts[-1] if sd_pts else None
    cds_last = cds_pts[-1] if cds_pts else None

    kpis = [
        kpi("H100 임대가 (중위)", h100_v, f"getdeploying, {fmt_date_kr(h100_d) if h100_d else '수집 대기'}", h100_del, "$/hr"),
        kpi("GB300 임대가 (중위)", gb300_v, f"getdeploying, {fmt_date_kr(gb300_d) if gb300_d else '수집 대기'}", gb300_del, "$/hr"),
        kpi("B200 임대가 (중위)", b200_v, f"getdeploying, {fmt_date_kr(b200_d) if b200_d else '수집 대기'}", b200_del, "$/hr"),
        kpi("SDH100RT 정식 지수", sd_last[1] if sd_last else None,
            f"Bloomberg 수동, {fmt_date_kr(sd_last[0]) if sd_last else '—'}", None, "$/hr"),
        kpi("CoreWeave 주가", crwv_v, f"stooq, {fmt_date_kr(crwv_d) if crwv_d else '수집 대기'}", crwv_del, "$"),
        kpi("CoreWeave CDS 5Y", cds_last[1] if cds_last else None,
            f"Bloomberg 수동, {fmt_date_kr(cds_last[0]) if cds_last else '—'}", None, "bp"),
    ]

    # ---- 차트 1: H100 ----
    c1 = svg_chart([
        {"label": "H100 중위가(자동)", "color": T["primary"],
         "points": series_from_cache(cache, gd_med("H100"))},
        {"label": "Vast.ai 호가", "color": T["chartGray"], "width": 1.4,
         "points": series_from_cache(cache, va_med("H100"))},
        {"label": "SDH100RT(수동)", "color": T["gold"], "dash": True, "markers": True,
         "points": sd_pts},
    ])

    # ---- 차트 2: 차세대 (Blackwell 세대) ----
    c2 = svg_chart([
        {"label": "GB300 중위가", "color": T["primary"],
         "points": series_from_cache(cache, gd_med("GB300"))},
        {"label": "B200 중위가", "color": T["tealLight"], "width": 1.6,
         "points": series_from_cache(cache, gd_med("B200"))},
        {"label": "GB200 중위가", "color": T["chartGray"], "width": 1.4,
         "points": series_from_cache(cache, gd_med("GB200"))},
        {"label": "블랙웰 지수(수동)", "color": T["gold"], "dash": True, "markers": True,
         "points": bw_pts},
    ])

    # ---- 차트 3: 신용 — 주가 리베이스 ----
    stock_series = []
    for tk, color, wd in [("CRWV", T["primary"], 2.2), ("NBIS", T["tealLight"], 1.6),
                          ("IREN", T["chartGray"], 1.4), ("APLD", T["peer"], 1.4)]:
        pts = series_from_cache(cache, stock(tk))
        if pts:
            base = pts[0][1]
            stock_series.append({"label": tk, "color": color, "width": wd,
                                 "points": [(d, round(v / base * 100, 2)) for d, v in pts]})
    c3 = svg_chart(stock_series, yfmt=lambda v: f"{v:,.0f}") if stock_series else svg_chart([])
    c4 = svg_chart([{"label": "CDS 5Y", "color": T["red"], "dash": True, "markers": True,
                     "points": cds_pts}], yfmt=lambda v: f"{v:,.0f}bp")

    # ---- 제공업체 테이블 ----
    prov_rows = []
    last_gd = None
    for day in reversed(days):
        if "getdeploying" in cache["daily"][day]:
            last_gd = (day, cache["daily"][day]["getdeploying"])
            break
    if last_gd:
        for model in ("H100", "B200", "GB300"):
            m = last_gd[1].get("models", {}).get(model)
            if not m:
                continue
            for i, p in enumerate(m.get("providers", [])[:5]):
                prov_rows.append(
                    f'<tr>{"<td rowspan=%d><b>%s</b></td>" % (min(5, len(m.get("providers", [])[:5])), model) if i == 0 else ""}'
                    f'<td>{htmlmod.escape(str(p.get("provider", "")))}</td>'
                    f'<td>{htmlmod.escape(str(p.get("billing", "")) or "On-demand")}</td>'
                    f'<td class="num">${p.get("usd_hr"):,.2f}</td></tr>')
    prov_table = (f'<table class="data"><thead><tr><th>GPU</th><th>제공업체</th>'
                  f'<th>과금</th><th class="num">$/GPU/시간</th></tr></thead>'
                  f'<tbody>{"".join(prov_rows)}</tbody></table>') if prov_rows else \
        f'<div style="color:{T["faint"]};font-size:12px">첫 자동 수집 후 표시됩니다</div>'

    # ---- 해석 ----
    paras = "".join(f'<p class="ana">{p}</p>' for p in ana.get("paragraphs", []))

    html_doc = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 클라우드 임대가격 트래커</title>
<style>
:root {{ --primary:{T['primary']}; --heading:{T['heading']}; --teal:{T['teal']};
--red:{T['red']}; --gold:{T['gold']}; --text:{T['text']}; --muted:{T['muted']};
--faint:{T['faint']}; --hair:{T['hair']}; --tint:{T['bgTint']}; --fill:{T['fillLight']}; }}
* {{ box-sizing:border-box }}
body {{ font-family:"Pretendard","Noto Sans KR","Malgun Gothic","Apple SD Gothic Neo",Arial,sans-serif;
color:var(--text); margin:0; background:#F7F7F4; font-variant-numeric:tabular-nums;
-webkit-font-smoothing:antialiased }}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 20px 60px }}
header.hero {{ background:var(--primary); color:#fff; padding:34px 0 26px; margin-bottom:22px }}
.hero-in {{ max-width:1080px; margin:0 auto; padding:0 20px; display:flex; align-items:center; gap:18px; position:relative }}
.back {{ position:absolute; top:-18px; left:20px; color:#fff; opacity:.72; font-size:11.5px;
text-decoration:none; font-weight:600 }}
.back:hover {{ opacity:1; text-decoration:underline }}
.stripes {{ display:inline-flex; gap:5px }}
.stripes i {{ width:7px;height:40px;background:#fff;transform:skewX(-18deg);display:block;border-radius:1px }}
.stripes i:nth-child(2){{height:31px;margin-top:9px}} .stripes i:nth-child(3){{height:22px;margin-top:18px}}
.hero h1 {{ font-size:24px; font-weight:800; margin:0 }}
.hero .sub {{ font-size:12.5px; opacity:.85; margin-top:4px }}
.thesis {{ background:var(--tint); color:var(--heading); font-weight:700; font-size:14.5px;
padding:14px 18px; border-left:4px solid var(--primary); margin:0 0 22px; line-height:1.5 }}
.kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:26px }}
.kpi-card {{ background:#fff; border:1px solid var(--hair); border-radius:8px; padding:12px 14px;
box-shadow:0 1px 3px rgba(0,0,0,.05) }}
.kpi-label {{ font-size:11px; font-weight:700; color:var(--muted) }}
.kpi-num {{ font-size:26px; font-weight:800; color:var(--heading); margin:2px 0 }}
.kpi-unit {{ font-size:12px; color:var(--faint); font-weight:600; margin-left:2px }}
.kpi-sub {{ font-size:10px; color:var(--faint) }}
.banner {{ background:var(--primary); color:#fff; font-size:14px; font-weight:700;
padding:6px 12px; margin:0 0 12px; display:block }}
.banner .u {{ font-weight:400; font-size:11px; opacity:.8; float:right }}
.card-sec {{ background:#fff; border:1px solid var(--hair); border-radius:8px; padding:18px;
margin-bottom:22px; box-shadow:0 1px 3px rgba(0,0,0,.05) }}
.cols-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px }}
@media (max-width:760px) {{ .cols-2 {{ grid-template-columns:1fr }} }}
table.data {{ width:100%; border-collapse:collapse; font-size:12px }}
table.data thead th {{ background:var(--primary); color:#fff; font-weight:700; padding:5px 8px; text-align:left }}
table.data td {{ padding:5px 8px; border-bottom:1px solid #EFEFEF; vertical-align:top }}
table.data td.num {{ text-align:right }}
.pos {{ color:var(--teal); font-weight:700 }} .neg {{ color:var(--red); font-weight:700 }}
.ana {{ font-size:13.5px; line-height:1.65; margin:0 0 12px }}
.ana b {{ color:var(--heading) }}
footer {{ font-size:10px; color:var(--faint); line-height:1.6; border-top:1px solid var(--hair);
padding-top:12px; margin-top:8px }}
</style></head><body>
<header class="hero"><div class="hero-in">
<a class="back" href="../">← 리서치 아카이브</a>
<span class="stripes"><i></i><i></i><i></i></span>
<div><h1>AI 클라우드 임대가격 트래커</h1>
<div class="sub">GPU 시간당 임대가 · 네오클라우드 신용지표 — 매일 자동 수집 | 빌드 {now} | 누적 {n_days}일</div></div>
</div></header>
<div class="wrap">
<div class="thesis">{htmlmod.escape(ana.get('thesis', ''))}</div>
<div class="kpi-row">{''.join(kpis)}</div>

<div class="card-sec">
<span class="banner">H100 임대가 추이 ($/GPU-시간) <span class="u">자동: getdeploying 온디맨드 중위가 · Vast.ai 호가 | 수동: SDH100RT</span></span>
{c1}
</div>

<div class="card-sec">
<span class="banner">차세대(Blackwell 세대) 임대가 추이 ($/GPU-시간) <span class="u">GB300 · B200 · GB200</span></span>
{c2}
</div>

<div class="cols-2">
<div class="card-sec">
<span class="banner">네오클라우드 주가 (기준일=100) <span class="u">CRWV·NBIS·IREN·APLD</span></span>
{c3}
</div>
<div class="card-sec">
<span class="banner">CoreWeave CDS 5Y (bp) <span class="u">Bloomberg 수동 기록</span></span>
{c4}
</div>
</div>

<div class="card-sec">
<span class="banner">제공업체별 온디맨드 최저가 Top5 <span class="u">getdeploying 최근 수집분</span></span>
{prov_table}
</div>

<div class="card-sec">
<span class="banner">해석 — 임대가와 신용의 연결고리 <span class="u">Claude 수동 갱신 {fmt_date_kr(ana.get('updated', '2026-08-18'))}</span></span>
{paras}
</div>

<footer>
Source: getdeploying.com (75개 클라우드 제공업체 가격 집계), Vast.ai 공개 API, stooq.com (주가),
Bloomberg SDH100RT·CDS (사용자 제공 PDF 수동 기록).<br>
Note: getdeploying 중위가는 전체 클라우드 시장 기준으로, 네오클라우드 가중 방식인 Silicon Data 지수와 절대 수준이 다를 수 있음.
수동 기록값에는 판독 오차가 있을 수 있음 (잠정).<br>
본 페이지는 정보 제공 목적이며 투자 권유가 아닙니다. Generated automatically via GitHub Actions.
</footer>
</div></body></html>"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"gpu-cloud/index.html 생성 완료 ({len(html_doc):,} bytes, 데이터 {n_days}일)")


# ============================================================
# 5. 메인
# ============================================================


def main():
    cache = load_json(CACHE_FILE, {"daily": {}})
    day = today_kst()
    print(f"== {day} 수집 시작 ==")

    snap = {}
    print("[1/3] getdeploying.com")
    gd_data = collect_getdeploying()
    if gd_data.get("models") or gd_data.get("overview_median"):
        snap["getdeploying"] = gd_data

    print("[2/3] vast.ai")
    va_data = collect_vastai()
    if va_data:
        snap["vastai"] = va_data

    print("[3/3] stooq (주가)")
    st_data = collect_stocks()
    if st_data:
        snap["stocks"] = st_data

    if snap:
        cache["daily"][day] = snap
        save_json(CACHE_FILE, cache)
        print(f"cache.json 갱신 (누적 {len(cache['daily'])}일)")
    else:
        print("[warn] 모든 소스 수집 실패 — 기존 데이터로 화면만 다시 생성")

    build_html()


if __name__ == "__main__":
    main()
