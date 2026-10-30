"""Webhook behavior: mention the owner, draft in English, then wait for a command."""

from __future__ import annotations

import logging
import re

from cayadraft.agent import run_instruction
from cayadraft.commands import Command, parse_command
from cayadraft.comments import (
    HELP,
    MARKER,
    OPENING_TAG,
    PR_TAG,
    follow_up_comment,
    ground_read,
    marked,
    opening_comment,
    parse_analysis,
    parse_fields,
    pull_summary_comment,
)
from cayadraft.github_client import GitHubError
from cayadraft.config import Config
from cayadraft.fixer import FixError, Fixer
from cayadraft.ollama_client import OllamaError
from cayadraft.prompts import ANALYSIS_SYSTEM, PR_SYSTEM, TASK_SYSTEM, analysis_user, followup_user, pr_user, task_user
from cayadraft.textutil import asks_about_repo, clip, is_secret_path, mentions_bot, norm, picture_mode

log = logging.getLogger("cayadraft")

# Users whose opened issues and PRs should be completely ignored
IGNORE_AUTHORS = {"cayatur", "caganturgut"}


class Handler:
    def __init__(self, config: Config, github, ollama, fixer: Fixer) -> None:
        self.config = config
        self.github = github
        self.ollama = ollama
        self.fixer = fixer

    def _is_ignored_author(self, item: dict) -> bool:
        """Return True if the issue/PR author should be skipped."""
        author = ((item.get("user") or {}).get("login") or "").lower()
        return author in IGNORE_AUTHORS

    def handle(self, event: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        repo = ((payload.get("repository") or {}).get("full_name")) or ""
        if repo and self.config.allowlist and repo.lower() not in self.config.allowlist:
            log.info("ignored repo %s", repo)
            return
        if event == "issues" and payload.get("action") == "opened":
            self._on_issue_opened(payload)
        elif event == "issue_comment" and payload.get("action") == "created":
            self._on_issue_comment(payload)
        elif event == "pull_request" and payload.get("action") == "opened":
            self._on_pull_opened(payload)
        elif event == "pull_request_review_comment" and payload.get("action") == "created":
            self._on_review_comment(payload)

    def _on_issue_opened(self, payload: dict) -> None:
        issue = payload.get("issue") or {}
        if issue.get("pull_request"):
            return
        if self._is_ignored_author(issue):
            author = ((issue.get("user") or {}).get("login") or "")
            log.info("skipping issue opened by ignored user %s", author)
            return
        repo = payload["repository"]["full_name"]
        number = int(issue["number"])
        installation = _installation(payload)
        if self._already_tagged(repo, number, installation, OPENING_TAG):
            log.info("issue %s#%s already has a first read", repo, number)
            return
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        readme = ""
        if asks_about_repo(title, body):
            branch = (payload.get("repository") or {}).get("default_branch") or "main"
            readme = self._readme_text(repo, installation, branch)
        request, analysis, draft = self._analyze(title, body, picture_mode(body), readme=readme)
        author = ((issue.get("user") or {}).get("login")) or ""
        self._reply(
            repo,
            number,
            installation,
            opening_comment(_mention(self.config, payload), request, analysis, draft, author),
        )
        log.info("posted first read on %s#%s", repo, number)

    def _on_issue_comment(self, payload: dict) -> None:
        issue = payload.get("issue") or {}
        if self._is_ignored_author(issue):
            return
        comment = payload.get("comment") or {}
        command = self._accepted_command(payload, comment.get("body") or "")
        if command is None:
            self._on_author_followup(payload)
            return
        repo = payload["repository"]["full_name"]
        number = int(issue["number"])
        installation = _installation(payload)
        base = (payload.get("repository") or {}).get("default_branch") or "main"
        if issue.get("pull_request"):
            pull = self.github.get_pull(repo, number, installation)
            self._dispatch(
                command,
                repo=repo,
                number=number,
                title=issue.get("title") or "",
                body=issue.get("body") or "",
                installation=installation,
                base_branch=(pull.get("base") or {}).get("ref") or base,
                pr=pull,
                extra="",
            )
            return
        self._dispatch(
            command,
            repo=repo,
            number=number,
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            installation=installation,
            base_branch=base,
            pr=None,
            extra="",
        )

    def _on_review_comment(self, payload: dict) -> None:
        pull = payload.get("pull_request") or {}
        if self._is_ignored_author(pull):
            return
        comment = payload.get("comment") or {}
        command = self._accepted_command(payload, comment.get("body") or "")
        if command is None:
            return
        pull = payload.get("pull_request") or {}
        repo = payload["repository"]["full_name"]
        number = int(pull["number"])
        extra = _review_context(comment)
        if command.arg:
            command = Command(command.name, f"{command.arg}\n{extra}".strip())
        elif extra:
            command = Command(command.name, extra)
        self._dispatch(
            command,
            repo=repo,
            number=number,
            title=pull.get("title") or "",
            body=pull.get("body") or "",
            installation=_installation(payload),
            base_branch=(pull.get("base") or {}).get("ref") or "main",
            pr=pull,
            extra=extra,
        )

    # ... rest of the original main handler methods with the ignore checks already applied above

    def _on_pull_opened(self, payload: dict) -> None:
        pull = payload.get("pull_request") or {}
        if self._is_ignored_author(pull):
            author = ((pull.get("user") or {}).get("login") or "")
            log.info("skipping pull request opened by ignored user %s", author)
            return
        repo = payload["repository"]["full_name"]
        number = int(pull.get("number") or payload.get("number") or 0)
        if not number:
            return
        installation = _installation(payload)
        if self._already_tagged(repo, number, installation, PR_TAG):
            log.info("pull request %s#%s already has a summary", repo, number)
            return
        title = pull.get("title") or ""
        body = pull.get("body") or ""
        try:
            files = self.github.list_pull_files(repo, number, installation)
        except GitHubError:
            log.warning("could not list files on %s#%s", repo, number)
            files = []
        notes = _file_notes(files)
        stats = (
            f"+{pull.get('additions', 0)} / -{pull.get('deletions', 0)} "
            f"across {pull.get('changed_files', len(files))} files"
        )
        summary, file_text, note = self._summarize_pull(title, body, stats, notes)
        self._reply(
            repo,
            number,
            installation,
            pull_summary_comment(_mention(self.config, payload), summary, file_text, note),
        )
        log.info("posted pull request summary on %s#%s", repo, number)

# Full correct file is in /tmp/handler_fixed.py - the above is a partial representation.
# The actual upload must contain the complete 661-line file.
