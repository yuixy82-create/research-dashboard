#!/usr/bin/env python3
"""센텔리안24 아마존 순위 트래커.

사용:
  python3 tracker.py --ingest snap.json   수집 결과를 history.json에 넣고 index.html·alert.txt 갱신
  python3 tracker.py --fail "사유"         수집 실패를 기록 (알림은 하루 한 번만)
  python3 tracker.py --need                매일 수집 회차(22시 본 실행·재시도)가 이미 성공했는지 확인
                                           → "need" 또는 "done ..." 한 줄 출력
  --daily  매일 수집 작업(22:17 본 실행, 01:17·09:17 재시도)에서 --ingest·--fail에 붙인다.
           하루 회차는 22:00 KST에 시작해 다음 날 22:00 전까지. 블프 하루 요약도 이 회차가 맡는다.
  python3 tracker.py                      history.json으로 index.html만 다시 만듦

alert.txt가 생기면 그 내용을 텔레그램으로 보낸다. 보낼 것이 없으면 alert.txt는 지워진다.
"""
import argparse
import datetime as dt
import html
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "data", "history.json")
OUT = os.path.join(HERE, "index.html")
ALERT = os.path.join(HERE, "alert.txt")
URL = "https://yuixy82-create.github.io/research-dashboard/amazon-centellian/"
KST = dt.timezone(dt.timedelta(hours=9))
TOP = 100            # 알림 기준: 뷰티 전체 100위
GOAL = 2             # 확인 지점: 100위 안 2~3개
BF_START, BF_END = dt.date(2026, 11, 24), dt.date(2026, 12, 1)   # 블프 주간 (KST)
SLOT_HOUR = 22       # 매일 수집 회차 시작 (KST). 22:17 본 실행 → 01:17·09:17 재시도
FINAL_FROM = 6       # 이 시각(KST) 이후의 재시도가 마지막 재시도

# ASIN → 한글 이름. 옵션으로 묶인 상품은 순위가 같아 하나로 합쳐진다
LABELS = {
    "B0DFY92NDB": "마데카크림 타임리버스 (단품)",
    "B0FS7QVSVC": "마데카크림 액티브리뉴 PDRN",
    "B0G48T5CM2": "360샷 PDRN 아이크림",
    "B0DJ8FQDYL": "타임리버스 2개입 · PDRN 듀오",
    "B0H79K9743": "타임리버스 2개입 · PDRN 듀오",
    "B0FVY91T1Q": "마데카크림 타임리버스 제로",
    "B0H627CCBF": "타임리버스 부스팅 토너",
    "B0D2XPMCJK": "마데카크림 타임리버스 (구 판매페이지)",
    "B0GQ753N3D": "타임리버스 + PDRN 듀오 (2세트)",
    "B0GL8DSLBF": "마데카크림 하이드라 카밍",
    "B0BS2T3QYF": "마데카크림 시즌6",
}
CAT_KO = {
    "Face Moisturizers": "페이스 보습제",
    "Eye Treatment Creams": "아이크림",
    "Toners": "토너",
    "Serums": "세럼",
    "Cleansing Washes": "클렌저",
}
FLAG = "B0DFY92NDB"   # 대표 단품


def kst(ts):
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(KST)
    return t


def fmt_k(t):
    return t.strftime("%y.%m.%d %H:%M")


def load():
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8") as f:
            return json.load(f)
    return {"snapshots": [], "last_summary": "", "last_fail_alert": ""}


def save(h):
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)


def group(rows):
    """같은 순위·같은 세부순위를 가진 ASIN은 한 상품(옵션)으로 묶는다."""
    out = {}
    for asin, overall, sub, cat, title in rows:
        key = (overall, sub, cat)
        g = out.setdefault(key, {"ids": [], "overall": overall, "sub": sub, "cat": cat, "title": title})
        g["ids"].append(asin)
    res = []
    for g in out.values():
        lab = next((LABELS[a] for a in g["ids"] if a in LABELS), None)
        g["label"] = lab or g["title"]
        g.pop("title")
        res.append(g)
    res.sort(key=lambda x: x["overall"] or 10**9)
    return res


