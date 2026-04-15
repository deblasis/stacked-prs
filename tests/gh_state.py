"""GitHub state + behavior rules for the offline harness.

Shared between ``fake_gh.py`` (runs as a subprocess on PATH) and
``conftest.py`` (fixtures + direct state manipulation from tests). Kept
in one module so rules don't drift between the two sides.

State schema::

    {
      "repos": {
        "owner/repo": {
          "bare_path": "/abs/path/to/bare.git",
          "config": {
            "token_valid": true,
            "auto_delete_on_merge": false,
            "mergeability_resolves_after": 0,  # requests
            "rate_limit_after_requests": null,
            "missing": false                     # 404 on any call
          },
          "request_count": 0,
          "prs": {
            "<number>": {
              "state": "OPEN",                   # OPEN | MERGED | CLOSED
              "headRefName": "feat-a",
              "baseRefName": "main",
              "mergeable": "MERGEABLE",          # MERGEABLE | CONFLICTING | UNKNOWN
              "mergeability_countdown": 0,       # request-ticks until resolve
              "closed_reason": null,
              "comments": []
            }
          }
        }
      }
    }

Callers never touch this dict directly -- they go through ``read`` /
``write`` / ``configure`` / ``set_pr`` / ``converge`` so that rules
always run on write and every read sees a converged view.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


# ── low-level state I/O ────────────────────────────────────────


def read(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write(path: Path, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True))


def default_repo_config() -> dict:
    return {
        "token_valid": True,
        "auto_delete_on_merge": False,
        "mergeability_resolves_after": 0,
        "rate_limit_after_requests": None,
        "missing": False,
    }


# ── bare-repo probes (single source of truth for on-disk state) ─


def branch_sha(bare_path: str, branch: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", bare_path, "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def branch_exists(bare_path: str, branch: str) -> bool:
    return branch_sha(bare_path, branch) is not None


def delete_branch_on_bare(bare_path: str, branch: str) -> None:
    subprocess.run(
        ["git", "-C", bare_path, "update-ref", "-d", f"refs/heads/{branch}"],
        check=False,
    )


# ── rules ──────────────────────────────────────────────────────
# Each rule is a pure function ``(state) -> bool`` (True if it mutated
# state). The caller runs them to a fixed point so cascading effects
# all land in one converge call.


def _rule_auto_delete_on_merge(state: dict) -> bool:
    """When a PR flips to MERGED on a repo configured with
    ``auto_delete_on_merge``, delete the head branch on the bare repo
    if it still exists. Mirrors GitHub's 'Automatically delete head
    branches' repo setting."""
    changed = False
    for repo, data in state["repos"].items():
        if not data.get("config", {}).get("auto_delete_on_merge"):
            continue
        bare = data["bare_path"]
        for pr in data["prs"].values():
            if pr["state"] != "MERGED":
                continue
            head = pr["headRefName"]
            if branch_exists(bare, head):
                delete_branch_on_bare(bare, head)
                changed = True
    return changed


def _rule_close_prs_with_missing_base(state: dict) -> bool:
    """Open PRs whose base branch no longer exists on the bare repo
    flip to CLOSED (reason: base_deleted). Matches GitHub's behavior
    of auto-closing child PRs when their base branch is deleted --
    the closed-reason flag lets tests differentiate from explicit
    closes."""
    changed = False
    for repo, data in state["repos"].items():
        bare = data["bare_path"]
        for pr in data["prs"].values():
            if pr["state"] != "OPEN":
                continue
            if not branch_exists(bare, pr["baseRefName"]):
                pr["state"] = "CLOSED"
                pr["closed_reason"] = "base_deleted"
                changed = True
    return changed


def _rule_mergeability_resolution(state: dict) -> bool:
    """Pushes reset mergeability to UNKNOWN with a countdown. Each
    API request against the repo ticks the counter down by one. At
    zero, the state resolves to whichever value the test declared via
    ``mark_mergeability_pending`` (default MERGEABLE)."""
    changed = False
    for repo, data in state["repos"].items():
        for pr in data["prs"].values():
            if pr.get("mergeable") == "UNKNOWN" and pr.get("mergeability_countdown", 0) <= 0:
                pr["mergeable"] = pr.pop("_pending_resolution", "MERGEABLE")
                changed = True
    return changed


RULES = [
    _rule_auto_delete_on_merge,
    _rule_close_prs_with_missing_base,
    _rule_mergeability_resolution,
]


def converge(state: dict, max_iterations: int = 10) -> None:
    """Apply every rule repeatedly until the state stops changing."""
    for _ in range(max_iterations):
        if not any(rule(state) for rule in RULES):
            return
    raise RuntimeError("gh_state rules did not converge in 10 iterations")


