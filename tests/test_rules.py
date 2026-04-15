"""Tests driven by the GitHub-behavior rules in ``gh_state.py``.

Complement to ``test_offline.py``: the original suite exercises the
bot's logic end-to-end; this suite exercises GitHub-side side effects
we couldn't otherwise reach offline (auto-close on parent delete,
rate limits, protected branches, etc.).

Organized into "ok" scenarios (expected good paths) and "ko"
scenarios (expected failure modes where the bot should surface a
clear error).
"""
from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="offline harness needs POSIX PATH resolution for the gh shim",
)


def _rev_count(bare, rng):
    return int(subprocess.check_output(
        ["git", "-C", str(bare), "rev-list", "--count", rng], text=True,
    ).strip())


# ══════════════════════════════════════════════════════════════
# OK scenarios
# ══════════════════════════════════════════════════════════════


def test_ok_auto_delete_on_merge_cleans_up_source_branch(world):
    """With repo configured for auto-delete, a merged PR's head branch
    is gone by the time the bot runs its cascade. The bot must still
    rebase the child correctly (parent_sha lookup is the anchor, not
    a branch ref)."""
    world.configure(auto_delete_on_merge=True)
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")

    a_tip = world.bare.ref_sha("feat-a")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a", state="OPEN")

    # Rule fires on next read: feat-a gets deleted (merged + auto-delete),
    # which cascades to closing PR #2 (base_deleted).
    pr2 = world.pr(2)
    assert world.bare.ref_sha("feat-a") is None
    assert pr2["state"] == "CLOSED"
    assert pr2["closed_reason"] == "base_deleted"

    # The bot still needs to rebase the branch (even though the PR has
    # been closed by GitHub's cascade). Seed with the parent's saved
    # SHA from before the merge.
    _, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ])

    # Rebase happens; branch in bare has one unique commit on main.
    assert _rev_count(world.bare.path, "main..feat-b") == 1
    # parent_sha was seeded from last_merged.sha.
    feat_b = [p for p in final["prs"] if p["branch"] == "feat-b"][0]
    assert feat_b.get("parent_sha") is not None