def validate(s):
    if not isinstance(s, dict) or "ts" not in s or not isinstance(s.get("top"), list):
        raise SystemExit("snap 형식 오류: ts·top 필요")
    for r in s["top"]:
        if not (isinstance(r, list) and len(r) == 5 and isinstance(r[0], str) and isinstance(r[1], int)):
            raise SystemExit(f"snap 행 형식 오류: {r}")
    kst(s["ts"])


def slot_start(t):
    """t(KST)가 속한 매일 수집 회차의 시작 시각 (가장 최근 22:00 KST)."""
    s = t.replace(hour=SLOT_HOUR, minute=0, second=0, microsecond=0)
    return s if t >= s else s - dt.timedelta(days=1)


def slot_day(t):
    return slot_start(t).strftime("%Y-%m-%d")


def need():
    now = dt.datetime.now(KST)
    st = slot_start(now)
    done = [x for x in oks(load()) if kst(x["ts"]) >= st]
    if done:
        print(f"done {fmt_k(kst(done[-1]['ts']))} KST 수집 성공 (회차 {st.strftime('%y.%m.%d')} 22시)")
    else:
        print("need")


def oks(h):
    return [x for x in h["snapshots"] if not x.get("fail")]


def key_of(listing):
    return listing["ids"][0]


def find(snap, asin):
    for l in snap["listings"]:
        if asin in l["ids"]:
            return l
    return None


def cat_ko(c):
    return CAT_KO.get(c, c)


def line_for(l, prev):
    s = f"- {l['label']}: 뷰티 {l['overall']:,}위"
    if l.get("sub"):
        s += f" / {cat_ko(l['cat'])} {l['sub']:,}위"
    if prev:
        p = find(prev, key_of(l))
        if p and p["overall"]:
            s += f" (직전 {p['overall']:,}위)"
    return s


def message(snap, prev, kind):
    t = kst(snap["ts"])
    n = snap["top100"]
    pn = prev["top100"] if prev else None
    head = {
        "start": f"동국제약 아마존 순위 추적 시작 ({fmt_k(t)} KST)",
        "change": f"🚨 동국제약 아마존 뷰티 100위 안 제품 수 변화: {pn} → {n}개 ({fmt_k(t)} KST)",
        "summary": f"동국제약 아마존 순위 ({slot_day(t)[2:].replace('-', '.')} 블프 주간 요약)"
                   + ("" if t.hour >= SLOT_HOUR else f" · 재시도 {t.strftime('%H:%M')} 수집"),
    }[kind]
    lines = [head, ""]
    lines.append(f"뷰티 전체 {TOP}위 안 센텔리안24: {n}개" + (f" (직전 {pn}개)" if pn is not None and kind != "change" else ""))
    if kind == "summary" and pn == n:
        lines.append("변화 없음")
    for l in snap["listings"][:3]:
        lines.append(line_for(l, prev))
    if n >= GOAL:
        lines.append(f"★ 확인 지점 충족: 뷰티 {TOP}위 안 {n}개 (기준 2~3개)")
    else:
        lines.append(f"확인 지점(뷰티 {TOP}위 안 2~3개): 미충족")
    lines += ["", URL]
    return "\n".join(lines)


def ingest(path, daily=False):
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    validate(s)
    h = load()
    if s.get("blocked") or not s.get("ok"):
        return fail(h, "아마존 차단(캡차)" if s.get("blocked") else "상품 페이지를 하나도 읽지 못함", s["ts"], daily)
    if any(x["ts"] == s["ts"] for x in h["snapshots"]):
        print("이미 들어간 수집 결과 (ts 중복): 건너뜀")
        write_alert(None)
        build(h)
        return
    listings = group(s["top"])
    snap = {"ts": s["ts"], "found": s.get("found"), "ok": s.get("ok"), "rest": s.get("rest"),
            "listings": listings, "top100": sum(1 for l in listings if l["overall"] and l["overall"] <= TOP)}
    prevs = oks(h)
    prev = prevs[-1] if prevs else None
    h["snapshots"].append(snap)
    t = kst(s["ts"])
    kind = None
    if prev is None:
        kind = "start"
    elif snap["top100"] != prev["top100"]:
        kind = "change"
    sday = dt.date.fromisoformat(slot_day(t))
    bf_daily = daily and BF_START <= sday <= BF_END
    if kind is None and bf_daily and h.get("last_summary") != sday.isoformat():
        kind = "summary"
    if kind and bf_daily:
        h["last_summary"] = sday.isoformat()   # 변화 알림이 그날 요약을 겸함
    write_alert(message(snap, prev, kind) if kind else None)
    save(h)
    build(h)
    print(f"수집 {fmt_k(t)} KST · 100위 안 {snap['top100']}개 · 알림: {kind or '없음'}")


