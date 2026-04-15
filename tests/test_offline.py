"""Offline scenario tests -- no network, no real GitHub.

Each test wires up a local bare repo as ``origin``, declares PRs via the
``World`` fixture (which persists to the JSON state file read by the
fake ``gh`` shim), then runs ``stack_manager.py`` and asserts on the
resulting YAML + bare-repo state.

Mirrors the scenarios in ``e2e_live.py``; the two suites are intended
to stay in sync so offline CI catches the same regressions that a live
run would.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# The harness PATH-injects a ``gh`` wrapper that shells out to
# ``python fake_gh.py``. CPython's subprocess on Windows won't
# resolve ``gh`` against a ``gh.cmd`` shim when called with
# ``shell=False`` (which is what ``stack_manager.py`` uses), so the
# real ``gh`` binary wins the PATH lookup. Run these under POSIX
# (Linux, macOS, WSL). CI is Linux.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="offline harness needs POSIX PATH resolution for the gh shim",
)


def _rev(bare: Path, ref: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(bare), "rev-parse", ref], text=True,
    ).strip()


def _rev_count(bare: Path, rng: str) -> int:
    return int(subprocess.check_output(
        ["git", "-C", str(bare), "rev-list", "--count", rng], text=True,
    ).strip())


def test_happy_path_cascade(world):
    """Merging the bottom PR of a 3-deep stack triggers retarget +
    rebase cascade for both children; parent_sha is recorded for each."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-a", "main", "a.txt", "A")
    world.bare.commit_on_branch("feat-b", "feat-a", "b.txt", "B")
    world.bare.commit_on_branch("feat-c", "feat-b", "c.txt", "C")

    a_tip = world.bare.ref_sha("feat-a")
    world.bare.squash_merge("feat-a", "main")
    world.set_pr(1, head="feat-a", base="main", state="MERGED")
    world.set_pr(2, head="feat-b", base="feat-a", state="OPEN")
    world.set_pr(3, head="feat-c", base="feat-b", state="OPEN")

    _, out, final = world.run_manager([
        {"branch": "feat-a", "pr": 1, "status": "open"},
        {"branch": "feat-b", "pr": 2, "status": "open"},
        {"branch": "feat-c", "pr": 3, "status": "open"},
    ])

    assert "PR #1" in out and "merged" in out, out
    assert final is not None
    assert len(final["prs"]) == 2
    names = [p["branch"] for p in final["prs"]]
    assert names == ["feat-b", "feat-c"]
    # parent_sha recorded on both children. feat-b anchored on feat-a's
    # pre-merge tip (via last_merged.sha); feat-c anchored on the newly
    # rebased feat-b tip.
    assert all(p.get("parent_sha") for p in final["prs"])
    # Retarget for feat-b was applied to the fake state.
    assert world.pr(2)["baseRefName"] == "main"
    # feat-b now has a single unique commit on top of main.
    assert _rev_count(world.bare.path, "main..feat-b") == 1
    assert _rev_count(world.bare.path, "feat-b..feat-c") == 1


def test_stale_snapshot_parent_deleted(world):
    """Original bug: parent force-pushed after snapshot, then branch
    deleted at merge time (auto-delete). The YAML-recorded snapshot is
    a pre-force-push SHA that's still an ancestor of the child; the
    seed logic picks it up as last_merged.sha and produces a clean
    --onto anchor."""
    world.bare.seed_initial_commit()
    x_old = world.bare.commit_on_branch("feat-x", "main", "x.txt", "X")
    world.bare.commit_on_branch("feat-y", "feat-x", "y.txt", "Y")
    # Parent force-pushed AFTER the snapshot was taken
    world.bare.amend_tip("feat-x", "X amended externally")
    # Parent merges (squash), then its branch is deleted (auto-delete)
    world.bare.squash_merge("feat-x", "main")
    world.bare.delete_branch("feat-x")
    world.set_pr(10, head="feat-x", base="main", state="MERGED")
    world.set_pr(11, head="feat-y", base="feat-x", state="OPEN")

    _, out, final = world.run_manager([
        # Snapshot recorded the PRE-amend x tip (the race condition)
        {"branch": "feat-x", "pr": 10, "status": "open", "sha": x_old},
        {"branch": "feat-y", "pr": 11, "status": "open"},
    ])

    assert f"parent_sha for feat-y: {x_old[:12]}" in out, out
    assert final is not None
    assert [p["branch"] for p in final["prs"]] == ["feat-y"]
    # feat-y rebased onto new main: one unique commit over main.
    assert _rev_count(world.bare.path, "main..feat-y") == 1


