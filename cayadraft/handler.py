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


class Handler:
    def __init__(self, config: Config, github, ollama, fixer: Fixer) -> None:
        self.config = config
        self.github = github
        self.ollama = ollama
        self.fixer = fixer

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

    def _accepted_command(self, payload: dict, body: str) -> Command | None:
        sender = payload.get("sender") or {}
        login = sender.get("login") or ""
        if sender.get("type") == "Bot" or login.lower().endswith("[bot]"):
            return None
        command = parse_command(body)
        if command is None:
            return None
        installed = _installed_login(payload)
        if installed:
            allowed = login.lower() == installed.lower()
        else:
            allowed = self.config.owns(login)
        if not allowed:
            log.info("ignored command from %s", login)
            return None
        return command

    def _dispatch(self, command: Command, **ctx) -> None:
        if ctx["pr"] is not None and command.name == "fix":
            command = Command("revise", command.arg)
        log.info("command %s on %s#%s", command.name, ctx["repo"], ctx["number"])
        method = getattr(self, f"_cmd_{command.name}", None)
        if method is None:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], "I don't know that command.\n\n" + HELP)
            return
        method(command, **ctx)

    def _cmd_help(self, command: Command, **ctx) -> None:
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], HELP)

    def _cmd_unknown(self, command: Command, **ctx) -> None:
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], "I don't know that command.\n\n" + HELP)

    def _cmd_instruct(self, command: Command, **ctx) -> None:
        """Owner prose after @cayadraft. The model picks the tools. Slash commands stay single-purpose."""
        try:
            text = run_instruction(
                ollama=self.ollama,
                github=self.github,
                fixer=self.fixer,
                ctx=ctx,
                instruction=command.arg,
                num_predict=self.config.edit_tokens,
            )
        except OllamaError as exc:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], str(exc))
            return
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], text)

    def _cmd_wait(self, command: Command, **ctx) -> None:
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], "Waiting. I will not change anything until your next command.")

    def _cmd_analyze(self, command: Command, **ctx) -> None:
        readme = ""
        if asks_about_repo(ctx["title"], ctx["body"]):
            readme = self._readme_text(ctx["repo"], ctx["installation"], ctx["base_branch"])
        request, analysis, draft = self._analyze(ctx["title"], ctx["body"], picture_mode(ctx["body"]), readme=readme)
        self._reply(
            ctx["repo"],
            ctx["number"],
            ctx["installation"],
            f"**Request**\n{request or '—'}\n\n**Analysis**\n{analysis or '—'}\n\n**Suggested draft**\n{draft or '—'}",
        )

    def _cmd_summarize(self, command: Command, **ctx) -> None:
        self._task(
            ctx,
            "Summarize the issue in English in at most five short lines.",
        )

    def _cmd_checklist(self, command: Command, **ctx) -> None:
        self._task(ctx, "Turn the issue into a short checklist of concrete checks. Use Markdown checkboxes.")

    def _cmd_draft(self, command: Command, **ctx) -> None:
        self._task(ctx, "Write a suggested next step for the owner. Do not claim the code was changed.")

    def _cmd_reply(self, command: Command, **ctx) -> None:
        self._task(
            ctx,
            "Draft a polite reply the owner could post to the reporter. "
            "Make clear this is a draft and that you did not post it as the owner.",
        )

    def _cmd_ask(self, command: Command, **ctx) -> None:
        if not command.arg.strip():
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], "Add a question. Example: `/caya ask what is still unclear?`")
            return
        self._task(ctx, "Answer the owner's question using only the issue. If the issue does not say, say so.", command.arg)

    def _cmd_translate(self, command: Command, **ctx) -> None:
        target = "Turkish" if norm(command.arg).startswith("tr") or "turk" in norm(command.arg) else "English"
        self._task(ctx, f"Translate the issue title and body into {target}. Keep code and paths unchanged.")

    def _cmd_labels(self, command: Command, **ctx) -> None:
        self._labels(ctx, apply=False, requested=command.arg)

    def _cmd_labels_apply(self, command: Command, **ctx) -> None:
        self._labels(ctx, apply=True, requested=command.arg)

    def _cmd_title(self, command: Command, **ctx) -> None:
        self._title(ctx, apply=False, requested=command.arg)

    def _cmd_title_apply(self, command: Command, **ctx) -> None:
        self._title(ctx, apply=True, requested=command.arg)

    def _cmd_plan(self, command: Command, **ctx) -> None:
        self._code_action("plan", command, **ctx)

    def _cmd_fix(self, command: Command, **ctx) -> None:
        self._reply(
            ctx["repo"],
            ctx["number"],
            ctx["installation"],
            "Starting a small fix on a new branch. I will open a pull request only if the edit applies cleanly.",
        )
        self._code_action("fix", command, **ctx)

    def _cmd_revise(self, command: Command, **ctx) -> None:
        if ctx["pr"] is None:
            self._reply(
                ctx["repo"],
                ctx["number"],
                ctx["installation"],
                "Use `/caya revise` on the pull request. On an issue, use `/caya fix`.",
            )
            return
        self._code_action("revise", command, **ctx)

    def _cmd_explain(self, command: Command, **ctx) -> None:
        if ctx["pr"] is None:
            self._cmd_summarize(command, **ctx)
            return
        try:
            text = self.fixer.explain(
                repo=ctx["repo"],
                installation_id=ctx["installation"],
                base_branch=ctx["base_branch"],
                head_branch=ctx["pr"]["head"]["ref"],
            )
        except FixError as exc:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], str(exc))
            return
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], text)

    def _cmd_revert_last(self, command: Command, **ctx) -> None:
        self._revert(ctx, last_only=True)

    def _cmd_revert(self, command: Command, **ctx) -> None:
        self._revert(ctx, last_only=False)

    def _code_action(self, kind: str, command: Command, **ctx) -> None:
        try:
            if kind == "plan":
                head = ((ctx["pr"] or {}).get("head") or {}) if ctx["pr"] else {}
                text = self.fixer.plan(
                    repo=ctx["repo"],
                    number=ctx["number"],
                    title=ctx["title"],
                    body=ctx["body"],
                    instruction=command.arg,
                    installation_id=ctx["installation"],
                    base_branch=ctx["base_branch"],
                    head_branch=head.get("ref"),
                )
                self._reply(ctx["repo"], ctx["number"], ctx["installation"], text)
                return
            if kind == "revise":
                result = self.fixer.revise(
                    repo=ctx["repo"],
                    number=ctx["number"],
                    title=ctx["title"],
                    body=ctx["body"],
                    instruction=command.arg,
                    installation_id=ctx["installation"],
                    base_branch=ctx["base_branch"],
                    head_branch=ctx["pr"]["head"]["ref"],
                    html_url=ctx["pr"].get("html_url") or "",
                )
                self._reply(
                    ctx["repo"],
                    ctx["number"],
                    ctx["installation"],
                    f"I pushed another commit to `{result.branch}`.\n\n**Summary** {result.summary or 'Updated the branch.'}\n\nI did not merge.",
                )
                return
            result = self.fixer.fix(
                repo=ctx["repo"],
                number=ctx["number"],
                title=ctx["title"],
                body=ctx["body"],
                instruction=command.arg,
                installation_id=ctx["installation"],
                base_branch=ctx["base_branch"],
            )
        except FixError as exc:
            extra = f"\n\nThe branch `{exc.branch}` is on the remote, but the pull request was not opened." if exc.pushed else ""
            prefix = "" if exc.pushed else "I stopped without opening a pull request.\n\n"
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], f"{prefix}{exc}{extra}")
            return
        if result.created:
            lead = f"Pull request opened: {result.url}"
        else:
            lead = f"I added a commit to the existing pull request: {result.url}"
        review = f"\n\n{result.review_note}" if result.review_note else ""
        self._reply(
            ctx["repo"],
            ctx["number"],
            ctx["installation"],
            f"{lead}\n\n**Summary** {result.summary or 'Small fix.'}{review}\n\n"
            "Review it there. I will not merge. Use `/caya revise`, `/caya revert last`, or `/caya revert` on that pull request.",
        )

    def _revert(self, ctx: dict, *, last_only: bool) -> None:
        if ctx["pr"] is None:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], "Revert works on the pull request, not on the issue.")
            return
        try:
            message = self.fixer.revert(
                repo=ctx["repo"],
                number=ctx["number"],
                installation_id=ctx["installation"],
                base_branch=ctx["base_branch"],
                head_branch=ctx["pr"]["head"]["ref"],
                last_only=last_only,
            )
        except FixError as exc:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], f"I did not revert.\n\n{exc}")
            return
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], message)

    def _labels(self, ctx: dict, *, apply: bool, requested: str) -> None:
        try:
            existing = self.github.list_labels(ctx["repo"], ctx["installation"])
        except GitHubError as exc:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], f"I could not read labels. {exc}")
            return
        if not existing:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], "This repository has no labels yet. I did not create any.")
            return
        if requested.strip():
            wanted = [part.strip() for part in re.split(r"[,\n]", requested) if part.strip()]
        else:
            try:
                raw = self.ollama.complete(
                    system=TASK_SYSTEM,
                    user=task_user(
                        "Pick labels only from the provided list. Reply with one line: LABELS: name, name. "
                        "If none fit, reply LABELS: none.",
                        ctx["title"],
                        ctx["body"],
                        "Existing labels: " + ", ".join(existing),
                    ),
                    temperature=0.1,
                    num_predict=120,
                )
            except OllamaError as exc:
                self._reply(ctx["repo"], ctx["number"], ctx["installation"], str(exc))
                return
            match = re.search(r"LABELS:\s*(.+)", raw)
            chosen = match.group(1) if match else raw
            if norm(chosen).startswith("none"):
                self._reply(ctx["repo"], ctx["number"], ctx["installation"], "None of the existing labels fit.")
                return
            wanted = [part.strip() for part in chosen.split(",") if part.strip()]
        by_lower = {label.lower(): label for label in existing}
        matched = []
        missing = []
        for label in wanted:
            found = by_lower.get(label.lower())
            if found and found not in matched:
                matched.append(found)
            elif not found:
                missing.append(label)
        if not matched:
            self._reply(
                ctx["repo"],
                ctx["number"],
                ctx["installation"],
                "No existing label matched. I did not create a new one.\n\nExisting: " + ", ".join(existing),
            )
            return
        if not apply:
            self._reply(
                ctx["repo"],
                ctx["number"],
                ctx["installation"],
                "Suggested labels: " + ", ".join(f"`{label}`" for label in matched) + "\n\n`/caya labels apply` adds them.",
            )
            return
        self.github.add_labels(ctx["repo"], ctx["number"], matched, ctx["installation"])
        note = f" I skipped unknown labels: {', '.join(missing)}." if missing else ""
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], "Labels added: " + ", ".join(f"`{label}`" for label in matched) + "." + note)

    def _title(self, ctx: dict, *, apply: bool, requested: str) -> None:
        if apply and requested.strip():
            title = requested.strip().splitlines()[0][:120]
        else:
            try:
                raw = self.ollama.complete(
                    system=TASK_SYSTEM,
                    user=task_user(
                        "Suggest one clearer issue title in English. Reply with one line: TITLE: the title",
                        ctx["title"],
                        ctx["body"],
                    ),
                    temperature=0.2,
                    num_predict=80,
                )
            except OllamaError as exc:
                self._reply(ctx["repo"], ctx["number"], ctx["installation"], str(exc))
                return
            match = re.search(r"TITLE:\s*(.+)", raw)
            title = (match.group(1) if match else raw).strip().splitlines()[0][:120]
        if not apply:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], f"Suggested title: {title}\n\n`/caya title apply` renames the issue.")
            return
        self.github.set_title(ctx["repo"], ctx["number"], title, ctx["installation"])
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], f"Title updated to: {title}")

    def _task(self, ctx: dict, task: str, extra: str = "") -> None:
        try:
            text = self.ollama.complete(
                system=TASK_SYSTEM,
                user=task_user(task, ctx["title"], ctx["body"], extra),
                temperature=0.2,
                num_predict=self.config.analysis_tokens,
            )
        except OllamaError as exc:
            self._reply(ctx["repo"], ctx["number"], ctx["installation"], str(exc))
            return
        self._reply(ctx["repo"], ctx["number"], ctx["installation"], clip(text, 6000))

    def _on_author_followup(self, payload: dict) -> None:
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        sender = payload.get("sender") or {}
        login = sender.get("login") or ""
        if sender.get("type") == "Bot" or login.lower().endswith("[bot]"):
            return
        text = comment.get("body") or ""
        if not text.strip() or MARKER in text:
            return
        if not mentions_bot(text, "cayadraft"):
            return
        repo = payload["repository"]["full_name"]
        number = int(issue["number"])
        installation = _installation(payload)
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        request, analysis, draft = self._analyze(title, body, picture_mode(body, text), comment=text)
        self._reply(
            repo,
            number,
            installation,
            follow_up_comment(_mention(self.config, payload), request, analysis, draft),
        )
        log.info("posted update on %s#%s", repo, number)

    def _on_pull_opened(self, payload: dict) -> None:
        pull = payload.get("pull_request") or {}
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

    def _readme_text(self, repo: str, installation: int | None, branch: str) -> str:
        for path in ("README.md", "README.txt", "README"):
            try:
                item = self.github.get_file(repo, path, branch, installation)
            except GitHubError:
                continue
            return item.get("bytes", b"").decode("utf-8", "replace")
        return ""

    def _analyze(self, title: str, body: str, picture: str = "", comment: str = "", readme: str = "") -> tuple[str, str, str]:
        user = followup_user(title, body, comment, picture) if comment else analysis_user(title, body, picture, readme)
        try:
            raw = self.ollama.complete(
                system=ANALYSIS_SYSTEM,
                user=user,
                temperature=0.2,
                num_predict=self.config.analysis_tokens,
            )
        except OllamaError:
            return (
                title or "New issue",
                "The local model is unavailable, so I could not draft an analysis. I stopped after notifying you.",
                "Start Ollama with qwen3.5:4b, then comment `/caya analyze`.",
            )
        request, analysis, draft = parse_analysis(raw)
        if not analysis and not draft:
            analysis = raw
        return ground_read(title, body, request, analysis, draft, picture, readme, comment)

    def _summarize_pull(self, title: str, body: str, stats: str, notes: str) -> tuple[str, str, str]:
        try:
            raw = self.ollama.complete(
                system=PR_SYSTEM,
                user=pr_user(title, body, stats, notes, picture_mode(body)),
                temperature=0.2,
                num_predict=self.config.analysis_tokens,
            )
        except OllamaError:
            return (
                title or "New pull request",
                notes or "The file list was not available.",
                "The local model is unavailable, so this is only the title and file list.",
            )
        fields = parse_fields(raw, ("SUMMARY", "FILES", "NOTE"))
        summary = fields["SUMMARY"] or clip(raw, 2000)
        file_text = fields["FILES"] or "See the pull request for the file list."
        note = fields["NOTE"] or "Nothing else was called out."
        return summary, file_text, note

    def _already_tagged(self, repo: str, number: int, installation: int | None, tag: str) -> bool:
        try:
            comments = self.github.list_comments(repo, number, installation)
        except GitHubError:
            log.warning("could not list comments on %s#%s", repo, number)
            return False
        return any(tag in (item.get("body") or "") for item in comments)

    def _reply(self, repo: str, number: int, installation: int | None, body: str) -> None:
        self.github.post_comment(repo, number, marked(body), installation)


