# Tests

Two suites -- keep them in sync.

## Offline (`tests/test_offline.py`)

Zero network, zero real GitHub. A bare local git repo plays the role of
``origin``; a ``gh`` shim on PATH (``tests/fake_gh.py``) reads/writes a
JSON state file so ``stack_manager.py`` sees a coherent API surface.

```bash
pip install pyyaml pytest
pytest tests/test_offline.py -v
```

POSIX only -- Python's subprocess on Windows won't resolve ``gh`` to a
``.cmd`` shim without ``shell=True``, and ``stack_manager.py`` runs its
subprocesses without a shell. Use Linux, macOS, or WSL. CI runs on
Linux.

Runs on every push + PR via ``.github/workflows/test.yml``.

## Live (`tests/e2e_live.py`)

Drives real scenarios against a real throwaway GitHub repo. Slower
(each scenario opens real PRs, waits on GitHub's async mergeability
recompute, squash-merges) but catches behaviour the offline harness
can't (GitHub's own retargeting rules, branch-delete side effects).

```bash
TEST_REPO=you/stacked-prs-e2e python tests/e2e_live.py
```

Requires:

- ``gh`` authenticated with ``repo`` scope
- A public throwaway repo with a ``main`` branch. The script pushes
  to ``main`` directly, opens PRs, force-pushes branches, and merges.
  Nothing it does should be run against a real project.

Each scenario uses a unique branch prefix, so you can re-run without
cleaning up. The script DELETEs its own branches at the end of each
scenario. Leftover closed PRs accumulate on the test repo across runs --
that's fine, they're inert.

Not run in CI (needs real GitHub credentials + a dedicated repo).

## Scenarios covered

Both suites exercise the same list:

1. **Happy path cascade** -- bottom PR merges, middle + top retargeted
   and rebased, ``parent_sha`` recorded.
2. **Recorded ``parent_sha`` re-used** -- second cascade uses the
   recorded anchor without a stale-reseed message.
3. **Stale snapshot + parent branch deleted** -- the original bug.
   ``last_merged.sha`` recorded pre-force-push is still an ancestor of
   the child; seeding picks it up.
4. **External restack** -- child rebased by hand onto a force-pushed
   parent. Recorded ``parent_sha`` is no longer an ancestor; seeding
   detects and re-seeds.
5. **Legit conflict** -- child's rebase conflicts with the new base;
   ``status: conflict``, comment posted, cascade halts.

## Adding a scenario

1. Add a function to both files following the existing shape.
2. Keep scenario semantics identical across suites so a regression
   shows up in both.

## What the offline harness does NOT test

- GitHub's own retargeting of child PRs when a parent branch is
  deleted via the UI vs via API -- live-only.
- ``gh pr merge --delete-branch`` side effects on downstream PRs --
  live-only, see the caveat in the root README.
