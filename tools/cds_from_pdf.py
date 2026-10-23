#!/usr/bin/env python3
"""Bloomberg CDS 차트 PDF → gpu-cloud/cds.json 자동 반영.

입력 PDF: "코어위브 CDS 등_YYYYMMDD.pdf" (텔레그램으로 받는 Bloomberg 캡처, 차트 4장).
  이미지 순서: ① CoreWeave CDS 5Y  ② Oracle CDS 5Y  ③ B200 지수  ④ H100 지수  (①②만 사용)
  차트 창은 최근 1년(1Y). 마지막 점 = 파일 날짜의 직전 영업일.

원리
  - 검은 배경 위의 밝은 선(가격선) 픽셀을 열(column)마다 찾아 중앙 y를 얻는다.
    (남색 채움 영역의 윗선은 선보다 2~10px 낮게 잡혀 쓰지 않는다)
  - 플롯 오른쪽 경계(세로 축선)를 자동 검출해 그 왼쪽만 읽는다. 범례 상자·툴바는 제외.
  - x축: 첫 열 = 마지막 날짜 − 1년 + 1일, 마지막 열 = 마지막 날짜. 영업일 기준 선형 배치.
  - y축: 글자가 너무 작아(601px 폭) OCR이 안 된다. 기존 cds.json과 겹치는 기간으로
    1차식 v = a·y + b 를 최소제곱으로 맞춘다 (겹침 40일 이상, 잔차 4% 이하 요구).
    첫 회(기존 시계열 없음)는 --anchors 로 범례의 Low/Last/High 값을 직접 준다.
  - 보정 뒤 기존 시계열 마지막 날짜 이후의 점만 덧붙인다.

사용
  python3 tools/cds_from_pdf.py <pdf> [--store gpu-cloud/cds.json] [--date YYYYMMDD]
         [--anchors cds=LOW,LAST,HIGH --anchors oracle_cds=LOW,LAST,HIGH] [--replace] [--dry]
  파일 이름에 YYYYMMDD가 있으면 --date 생략 가능. 종료코드 0 = 성공, 1 = 실패(저장 안 함).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata

import numpy as np
from PIL import Image

KEYS = ["cds", "oracle_cds"]          # 이미지 0, 1 순서
MIN_OVERLAP = 40
MAX_RESID_PCT = 4.0                   # 보정 잔차(평균 절대 오차 %) 상한


def bdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def extract_line(png):
    """가격선의 열별 y(픽셀). 반환 (xs, ys): 선이 있는 첫 열부터 마지막 열까지 보간."""
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    h, w, _ = im.shape
    s = im.sum(axis=2)
    # 오른쪽 축선: 우반부에서 중간 밝기 픽셀이 세로로 가장 길게 이어진 열
    axis_x = max(range(w // 2, w), key=lambda x: int(((s[:, x] > 120) & (s[:, x] < 400)).sum()))
    ln = (im[..., 0] >= 40) & (s >= 100)          # 선 = 회청색 밝은 픽셀 (채움은 r<=12)
    ln[:46, :] = False                            # 상단 툴바
    ln[:72, :130] = False                         # 범례 상자 (Last/High/Average/Low)
    ln[:, axis_x - 1:] = False                    # 축선과 오른쪽 라벨·마지막 가격 상자
    # 아래 축선: 하반부에서 중간 밝기 픽셀이 가로로 가장 길게 이어진 행. 그 아래(월 라벨)까지 제외
    axis_y = max(range(h // 2, h), key=lambda y: int(((s[y, :axis_x] > 120) & (s[y, :axis_x] < 400)).sum()))
    ln[axis_y - 1:, :] = False
    # 열마다 픽셀 덩어리(cluster)를 나누고, 이전 열의 y에 가장 가까운 덩어리를 따라간다
    # (축 눈금·격자선 조각 같은 떨어진 점을 걸러냄). 30px 넘게 튀면 그 열은 건너뛰고 보간.
    cols, ymed, prev = [], [], None
    for x in range(w):
        rows = np.where(ln[:, x])[0]
        if not len(rows):
            continue
        clusters, cur = [], [rows[0]]
        for r_ in rows[1:]:
            if r_ - cur[-1] > 3:
                clusters.append(cur); cur = [r_]
            else:
                cur.append(r_)
        clusters.append(cur)
        cands = [float(np.median(c)) for c in clusters]
        if prev is None:
            y = cands[int(np.argmax([len(c) for c in clusters]))]
        else:
            y = min(cands, key=lambda v: abs(v - prev))
            if abs(y - prev) > 30:
                continue
        cols.append(x); ymed.append(y); prev = y
    if len(cols) < 300:
        raise RuntimeError("가격선을 찾지 못함 (%s, %d열)" % (png, len(cols)))
    xs = np.arange(cols[0], cols[-1] + 1)
    ys = np.interp(xs, cols, ymed)
    return xs, ys


def curve_to_days(xs, ys, last_day):
    start = last_day - dt.timedelta(days=364)
    days = bdays(start, last_day)
    n = len(days)
    idx = np.rint((xs - xs[0]) / (xs[-1] - xs[0]) * (n - 1)).astype(int)
    out = {}
    for i in range(n):
        sel = ys[idx == i]
        if len(sel):
            out[days[i].isoformat()] = float(np.mean(sel))
    return out


def shift_days(ypx, k):
    """날짜를 영업일 k일만큼 민다 (x축 창 시작일이 하루 어긋나는 경우 대비)."""
    if k == 0:
        return ypx
    ds = sorted(ypx)
    lo = dt.date.fromisoformat(ds[0]) - dt.timedelta(days=30)
    hi = dt.date.fromisoformat(ds[-1]) + dt.timedelta(days=30)
    cal = [d.isoformat() for d in bdays(lo, hi)]
    pos = {d: i for i, d in enumerate(cal)}
    return {cal[pos[d] + k]: y for d, y in ypx.items()}


def fit_overlap(ypx, existing, max_shift=3):
    """기존 시계열과 겹치는 날짜로 1차 보정. 날짜 어긋남(±max_shift 영업일)도 같이 찾는다.
    반환 (a, b, n, resid%, shift)"""
    ex = dict(existing)
    best = None
    for k in range(-max_shift, max_shift + 1):
        sh = shift_days(ypx, k)
        common = sorted(d for d in sh if d in ex)
        if len(common) < MIN_OVERLAP:
            continue
        Y = np.array([sh[d] for d in common]); V = np.array([ex[d] for d in common])
        a, b = np.polyfit(Y, V, 1)
        resid = float(np.mean(np.abs(a * Y + b - V) / np.maximum(V, 1)) * 100)
        if best is None or resid < best[3]:
            best = (a, b, len(common), resid, k)
    if best is None:
        raise RuntimeError("겹치는 날짜가 %d일 미만: 보정 불가" % MIN_OVERLAP)
    return best


def fit_anchors(ypx, low, last, high):
    """범례의 Low(최저)·Last(마지막)·High(최고) 값으로 보정. y는 아래로 갈수록 큼."""
    ds = sorted(ypx); Y = np.array([ypx[d] for d in ds])
    pts = [(Y.max(), low), (Y[-1], last), (Y.min(), high)]
    a, b = np.polyfit([p[0] for p in pts], [p[1] for p in pts], 1)
    resid = float(np.mean([abs(a * y + b - v) / v for y, v in pts]) * 100)
    return a, b, 3, resid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--store", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gpu-cloud", "cds.json"))
    ap.add_argument("--date", help="YYYYMMDD (기본: 파일 이름에서 추출)")
    ap.add_argument("--anchors", action="append", default=[], help="key=LOW,LAST,HIGH (첫 회 수동 보정)")
    ap.add_argument("--replace", action="store_true", help="겹치는 기간도 새 판독으로 덮어쓴다")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    m = re.search(r"(\d{8})", a.date or os.path.basename(a.pdf))
    if not m:
        sys.exit("날짜를 알 수 없음: --date YYYYMMDD 지정")
    pdf_day = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    last_day = pdf_day - dt.timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day -= dt.timedelta(days=1)
    anchors = {}
    for s in a.anchors:
        k, v = s.split("=")
        anchors[k] = [float(x) for x in v.split(",")]

    store = {}
    if os.path.exists(a.store):
        with open(a.store, encoding="utf-8") as f:
            store = json.load(f)
    tag = unicodedata.normalize("NFC", os.path.basename(a.pdf))   # 맥·VM 마운트는 NFD로 올 수 있음
    store["processed"] = [unicodedata.normalize("NFC", t) for t in store.get("processed", [])]
    if tag in store["processed"] and not a.replace:
        print("이미 처리한 파일:", tag); return

    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdfimages", "-png", a.pdf, os.path.join(td, "img")], check=True)
        imgs = sorted(p for p in os.listdir(td) if p.endswith(".png"))
        if len(imgs) < 2:
            sys.exit("PDF에서 차트 이미지 2장 이상을 찾지 못함")
        results = {}
        for key, img in zip(KEYS, imgs[:2]):
            xs, ys = extract_line(os.path.join(td, img))
            ypx = curve_to_days(xs, ys, last_day)
            existing = store.get(key, [])
            if key in anchors:
                A, B, n, resid = fit_anchors(ypx, *anchors[key])
                how = "앵커 3점"
            elif existing:
                A, B, n, resid, k = fit_overlap(ypx, existing)
                ypx = shift_days(ypx, k)
                how = "겹침 %d일, 날짜 보정 %+d일" % (n, k)
            else:
                sys.exit("%s: 기존 시계열이 없음. --anchors %s=LOW,LAST,HIGH 로 첫 보정을 준다" % (key, key))
            print("%s: %s, 잔차 %.2f%%, 눈금 %.3f bp/px, 마지막 %.1f (%s)"
                  % (key, how, resid, -A, A * ypx[max(ypx)] + B, max(ypx)))
            if resid > MAX_RESID_PCT:
                sys.exit("%s: 잔차 %.1f%% > %.1f%% — 차트 창·축이 달라진 듯, 수동 확인 필요" % (key, resid, MAX_RESID_PCT))
            results[key] = [[d, round(A * y + B, 1)] for d, y in sorted(ypx.items())]

    changed = False
    for key, new in results.items():
        existing = store.get(key, [])
        if a.replace or not existing:
            merged = {d: v for d, v in existing}
            merged.update({d: v for d, v in new})
            added = len(new)
        else:
            last_have = max(d for d, _ in existing)
            fresh = [p for p in new if p[0] > last_have]
            merged = {d: v for d, v in existing + fresh}
            added = len(fresh)
        store[key] = sorted([[d, v] for d, v in merged.items()])
        changed |= added > 0
        print("  %s: +%d일 → 총 %d일 (%s ~ %s)" % (key, added, len(store[key]), store[key][0][0], store[key][-1][0]))

    store.setdefault("processed", [])
    if tag not in store["processed"]:
        store["processed"].append(tag)
    store["asof"] = max(store.get("asof") or "", last_day.isoformat())
    store["source"] = "Bloomberg 1Y 차트 PDF 픽셀 판독 (tools/cds_from_pdf.py)"
    if a.dry:
        print("(dry) 저장 생략"); return
    with open(a.store, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    print("저장:", os.path.abspath(a.store), "(변경 %s)" % ("있음" if changed else "없음"))


if __name__ == "__main__":
    main()
