#!/usr/bin/env python3
"""refining/data에 변경이 있으면 커밋·푸시한다."""
import subprocess, sys

def sh(*a, check=False):
    r = subprocess.run(a, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        print(r.stdout, r.stderr); sys.exit(r.returncode)
    return r.stdout

if not sh("git", "status", "--porcelain", "refining/data").strip():
    print("커밋할 변경 없음"); sys.exit(0)
sh("git", "config", "user.name", "tracker-bot")
sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
sh("git", "add", "refining/data", check=True)
sh("git", "commit", "-m", "refining: daily update", check=True)
sh("git", "push", check=True)
print("커밋 · 푸시 완료")
