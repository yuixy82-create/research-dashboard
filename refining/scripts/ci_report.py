#!/usr/bin/env python3
"""auto.json 상태를 로그에 찍고, ok가 아니면 워크플로를 실패로 끝낸다."""
import json, pathlib, sys
d = json.loads(pathlib.Path("refining/data/auto.json").read_text(encoding="utf-8"))
print("status:", d.get("status"))
for k, v in (d.get("indicators") or {}).items():
    print(f"  {k}: {v.get('value')} {v.get('unit')} ({v.get('asOf')})" + ("  [잠정]" if v.get("note") == "장중 잠정치" else ""))
for e in (d.get("errors") or []):
    print("  ERR", e)
sys.exit(0 if d.get("status") == "ok" else 1)
