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
import time
import urllib.request
import urllib.error
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

STACKS_DIR = Path("stacks")
LOCKS_DIR = Path(".locks")
LOCK_MAX_AGE_SECONDS = 600  # 10 minutes — stale lock threshold


def acquire_lock(fork_repo):
    """Acquire a file-based lock for a fork repo.

    Returns True if acquired, False if another run holds it.
    Lock files are committed to the repo so they survive across runs.
    """
    LOCKS_DIR.mkdir(exist_ok=True)
    lock_file = LOCKS_DIR / f"{fork_repo.replace('/', '_')}.lock"

    if lock_file.exists():
        try:
            lock_data = json.loads(lock_file.read_text())
            lock_time = lock_data.get("timestamp", 0)
            age = time.time() - lock_time
            if age < LOCK_MAX_AGE_SECONDS:
                print(f"  Lock held for {fork_repo} (age: {int(age)}s) — skipping")
                return False
            print(f"  Stale lock for {fork_repo} (age: {int(age)}s) — reclaiming")
        except (json.JSONDecodeError, KeyError):
            print(f"  Corrupt lock for {fork_repo} — reclaiming")

    lock_file.write_text(json.dumps({
        "fork": fork_repo,
        "timestamp": time.time(),
        "pid": os.getpid(),
    }))
    return True


def release_lock(fork_repo):
    """Release the lock for a fork repo."""
    lock_file = LOCKS_DIR / f"{fork_repo.replace('/', '_')}.lock"
    lock_file.unlink(missing_ok=True)


# ── Discord notifications ──────────────────────────────────────

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
_discord_log = []  # collect events during the run, send summary at the end


def discord_event(msg):
    """Queue a Discord notification line for the end-of-run summary."""
    _discord_log.append(msg)


def discord_flush():
    """Send all queued events as a single Discord message. Best-effort."""
    if not DISCORD_WEBHOOK_URL or not _discord_log:
        return
    body = "\n".join(_discord_log)
    payload = json.dumps({
        "username": "Stacked PR Manager",
        "content": body[:2000],  # Discord message limit
    }).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"  Warning: Discord notification failed: {exc}")


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


# ── URL helpers ─────────────────────────────────────────────────


def repo_url(repo):
    return f"https://github.com/{repo}"


def pr_url(repo, pr_number):
    return f"https://github.com/{repo}/pull/{pr_number}"


def branch_url(repo, branch):
    return f"https://github.com/{repo}/tree/{branch}"


# ── GitHub helpers ──────────────────────────────────────────────


def _snapshot_branch_shas(repo, fork, prs):
    """Save the current tip SHA of each tracked branch into the YAML entries.

    This lets us use the SHA as old-base even after the branch is deleted
    (e.g., GitHub auto-deletes merged branches).  Returns True if any SHA
    was updated.
    """
    updated = False
    for pr_entry in prs:
        if pr_entry["status"] in ("merged", "closed"):
            continue
        branch = pr_entry["branch"]
        try:
            data = gh_json(
                "api", f"repos/{fork}/git/ref/heads/{branch}",
            )
            sha = data["object"]["sha"]
            if pr_entry.get("sha") != sha:
                pr_entry["sha"] = sha
                updated = True
        except Exception:
            pass  # branch might not exist yet
    return updated


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