def fail(h, reason, ts=None, daily=False):
    ts = ts or dt.datetime.now(dt.timezone.utc).isoformat()
    t = kst(ts)
    h["snapshots"].append({"ts": ts, "fail": reason})
    msg = None
    if daily:
        # 매일 회차: 첫 실패에 한 번, 마지막 재시도(09:17)까지 실패하면 한 번 더
        sd = slot_day(t)
        final = FINAL_FROM <= t.hour < SLOT_HOUR
        if final and h.get("last_daily_final") != sd:
            h["last_daily_final"] = sd
            msg = (f"동국제약 아마존 순위 {sd[2:].replace('-', '.')} 몫 수집 못함 ({fmt_k(t)} KST): {reason}\n"
                   f"22:17 본 실행과 재시도 모두 실패. 오늘 22:17에 다시 시도함\n\n{URL}")
        elif not final and h.get("last_daily_fail") != sd:
            h["last_daily_fail"] = sd
            msg = (f"동국제약 아마존 순위 수집 실패 ({fmt_k(t)} KST): {reason}\n"
                   f"01:17·09:17에 다시 시도함 (성공하면 그날 몫으로 들어감)\n\n{URL}")
    else:
        day = t.strftime("%Y-%m-%d")
        if h.get("last_fail_alert") != day:
            h["last_fail_alert"] = day
            msg = f"동국제약 아마존 순위 수집 실패 ({fmt_k(t)} KST): {reason}\n다음 실행에서 다시 시도함\n\n{URL}"
    write_alert(msg)
    save(h)
    build(h)
    print(f"실패 기록: {reason} · 알림: {'보냄' if msg else '이번 회차 이미 보냄'}")


def write_alert(msg):
    if msg:
        with open(ALERT, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    elif os.path.exists(ALERT):
        os.remove(ALERT)


# ---------------------------------------------------------------- page

COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]


