#!/usr/bin/env python3
"""End-to-end live tests for stacked-prs against a real GitHub repo.

Creates uniquely-named branches per run (no collision across invocations),
opens PRs, drives each scenario, and asserts on outcomes. Each scenario
runs ``stack_manager.py`` in a temp sandbox so the driver's own git state
is untouched.

Requires:
  * ``gh`` authenticated with ``repo`` scope
  * ``TEST_REPO`` env var pointing at a throwaway repo with a ``main``
    branch that has at least one commit (will be pushed-to and modified)

Usage::

    TEST_REPO=your-user/stacked-prs-e2e python tests/e2e_live.py

Exits non-zero on any scenario failure. Prints per-scenario result.

Scenarios
---------
1. Happy path -- bottom merges, retarget + rebase cascade.
2. Recorded parent_sha re-used on a second cascade.
3. Original bug: parent force-pushed + ref deleted before child retargeting.
4. External restack: child rebased externally -> parent_sha goes stale
   -> detected and re-seeded.
5. Legit conflict in child -> reported on PR, cascade stops.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
STACK_MANAGER = REPO_ROOT / "stack_manager.py"


# ── subprocess helpers ─────────────────────────────────────────


def _run(cmd, cwd=None, check=True, env=None, capture=True):
    kwargs = dict(cwd=cwd, text=True, env=env)
    if capture:
        kwargs["capture_output"] = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        out = result.stdout if capture else ""
        err = result.stderr if capture else ""
        raise RuntimeError(f"{' '.join(cmd)} failed\n{err}\n{out}")
    return result


def git(*args, cwd, check=True):
    return _run(["git", *args], cwd=cwd, check=check)


def git_out(*args, cwd):
    return _run(["git", *args], cwd=cwd).stdout.strip()


def gh(*args, check=True):
    return _run(["gh", *args], check=check)


def gh_json(*args):
    return json.loads(_run(["gh", *args]).stdout)


# ── scenario helpers ───────────────────────────────────────────


def unique_prefix():
    return f"e2e-{int(time.time())}-{secrets.token_hex(2)}"


class TestClone:
    """A throwaway local clone of the test repo for setting up branches."""

    def __init__(self, test_repo: str):
        self.test_repo = test_repo
        self.dir = Path(tempfile.mkdtemp(prefix="sp-e2e-clone-"))
        gh("repo", "clone", test_repo, str(self.dir), "--", "-q")
        git("config", "user.name", "e2e-test", cwd=self.dir)
        git("config", "user.email", "e2e@test.local", cwd=self.dir)
        git("checkout", "-q", "main", cwd=self.dir)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def push_branch(self, branch: str, filename: str, content: str):
        git("checkout", "-qb", branch, cwd=self.dir)
        (self.dir / filename).write_text(content)
        git("add", filename, cwd=self.dir)
        git("commit", "-qm", f"{branch}: {filename}={content}", cwd=self.dir)
        git("push", "-q", "-u", "origin", branch, cwd=self.dir)
        return git_out("rev-parse", "HEAD", cwd=self.dir)


def open_pr_number(test_repo: str, head: str, base: str) -> int:
    """gh pr create doesn't support --json so we parse the URL."""
    result = _run([
        "gh", "pr", "create",
        "--repo", test_repo,
        "--head", head, "--base", base,
        "--title", head, "--body", "e2e",
    ])
    # last line is the PR URL; PR number is the trailing path component.
    url = result.stdout.strip().splitlines()[-1]
    return int(url.rsplit("/", 1)[-1])


def setup_sandbox() -> Path:
    """Isolated dir mirroring the stacked-prs checkout: a copy of
    stack_manager.py, an empty stacks/, and a git repo so the script's
    post-run ``git add stacks/`` doesn't blow up before the expected
    final ``git push`` failure (no remote)."""
    sandbox = Path(tempfile.mkdtemp(prefix="sp-e2e-sbx-"))
    shutil.copy(STACK_MANAGER, sandbox)
    (sandbox / "stacks").mkdir()
    git("init", "-qb", "master", cwd=sandbox)
    git("config", "user.name", "e2e", cwd=sandbox)
    git("config", "user.email", "e2e@test.local", cwd=sandbox)
    (sandbox / ".gitignore").write_text("")
    git("add", "-A", cwd=sandbox)
    git("commit", "-qm", "init", cwd=sandbox)
    return sandbox


def run_manager(sandbox: Path, stack: dict) -> tuple[str, dict]:
    """Write the stack yaml, run stack_manager.py, return (stdout, final yaml)."""
    yaml_path = sandbox / "stacks" / "stack.yml"
    yaml_path.write_text(yaml.dump(stack, sort_keys=False))
    env = {**os.environ, "GH_TOKEN": _run(["gh", "auth", "token"]).stdout.strip()}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "stack_manager.py"],
        cwd=sandbox, capture_output=True, text=True, env=env,
    )
    # The script exits non-zero on the final ``git push`` (no remote in
    # the sandbox) -- that's expected. Any earlier failure bubbles up as
    # an assertion on stdout/stderr content.
    final = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else None
    return result.stdout + result.stderr, final


