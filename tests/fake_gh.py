#!/usr/bin/env python3
"""Fake ``gh`` CLI used by the offline test harness.

Delegates state inspection + mutation + rule application to
``gh_state.py``. Each command runs ``converge`` before returning, so
side effects from previous test operations (branch deletions,
auto-delete-on-merge cascades, etc.) are always visible.

Supported commands (only what ``stack_manager.py`` actually uses):
  * gh auth token
  * gh pr view <N> --repo <R> --json <fields>
  * gh pr edit <N> --repo <R> --base <B>
  * gh pr comment <N> --repo <R> --body <body>
  * gh api repos/<R>/git/ref/heads/<B>
  * gh api -X DELETE repos/<R>/git/refs/heads/<B>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_state as S  # noqa: E402


def _state_path() -> Path:
    p = os.environ.get("FAKE_GH_STATE")
    if not p:
        _die("FAKE_GH_STATE not set", 2)
    return Path(p)


def _die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _emit_err(err: S.GhError, code: int = 1):
    """Shape of ``gh`` failures: nonzero exit + HTTP NNN line on stderr.
    ``stack_manager.RuntimeError`` format uses this for reporting."""
    print(f"HTTP {err.http_status}: {err.message}", file=sys.stderr)
    sys.exit(code)


def _flag(args, name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        _die(f"fake_gh: missing flag {name}")


# ── commands ───────────────────────────────────────────────────


def cmd_auth_token(args):
    print("fake-token-offline")


def cmd_pr_view(args):
    pr_num = args[0]
    repo = _flag(args, "--repo")
    fields = _flag(args, "--json").split(",")
    path = _state_path()
    state = S.read(path)
    try:
        data = S.check_repo_accessible(state, repo)
        S.tick_request(state, repo)
    except S.GhError as err:
        _emit_err(err)
    S.converge(state)
    S.write(path, state)

    pr = data["prs"].get(pr_num)
    if pr is None:
        _emit_err(S.GhError(404, f"PR #{pr_num} not found"))

    bare = data["bare_path"]
    out = {}
    for f in fields:
        if f == "state":
            out["state"] = pr["state"]
        elif f == "headRefOid":
            out["headRefOid"] = S.branch_sha(bare, pr["headRefName"])
        elif f == "baseRefName":
            out["baseRefName"] = pr["baseRefName"]
        elif f == "headRefName":
            out["headRefName"] = pr["headRefName"]
        elif f == "mergeable":
            out["mergeable"] = pr.get("mergeable", "MERGEABLE")
        else:
            _die(f"fake_gh: unsupported field '{f}'")
    S.write(path, state)
    print(json.dumps(out))


def cmd_pr_edit(args):
    pr_num = args[0]
    repo = _flag(args, "--repo")
    new_base = _flag(args, "--base")
    path = _state_path()
    state = S.read(path)
    try:
        S.check_repo_accessible(state, repo)
        S.tick_request(state, repo)
        S.check_retarget(state, repo, pr_num, new_base)
    except S.GhError as err:
        _emit_err(err)
    state["repos"][repo]["prs"][pr_num]["baseRefName"] = new_base
    S.converge(state)
    S.write(path, state)


def cmd_pr_comment(args):
    pr_num = args[0]
    repo = _flag(args, "--repo")
    body = _flag(args, "--body")
    path = _state_path()
    state = S.read(path)
    try:
        S.check_repo_accessible(state, repo)
        S.tick_request(state, repo)
    except S.GhError as err:
        _emit_err(err)
    pr = state["repos"][repo]["prs"].get(pr_num)
    if pr is None:
        _emit_err(S.GhError(404, f"PR #{pr_num} not found"))
    pr.setdefault("comments", []).append(body)
    S.write(path, state)


def cmd_api(args):
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
    api_path = path_args[0]
    parts = api_path.split("/")

    state_path_obj = _state_path()
    state = S.read(state_path_obj)

    # repos/<owner>/<repo>/git/ref(s)/heads/<branch>
    if (len(parts) >= 6 and parts[0] == "repos" and parts[3] == "git"
            and parts[4] in ("ref", "refs") and parts[5] == "heads"):
        repo = f"{parts[1]}/{parts[2]}"
        branch = "/".join(parts[6:])
        try:
            data = S.check_repo_accessible(state, repo)
            S.tick_request(state, repo)
        except S.GhError as err:
            _emit_err(err)

        bare = data["bare_path"]
        if method == "GET":
            sha = S.branch_sha(bare, branch)
            if sha is None:
                _emit_err(S.GhError(404, f"Reference 'refs/heads/{branch}' not found"))
            S.converge(state)
            S.write(state_path_obj, state)
            print(json.dumps({"object": {"sha": sha, "type": "commit"}}))
            return
        if method == "DELETE":
            S.delete_branch_on_bare(bare, branch)
            S.converge(state)
            S.write(state_path_obj, state)
            return

    _die(f"fake_gh: unsupported api path {api_path} method {method}")


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
