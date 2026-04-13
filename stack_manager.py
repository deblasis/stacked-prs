#!/usr/bin/env python3
"""
Stacked PR Manager — proactively rebases stacked PRs when lower PRs get merged.

Polls upstream repos for merge status, then rebases remaining PRs in the stack
using `git rebase --onto` to cleanly transplant only the relevant commits.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

STACKS_DIR = Path("stacks")


def run_cmd(cmd, cwd=None, check=True):
    """Run a command and return the CompletedProcess."""
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
    return result


def git(*args, cwd=None, check=True):
    return run_cmd(["git", *args], cwd=cwd, check=check)


def git_output(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


def gh(*args):
    return run_cmd(["gh", *args]).stdout.strip()


def gh_json(*args):
    return json.loads(gh(*args))


# ── GitHub helpers ──────────────────────────────────────────────


def get_pr_state(repo, pr_number):
    """Return PR state: OPEN, MERGED, or CLOSED."""
    data = gh_json("pr", "view", str(pr_number), "--repo", repo, "--json", "state")
    return data["state"]  # OPEN | MERGED | CLOSED


def comment_on_pr(repo, pr_number, body):
    """Post a comment on a PR (best-effort)."""
    try:
        gh("pr", "comment", str(pr_number), "--repo", repo, "--body", body)
    except Exception as exc:
        print(f"  Warning: could not comment on PR #{pr_number}: {exc}")


# ── Git / clone helpers ────────────────────────────────────────


def setup_clone(fork_repo, upstream_repo, clone_dir):
    """Clone the fork and optionally add an upstream remote."""
    token = os.environ.get("GH_TOKEN", "")
    auth = f"x-access-token:{token}@" if token else ""

    git("clone", f"https://{auth}github.com/{fork_repo}.git", str(clone_dir))
    git("config", "user.name", "Stacked PR Manager", cwd=clone_dir)
    git("config", "user.email", "stacked-prs[bot]@users.noreply.github.com", cwd=clone_dir)

    if upstream_repo != fork_repo:
        git(
            "remote", "add", "upstream",
            f"https://{auth}github.com/{upstream_repo}.git",
            cwd=clone_dir,
        )
        git("fetch", "upstream", cwd=clone_dir)


# ── Rebase engine ──────────────────────────────────────────────


def rebase_remaining(clone_dir, base_branch, remaining_prs,
                     last_merged_branch, upstream_remote, upstream_repo):
    """Rebase every remaining PR in the stack after lower PRs were merged.

    Uses ``git rebase --onto`` so only the commits unique to each branch are
    transplanted, which handles squash-merges cleanly.
    """
    results = []
    base_ref = f"{upstream_remote}/{base_branch}"

    # The "old base" for the first remaining PR is the last merged branch
    # (which still exists on the fork as origin/<branch>).
    old_base_ref = f"origin/{last_merged_branch}"

    for i, pr_entry in enumerate(remaining_prs):
        branch = pr_entry["branch"]

        # What we rebase onto
        onto_ref = base_ref if i == 0 else remaining_prs[i - 1]["branch"]

        # Make sure the local branch tracks origin
        git("checkout", "-B", branch, f"origin/{branch}", cwd=clone_dir)

        # Save the current tip — it becomes `old_base_ref` for the next PR
        old_tip = git_output("rev-parse", "HEAD", cwd=clone_dir)

        print(f"  Rebasing {branch} --onto {onto_ref} {old_base_ref}")
        result = git(
            "rebase", "--onto", onto_ref, old_base_ref, branch,
            cwd=clone_dir, check=False,
        )

        if result.returncode != 0:
            git("rebase", "--abort", cwd=clone_dir, check=False)
            pr_entry["status"] = "conflict"
            error_msg = f"Conflict rebasing `{branch}` onto `{onto_ref}`"
            results.append((pr_entry, False, error_msg))

            if pr_entry.get("pr"):
                comment_on_pr(
                    upstream_repo, pr_entry["pr"],
                    "⚠️ **Stacked PR Manager** 🤖: rebase conflict\n\n"
                    f"Could not rebase `{branch}` onto `{onto_ref}`.\n"
                    "Please resolve manually and push.",
                )
            break  # stop cascading

        # Force-push (with lease for safety)
        push = git(
            "push", "origin", branch, "--force-with-lease",
            cwd=clone_dir, check=False,
        )
        if push.returncode != 0:
            pr_entry["status"] = "push_failed"
            results.append(
                (pr_entry, False, f"Force-push failed for `{branch}`")
            )
            break

        pr_entry["status"] = "open"
        results.append((pr_entry, True, None))

        if pr_entry.get("pr"):
            comment_on_pr(
                upstream_repo, pr_entry["pr"],
                f"♻️ **Stacked PR Manager** 🤖: rebased `{branch}` onto "
                f"`{onto_ref}` after a lower PR in the stack was merged.",
            )

        # Next iteration's old base is this branch's pre-rebase tip
        old_base_ref = old_tip

    return results


# ── Stack processing ───────────────────────────────────────────


def process_stack(stack_file, dry_run=False):
    """Process one stack file.  Returns (changed: bool, errors: list)."""
    with open(stack_file) as fh:
        stack = yaml.safe_load(fh)

    repo = stack["repo"]
    fork = stack.get("fork", repo)
    base = stack["base"]
    prs = stack["prs"]
    errors = []

    print(f"  repo={repo}  fork={fork}  base={base}  prs={len(prs)}")

    # ── Detect newly merged PRs (bottom-up) ────────────────────
    newly_merged = []
    for pr_entry in prs:
        if pr_entry["status"] == "merged":
            continue
        if pr_entry.get("pr") is None:
            break
        state = get_pr_state(repo, pr_entry["pr"])
        if state == "MERGED":
            pr_entry["status"] = "merged"
            newly_merged.append(pr_entry)
            print(f"  ✓ PR #{pr_entry['pr']} ({pr_entry['branch']}) merged")
        else:
            break  # stop at first non-merged

    if not newly_merged:
        print("  Nothing new")
        return False, errors

    remaining = [p for p in prs if p["status"] != "merged"]
    last_merged = newly_merged[-1]

    if not remaining:
        print("  Stack complete — all PRs merged")
        stack["prs"] = []
        with open(stack_file, "w") as fh:
            yaml.dump(stack, fh, default_flow_style=False, sort_keys=False)
        return True, errors

    if dry_run:
        print(f"  [DRY RUN] would rebase {len(remaining)} PR(s)")
        stack["prs"] = remaining
        with open(stack_file, "w") as fh:
            yaml.dump(stack, fh, default_flow_style=False, sort_keys=False)
        return True, errors

    # ── Clone, rebase, push ─────────────────────────────────────
    upstream_remote = "upstream" if fork != repo else "origin"

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "repo"
        setup_clone(fork, repo, clone_dir)

        git("fetch", upstream_remote, base, cwd=clone_dir)
        for pr_entry in remaining:
            git("fetch", "origin", pr_entry["branch"], cwd=clone_dir)

        # Fetch the last merged branch — needed as old-base reference
        fetch = git(
            "fetch", "origin", last_merged["branch"],
            cwd=clone_dir, check=False,
        )
        if fetch.returncode != 0:
            msg = (
                f"Old base branch `{last_merged['branch']}` no longer exists "
                "on the fork — cannot determine rebase range"
            )
            print(f"  ERROR: {msg}")
            errors.append((None, msg))
            stack["prs"] = remaining
            with open(stack_file, "w") as fh:
                yaml.dump(stack, fh, default_flow_style=False, sort_keys=False)
            return True, errors

        results = rebase_remaining(
            clone_dir, base, remaining,
            last_merged["branch"], upstream_remote, repo,
        )
        for entry, ok, error in results:
            if not ok:
                errors.append((entry, error))

    # ── Persist updated status ──────────────────────────────────
    stack["prs"] = remaining
    with open(stack_file, "w") as fh:
        yaml.dump(stack, fh, default_flow_style=False, sort_keys=False)

    return True, errors


# ── Entrypoint ─────────────────────────────────────────────────


def main():
    if not STACKS_DIR.exists():
        print("No stacks/ directory — nothing to do.")
        return

    stack_files = sorted(STACKS_DIR.glob("*.yml"))
    if not stack_files:
        print("No stack files in stacks/")
        return

    dry_run = "--dry-run" in sys.argv
    any_changed = False
    all_errors = []

    for sf in stack_files:
        print(f"\n{'=' * 60}")
        print(f"Stack: {sf.name}")
        print(f"{'=' * 60}")
        try:
            changed, errs = process_stack(sf, dry_run=dry_run)
            if changed:
                any_changed = True
            all_errors.extend(errs)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            all_errors.append((None, str(exc)))

    # Commit status changes back to the control-plane repo
    if any_changed:
        print(f"\n{'=' * 60}")
        print("Committing status changes")
        print(f"{'=' * 60}")
        git("add", "stacks/")
        diff = git("diff", "--cached", "--quiet", check=False)
        if diff.returncode != 0:
            git("commit", "-m", "chore: update stack status after rebase")
            git("push")
            print("  Pushed")
        else:
            print("  No diff to commit")

    if all_errors:
        print(f"\nERRORS ({len(all_errors)}):")
        for entry, error in all_errors:
            tag = f"PR #{entry['pr']}" if entry and entry.get("pr") else "–"
            print(f"  {tag}: {error}")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