def cleanup_branches(test_repo: str, branches: list[str]):
    for b in branches:
        _run(["gh", "api", "-X", "DELETE", f"repos/{test_repo}/git/refs/heads/{b}"],
             check=False)


def wait_mergeable(test_repo: str, pr: int, timeout_s: int = 30):
    """GitHub computes mergeability asynchronously after pushes. Polls
    until it resolves or the timeout elapses. Non-fatal on timeout --
    caller can still try ``gh pr merge``."""
    for _ in range(timeout_s):
        state = gh_json("pr", "view", str(pr), "--repo", test_repo,
                        "--json", "mergeable")
        if state.get("mergeable") in ("MERGEABLE", "CONFLICTING"):
            return state["mergeable"]
        time.sleep(1)
    return "UNKNOWN"


def merge_pr(test_repo: str, pr: int):
    wait_mergeable(test_repo, pr)
    gh("pr", "merge", str(pr), "--repo", test_repo, "--squash")


# ── scenarios ──────────────────────────────────────────────────


def scenario_happy_path(test_repo: str):
    prefix = unique_prefix()
    branches = [f"{prefix}-a", f"{prefix}-b", f"{prefix}-c"]
    clone = TestClone(test_repo)
    try:
        clone.push_branch(branches[0], f"{branches[0]}.txt", "A")
        clone.push_branch(branches[1], f"{branches[1]}.txt", "B")
        clone.push_branch(branches[2], f"{branches[2]}.txt", "C")
        pr_a = open_pr_number(test_repo, branches[0], "main")
        pr_b = open_pr_number(test_repo, branches[1], branches[0])
        pr_c = open_pr_number(test_repo, branches[2], branches[1])

        merge_pr(test_repo, pr_a)

        stack = {
            "repo": test_repo, "base": "main",
            "prs": [
                {"branch": branches[0], "pr": pr_a, "status": "open"},
                {"branch": branches[1], "pr": pr_b, "status": "open"},
                {"branch": branches[2], "pr": pr_c, "status": "open"},
            ],
        }
        sandbox = setup_sandbox()
        try:
            out, final = run_manager(sandbox, stack)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

        assert f"PR #{pr_a}" in out and "merged" in out, "bottom not detected"
        assert final is not None and len(final["prs"]) == 2, "expected 2 remaining"
        for pr in final["prs"]:
            assert "parent_sha" in pr and pr["parent_sha"], f"missing parent_sha: {pr}"
        b_state = gh_json("pr", "view", str(pr_b), "--repo", test_repo,
                          "--json", "baseRefName,mergeable,state")
        assert b_state["baseRefName"] == "main", f"pr_b not retargeted: {b_state}"
        assert b_state["state"] == "OPEN", f"pr_b not open: {b_state}"
    finally:
        clone.close()
        cleanup_branches(test_repo, branches)


def scenario_recorded_parent_sha_reused(test_repo: str):
    """After a successful cascade, records parent_sha. A second merge
    (one level up) should re-use the recorded value without the 'stale'
    message."""
    prefix = unique_prefix()
    branches = [f"{prefix}-a", f"{prefix}-b"]
    clone = TestClone(test_repo)
    try:
        clone.push_branch(branches[0], f"{branches[0]}.txt", "A")
        clone.push_branch(branches[1], f"{branches[1]}.txt", "B")
        pr_a = open_pr_number(test_repo, branches[0], "main")
        pr_b = open_pr_number(test_repo, branches[1], branches[0])

        # First cascade: seed parent_sha
        merge_pr(test_repo, pr_a)
        stack = {
            "repo": test_repo, "base": "main",
            "prs": [
                {"branch": branches[0], "pr": pr_a, "status": "open"},
                {"branch": branches[1], "pr": pr_b, "status": "open"},
            ],
        }
        sandbox = setup_sandbox()
        try:
            _, final = run_manager(sandbox, stack)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        assert final and len(final["prs"]) == 1
        recorded_parent = final["prs"][0]["parent_sha"]

        # Second cascade with the recorded value carried forward.
        merge_pr(test_repo, pr_b)
        stack2 = {"repo": test_repo, "base": "main", "prs": final["prs"]}
        sandbox = setup_sandbox()
        try:
            out, final2 = run_manager(sandbox, stack2)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        assert "stale" not in out, f"unexpected stale re-seed:\n{out}"
        # After pr_b merges, the stack empties.
        assert final2 and final2["prs"] == [], f"expected empty, got {final2}"
        # The previously-recorded parent_sha is what was used.
        assert recorded_parent, "parent_sha was missing in first pass"
    finally:
        clone.close()
        cleanup_branches(test_repo, branches)


