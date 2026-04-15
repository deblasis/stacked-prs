# Stacked PR Manager

Proactively rebases stacked PRs when lower PRs in the stack get merged. Zero infrastructure — runs entirely on GitHub Actions (free for public repos).

## How it works

1. A cron job polls upstream repos every 5 minutes
2. When a PR at the bottom of a stack is merged, it rebases the remaining PRs in order
3. Uses `git rebase --onto <new-base> <parent_sha> <branch>` to transplant only the commits unique to each PR. `parent_sha` is recorded per branch as the fork point it was last rebased on — the same approach Graphite takes with `refs/branch-metadata/<branch>:parentBranchRevision`. This survives squash merges, force-pushes to the parent branch, and branch deletion.
4. Force-pushes rebased branches to the fork
5. Posts status comments on the upstream PRs
6. Commits updated stack state back to this repo

## Repo structure

```
stacked-prs/
  .github/workflows/
    manage-stacks.yml        # GitHub Actions workflow (cron + manual trigger)
  stacks/
    my-feature-stack.yml     # one file per stack — you create these
  stack_manager.py           # the rebase engine
```

## Stack file format

Each YAML file in `stacks/` defines one stack:

```yaml
repo: upstream-org/upstream-repo     # where the PRs live
fork: your-user/your-fork            # where the branches live (omit if same as repo)
base: main                           # upstream branch the PRs target
prs:
  - branch: feature-a                # bottom of the stack
    pr: 123                          # PR number on upstream
    status: open                     # open | merged | conflict | push_failed
  - branch: feature-b                # stacked on feature-a
    pr: 124
    status: open
  - branch: feature-c                # stacked on feature-b
    pr: 125
    status: open
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `repo` | yes | Upstream repo (`owner/name`) where PRs are opened |
| `fork` | no | Fork repo where branches live. Defaults to `repo` if omitted (same-repo PRs) |
| `base` | yes | Upstream base branch (e.g., `main`, `windows`) |
| `prs` | yes | Ordered list of PRs, bottom of stack first |
| `prs[].branch` | yes | Branch name on the fork |
| `prs[].pr` | yes | PR number on the upstream repo. Use `null` if PR hasn't been opened yet |
| `prs[].status` | yes | Current status: `open`, `merged`, `conflict`, `push_failed` |
| `prs[].sha` | auto | Branch tip SHA, snapshotted automatically on each poll. Do not set manually. |
| `prs[].parent_sha` | auto | Fork point — the commit this branch was last rebased onto. Used as the `<upstream>` argument to `git rebase --onto`. Seeded on first run (from the merged parent's saved tip when a merge triggered the run, otherwise from `git merge-base`), then updated on every successful rebase. Automatically re-seeded if it stops being an ancestor of the branch (signals an external rebase). Do not set manually. |

### Status values

- **`open`** — PR is open and will be monitored
- **`merged`** — PR was merged upstream (auto-detected, then removed from stack on next run)
- **`conflict`** — Rebase failed due to conflicts. Resolve manually, push, then set back to `open`
- **`push_failed`** — Force-push failed (e.g., someone pushed between fetch and push). Retries automatically on next run

## Setup

### 1. Fork this repo (or create your own)

You need a public repo to get free GitHub Actions minutes.

### 2. Create a GitHub Personal Access Token

Create a PAT with `repo` scope at https://github.com/settings/tokens. It needs access to:
- Read PR status on upstream repos
- Clone and push to your fork repos
- Post comments on upstream PRs

### 3. Add the token as a repo secret

```bash
gh secret set STACKED_PRS_TOKEN --repo your-user/stacked-prs
# paste the PAT when prompted
```

Or use your existing `gh` auth token:

```bash
gh auth token | gh secret set STACKED_PRS_TOKEN --repo your-user/stacked-prs
```

### 4. (Optional) Set up Discord notifications

Get notified on Discord when PRs are merged, rebased, or hit conflicts. All messages include clickable links to PRs, repos, and branches.

1. In your Discord server: channel settings -> Integrations -> Webhooks -> New Webhook
2. Copy the webhook URL
3. Add it as a secret:

```bash
gh secret set DISCORD_WEBHOOK_URL --repo your-user/stacked-prs
# paste the webhook URL when prompted
```

If not set, the script runs silently (no errors). If Discord is unreachable, the script continues normally — notifications are best-effort.

Example notifications:
> 📦 [upstream-org/project](https://github.com/upstream-org/project) [PR #100](https://github.com/upstream-org/project/pull/100) (`feature/auth`) merged
>
> ♻️ [upstream-org/project](https://github.com/upstream-org/project) [PR #101](https://github.com/upstream-org/project/pull/101): retargeted to `main`, rebased [`feature/dashboard`](https://github.com/your-user/project/tree/feature/dashboard)
>
> ⚠️ [upstream-org/project](https://github.com/upstream-org/project) [PR #102](https://github.com/upstream-org/project/pull/102): conflict rebasing `feature/api` onto `main`

### 5. Create a stack file


Create a YAML file in `stacks/` describing your stack. Example for a cross-repo (fork to upstream) setup:

```yaml
repo: upstream-org/project
fork: your-user/project
base: main
prs:
  - branch: feature/auth
    pr: 100
    status: open
  - branch: feature/dashboard
    pr: 101
    status: open
  - branch: feature/api
    pr: 102
    status: open