def retarget_pr(repo, pr_number, new_base):
    """Change a PR's base branch (best-effort)."""
    try:
        gh("pr", "edit", str(pr_number), "--repo", repo, "--base", new_base)
        print(f"  Retargeted PR #{pr_number} to `{new_base}`")
        return True
    except Exception as exc:
        print(f"  Warning: could not retarget PR #{pr_number}: {exc}")
        return False


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
                     old_base_ref, upstream_remote, upstream_repo, fork_repo):
    """Rebase every remaining PR in the stack after lower PRs were merged.

    ``old_base_ref`` can be:
    - ``origin/<branch>`` — the merged branch still exists on the fork
    - a SHA string — branch was deleted but we saved the tip SHA
    - ``None`` — branch deleted and no SHA saved; falls back to plain rebase
      (works for regular merges, may conflict on squash merges)

    For the first remaining PR, also retargets it to the stack's base branch
    so the GitHub PR diff is clean regardless of branch deletion timing.
    """
    results = []
    base_ref = f"{upstream_remote}/{base_branch}"

    for i, pr_entry in enumerate(remaining_prs):
        branch = pr_entry["branch"]

        # What we rebase onto
        onto_ref = base_ref if i == 0 else remaining_prs[i - 1]["branch"]

        # Retarget the first remaining PR to the base branch
        # (it was previously targeting the now-merged branch)
        if i == 0 and pr_entry.get("pr"):
            retarget_pr(upstream_repo, pr_entry["pr"], base_branch)

        # Make sure the local branch tracks origin
        git("checkout", "-B", branch, f"origin/{branch}", cwd=clone_dir)

        # Save the current tip — it becomes `old_base_ref` for the next PR
        old_tip = git_output("rev-parse", "HEAD", cwd=clone_dir)

        if old_base_ref:
            # Precise rebase: transplant only commits unique to this branch
            print(f"  Rebasing {branch} --onto {onto_ref} {old_base_ref}")
            result = git(
                "rebase", "--onto", onto_ref, old_base_ref, branch,
                cwd=clone_dir, check=False,
            )
        else:
            # Fallback: old base branch was deleted, use plain rebase
            # Git's patch-id matching skips already-applied commits
            print(f"  Rebasing {branch} onto {onto_ref} (old base deleted, using fallback)")
            result = git(
                "rebase", onto_ref, branch,
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
            _pr_num = pr_entry.get("pr", "?")
            discord_event(
                f"⚠️ [{upstream_repo}](<{repo_url(upstream_repo)}>) "
                f"[PR #{_pr_num}](<{pr_url(upstream_repo, _pr_num)}>): "
                f"conflict rebasing [`{branch}`](<{branch_url(fork_repo, branch)}>) onto `{onto_ref}`"
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
            discord_event(
                f"❌ [{upstream_repo}](<{repo_url(upstream_repo)}>) "
                f"[PR #{pr_entry.get('pr', '?')}](<{pr_url(upstream_repo, pr_entry.get('pr', 0))}>): "
                f"force-push failed for [`{branch}`](<{branch_url(fork_repo, branch)}>)"
            )
            break

        pr_entry["status"] = "open"
        results.append((pr_entry, True, None))

        pr_num = pr_entry.get("pr", "?")
        if pr_entry.get("pr"):
            if i == 0:
                comment_on_pr(
                    upstream_repo, pr_entry["pr"],
                    f"♻️ **Stacked PR Manager** 🤖: a lower PR in the stack was merged.\n\n"
                    f"- Retargeted this PR to `{base_branch}`\n"
                    f"- Rebased `{branch}` onto `{base_branch}`\n"
                    f"- Force-pushed to update the diff",
                )
                discord_event(
                    f"♻️ [{upstream_repo}](<{repo_url(upstream_repo)}>) "
                    f"[PR #{pr_num}](<{pr_url(upstream_repo, pr_num)}>): "
                    f"retargeted to `{base_branch}`, rebased [`{branch}`](<{branch_url(fork_repo, branch)}>)"
                )
            else:
                prev_branch = remaining_prs[i - 1]["branch"]
                comment_on_pr(
                    upstream_repo, pr_entry["pr"],
                    f"♻️ **Stacked PR Manager** 🤖: rebased `{branch}` onto "
                    f"`{prev_branch}` (cascade from lower PR merge).",
                )
                discord_event(
                    f"♻️ [{upstream_repo}](<{repo_url(upstream_repo)}>) "
                    f"[PR #{pr_num}](<{pr_url(upstream_repo, pr_num)}>): "
                    f"rebased [`{branch}`](<{branch_url(fork_repo, branch)}>) onto "
                    f"[`{prev_branch}`](<{branch_url(fork_repo, prev_branch)}>)"
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

    # ── Skip stacks with no actionable PRs ─────────────────────
    actionable = [p for p in prs if p["status"] not in ("merged", "closed")]
    if not actionable:
        print("  No actionable PRs — skipping")
        return False, errors

    # ── Snapshot branch SHAs (survives branch deletion) ────────
    if not dry_run:
        changed_by_sha = _snapshot_branch_shas(repo, fork, prs)
    else:
        changed_by_sha = False

    # ── Detect newly merged PRs (bottom-up) ────────────────────
    newly_merged = []
    for pr_entry in prs:
        if pr_entry["status"] in ("merged", "closed"):
            continue
        if pr_entry.get("pr") is None:
            break
        state = get_pr_state(repo, pr_entry["pr"])
        if state == "MERGED":
            pr_entry["status"] = "merged"
            newly_merged.append(pr_entry)
            print(f"  ✓ PR #{pr_entry['pr']} ({pr_entry['branch']}) merged")
            discord_event(
                f"📦 [{repo}](<{repo_url(repo)}>) "
                f"[PR #{pr_entry['pr']}](<{pr_url(repo, pr_entry['pr'])}>) "
                f"(`{pr_entry['branch']}`) merged"
            )
        elif state == "CLOSED":
            pr_entry["status"] = "closed"
            print(f"  ✗ PR #{pr_entry['pr']} ({pr_entry['branch']}) closed")
        else:
            break  # stop at first open PR

    if not newly_merged:
        print("  Nothing new")
        if changed_by_sha:
            # Persist updated SHAs even when no merges detected
            with open(stack_file, "w") as fh:
                yaml.dump(stack, fh, default_flow_style=False, sort_keys=False)
        return changed_by_sha, errors

    remaining = [p for p in prs if p["status"] != "merged"]
    last_merged = newly_merged[-1]

    if not remaining:
        print("  Stack complete — all PRs merged")
        discord_event(f"🎉 [{repo}](<{repo_url(repo)}>) stack complete — all PRs merged")
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

    # ── Acquire per-fork lock ──────────────────────────────────
    if not acquire_lock(fork):
        errors.append((None, f"Skipped — lock held for fork {fork}"))
        return False, errors

    # ── Clone, rebase, push ─────────────────────────────────────
    upstream_remote = "upstream" if fork != repo else "origin"

    try:
        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = Path(tmp) / "repo"
            setup_clone(fork, repo, clone_dir)

            git("fetch", upstream_remote, base, cwd=clone_dir)
            for pr_entry in remaining:
                git("fetch", "origin", pr_entry["branch"], cwd=clone_dir)

            # Fetch the last merged branch — needed as old-base reference.
            # If deleted (common after merge), use the saved SHA instead.
            fetch = git(
                "fetch", "origin", last_merged["branch"],
                cwd=clone_dir, check=False,
            )
            if fetch.returncode == 0:
                merged_branch_ref = f"origin/{last_merged['branch']}"
            elif last_merged.get("sha"):
                # Branch deleted but we have the SHA from a previous snapshot
                merged_branch_ref = last_merged["sha"]
                print(f"  Branch `{last_merged['branch']}` deleted — using saved SHA {merged_branch_ref[:12]}")
            else:
                merged_branch_ref = None
                print(f"  Branch `{last_merged['branch']}` deleted, no saved SHA — using fallback rebase")

            results = rebase_remaining(
                clone_dir, base, remaining,
                merged_branch_ref, upstream_remote, repo, fork,
            )
            for entry, ok, error in results:
                if not ok:
                    errors.append((entry, error))
    finally:
        release_lock(fork)

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
    if any_changed and not dry_run:
        print(f"\n{'=' * 60}")
        print("Committing status changes")
        print(f"{'=' * 60}")
        git("add", "stacks/")
        diff = git("diff", "--cached", "--quiet", check=False)
        if diff.returncode != 0:
            git("commit", "-m", "chore: update stack status after rebase")
            git("pull", "--rebase", check=False)
            git("push")
            print("  Pushed")
        else:
            print("  No diff to commit")

    if all_errors:
        print(f"\nERRORS ({len(all_errors)}):")
        for entry, error in all_errors:
            tag = f"PR #{entry['pr']}" if entry and entry.get("pr") else "–"
            print(f"  {tag}: {error}")
            discord_event(f"❌ {tag}: {error}")

    discord_flush()

    if all_errors:
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