def _file_notes(files: list[dict]) -> str:
    chunks = []
    for item in files[:12]:
        name = str(item.get("filename") or "file")
        status = item.get("status") or "modified"
        added = item.get("additions", 0)
        removed = item.get("deletions", 0)
        line = f"{name} ({status}, +{added}/-{removed})"
        patch = item.get("patch") or ""
        if patch and not is_secret_path(name):
            line += "\n" + clip(patch, 400)
        chunks.append(line)
    extra = len(files) - 12
    if extra > 0:
        chunks.append(f"{extra} more files are not shown.")
    return "\n\n".join(chunks) or "(no file list)"


def _installed_login(payload: dict) -> str:
    account = (payload.get("installation") or {}).get("account") or {}
    return str(account.get("login") or "").strip()


def _mention(config, payload: dict) -> str:
    """Tag the account that installed the app. The env name is only a fallback."""
    return _installed_login(payload) or config.mention_login


def _installation(payload: dict) -> int | None:
    installation = payload.get("installation") or {}
    value = installation.get("id")
    return int(value) if value else None


def _review_context(comment: dict) -> str:
    path = comment.get("path") or ""
    line = comment.get("line") or comment.get("original_line") or ""
    hunk = clip(comment.get("diff_hunk") or "", 2000)
    parts = []
    if path:
        parts.append(f"File: {path}")
    if line:
        parts.append(f"Line: {line}")
    if hunk:
        parts.append("Diff hunk:\n" + hunk)
    return "\n".join(parts)
