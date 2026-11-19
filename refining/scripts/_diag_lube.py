#!/usr/bin/env python3
"""임시 진단: Group III 점이 26.10.26 이후 안 붙는 원인 확인. 파일을 고치지 않고 출력만 한다. 확인 뒤 삭제."""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_lube as L

price = L.load_series("group3_price")
seen = set(price.get("seen") or [])
print("seen", len(seen), "points", len(price.get("points", [])), "last", (price.get("points") or [{}])[-1].get("d"))
try:
    x = L.get(L.SITEMAP)
    print("sitemap len", len(x), "| xml?", "<urlset" in x, "| head:", re.sub(r"\s+", " ", x[:160]))
    items = L.sitemap()
except Exception as e:
    print("SITEMAP FAIL", e); sys.exit(0)
print("items", len(items), "new(not seen)", sum(1 for u, _ in items if u not in seen))
for u, lm in items[:14]:
    print(("NEW " if u not in seen else "seen"), lm[:19], u.replace("https://www.lubesngreases.com", ""))
for u, lm in items[:4]:
    time.sleep(5)
    try:
        h = L.get(u)
    except Exception as e:
        print("GET FAIL", u, e); continue
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h).replace("&nbsp;", " "))
    gi = t.rfind("Group III")
    m = re.search(r'"datePublished":"([^"]+)"', h) or re.search(r"Published Time:\s*([^\n]+)", h)
    print("---", u.rsplit("/", 2)[-2], "| len", len(h), "| html?", "<html" in h[:600].lower(), "| date", m.group(1)[:30] if m else None,
          "| GroupIII count", t.count("Group III"), "| $/t count", len(re.findall(r"\$[\d,]+/t", t)), "| parse", L.parse(h))
    print("    after last 'Group III':", t[gi:gi + 260] if gi >= 0 else "(없음)")
    for kw in ("subscribe", "Subscribe", "premium", "log in", "Log in"):
        if kw in t:
            j = t.find(kw); print("    kw[%s]:" % kw, t[max(0, j - 60):j + 100]); break
