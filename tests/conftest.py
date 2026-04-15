"""Pytest fixtures for the offline harness.

Builds a self-contained world per test:

* A bare git repo playing the role of ``origin`` (the fork that
  ``stack_manager.py`` clones + pushes to).
* A JSON state file describing PRs open on that repo.
* A ``gh`` shim on PATH that reads/writes the state file.
* A sandbox directory with a copy of ``stack_manager.py`` and an empty
  ``stacks/`` dir so the manager can be invoked the same way it is in
  production.

Tests drive git operations against the bare repo (creating branches,
adding commits, merging) and mutate PR states via the state file; the
manager sees exactly what it would against a real GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
STACK_MANAGER = REPO_ROOT / "stack_manager.py"
FAKE_GH = TESTS_DIR / "fake_gh.py"


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=check,
    )


def _git_out(*args, cwd):
    return _git(*args, cwd=cwd).stdout.strip()


class BareRepo:
    """Wraps a bare git repo plus a single long-lived worktree used as
    the staging area for test-driven commits, branches, and merges.

    Keeping one worktree (instead of cloning per operation) avoids
    file-handle / rmtree races on Windows and makes each operation
    cheap.
    """

    def __init__(self, path: Path):
        self.path = path
        _git("init", "--bare", "-b", "main", str(path), cwd=path.parent)
        self.work = path.parent / "work"
        _git("clone", "-q", str(path), str(self.work), cwd=path.parent)
        _git("config", "user.name", "test", cwd=self.work)
        _git("config", "user.email", "test@test", cwd=self.work)

    def _fetch(self):
        _git("fetch", "-q", "origin", cwd=self.work)

    def seed_initial_commit(self) -> str:
        (self.work / "README.md").write_text("# test\n")
        _git("add", "README.md", cwd=self.work)
        _git("commit", "-qm", "initial", cwd=self.work)
        _git("push", "-q", "origin", "main", cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def commit_on_branch(self, branch: str, from_ref: str,
                         filename: str, content: str) -> str:
        """Create or extend ``branch`` with one commit adding ``filename``.
        ``from_ref`` is the base (ignored if branch already exists)."""
        self._fetch()
        exists = _git("show-ref", "--verify", "--quiet",
                      f"refs/heads/{branch}", cwd=self.path, check=False
                      ).returncode == 0
        if exists:
            _git("checkout", "-qB", branch, f"origin/{branch}", cwd=self.work)
        else:
            _git("checkout", "-qb", branch, from_ref, cwd=self.work)
        (self.work / filename).write_text(content)
        _git("add", filename, cwd=self.work)
        _git("commit", "-qm", f"{branch}: {filename}", cwd=self.work)
        _git("push", "-q", "origin", branch, cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def amend_tip(self, branch: str, new_msg: str) -> str:
        """Rewrite the branch's tip (amend) and force-push."""
        self._fetch()
        _git("checkout", "-qB", branch, f"origin/{branch}", cwd=self.work)
        _git("commit", "-q", "--amend", "-m", new_msg, cwd=self.work)
        _git("push", "-qf", "origin", branch, cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def squash_merge(self, source: str, target: str) -> str:
        """Squash-merge ``source`` into ``target`` with one new commit."""
        self._fetch()
        _git("checkout", "-qB", target, f"origin/{target}", cwd=self.work)
        _git("merge", "--squash", f"origin/{source}", cwd=self.work)
        _git("commit", "-qm", f"squash of {source}", cwd=self.work)
        _git("push", "-q", "origin", target, cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def overwrite_on(self, branch: str, filename: str, content: str) -> str:
        """Direct-commit on ``branch`` -- useful for simulating someone
        pushing straight to ``main`` while a stack is pending."""
        self._fetch()
        _git("checkout", "-qB", branch, f"origin/{branch}", cwd=self.work)
        (self.work / filename).write_text(content)
        _git("add", filename, cwd=self.work)
        _git("commit", "-qm", f"overwrite {filename}", cwd=self.work)
        _git("push", "-q", "origin", branch, cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def rebase_onto(self, branch: str, new_base_sha: str,
                    old_base_sha: str) -> str:
        """Perform ``git rebase --onto new_base_sha old_base_sha branch``
        and force-push. Used to simulate an external ``gt restack``."""
        self._fetch()
        _git("checkout", "-qB", branch, f"origin/{branch}", cwd=self.work)
        _git("rebase", "--onto", new_base_sha, old_base_sha, cwd=self.work)
        _git("push", "-qf", "origin", branch, cwd=self.work)
        return _git_out("rev-parse", "HEAD", cwd=self.work)

    def ref_sha(self, branch: str) -> str | None:
        r = _git("rev-parse", f"refs/heads/{branch}", cwd=self.path, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def delete_branch(self, branch: str):
        _git("update-ref", "-d", f"refs/heads/{branch}", cwd=self.path)


class World:
    """All harness state for one test. Call ``set_pr`` to declare a PR,
    ``run_manager`` to invoke the script, then inspect the returned
    tuple or ``bare`` / ``state`` attributes."""

    def __init__(self, tmp_path: Path, bin_dir: Path, state_path: Path):
        self.tmp_path = tmp_path
        self.bin_dir = bin_dir
        self.state_path = state_path

        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        self.bare = BareRepo(bare_dir / "repo.git")
        self.repo_name = "test/repo"

        self.state_path.write_text(json.dumps({
            "repos": {
                self.repo_name: {
                    "bare_path": str(self.bare.path),
                    "prs": {},
                }
            }
        }, indent=2))

        self.sandbox = tmp_path / "sandbox"
        self.sandbox.mkdir()
        shutil.copy(STACK_MANAGER, self.sandbox)
        (self.sandbox / "stacks").mkdir()
        _git("init", "-qb", "master", cwd=self.sandbox)
        _git("config", "user.name", "test", cwd=self.sandbox)
        _git("config", "user.email", "test@test", cwd=self.sandbox)
        (self.sandbox / ".gitignore").write_text("")
        _git("add", "-A", cwd=self.sandbox)
        _git("commit", "-qm", "init", cwd=self.sandbox)

    def set_pr(self, pr: int, *, head: str, base: str, state: str = "OPEN"):
        s = json.loads(self.state_path.read_text())
        s["repos"][self.repo_name]["prs"][str(pr)] = {
            "state": state,
            "headRefName": head,
            "baseRefName": base,
            "comments": [],
        }
        self.state_path.write_text(json.dumps(s, indent=2))

    def mark_pr_state(self, pr: int, state: str):
        s = json.loads(self.state_path.read_text())
        s["repos"][self.repo_name]["prs"][str(pr)]["state"] = state
        self.state_path.write_text(json.dumps(s, indent=2))

    def pr(self, pr: int) -> dict:
        s = json.loads(self.state_path.read_text())
        return s["repos"][self.repo_name]["prs"][str(pr)]

    def run_manager(self, stack_prs: list[dict]) -> tuple[int, str, dict]:
        """Write the stack YAML, exec stack_manager.py, return
        (returncode, combined stdout+stderr, final yaml dict)."""
        import yaml
        yaml_path = self.sandbox / "stacks" / "stack.yml"
        yaml_path.write_text(yaml.dump(
            {"repo": self.repo_name, "base": "main", "prs": stack_prs},
            sort_keys=False,
        ))
        # origin for the sandbox clone URL resolves via fake `gh repo
        # clone` -- but stack_manager uses git clone directly with a
        # https:// URL. Replace the URL rewriting by pointing HTTPS at
        # the bare repo via insteadOf at the process-git level.
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_STATE": str(self.state_path),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0":
                f"url.{self.bare.path}.insteadOf",
            "GIT_CONFIG_VALUE_0":
                f"https://x-access-token:fake-token-offline@github.com/{self.repo_name}.git",
            # Also handle the un-tokenized variant if any part strips creds
            "GH_TOKEN": "fake-token-offline",
        }
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "stack_manager.py"],
            cwd=self.sandbox, capture_output=True, text=True, env=env,
        )
        final = None
        if yaml_path.exists():
            final = yaml.safe_load(yaml_path.read_text())
        return proc.returncode, proc.stdout + proc.stderr, final


@pytest.fixture
def fake_gh_bin(tmp_path: Path) -> Path:
    """Directory prepended to PATH containing ``gh`` / ``gh.cmd`` wrappers
    that exec fake_gh.py with the current interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sh = bin_dir / "gh"
    sh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n'
    )
    sh.chmod(0o755)
    # Windows fallback -- cmd.exe resolves .cmd ahead of extensionless.
    cmd = bin_dir / "gh.cmd"
    cmd.write_text(
        f'@echo off\r\n"{sys.executable}" "{FAKE_GH}" %*\r\n'
    )
    return bin_dir


@pytest.fixture
def world(tmp_path: Path, fake_gh_bin: Path) -> World:
    return World(tmp_path, fake_gh_bin, tmp_path / "gh_state.json")