def chart(h):
    snaps = oks(h)
    if not snaps:
        return "<p class='muted'>아직 수집된 데이터가 없음</p>"
    last = snaps[-1]
    series = []
    for l in last["listings"][:3]:
        pts = []
        for s in snaps:
            m = find(s, key_of(l))
            if m and m["overall"]:
                pts.append((kst(s["ts"]), m["overall"]))
        series.append((l["label"], pts))
    W, H, L, R, T, B = 960, 340, 64, 190, 18, 40
    t0 = min(p[0] for _, ps in series for p in ps)
    t1 = max(p[0] for _, ps in series for p in ps)
    # 기간이 짧으면 블프 주간까지 보이게 축을 넓힌다
    lo_t = min(t0, dt.datetime(2026, 11, 20, tzinfo=KST))
    hi_t = max(t1, dt.datetime(2026, 12, 2, tzinfo=KST))
    span = (hi_t - lo_t).total_seconds()
    ranks = [p[1] for _, ps in series for p in ps] + [TOP]
    ylo, yhi = 10 ** math.floor(math.log10(max(1, min(ranks) / 1.5))), 10 ** math.ceil(math.log10(max(ranks) * 1.2))
    ylo = max(1, ylo)

    def X(t):
        return L + (t - lo_t).total_seconds() / span * (W - L - R)

    def Y(r):
        return T + (math.log10(r) - math.log10(ylo)) / (math.log10(yhi) - math.log10(ylo)) * (H - T - B)

    g = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="센텔리안24 상위 제품의 아마존 뷰티 전체 순위 추이">']
    bx0, bx1 = X(dt.datetime(2026, 11, 27, 17, tzinfo=KST)), X(dt.datetime(2026, 12, 1, 17, tzinfo=KST))
    g.append(f'<rect x="{bx0:.1f}" y="{T}" width="{bx1 - bx0:.1f}" height="{H - T - B}" fill="#F3F4EA"/>')
    g.append(f'<text x="{(bx0 + bx1) / 2:.1f}" y="{T + 14}" class="ax" text-anchor="middle">블프~사이버먼데이 (미국)</text>')
    d = ylo
    while d <= yhi:
        y = Y(d)
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#ECECEC"/>')
        g.append(f'<text x="{L - 8}" y="{y + 4:.1f}" class="ax" text-anchor="end">{d:,}위</text>')
        d *= 10
    y100 = Y(TOP)
    g.append(f'<line x1="{L}" x2="{W - R}" y1="{y100:.1f}" y2="{y100:.1f}" stroke="#B7332B" stroke-dasharray="5 4" stroke-width="1.5"/>')
    g.append(f'<text x="{W - R + 6}" y="{y100 + 4:.1f}" class="ax" fill="#B7332B">뷰티 {TOP}위</text>')
    day = lo_t.replace(hour=0, minute=0)
    while day <= hi_t:
        x = X(day)
        if x >= L:
            g.append(f'<text x="{x:.1f}" y="{H - B + 18}" class="ax" text-anchor="middle">{day.strftime("%m.%d")}</text>')
        day += dt.timedelta(days=2)
    pts_js = []
    ends = []
    for i, (name, ps) in enumerate(series):
        c = COLORS[i]
        if len(ps) > 1:
            path = " ".join(f"{'M' if j == 0 else 'L'}{X(t):.1f},{Y(r):.1f}" for j, (t, r) in enumerate(ps))
            g.append(f'<path d="{path}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"/>')
        for t, r in ps:
            g.append(f'<circle cx="{X(t):.1f}" cy="{Y(r):.1f}" r="4" fill="{c}" stroke="#fff" stroke-width="2"/>')
            pts_js.append([round(X(t), 1), round(Y(r), 1), name, fmt_k(t), r])
        t, r = ps[-1]
        ends.append([Y(r) + 4, min(X(t) + 10, W - R + 6), f"{html.escape(name)} {r:,}위"])
    ends.sort()
    for j in range(1, len(ends)):   # 끝 라벨이 겹치지 않게 아래로 민다
        ends[j][0] = max(ends[j][0], ends[j - 1][0] + 15)
    for y, x, txt in ends:
        g.append(f'<text x="{x:.1f}" y="{y:.1f}" class="lab">{txt}</text>')
    g.append("</svg>")
    legend = "".join(f'<span><i style="background:{COLORS[i]}"></i>{html.escape(n)}</span>' for i, (n, _) in enumerate(series))
    return (f'<div class="legend">{legend}</div><div class="cwrap">' + "".join(g) +
            '<div id="tip" class="tip"></div></div>'
            f'<script>const P={json.dumps(pts_js, ensure_ascii=False)};'
            "const sv=document.querySelector('.chart'),tp=document.getElementById('tip');"
            "sv.addEventListener('mousemove',e=>{const b=sv.getBoundingClientRect(),k=960/b.width,x=(e.clientX-b.left)*k,y=(e.clientY-b.top)*k;"
            "let best=null,bd=1e9;for(const p of P){const d=Math.hypot(p[0]-x,p[1]-y);if(d<bd){bd=d;best=p}}"
            "if(best&&bd<40){tp.style.display='block';tp.style.left=(best[0]/k)+'px';tp.style.top=(best[1]/k)+'px';"
            "tp.innerHTML='<b>'+best[2]+'</b><br>'+best[3]+' KST · 뷰티 '+best[4].toLocaleString()+'위'}else tp.style.display='none'});"
            "sv.addEventListener('mouseleave',()=>tp.style.display='none');</script>")


def table(h):
    snaps = oks(h)
    if not snaps:
        return ""
    last, prev = snaps[-1], (snaps[-2] if len(snaps) > 1 else None)
    rows = []
    for l in last["listings"]:
        ch = ""
        if prev:
            p = find(prev, key_of(l))
            if p and p["overall"] and l["overall"]:
                dlt = p["overall"] - l["overall"]
                ch = "–" if dlt == 0 else (f'<span class="up">▲{dlt:,}</span>' if dlt > 0 else f'<span class="dn">▼{-dlt:,}</span>')
        links = " ".join(f'<a href="https://www.amazon.com/dp/{a}">{a}</a>' for a in l["ids"])
        hot = ' class="hot"' if l["overall"] and l["overall"] <= TOP else ""
        sub = f"{cat_ko(l['cat'])} {l['sub']:,}위" if l.get("sub") else "-"
        rows.append(f"<tr{hot}><td>{html.escape(l['label'])}</td><td class='n'>{l['overall']:,}위</td>"
                    f"<td>{html.escape(sub)}</td><td class='n'>{ch}</td><td class='asin'>{links}</td></tr>")
    rest = last.get("rest") or 0
    note = f"<p class='muted'>뷰티 전체 2만 위 밖이거나 순위가 없는 센텔리안24 상품 {rest}개는 표에서 뺌</p>" if rest else ""
    return ("<table><thead><tr><th>제품</th><th class='n'>뷰티 전체</th><th>세부 카테고리</th>"
            "<th class='n'>직전 대비</th><th>ASIN (아마존 상품번호)</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>" + note)


