#!/usr/bin/env python3
"""refining/data의 값 변경을 GitHub API로 커밋한다. git 명령을 쓰지 않는다.

self-hosted 러너(형 메인컴)의 서비스 계정에는 git이 없고, checkout@v4도 git 없이
API로 파일만 내려받는다. 그래서 커밋도 Git Data API(blob → tree → commit → ref)로 만든다.

updatedAt만 바뀐 파일은 무시한다(빈 커밋 방지). 값이 바뀐 파일만 한 커밋에 담는다.
필요 환경변수: GITHUB_TOKEN · GITHUB_REPOSITORY (Actions가 넣어줌)
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = os.environ.get("GITHUB_TOKEN") or ""
REPO = os.environ.get("GITHUB_REPOSITORY") or "yuixy82-create/research-dashboard"
BRANCH = os.environ.get("GITHUB_REF_NAME") or "main"
API = f"https://api.github.com/repos/{REPO}"
DATA = Path("refining/data")


def gh(path, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "research-dashboard-ci",
    }, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}")


def remote_json(path):
    """HEAD에 있는 파일 내용. 없으면 None."""
    try:
        d = gh(f"/contents/{path}?ref={BRANCH}")
    except RuntimeError as e:
        if " 404 " in str(e):
            return None
        raise
    return json.loads(base64.b64decode(d["content"]).decode("utf-8"))


def changed_files():
    out = []
    for f in sorted(DATA.rglob("*.json")):
        if f.name == "manual.json":            # 손으로 고치는 파일. 봇이 건드리지 않음
            continue
        rel = f.as_posix()
        try:
            new = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        old = remote_json(rel)
        if old is None:
            out.append(rel); continue
        a, b = dict(old), dict(new)
        a.pop("updatedAt", None); b.pop("updatedAt", None)
        if a != b:
            out.append(rel)
    return out


def main():
    if not TOKEN:
        print("GITHUB_TOKEN 없음"); return 1
    files = changed_files()
    if not files:
        print("커밋할 변경 없음 (값 동일 또는 타임스탬프만 변경)"); return 0
    print("값이 바뀐 파일:", files)

    head = gh(f"/git/ref/heads/{BRANCH}")["object"]["sha"]
    base_tree = gh(f"/git/commits/{head}")["tree"]["sha"]
    tree = []
    for rel in files:
        blob = gh("/git/blobs", "POST", {
            "content": Path(rel).read_text(encoding="utf-8"), "encoding": "utf-8"})
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = gh("/git/trees", "POST", {"base_tree": base_tree, "tree": tree})["sha"]
    commit = gh("/git/commits", "POST", {
        "message": "refining: daily update",
        "tree": new_tree, "parents": [head],
        "author": {"name": "tracker-bot",
                   "email": "41898282+github-actions[bot]@users.noreply.github.com"},
    })["sha"]
    gh(f"/git/refs/heads/{BRANCH}", "PATCH", {"sha": commit, "force": False})
    print("커밋 완료:", commit[:7], f"({len(files)}개 파일)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