def scenario_stale_snapshot_parent_deleted(test_repo: str):
    """The original bug: parent force-pushed, then its branch deleted
    before the cascade runs. The YAML's stored 'sha' for the merged
    parent is stale-but-still-an-ancestor of the child, so seeding picks
    it up and produces the correct --onto anchor."""
    prefix = unique_prefix()
    branches = [f"{prefix}-x", f"{prefix}-y"]
    clone = TestClone(test_repo)
    try:
        x_old = clone.push_branch(branches[0], f"{branches[0]}.txt", "X")
        clone.push_branch(branches[1], f"{branches[1]}.txt", "Y")
        pr_x = open_pr_number(test_repo, branches[0], "main")
        pr_y = open_pr_number(test_repo, branches[1], branches[0])

        # Force-push new tip on x, child NOT restacked
        git("checkout", "-q", branches[0], cwd=clone.dir)
        git("commit", "-q", "--amend", "-m", "X amended", cwd=clone.dir)
        git("push", "-qf", "origin", branches[0], cwd=clone.dir)

        # Wait briefly so GitHub computes mergeability
        for _ in range(10):
            s = gh_json("pr", "view", str(pr_x), "--repo", test_repo,
                        "--json", "mergeable")
            if s.get("mergeable") == "MERGEABLE":
                break
            time.sleep(1)

        merge_pr(test_repo, pr_x)
        # Simulate auto-delete-on-merge (but AFTER the merge, so the
        # child PR has no chance to be retargeted first).
        _run(["gh", "api", "-X", "DELETE",
              f"repos/{test_repo}/git/refs/heads/{branches[0]}"],
             check=False)

        # YAML deliberately carries the PRE-amend sha as the snapshot
        # (this is the race the fix targets).
        stack = {
            "repo": test_repo, "base": "main",
            "prs": [
                {"branch": branches[0], "pr": pr_x, "status": "open", "sha": x_old},
                {"branch": branches[1], "pr": pr_y, "status": "open"},
            ],
        }
        sandbox = setup_sandbox()
        try:
            out, final = run_manager(sandbox, stack)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

        assert f"parent_sha for {branches[1]}: {x_old[:12]}" in out, \
            f"expected last_merged.sha seed of {x_old[:12]}, got:\n{out}"
        assert final and len(final["prs"]) == 1
        entry = final["prs"][0]
        assert entry["branch"] == branches[1]
        assert entry["status"] == "open"
        # Verify the rebase actually succeeded: branch should now have a
        # single unique commit on top of origin/main.
        git("fetch", "-q", "origin", branches[1], "main", cwd=clone.dir)
        count = int(git_out("rev-list", "--count",
                            f"origin/main..origin/{branches[1]}", cwd=clone.dir))
        assert count == 1, f"expected 1 unique commit after rebase, got {count}"
    finally:
        clone.close()
        cleanup_branches(test_repo, branches)


def scenario_external_restack_detects_stale(test_repo: str):
    """User externally restacks child onto a force-pushed parent. The
    recorded parent_sha is no longer an ancestor of the child; the bot
    must detect and re-seed."""
    prefix = unique_prefix()
    branches = [f"{prefix}-m", f"{prefix}-n", f"{prefix}-o"]
    clone = TestClone(test_repo)
    try:
        clone.push_branch(branches[0], f"{branches[0]}.txt", "M")
        clone.push_branch(branches[1], f"{branches[1]}.txt", "N")
        clone.push_branch(branches[2], f"{branches[2]}.txt", "O")
        pr_m = open_pr_number(test_repo, branches[0], "main")
        pr_n = open_pr_number(test_repo, branches[1], branches[0])
        pr_o = open_pr_number(test_repo, branches[2], branches[1])

        # First cascade: seed parent_sha for n and o.
        merge_pr(test_repo, pr_m)
        stack1 = {
            "repo": test_repo, "base": "main",
            "prs": [
                {"branch": branches[0], "pr": pr_m, "status": "open"},
                {"branch": branches[1], "pr": pr_n, "status": "open"},
                {"branch": branches[2], "pr": pr_o, "status": "open"},
            ],
        }
        sandbox = setup_sandbox()
        try:
            _, final1 = run_manager(sandbox, stack1)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        assert final1 and len(final1["prs"]) == 2
        o_recorded = final1["prs"][1]["parent_sha"]  # old n tip

        # External restack: amend n, rebase o onto new n tip, force-push both.
        git("fetch", "-q", "origin", branches[1], branches[2], cwd=clone.dir)
        git("checkout", "-qB", branches[1], f"origin/{branches[1]}", cwd=clone.dir)
        git("commit", "-q", "--amend", "-m", "N amended externally", cwd=clone.dir)
        new_n = git_out("rev-parse", "HEAD", cwd=clone.dir)
        git("push", "-qf", "origin", branches[1], cwd=clone.dir)
        git("checkout", "-qB", branches[2], f"origin/{branches[2]}", cwd=clone.dir)
        git("rebase", "--onto", new_n, o_recorded, cwd=clone.dir)
        git("push", "-qf", "origin", branches[2], cwd=clone.dir)

        # Wait for mergeability recompute
        for _ in range(10):
            s = gh_json("pr", "view", str(pr_n), "--repo", test_repo,
                        "--json", "mergeable")
            if s.get("mergeable") == "MERGEABLE":
                break
            time.sleep(1)

        # Second cascade with the stale parent_sha on o.
        merge_pr(test_repo, pr_n)
        stack2 = {"repo": test_repo, "base": "main", "prs": final1["prs"]}
        sandbox = setup_sandbox()
        try:
            out, final2 = run_manager(sandbox, stack2)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

        assert f"parent_sha for {branches[2]} is stale" in out, \
            f"expected stale detection, got:\n{out}"
        assert final2 and len(final2["prs"]) == 1
        assert final2["prs"][0]["status"] == "open"
    finally:
        clone.close()
        cleanup_branches(test_repo, branches)