# ── validation helpers (used by fake_gh to emit realistic errors) ─


class GhError(Exception):
    """Raised by validators; ``fake_gh`` translates to the matching
    CLI exit format."""

    def __init__(self, http_status: int, message: str):
        self.http_status = http_status
        self.message = message
        super().__init__(message)


def check_repo_accessible(state: dict, repo: str) -> dict:
    """Returns the repo entry or raises GhError.

    401 before 404 so a bad token can't be used to probe repo existence
    -- matches real GitHub's response order."""
    data = state["repos"].get(repo)
    if data is None:
        raise GhError(404, f"Could not resolve to a Repository with the name '{repo}'.")
    config = data.get("config", {})
    if not config.get("token_valid", True):
        raise GhError(401, "Bad credentials")
    if config.get("missing"):
        raise GhError(404, f"Not Found (repo '{repo}' marked missing)")
    return data


def tick_request(state: dict, repo: str) -> None:
    """Increment request counter; apply rate-limit rule + mergeability
    countdown. Call at the START of every fake_gh command that talks
    to a specific repo."""
    data = state["repos"][repo]
    data["request_count"] = data.get("request_count", 0) + 1

    limit = data.get("config", {}).get("rate_limit_after_requests")
    if limit is not None and data["request_count"] > limit:
        raise GhError(403, "API rate limit exceeded")

    for pr in data["prs"].values():
        if pr.get("mergeability_countdown", 0) > 0:
            pr["mergeability_countdown"] -= 1


def check_retarget(state: dict, repo: str, pr_num: str, new_base: str) -> None:
    """Validate a ``pr edit --base`` call. Errors mirror GitHub's 422."""
    data = state["repos"][repo]
    pr = data["prs"].get(pr_num)
    if pr is None:
        raise GhError(404, f"PR #{pr_num} not found")
    if pr["state"] == "MERGED":
        raise GhError(422, "Cannot edit a merged pull request")
    if new_base == pr["headRefName"]:
        raise GhError(422, "Base and head must be different")
    if not branch_exists(data["bare_path"], new_base):
        raise GhError(422, f"Base branch '{new_base}' does not exist")


# ── configuration / seeding API used by tests ──────────────────


def init_state(state_path: Path, repo_name: str, bare_path: str) -> None:
    """Create a fresh state file with one repo + default config."""
    write(state_path, {
        "repos": {
            repo_name: {
                "bare_path": bare_path,
                "config": default_repo_config(),
                "request_count": 0,
                "prs": {},
            }
        }
    })


def configure(state_path: Path, repo_name: str, **overrides: Any) -> None:
    state = read(state_path)
    state["repos"][repo_name]["config"].update(overrides)
    write(state_path, state)


def set_pr(state_path: Path, repo_name: str, pr_num: int, *,
           head: str, base: str, state_val: str = "OPEN",
           mergeable: str = "MERGEABLE") -> None:
    state = read(state_path)
    state["repos"][repo_name]["prs"][str(pr_num)] = {
        "state": state_val,
        "headRefName": head,
        "baseRefName": base,
        "mergeable": mergeable,
        "mergeability_countdown": 0,
        "closed_reason": None,
        "comments": [],
    }
    converge(state)
    write(state_path, state)


def mark_pr_state(state_path: Path, repo_name: str, pr_num: int, new_state: str,
                  *, closed_reason: str | None = None) -> None:
    state = read(state_path)
    pr = state["repos"][repo_name]["prs"][str(pr_num)]
    pr["state"] = new_state
    pr["closed_reason"] = closed_reason
    converge(state)
    write(state_path, state)


def mark_mergeability_pending(state_path: Path, repo_name: str, pr_num: int,
                              ticks: int, final: str = "MERGEABLE") -> None:
    """Next ``ticks`` pr-view requests return UNKNOWN; after that the
    final value is returned. Models GitHub's async mergeability
    recompute."""
    state = read(state_path)
    pr = state["repos"][repo_name]["prs"][str(pr_num)]
    pr["mergeable"] = "UNKNOWN" if ticks > 0 else final
    pr["mergeability_countdown"] = ticks
    # Remember what to resolve to when countdown expires.
    pr["_pending_resolution"] = final
    write(state_path, state)


def get_pr(state_path: Path, repo_name: str, pr_num: int) -> dict:
    state = read(state_path)
    converge(state)
    write(state_path, state)
    return state["repos"][repo_name]["prs"][str(pr_num)]


def converge_on_disk(state_path: Path) -> None:
    """Re-run rules and persist. Tests call this after direct bare-repo
    mutations (e.g., manual branch deletion) so subsequent reads see
    the cascaded state."""
    state = read(state_path)
    converge(state)
    write(state_path, state)