def test_external_restack_detects_stale(world):
    """User externally rebased a child onto a force-pushed parent. The
    recorded parent_sha is no longer an ancestor of the branch; the
    seed logic re-seeds via last_merged.sha."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-m", "main", "m.txt", "M")
    world.bare.commit_on_branch("feat-n", "feat-m", "n.txt", "N")
    world.bare.commit_on_branch("feat-o", "feat-n", "o.txt", "O")

    # First cascade: merge m, run manager to seed parent_sha on n + o.
    world.bare.squash_merge("feat-m", "main")
    world.set_pr(20, head="feat-m", base="main", state="MERGED")
    world.set_pr(21, head="feat-n", base="feat-m", state="OPEN")
    world.set_pr(22, head="feat-o", base="feat-n", state="OPEN")
    _, _, final1 = world.run_manager([
        {"branch": "feat-m", "pr": 20, "status": "open"},
        {"branch": "feat-n", "pr": 21, "status": "open"},
        {"branch": "feat-o", "pr": 22, "status": "open"},
    ])
    assert [p["branch"] for p in final1["prs"]] == ["feat-n", "feat-o"]
    o_recorded_parent = final1["prs"][1]["parent_sha"]  # = feat-n's tip after rebase

    # External restack: amend feat-n, then rebase feat-o onto the new tip.
    new_n = world.bare.amend_tip("feat-n", "N amended externally")
    world.bare.rebase_onto("feat-o", new_n, o_recorded_parent)

    # Merge n; run second cascade -- feat-o's parent_sha is stale.
    world.bare.squash_merge("feat-n", "main")
    world.mark_pr_state(21, "MERGED")

    _, out, final2 = world.run_manager(final1["prs"])

    assert "parent_sha for feat-o is stale" in out, out
    assert final2 is not None
    assert [p["branch"] for p in final2["prs"]] == ["feat-o"]
    assert _rev_count(world.bare.path, "main..feat-o") == 1


def test_conflict_reported_and_cascade_stops(world):
    """A real conflict during child rebase flips status=conflict and
    posts a comment; no subsequent PRs are touched."""
    world.bare.seed_initial_commit()
    world.bare.commit_on_branch("feat-r", "main", "shared.txt", "R\n")
    # feat-s edits the same file -- once r squashes and main changes
    # the context, s's patch won't apply cleanly.
    world.bare.commit_on_branch("feat-s", "feat-r", "shared.txt", "R\nand S\n")

    world.bare.squash_merge("feat-r", "main")
    # Break shared.txt context on main so feat-s's patch conflicts.
    world.bare.overwrite_on("main", "shared.txt", "COMPLETELY\nDIFFERENT\n")

    world.set_pr(30, head="feat-r", base="main", state="MERGED")
    world.set_pr(31, head="feat-s", base="feat-r", state="OPEN")

    _, out, final = world.run_manager([
        {"branch": "feat-r", "pr": 30, "status": "open"},
        {"branch": "feat-s", "pr": 31, "status": "open"},
    ])

    assert "conflict" in out.lower(), out
    assert final is not None
    assert [p["status"] for p in final["prs"]] == ["conflict"]
    # Comment posted on PR 31.
    assert any("rebase conflict" in c for c in world.pr(31)["comments"])
