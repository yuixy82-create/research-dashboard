// 아마존 센텔리안24 순위 수집기: amazon.com 탭에서 javascript_tool로 실행
// 1회차 호출: 수집 시작('started' 반환). 결과는 window.__cent에 JSON 문자열로 쌓임
// 2회차: window.__cent 를 읽음 (null이면 아직 진행 중, 10초 뒤 다시)
// 결과: {ts, found, ok, blocked, nerr, rest, top:[[asin, 뷰티전체순위, 세부순위, 세부카테고리, 제품명]]}
window.__cent = null;
(async () => {
  const SEED = ['B0DFY92NDB','B0DJ8FQDYL','B0H79K9743','B0FVY91T1Q','B0D2XPMCJK','B0H627CCBF',
    'B0GQ753N3D','B0GL8DSLBF','B0BS2T3QYF','B0BYJF5SJW','B0DFYFG49H','B0GWJW37NW'];
  const QUERIES = ['centellian24', 'centellian+24+madeca+cream', 'madeca+cream+centellian', 'centellian+24+pdrn'];
  const errors = [];
  let blocked = false;
  const get = async (url) => {
    for (let i = 0; i < 2; i++) {
      try {
        const r = await fetch(url, { credentials: 'include' });
        const h = await r.text();
        if (/captcha|Type the characters you see/i.test(h) && !/productTitle|s-search-result/.test(h)) { blocked = true; return null; }
        if (r.ok) return h;
        errors.push(url + ' HTTP ' + r.status);
      } catch (e) { errors.push(url + ' ' + e); }
      await new Promise(r => setTimeout(r, 1500));
    }
    return null;
  };
  const asins = new Set(SEED);
  for (const q of QUERIES) {
    const h = await get('https://www.amazon.com/s?k=' + q);
    if (!h) continue;
    const d = new DOMParser().parseFromString(h, 'text/html');
    d.querySelectorAll('[data-asin]').forEach(e => {
      const a = e.dataset.asin;
      if (a && /centellian/i.test(e.textContent || '')) asins.add(a);
    });
  }
  const products = [];
  const one = async (a) => {
    const h = await get('https://www.amazon.com/dp/' + a);
    if (!h) return;
    const d = new DOMParser().parseFromString(h, 'text/html');
    const title = ((d.querySelector('#productTitle') || {}).textContent || '').trim().replace(/\s+/g, ' ');
    if (!/centellian/i.test(title + ' ' + ((d.querySelector('#bylineInfo') || {}).textContent || ''))) return;
    let bsr = '';
    d.querySelectorAll('#detailBulletsWrapper_feature_div li, #productDetails_detailBullets_sections1 tr, #prodDetails tr, #detailBullets_feature_div li')
      .forEach(e => { if (!bsr && /Best Sellers Rank/i.test(e.textContent)) bsr = e.textContent.replace(/\s+/g, ' '); });
    const ranks = [...bsr.matchAll(/#([\d,]+) in ([^#(]+?)(?=\s*(?:\(|#|$))/g)].map(m => ({ rank: +m[1].replace(/,/g, ''), cat: m[2].trim() }));
    const top = ranks.find(r => /Beauty & Personal Care/i.test(r.cat));
    products.push({ asin: a, title: title.slice(0, 140), overall: top ? top.rank : null,
      subs: ranks.filter(r => r !== top) });
  };
  const list = [...asins];
  for (let i = 0; i < list.length; i += 4) await Promise.all(list.slice(i, i + 4).map(one));
  products.sort((x, y) => (x.overall || 1e9) - (y.overall || 1e9));
  // 출력 창이 짧아 뷰티 전체 2만 위 안쪽만 행으로 남기고 나머지는 개수만 센다
  const short = c => c.replace('Facial ', '').replace(' & Astringents', '').slice(0, 24);
  const top = products.filter(p => p.overall && p.overall <= 20000).slice(0, 12).map(p => {
    const s = p.subs[0] || {};
    return [p.asin, p.overall, s.rank || null, short(s.cat || ''), p.title.replace(/CENTELLIAN ?24/i, '').trim().slice(0, 34)];
  });
  return JSON.stringify({ ts: new Date().toISOString(), found: asins.size, ok: products.length,
    blocked, nerr: errors.length, rest: products.length - top.length, top });
})().then(r => { window.__cent = r; }, e => { window.__cent = JSON.stringify({ fatal: String(e) }); });
'started'