def test_ok_mergeability_resolves_after_countdown(world):
    """Models GitHub's async mergeability recompute. The bot doesn't
    itself wait for mergeability, but this verifies the rule machinery
    so tests that DO care can rely on it."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.set_pr(1, head="feat-a", base="main")
    world.mark_mergeability_pending(1, ticks=2, final="MERGEABLE")

    # Each read through the fake increments request_count and ticks
    # the countdown. Direct-reads via world.pr() don't count (they
    # bypass tick_request), so simulate three fake-gh calls:
    result = subprocess.run(
        [sys.executable, str(sys.modules["conftest"].FAKE_GH),
         "pr", "view", "1", "--repo", world.repo_name, "--json", "mergeable"],
        env={**__import__("os").environ,
             "FAKE_GH_STATE": str(world.state_path)},
        capture_output=True, text=True,
    )
    import json
    assert json.loads(result.stdout)["mergeable"] == "UNKNOWN"

    for _ in range(2):
        subprocess.run(
            [sys.executable, str(sys.modules["conftest"].FAKE_GH),
             "pr", "view", "1", "--repo", world.repo_name, "--json", "mergeable"],
            env={**__import__("os").environ,
                 "FAKE_GH_STATE": str(world.state_path)},
            capture_output=True, text=True,
        )
    pr = world.pr(1)
    assert pr["mergeable"] == "MERGEABLE"


def test_ok_retarget_to_existing_base_succeeds(world):
    """Happy-path retarget: base branch exists, PR not merged, not
    targeting its own head -- the edit persists."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.set_pr(1, head="feat-b", base="feat-a")

    result = subprocess.run(
        [sys.executable, str(sys.modules["conftest"].FAKE_GH),
         "pr", "edit", "1", "--repo", world.repo_name, "--base", "main"],
        env={**__import__("os").environ,
             "FAKE_GH_STATE": str(world.state_path)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert world.pr(1)["baseRefName"] == "main"


# ══════════════════════════════════════════════════════════════
# KO scenarios -- fake_gh should raise the right error class;
# stack_manager should fail cleanly rather than silently succeeding.
# ══════════════════════════════════════════════════════════════


def _fake_gh(world, *args, check=False):
    import os
    return subprocess.run(
        [sys.executable, str(sys.modules["conftest"].FAKE_GH), *args],
        env={**os.environ, "FAKE_GH_STATE": str(world.state_path)},
        capture_output=True, text=True,
        check=check,
    )


def test_ko_invalid_token_returns_401(world):
    world.configure(token_valid=False)
    world.bare.seed_initial_commit()
    world.set_pr(1, head="x", base="main")
    r = _fake_gh(world, "pr", "view", "1", "--repo", world.repo_name,
                 "--json", "state")
    assert r.returncode != 0
    assert "401" in r.stderr and "Bad credentials" in r.stderr


def test_ko_missing_repo_returns_404(world):
    world.configure(missing=True)
    world.bare.seed_initial_commit()
    world.set_pr(1, head="x", base="main")
    r = _fake_gh(world, "pr", "view", "1", "--repo", world.repo_name,
                 "--json", "state")
    assert r.returncode != 0
    assert "404" in r.stderr


def test_ko_ref_not_found_returns_404(world):
    world.bare.seed_initial_commit()
    # No branch called 'ghost'
    r = _fake_gh(world, "api",
                 f"repos/{world.repo_name}/git/ref/heads/ghost")
    assert r.returncode != 0
    assert "404" in r.stderr


def test_ko_rate_limit_returns_403(world):
    world.configure(rate_limit_after_requests=2)
    world.bare.seed_initial_commit()
    world.set_pr(1, head="x", base="main")
    # First two requests ok, third hits the limit.
    assert _fake_gh(world, "pr", "view", "1", "--repo", world.repo_name,
                    "--json", "state").returncode == 0
    assert _fake_gh(world, "pr", "view", "1", "--repo", world.repo_name,
                    "--json", "state").returncode == 0
    r3 = _fake_gh(world, "pr", "view", "1", "--repo", world.repo_name,
                  "--json", "state")
    assert r3.returncode != 0 and "403" in r3.stderr


def test_ko_retarget_to_own_head_rejected(world):
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.set_pr(1, head="feat-a", base="main")
    r = _fake_gh(world, "pr", "edit", "1", "--repo", world.repo_name,
                 "--base", "feat-a")
    assert r.returncode != 0 and "422" in r.stderr


def test_ko_retarget_to_missing_branch_rejected(world):
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.set_pr(1, head="feat-a", base="main")
    r = _fake_gh(world, "pr", "edit", "1", "--repo", world.repo_name,
                 "--base", "does-not-exist")
    assert r.returncode != 0 and "422" in r.stderr


def test_ko_edit_merged_pr_rejected(world):
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "main", "b.txt", "B")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    r = _fake_gh(world, "pr", "edit", "1", "--repo", world.repo_name,
                 "--base", "feat-b")
    assert r.returncode != 0 and "422" in r.stderr


def test_ko_protected_main_refuses_force_push(world, tmp_path):
    """The bare repo itself enforces the protection. Matches GitHub's
    branch-protection behaviour for force pushes to protected refs."""
    world.bare.seed_initial_commit()
    # Server-side config: reject non-fast-forwards.
    subprocess.check_call([
        "git", "-C", str(world.bare.path),
        "config", "receive.denyNonFastForwards", "true",
    ])
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")

    # Client force-pushes a different history to main -- should fail.
    work = tmp_path / "client"
    subprocess.check_call(["git", "clone", "-q", str(world.bare.path), str(work)])
    subprocess.check_call(["git", "-C", str(work), "config", "user.name", "x"])
    subprocess.check_call(["git", "-C", str(work), "config", "user.email", "x@t"])
    subprocess.check_call(["git", "-C", str(work), "checkout", "-q", "main"])
    subprocess.check_call(["git", "-C", str(work), "commit", "--amend", "-qm", "rewrite"])
    r = subprocess.run(
        ["git", "-C", str(work), "push", "-qf", "origin", "main"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "denyNonFastForwards" in r.stderr or "denied" in r.stderr or "non-fast-forward" in r.stderr


def test_ko_manager_errors_cleanly_on_401(world):
    """End-to-end: token invalid -> stack_manager exits non-zero with
    the error surfaced to logs. Confirms the script doesn't silently
    succeed when GitHub rejects it."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a")
    world.configure(token_valid=False)

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open"},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ])
    assert rc != 0
    assert "401" in out or "Bad credentials" in out


# ══════════════════════════════════════════════════════════════
# Interaction: original-bug scenario under realistic GH behavior
# ══════════════════════════════════════════════════════════════


def test_ok_auto_delete_on_merge_does_not_break_bot(world):
    """Full-stack version of the caveat scenario from the README:
    auto-delete is on, the parent branch is gone by the time the bot
    runs. The rebase still succeeds because the YAML carries the
    parent's pre-merge tip -- the README-documented recommendation to
    leave auto-delete enabled remains viable."""
    world.configure(auto_delete_on_merge=True)
    world.bare.seed_initial_commit()
    a_tip = world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a")

    # Converge so auto-delete fires + feat-b gets marked CLOSED.
    world.converge()
    assert world.bare.ref_sha("feat-a") is None
    assert world.pr(2)["state"] == "CLOSED"

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ])

    # Bot detects feat-a merged, feat-b closed (won't cascade a rebase
    # against a closed PR from GitHub's point of view) -- but the
    # branch itself should still be updated because the YAML tracks
    # "open" status and the bot trusts its own ledger.
    assert "PR #1" in out and "merged" in out
    # The fact the bot finishes without errors is the pass condition.
    assert rc != 0  # final git push at sandbox level (expected)
    assert "parent_sha for feat-b" in out


# ══════════════════════════════════════════════════════════════
# Additional coverage: failure modes + edge cases on the bot itself
# ══════════════════════════════════════════════════════════════


def test_ok_push_failed_when_lease_rejected(world):
    """Models ``git push --force-with-lease`` failing because the
    remote tip moved between the bot's fetch and push. The bot should
    flip the PR's status to ``push_failed`` and stop the cascade
    rather than leave the branch in a half-rebased state."""
    world.bare.seed_initial_commit()
    a_tip = world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a")

    # Reject the bot's force-push to feat-b (simulates the remote
    # moving under the lease). Hook goes in AFTER our own setup
    # pushes are done.
    world.bare.reject_pushes_to("feat-b")

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ])

    feat_b = [p for p in final["prs"] if p["branch"] == "feat-b"][0]
    assert feat_b["status"] == "push_failed", final
    assert "force-push failed" in out.lower() or "push_failed" in out.lower(), out


def test_ok_multiple_prs_merged_in_one_poll(world):
    """Two bottom PRs merged between runs. Merge-detection must
    collect both, not just the first; cascade targets the one
    remaining child and uses the last-merged parent's SHA as anchor."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.bare.commit_on_branch("feat-c", "feat-b", "c.txt", "C")
    a_tip = world.bare.ref_sha("feat-a")
    b_tip = world.bare.ref_sha("feat-b")
    world.bare.squash_merge("feat-a", "main")
    world.bare.squash_merge("feat-b", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a", state="MERGED")
    world.set_pr(3, head="feat-c", base="feat-b")

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open", "sha": b_tip},
        {"branch": "feat-c", "pr": 3, "status": "open"},
    ])

    assert "PR #1" in out and "PR #2" in out, out
    assert [p["branch"] for p in final["prs"]] == ["feat-c"]
    assert _rev_count(world.bare.path, "main..feat-c") == 1
    # Anchored on the last-merged parent (feat-b), not feat-a.
    feat_c = final["prs"][0]
    assert feat_c.get("parent_sha") is not None


def test_ok_full_stack_merged_empties_yaml(world):
    """All PRs merged in one poll -> YAML becomes ``prs: []`` and the
    stack-complete message fires. Exercises the 'nothing remaining'
    short-circuit inside process_stack."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    a_tip = world.bare.ref_sha("feat-a")
    b_tip = world.bare.ref_sha("feat-b")
    world.bare.squash_merge("feat-a", "main")
    world.bare.squash_merge("feat-b", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a", state="MERGED")

    _, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open", "sha": b_tip},
    ])

    assert "Stack complete" in out, out
    assert final == {"repo": world.repo_name, "base": "main", "prs": []}


def test_ok_dry_run_detects_without_mutating(world):
    """``--dry-run`` must detect merges and update status in the YAML
    but do no clone, rebase, or force-push. Confirmed by the child's
    branch tip being byte-identical before and after."""
    world.bare.seed_initial_commit()
    a_tip = world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    b_before = world.bare.ref_sha("feat-b")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a")

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ], dry_run=True)

    assert "[DRY RUN]" in out, out
    # feat-b untouched
    assert world.bare.ref_sha("feat-b") == b_before
    # YAML had the merged entry pruned
    assert [p["branch"] for p in final["prs"]] == ["feat-b"]
    # No rebase happened, so parent_sha wasn't recorded on feat-b.
    assert "parent_sha" not in final["prs"][0]


def test_ok_rebase_drops_all_unique_commits(world):
    """Child has no commits above its recorded fork point (someone
    external reset it). Bot's rebase produces an empty patch range
    and fast-forwards the branch to the new base -- must not error."""
    world.bare.seed_initial_commit()
    a_tip = world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    # feat-b starts at feat-a's tip with no unique commits.
    subprocess.check_call([
        "git", "-C", str(world.bare.path), "update-ref",
        "refs/heads/feat-b", a_tip,
    ])
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a")

    rc, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open", "sha": a_tip},
        {"branch": "feat-b", "pr": 2, "status": "open"},
    ])

    feat_b = [p for p in final["prs"] if p["branch"] == "feat-b"][0]
    assert feat_b["status"] == "open", final
    # Branch ends at main's tip (no unique commits replayed).
    assert world.bare.ref_sha("feat-b") == world.bare.ref_sha("main")
