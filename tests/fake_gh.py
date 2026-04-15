#!/usr/bin/env python3
"""Fake ``gh`` CLI used by the offline test harness.

Reads/writes a JSON state file (``$FAKE_GH_STATE``) that models enough
of GitHub to drive ``stack_manager.py``:

    {
      "repos": {
        "test/repo": {
          "bare_path": "/path/to/local/bare.git",
          "prs": {
            "1": {
              "state": "MERGED",            # OPEN | MERGED | CLOSED
              "headRefName": "feat-a",
              "baseRefName": "main",
              "comments": []
            }
          }
        }
      }
    }

``headRefOid`` and the ``git/ref/heads/<branch>`` API response are both
computed dynamically by shelling out against the bare repo, so the tip
always matches whatever the test has done locally.

Supported commands (only what stack_manager.py actually uses):
  * gh auth token
  * gh pr view <N> --repo <R> --json state[,headRefOid,...]
  * gh pr edit <N> --repo <R> --base <B>
  * gh pr comment <N> --repo <R> --body <body>
  * gh api repos/<R>/git/ref/heads/<B>
  * gh api -X DELETE repos/<R>/git/refs/heads/<B>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _state_path() -> Path:
    p = os.environ.get("FAKE_GH_STATE")
    if not p:
        print("FAKE_GH_STATE not set", file=sys.stderr)
        sys.exit(2)
    return Path(p)


def _load() -> dict:
    return json.loads(_state_path().read_text())


def _save(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2))


def _die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _repo_sha(bare_path: str, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", bare_path, "rev-parse", ref],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def cmd_auth_token(args):
    print("fake-token-offline")


def cmd_pr_view(args):
    # gh pr view <N> --repo <R> --json <fields>
    pr_num = args[0]
    repo = _flag(args, "--repo")
    fields = _flag(args, "--json").split(",")
    state = _load()
    prs = state["repos"][repo]["prs"]
    if pr_num not in prs:
        _die(f"fake_gh: no PR #{pr_num} for {repo}")
    pr = prs[pr_num]
    bare = state["repos"][repo]["bare_path"]

    out = {}
    for f in fields:
        if f == "state":
            out["state"] = pr["state"]
        elif f == "headRefOid":
            # Tip of the head branch on the bare repo; None if deleted.
            out["headRefOid"] = _repo_sha(bare, f"refs/heads/{pr['headRefName']}")
        elif f == "baseRefName":
            out["baseRefName"] = pr.get("baseRefName", "main")
        elif f == "headRefName":
            out["headRefName"] = pr["headRefName"]
        elif f == "mergeable":
            out["mergeable"] = pr.get("mergeable", "MERGEABLE")
        else:
            _die(f"fake_gh: unsupported field '{f}'")
    print(json.dumps(out))


def cmd_pr_edit(args):
    pr_num = args[0]
    repo = _flag(args, "--repo")
    new_base = _flag(args, "--base")
    state = _load()
    state["repos"][repo]["prs"][pr_num]["baseRefName"] = new_base
    _save(state)


def cmd_pr_comment(args):
    pr_num = args[0]
    repo = _flag(args, "--repo")
    body = _flag(args, "--body")
    state = _load()
    pr = state["repos"][repo]["prs"][pr_num]
    pr.setdefault("comments", []).append(body)
    _save(state)


def cmd_api(args):
    # Two shapes:
    #   gh api repos/<R>/git/ref/heads/<B>
    #   gh api -X DELETE repos/<R>/git/refs/heads/<B>
    method = "GET"
    path_args = []
    i = 0
    while i < len(args):
        if args[i] == "-X":
            method = args[i + 1]
            i += 2
        else:
            path_args.append(args[i])
            i += 1
    path = path_args[0]
    state = _load()

    # repos/<owner>/<repo>/git/ref/heads/<branch>  (singular "ref" for GET)
    # repos/<owner>/<repo>/git/refs/heads/<branch> (plural "refs" for DELETE)
    parts = path.split("/")
    if len(parts) >= 6 and parts[0] == "repos" and parts[3] == "git":
        repo = f"{parts[1]}/{parts[2]}"
        branch = "/".join(parts[6:])
        bare = state["repos"][repo]["bare_path"]
        if method == "GET" and parts[4] == "ref" and parts[5] == "heads":
            sha = _repo_sha(bare, f"refs/heads/{branch}")
            if sha is None:
                print('{"message":"Not Found","status":"404"}', file=sys.stderr)
                sys.exit(1)
            print(json.dumps({"object": {"sha": sha, "type": "commit"}}))
            return
        if method == "DELETE" and parts[4] == "refs" and parts[5] == "heads":
            subprocess.run(
                ["git", "-C", bare, "update-ref", "-d", f"refs/heads/{branch}"],
                check=False,
            )
            return
    _die(f"fake_gh: unsupported api path {path} method {method}")


def _flag(args, name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        _die(f"fake_gh: missing flag {name}")


def main():
    argv = sys.argv[1:]
    if not argv:
        _die("fake_gh: no command")
    if argv[0] == "auth" and argv[1:2] == ["token"]:
        return cmd_auth_token(argv[2:])
    if argv[0] == "pr" and argv[1] == "view":
        return cmd_pr_view(argv[2:])
    if argv[0] == "pr" and argv[1] == "edit":
        return cmd_pr_edit(argv[2:])
    if argv[0] == "pr" and argv[1] == "comment":
        return cmd_pr_comment(argv[2:])
    if argv[0] == "api":
        return cmd_api(argv[1:])
    _die(f"fake_gh: unsupported command {argv[:2]}")


if __name__ == "__main__":
    main()
