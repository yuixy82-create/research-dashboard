#!/usr/bin/env python3
"""값은 그대로인데 updatedAt만 바뀐 변경을 되돌린다. 빈 커밋 방지용.
윈도우 self-hosted 러너에서도 돌도록 bash 없이 파이썬만 쓴다."""
import json, subprocess, sys

def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, encoding="utf-8").stdout

files = sh("git", "diff", "--name-only", "--", "refining/data").split()
if not files:
    print("변경 없음"); sys.exit(0)
real = []
for f in files:
    try:
        old = json.loads(sh("git", "show", f"HEAD:{f}"))
        new = json.load(open(f, encoding="utf-8"))
    except Exception:
        real.append(f); continue
    old.pop("updatedAt", None); new.pop("updatedAt", None)
    if old != new:
        real.append(f)
print("값이 바뀐 파일:", real or "없음 (타임스탬프만 변경)")
if not real:
    subprocess.run(["git", "checkout", "--", "refining/data"])
