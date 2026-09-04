#!/usr/bin/env python3
"""팜유 가격 트래커 데이터 수집.
  KPBN 인니 현물 입찰가  : InfoSAWIT WP REST API
  FCPO 월물별 커브       : 같은 기사 본문
  MPOB 말레이 일일 현물  : MPOB BEPI 일일 가격표
기존 data/series.json 을 읽어 새 관측치만 병합한다."""
import json, os, re, sys, datetime as dt
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
PATH = os.path.join(ROOT, 'data', 'series.json')
UA = {'User-Agent': 'Mozilla/5.0 (compatible; palm-oil-tracker/1.0)'}

MON = {'januari':1,'februari':2,'maret':3,'april':4,'mei':5,'juni':6,
       'juli':7,'agustus':8,'september':9,'oktober':10,'november':11,'desember':12}
EN  = {'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
       'July':7,'August':8,'September':9,'October':10,'November':11,'December':12}


def get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def strip(h):
    h = re.sub(r'<[^>]+>', ' ', h)
    return re.sub(r'\s+', ' ', h.replace('&nbsp;', ' '))


# ---------- KPBN + FCPO ----------
def fetch_infosawit(pages=2):
    kpbn, fcpo = {}, {}
    for p in range(1, pages + 1):
        url = ('https://sumatera.infosawit.com/wp-json/wp/v2/posts'
               f'?search=KPBN&per_page=100&page={p}&_fields=date,title,content')
        try:
            posts = json.loads(get(url))
        except Exception as e:
            print('infosawit page %d failed: %s' % (p, e), file=sys.stderr)
            break
        if not posts:
            break
        for post in posts:
            title = strip(post['title']['rendered'])
            if 'KPBN' not in title.upper():
                continue
            body = strip(post['content']['rendered'])
            m = re.search(r'\((\d{1,2})/(\d{1,2})/(\d{4})\)', body)
            if not m:
                continue
            d = '%s-%02d-%02d' % (m.group(3), int(m.group(2)), int(m.group(1)))

            k = (re.search(r'Franco Dumai[^0-9]{0,40}Rp\s?([\d.]+)', body, re.I)
                 or re.search(r'Harga CPO ditetapkan Rp\s?([\d.]+)', body, re.I))
            if k:
                v = int(k.group(1).replace('.', ''))
                if 5000 < v < 20000:
                    kpbn[d] = v

            cur = int(d[:4]) * 12 + int(d[5:7])
            rows = {}
            for mm in re.finditer(
                    r'(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)'
                    r'\s*(\d{4})?[\s\S]{0,90}?RM\s?([\d.]+)\s*(?:per ton|/ton)', body, re.I):
                mo = MON[mm.group(1).lower()]
                yr = int(mm.group(2)) if mm.group(2) else int(d[:4])
                val = float(mm.group(3).replace('.', ''))
                if not (2000 < val < 9000):
                    continue
                key = yr * 12 + mo
                if cur <= key <= cur + 8:
                    rows.setdefault(key, val)
            if len(rows) >= 4:
                fcpo[d] = [['%04d-%02d' % ((k2 - 1) // 12, (k2 - 1) % 12 + 1), v]
                           for k2, v in sorted(rows.items())]
    return kpbn, fcpo


# ---------- MPOB ----------
def fetch_mpob():
    url = ('https://bepi.mpob.gov.my/admin2/'
           'price_local_daily_view_cpo_msia.php?jenis=1M&more=Y')
    html = get(url)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S | re.I)
    year = dt.date.today().year
    hdr, body = None, []
    for r in rows:
        cells = [strip(c).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S | re.I)]
        if not cells:
            continue
        if cells[0] == 'Date' and len(cells) > 10:
            hdr = cells
            continue
        if hdr:
            body.append(cells)
    if not hdr:
        raise RuntimeError('MPOB header row not found')
    out = {}
    for cells in body:
        try:
            day = int(cells[0])
        except ValueError:
            continue
        for j in range(1, min(len(cells), len(hdr))):
            mo = EN.get(hdr[j])
            if not mo:
                continue
            v = cells[j].replace(',', '')
            if re.fullmatch(r'\d+(\.\d+)?', v):
                out['%d-%02d-%02d' % (year, mo, day)] = float(v)
    return out


def merge(old_pairs, new_map):
    d = {k: v for k, v in old_pairs}
    added = [k for k in new_map if k not in d]
    d.update(new_map)
    return sorted([[k, v] for k, v in d.items()]), len(added)


def main():
    data = json.load(open(PATH, encoding='utf-8'))
    changed = []

    try:
        kpbn, fcpo = fetch_infosawit()
        if kpbn:
            data['kpbn'], n = merge(data['kpbn'], kpbn)
            if n:
                changed.append('kpbn +%d' % n)
        if fcpo:
            latest = max(fcpo)
            if latest > data.get('fcpo', {}).get('date', ''):
                data['fcpo'] = {'date': latest, 'rows': fcpo[latest]}
                changed.append('fcpo %s' % latest)
    except Exception as e:
        print('KPBN/FCPO step failed: %s' % e, file=sys.stderr)

    try:
        mpob = fetch_mpob()
        if mpob:
            data['mpob'], n = merge(data['mpob'], mpob)
            if n:
                changed.append('mpob +%d' % n)
    except Exception as e:
        print('MPOB step failed: %s' % e, file=sys.stderr)

    data['updated'] = dt.date.today().isoformat()
    json.dump(data, open(PATH, 'w', encoding='utf-8'),
              ensure_ascii=False, separators=(',', ':'))
    print('updated:', ', '.join(changed) if changed else 'no new observations')


if __name__ == '__main__':
    main()
