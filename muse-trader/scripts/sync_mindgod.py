#!/usr/bin/env python3
"""Mirror edge-engine into the private manishsaha/mindgod repo under muse-trader/.

Uses the Git Data API with the user-connected custom.github credential
(surrogate auth through authd; the script never sees the real token).
Creates one commit per invocation and fast-forwards main (no force push).

Usage:
    sync_mindgod.py --group code -m "muse-trader: initial scaffold"
    sync_mindgod.py --group docs -m "muse-trader: architecture docs"
    sync_mindgod.py -m "muse-trader: fee-aware EV fixes"   # mirrors everything
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_response_body

API_BASE = "https://api.github.com"
REPO = "manishsaha/mindgod"
DEST_PREFIX = "muse-trader"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".db", ".pyc", ".pyo"}

# Commit 1 (initial state): code. Commit 2: docs. Later updates: everything.
GROUPS = {
    "code": ["src", "tests", "scripts", "pyproject.toml", "config.example.yaml",
             ".env.example", "README.md"],
    "docs": ["docs", "infra"],
}


def api(method: str, path: str, data: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(API_BASE + path, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "muse-agent")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, "custom.github", entry_name="access_token",
                             allowed_hosts=["api.github.com"])
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = read_response_body(resp)
    except urllib.error.HTTPError as exc:
        raw = read_response_body(exc)
        try:
            msg = json.loads(raw.decode()) .get("message", raw.decode()[:300])
        except Exception:
            msg = raw.decode(errors="replace")[:300]
        raise RuntimeError(f"GitHub {method} {path}: HTTP {exc.code}: {msg}")
    return json.loads(raw.decode()) if raw.strip() else {}


def collect_files(group: str) -> list[Path]:
    members = GROUPS["code"] + GROUPS["docs"] if group == "all" else GROUPS[group]
    files: list[Path] = []
    for member in members:
        p = PROJECT_ROOT / member
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.suffix in EXCLUDE_SUFFIXES:
                    continue
                if any(part in EXCLUDE_DIRS for part in f.parts):
                    continue
                files.append(f)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("--group", choices=["code", "docs", "all"], default="all")
    args = ap.parse_args()

    files = collect_files(args.group)
    if not files:
        print("no files matched", file=sys.stderr)
        return 1
    print(f"mirroring {len(files)} files (group={args.group})")

    ref = api("GET", f"/repos/{REPO}/git/ref/heads/main")
    base_sha = ref["object"]["sha"]

    tree_entries = []
    for f in files:
        rel = f.relative_to(PROJECT_ROOT).as_posix()
        blob = api("POST", f"/repos/{REPO}/git/blobs", {
            "content": base64.b64encode(f.read_bytes()).decode(),
            "encoding": "base64",
        })
        tree_entries.append({
            "path": f"{DEST_PREFIX}/{rel}",
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
    print(f"created {len(tree_entries)} blobs")

    tree = api("POST", f"/repos/{REPO}/git/trees",
               {"base_tree": base_sha, "tree": tree_entries})
    commit = api("POST", f"/repos/{REPO}/git/commits", {
        "message": args.message,
        "tree": tree["sha"],
        "parents": [base_sha],
    })
    api("PATCH", f"/repos/{REPO}/git/refs/heads/main",
        {"sha": commit["sha"]})  # fast-forward only; fails if raced
    print(f"committed {commit['sha'][:7]} -> main")
    print(f"https://github.com/{REPO}/tree/main/{DEST_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