```

For same-repo PRs (branches in the same repo), omit `fork`:

```yaml
repo: your-user/your-repo
base: main
prs:
  - branch: feature-a
    pr: 1
    status: open
  - branch: feature-b
    pr: 2
    status: open
```

### 6. Push and wait

The cron runs every 5 minutes. When you merge the bottom PR of any stack, the remaining PRs get rebased automatically.

You can also trigger manually:

```bash
gh workflow run "Stack Manager" --repo your-user/stacked-prs
```

## What happens when a PR is merged

Given this stack:

```
main  <--  feature-a (PR #1)  <--  feature-b (PR #2)  <--  feature-c (PR #3)
```

When PR #1 is merged:

1. `feature-a` is detected as merged and removed from the stack
2. PR #2 is **retargeted** to `main` (so the GitHub diff is immediately correct)
3. `feature-b` is rebased onto `main` using `git rebase --onto main <feature-b.parent_sha> feature-b`. `parent_sha` was recorded the last time `feature-b` was rebased (or seeded from `merge-base(feature-a, feature-b)` on the very first run).
4. `feature-c` is rebased onto the new `feature-b` (PR #3 keeps targeting `feature-b`) using the same per-branch `parent_sha` anchor
5. Both branches are force-pushed to the fork, and their `parent_sha` is updated to the SHA they were just rebased onto
6. A comment is posted on PR #2:
   > ♻️ **Stacked PR Manager** 🤖: a lower PR in the stack was merged.
   > - Retargeted this PR to `main`
   > - Rebased `feature-b` onto `main`
   > - Force-pushed to update the diff
7. A comment is posted on PR #3:
   > ♻️ **Stacked PR Manager** 🤖: rebased `feature-c` onto `feature-b` (cascade from lower PR merge).
8. The stack YAML is updated and committed

If a rebase conflicts:

1. The conflicting PR status is set to `conflict`
2. A comment is posted on the PR:
   > ⚠️ **Stacked PR Manager** 🤖: rebase conflict
3. Cascading stops — PRs above the conflict are not rebased
4. You resolve manually, push, and set status back to `open` in the YAML

## Merge strategies, branch deletion, and force-pushes

All three GitHub merge strategies are supported:

| Merge strategy | How it works |
|---------------|-------------|
| **Merge commit** | branch_1's commits exist in main with original SHAs. Rebase skips them trivially. |
| **Rebase merge** | branch_1's commits are replayed with new SHAs but identical patches. Rebase handles this cleanly. |
| **Squash merge** | All of branch_1's commits become one commit with a different patch. The per-branch `parent_sha` records the exact fork point so the squashed range is known. |

### Why `parent_sha` instead of the parent branch's current tip

A naive implementation would pass the merged parent's tip (or last-known-tip if deleted) as `<upstream>` to `git rebase --onto`. That breaks in two common situations:

- **Parent force-pushed without the child being restacked.** The current parent tip is no longer an ancestor of the child. `git rebase --onto` ends up replaying every commit from the child back to the common ancestor, which produces phantom conflicts against squash-merged history that doesn't even belong to this PR.
- **The 5-minute poll lands after a force-push but before the merge.** Same failure: the snapshotted tip is newer than the child's fork point.

Tracking the fork point per-child fixes both: the anchor doesn't care what the parent did afterward. It's what Graphite calls `parentBranchRevision`.

### Branch deletion is safe

You can enable GitHub's "Automatically delete head branches" setting. Deletion no longer matters for the rebase engine — the per-child `parent_sha` is the only anchor it needs, and it lives in the YAML, not on any branch.

## Safety features

- **Automatic PR retargeting** — when a lower PR is merged, the next PR's base is updated to the stack's base branch so the GitHub diff is immediately clean
- **Force-push with lease** — won't clobber changes pushed between fetch and push
- **Per-fork locking** — prevents concurrent rebases of the same fork (10-minute stale lock threshold)
- **Concurrency control** — only one workflow run at a time; stale runs are cancelled
- **Explicit opt-in** — only PRs you list in a stack file are processed
- **Conflict escalation** — rebase conflicts stop the cascade and notify you via PR comment and Discord
- **Discord notifications** — optional, best-effort. Clickable links to PRs, repos, and branches. Never blocks the main workflow

## Running locally

```bash
# dry run (check status without rebasing)
python stack_manager.py --dry-run

# real run
python stack_manager.py
```

Requires: Python 3.10+, `pyyaml`, `gh` CLI authenticated, `git`.

## Cost

$0. Public repos get unlimited GitHub Actions minutes. Each poll run takes ~10-20 seconds.