def scenario_conflict_reported(test_repo: str):
    """Child's rebase onto new base truly conflicts -> status: conflict,
    cascade stops, comment posted."""
    prefix = unique_prefix()
    branches = [f"{prefix}-r", f"{prefix}-s"]
    shared_file = f"{prefix}-shared.txt"
    clone = TestClone(test_repo)
    try:
        clone.push_branch(branches[0], shared_file, "R")
        # s edits the same file so its patch context depends on "R"
        git("checkout", "-qb", branches[1], cwd=clone.dir)
        (clone.dir / shared_file).write_text("R and S")
        git("add", shared_file, cwd=clone.dir)
        git("commit", "-qm", f"s edits {shared_file}", cwd=clone.dir)
        git("push", "-q", "-u", "origin", branches[1], cwd=clone.dir)
        pr_r = open_pr_number(test_repo, branches[0], "main")
        pr_s = open_pr_number(test_repo, branches[1], branches[0])

        # Merge r (squash so main gets "R"), then rewrite the same file
        # on main directly so s's context-line "R" is gone -> conflict.
        merge_pr(test_repo, pr_r)
        git("fetch", "-q", "origin", "main", cwd=clone.dir)
        git("checkout", "-q", "main", cwd=clone.dir)
        git("reset", "-q", "--hard", "origin/main", cwd=clone.dir)
        (clone.dir / shared_file).write_text("COMPLETELY DIFFERENT")
        git("commit", "-qam", f"main: overwrite {shared_file}", cwd=clone.dir)
        git("push", "-q", "origin", "main", cwd=clone.dir)

        stack = {
            "repo": test_repo, "base": "main",
            "prs": [
                {"branch": branches[0], "pr": pr_r, "status": "open"},
                {"branch": branches[1], "pr": pr_s, "status": "open"},
            ],
        }
        sandbox = setup_sandbox()
        try:
            out, final = run_manager(sandbox, stack)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

        assert "rebase conflict" in out.lower() or "CONFLICT" in out, \
            f"expected conflict, got:\n{out}"
        assert final and len(final["prs"]) == 1
        assert final["prs"][0]["status"] == "conflict"
    finally:
        clone.close()
        cleanup_branches(test_repo, branches)


SCENARIOS = [
    ("1. happy path cascade", scenario_happy_path),
    ("2. recorded parent_sha re-used", scenario_recorded_parent_sha_reused),
    ("3. stale snapshot + parent branch deleted", scenario_stale_snapshot_parent_deleted),
    ("4. external restack -> stale detection", scenario_external_restack_detects_stale),
    ("5. legit conflict reported", scenario_conflict_reported),
]


def main():
    test_repo = os.environ.get("TEST_REPO", "").strip()
    if not test_repo:
        print("ERROR: set TEST_REPO env var to an owner/repo that can be"
              " written to (branches created, PRs opened, commits to main).")
        return 2

    failures = []
    for name, fn in SCENARIOS:
        print(f"\n== {name} ==", flush=True)
        t0 = time.time()
        try:
            fn(test_repo)
            print(f"   PASS ({time.time() - t0:.1f}s)")
        except AssertionError as exc:
            print(f"   FAIL: {exc}")
            failures.append(name)
        except Exception as exc:
            print(f"   ERROR: {exc!r}")
            failures.append(name)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAIL -- {len(failures)}/{len(SCENARIOS)} scenarios:")
        for n in failures:
            print(f"  - {n}")
        return 1
    print(f"PASS -- {len(SCENARIOS)}/{len(SCENARIOS)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