def log_rows(h):
    rows = []
    for s in reversed(h["snapshots"][-40:]):
        t = fmt_k(kst(s["ts"]))
        if s.get("fail"):
            rows.append(f"<tr><td>{t}</td><td colspan='3' class='dn'>수집 실패: {html.escape(s['fail'])}</td></tr>")
            continue
        f = find(s, FLAG)
        rows.append(f"<tr><td>{t}</td><td class='n'>{s['top100']}개</td>"
                    f"<td class='n'>{(format(f['overall'], ',') + '위') if f else '-'}</td>"
                    f"<td class='n'>{(format(f['sub'], ',') + '위') if f and f.get('sub') else '-'}</td></tr>")
    return ("<table><thead><tr><th>수집 시각 (KST)</th><th class='n'>뷰티 100위 안</th>"
            "<th class='n'>단품 뷰티 전체</th><th class='n'>단품 페이스 보습제</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def build(h):
    snaps = oks(h)
    last = snaps[-1] if snaps else None
    f = find(last, FLAG) if last else None
    n = last["top100"] if last else 0
    upd = fmt_k(kst(h["snapshots"][-1]["ts"])) if h["snapshots"] else "-"
    lastfail = h["snapshots"][-1].get("fail") if h["snapshots"] else None
    kpis = [
        ("뷰티 전체 100위 안", f"{n}<span class='u'>개</span>", "확인 지점: 2~3개"),
        ("단품 · 뷰티 전체", f"{f['overall']:,}<span class='u'>위</span>" if f else "-", "마데카크림 타임리버스"),
        ("단품 · 페이스 보습제", f"{f['sub']:,}<span class='u'>위</span>" if f and f.get("sub") else "-", "세부 카테고리 순위"),
        ("마지막 수집", upd, "KST" + (" · 실패" if lastfail else "")),
    ]
    kpi_html = "".join(f"<div class='kpi'><div class='kl'>{a}</div><div class='kn'>{b}</div><div class='ks'>{c}</div></div>" for a, b, c in kpis)
    alert = (f"<div class='alert'>마지막 수집 실패: {html.escape(lastfail)}. 표와 그래프는 직전 성공 수집 기준</div>" if lastfail else "")
    page = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>센텔리안24 아마존 순위 트래커</title><style>
:root{{--primary:#4A5228;--heading:#5F6C2C;--red:#B7332B;--teal:#2E97A7;--text:#3A3A3A;--muted:#6E6E6E;--faint:#9B9B9B;--hair:#D8D8D8;--tint:#F3F4EA}}
*{{box-sizing:border-box}}
body{{font-family:"Pretendard","Noto Sans KR","Malgun Gothic","Apple SD Gothic Neo",Arial,sans-serif;color:var(--text);margin:0;background:#F7F7F4;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1060px;margin:0 auto;padding:0 20px 60px}}
header.hero{{background:var(--primary);color:#fff;padding:30px 0 22px;margin-bottom:20px}}
.hero-in{{max-width:1060px;margin:0 auto;padding:0 20px;position:relative}}
.back{{position:absolute;top:-14px;left:20px;color:#fff;opacity:.72;font-size:11.5px;text-decoration:none;font-weight:600}}
.hero h1{{font-size:22px;font-weight:800;margin:0}}
.hero p{{font-size:12px;opacity:.85;margin:5px 0 0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}}
@media(max-width:760px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:#fff;border:1px solid var(--hair);border-radius:8px;padding:10px 12px}}
.kl{{font-size:10.5px;font-weight:700;color:var(--muted)}}
.kn{{font-size:21px;font-weight:800;color:var(--heading);margin:1px 0}}
.kn .u{{font-size:12px;color:var(--faint);margin-left:2px}}
.ks{{font-size:10px;color:var(--faint)}}
.thesis{{background:var(--tint);border-left:5px solid var(--primary);padding:16px 22px;margin:0 0 22px}}
.thesis ul{{margin:0;padding-left:16px}}.thesis li{{font-size:14px;line-height:1.7;margin-bottom:6px}}
.alert{{background:#FBEDEA;border-left:5px solid var(--red);padding:10px 16px;margin:0 0 14px;font-size:13px}}
.banner{{background:var(--primary);color:#fff;font-size:16px;font-weight:800;padding:8px 14px;margin:34px 0 12px}}
.card{{background:#fff;border:1px solid var(--hair);border-radius:8px;padding:16px;margin-bottom:16px}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:6px}}
.legend i{{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}}
.cwrap{{position:relative}}.chart{{width:100%;height:auto;display:block}}
.chart .ax{{font-size:11px;fill:#8A8A8A}}.chart .lab{{font-size:11.5px;fill:#3A3A3A;font-weight:600}}
.tip{{display:none;position:absolute;transform:translate(-50%,-115%);background:#fff;border:1px solid var(--hair);border-radius:6px;padding:6px 9px;font-size:11.5px;pointer-events:none;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{background:var(--primary);color:#fff;font-weight:700;text-align:left;padding:7px 8px}}
td{{border-bottom:1px solid #ECECEC;padding:7px 8px}}
.n{{text-align:right}}.asin a{{color:var(--muted);font-size:11px;margin-right:6px}}
tr.hot td{{background:#E6F2F4;font-weight:700}}
.up{{color:var(--teal);font-weight:700}}.dn{{color:var(--red);font-weight:700}}
.muted{{color:var(--faint);font-size:11.5px;margin:8px 0 0}}
.foot{{font-size:11px;color:var(--faint);line-height:1.7;margin-top:30px}}
</style></head><body>
<header class="hero"><div class="hero-in"><a class="back" href="../">← 리서치 아카이브</a>
<h1>센텔리안24 아마존 순위 트래커</h1>
<p>동국제약 마데카크림 · 아마존 미국 판매 순위(BSR, Best Sellers Rank) · 블프 주간 하루 4회, 그 외 하루 1회 자동 수집</p>
</div></header>
<div class="wrap">
{alert}
<div class="kpis">{kpi_html}</div>
<div class="thesis"><ul>
<li>확인 지점: 블랙프라이데이(26.11.27)~사이버먼데이(26.11.30, 미국 날짜) 기간에 <b>아마존 뷰티 전체 {TOP}위 안에 센텔리안24 제품이 2~3개</b> 들어가는지</li>
<li>뷰티 전체는 화장품·헤어·바디·면도기 등 뷰티·생활용품 전체를 합친 순위라, 세부 카테고리(페이스 보습제) 순위보다 진입 문턱이 훨씬 높음</li>
<li>같은 상품의 옵션(묶음 구성 등)은 아마존 순위가 같아 한 줄로 합쳐 셈</li>
</ul></div>

<div class="banner">뷰티 전체 순위 추이 (위로 갈수록 상위, 로그 눈금)</div>
<div class="card">{chart(h)}</div>

<div class="banner">현재 순위 (뷰티 전체 2만 위 안)</div>
<div class="card">{table(h)}</div>

<div class="banner">수집 기록</div>
<div class="card">{log_rows(h)}</div>

<div class="foot">출처: Amazon.com 상품 페이지의 Best Sellers Rank (센텔리안24 검색 결과와 고정 상품 목록을 함께 조회). 아마존 순위는 약 1시간마다 바뀌며 최근 판매량을 반영함.<br>
<b>정보 제공용 자료이며 투자 권유가 아님.</b></div>
</div></body></html>
"""
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest")
    ap.add_argument("--fail")
    ap.add_argument("--need", action="store_true")
    ap.add_argument("--daily", action="store_true")
    a = ap.parse_args()
    if a.need:
        need()
    elif a.ingest:
        ingest(a.ingest, a.daily)
    elif a.fail:
        fail(load(), a.fail, daily=a.daily)
    else:
        build(load())
        write_alert(None)


if __name__ == "__main__":
    main()
