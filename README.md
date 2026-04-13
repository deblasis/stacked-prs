# Stacked PR Manager

Proactively rebases stacked PRs when lower PRs in the stack get merged. Zero infrastructure — runs entirely on GitHub Actions (free for public repos).

## How it works

1. A cron job polls upstream repos every 2 minutes
2. When a PR at the bottom of a stack is merged, it rebases the remaining PRs in order
3. Uses `git rebase --onto` to cleanly transplant only the commits unique to each PR (handles squash merges)
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

### 4. Create a stack file

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

### 5. Push and wait

The cron runs every 2 minutes. When you merge the bottom PR of any stack, the remaining PRs get rebased automatically.

You can also trigger manually:

```bash
gh workflow run "Stack Manager" --repo your-user/stacked-prs
```

## What happens when a PR is merged

Given this stack:

```
main  <--  feature-a (PR #1)  <--  feature-b (PR #2)  <--  feature-c (PR #3)
```

When PR #1 is merged (regular or squash merge):

1. `feature-a` is detected as merged and removed from the stack
2. `feature-b` is rebased onto `main` using `git rebase --onto main origin/feature-a feature-b`
3. `feature-c` is rebased onto the new `feature-b`
4. Both branches are force-pushed to the fork
5. Comments are posted on PRs #2 and #3:
   > ♻️ **Stacked PR Manager** 🤖: rebased `feature-b` onto `upstream/main` after a lower PR in the stack was merged.
6. The stack YAML is updated and committed

If a rebase conflicts:

1. The conflicting PR status is set to `conflict`
2. A comment is posted on the PR:
   > ⚠️ **Stacked PR Manager** 🤖: rebase conflict
3. Cascading stops — PRs above the conflict are not rebased
4. You resolve manually, push, and set status back to `open` in the YAML

## Safety features

- **Force-push with lease** — won't clobber changes pushed between fetch and push
- **Per-fork locking** — prevents concurrent rebases of the same fork (10-minute stale lock threshold)
- **Concurrency control** — only one workflow run at a time; stale runs are cancelled
- **Explicit opt-in** — only PRs you list in a stack file are processed
- **Conflict escalation** — rebase conflicts stop the cascade and notify you via PR comment

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
