"""Local git clone, commit, and push. Commands are fixed argument lists, never a shell."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cayadraft.textutil import is_secret_path, scrub


class GitError(RuntimeError):
    pass


class GitWorkspace:
    def __init__(self, name: str, email: str, timeout: int = 120) -> None:
        self.name = name
        self.email = email
        self.timeout = timeout

    def clone(self, url: str, dest: Path, branch: str, token: str | None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            None,
            ["clone", "--depth", "20", "--branch", branch, "--single-branch", url, str(dest)],
            token=token if url.startswith("http") else None,
        )

    def checkout_new(self, dest: Path, branch: str) -> None:
        self.run(dest, ["checkout", "-b", branch])

    def fetch_branch(self, dest: Path, branch: str, token: str | None) -> None:
        self.run(dest, ["fetch", "--depth", "20", "origin", branch], token=self._auth_token(dest, token))
        self.run(dest, ["checkout", "-B", branch, "FETCH_HEAD"])

    def head_sha(self, dest: Path) -> str:
        return self.run(dest, ["rev-parse", "HEAD"]).strip()

    def diff_numstat(self, dest: Path) -> tuple[int, list[str]]:
        raw = self.run(dest, ["diff", "--numstat"])
        total = 0
        files: list[str] = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added = 0 if parts[0] == "-" else int(parts[0])
            deleted = 0 if parts[1] == "-" else int(parts[1])
            total += added + deleted
            files.append(parts[2])
        return total, files

    def changed_names(self, dest: Path) -> list[str]:
        raw = self.run(dest, ["status", "--porcelain"])
        names = []
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            names.append(line[3:].strip().strip('"'))
        return names

    def commit_all(self, dest: Path, message: str) -> str:
        self.run(dest, ["add", "-A"])
        self.run(dest, ["commit", "-m", message])
        return self.head_sha(dest)

    def push(self, dest: Path, branch: str, token: str | None) -> None:
        self.run(dest, ["push", "origin", f"HEAD:refs/heads/{branch}"], token=self._auth_token(dest, token))

    def log_subjects(self, dest: Path, base_branch: str) -> list[tuple[str, str]]:
        raw = self.run(dest, ["log", "--format=%H%x09%s", f"origin/{base_branch}..HEAD"])
        rows = []
        for line in raw.splitlines():
            sha, _, subject = line.partition("\t")
            if sha:
                rows.append((sha, subject))
        return rows

    def diff_against_base(self, dest: Path, base_branch: str) -> str:
        return self.run(dest, ["diff", "--stat", "--patch", f"origin/{base_branch}...HEAD"])

    def revert_commit(self, dest: Path, sha: str) -> None:
        self.run(dest, ["revert", "--no-commit", sha])

    def restore_to_base(self, dest: Path, base_branch: str) -> None:
        self.run(dest, ["checkout", f"origin/{base_branch}", "--", "."])
        self.run(dest, ["add", "-A"])

    def run(self, dest: Path | None, args: list[str], token: str | None = None) -> str:
        command = ["git", *self._config_args(token), *args]
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        try:
            completed = subprocess.run(
                command,
                cwd=str(dest) if dest else None,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"Git timed out while running `git {args[0]}`.") from exc
        except FileNotFoundError as exc:
            raise GitError("Git is not installed or not on PATH.") from exc
        if completed.returncode != 0:
            detail = scrub((completed.stderr or completed.stdout or "git failed").strip(), [token or ""])
            raise GitError(f"Git {args[0]} failed: {detail[:800]}")
        return completed.stdout

    def _config_args(self, token: str | None) -> list[str]:
        args = [
            "-c",
            "safe.directory=*",
            "-c",
            "core.autocrlf=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"user.name={self.name}",
            "-c",
            f"user.email={self.email}",
        ]
        if token:
            args.extend(["-c", f"http.extraheader=AUTHORIZATION: bearer {token}"])
        return args

    def _auth_token(self, dest: Path, token: str | None) -> str | None:
        if not token:
            return None
        url = self.run(dest, ["remote", "get-url", "origin"]).strip()
        if url.startswith("http://") or url.startswith("https://"):
            return token
        return None


def review_diff(workspace: GitWorkspace, dest: Path) -> tuple[list[str], int]:
    """Return changed files and the diff size. Length never blocks the change."""
    names = workspace.changed_names(dest)
    if not names:
        raise GitError("The edit did not change any file.")
    for name in names:
        if is_secret_path(name):
            raise GitError(f"The edit would touch `{name}`. I discarded it.")
    total, files = workspace.diff_numstat(dest)
    return files or names, total
