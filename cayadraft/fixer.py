"""Turn an owner request into a reviewable branch. Never merges, never runs repo code."""

from __future__ import annotations

import datetime
import difflib
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cayadraft.comments import pull_request_body
from cayadraft.config import Config
from cayadraft.context import SKIP_DIRS, extract_paths, gather_context, is_text_candidate
from cayadraft.edits import Edit, EditError, Proposal, apply_edits, clean_rel, format_proposal, parse_proposal, validate_rel
from cayadraft.gitwork import GitError, GitWorkspace, review_diff
from cayadraft.github_client import GitHubError
from cayadraft.ollama_client import OllamaError
from cayadraft.prompts import CREATE_SYSTEM, EDIT_SYSTEM, create_user, edit_user
from cayadraft.store import PlanStore
from cayadraft.textutil import clip, is_secret_path, strip_fences

log = logging.getLogger("cayadraft.fixer")

COMMIT_PREFIX = "CaYaDraft:"


class FixError(RuntimeError):
    def __init__(self, message: str, *, branch: str | None = None, pushed: bool = False) -> None:
        super().__init__(message)
        self.branch = branch
        self.pushed = pushed


@dataclass(frozen=True)
class FixResult:
    url: str
    branch: str
    summary: str
    number: int
    files: list[str]
    created: bool = True
    review_note: str = ""


class Fixer:
    def __init__(self, config: Config, github, ollama, workspace: GitWorkspace, plans: PlanStore, url_for=None) -> None:
        self.config = config
        self.github = github
        self.ollama = ollama
        self.git = workspace
        self.plans = plans
        self.url_for = url_for or (lambda repo: f"https://github.com/{repo}.git")

    def _uses_api(self, repo: str) -> bool:
        return self.url_for(repo).startswith("http")

    def plan(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str | None = None,
    ) -> str:
        if self._uses_api(repo):
            with self._workdir() as dest:
                sha, _blobs, _missing = self._fetch_named(
                    dest, repo, head_branch or base_branch, title, body, instruction, installation_id
                )
                if not _blobs:
                    raise FixError(_missing_file_message(_missing))
                proposal, _raw = self._propose(dest, repo, number, title, body, instruction, sha)
        else:
            with self._workdir() as dest:
                token = self.github.token_for(installation_id)
                self._checkout(dest, repo, base_branch, head_branch, token)
                proposal, _raw = self._propose(dest, repo, number, title, body, instruction, self.git.head_sha(dest))
        if proposal.cannot:
            raise FixError(proposal.cannot)
        return format_proposal(proposal)

    def fix(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
    ) -> FixResult:
        branch = f"cayadraft/issue-{number}"
        existing = self.github.find_open_pull(repo, branch, installation_id)
        if existing:
            return self.revise(
                repo=repo,
                number=int(existing["number"]),
                title=title,
                body=body,
                instruction=instruction or "Apply the requested fix.",
                installation_id=installation_id,
                base_branch=base_branch,
                head_branch=branch,
                html_url=existing.get("html_url") or "",
            )
        reopened = self._pull_from_existing_branch(
            repo=repo,
            number=number,
            title=title,
            installation_id=installation_id,
            base_branch=base_branch,
            branch=branch,
        )
        if reopened:
            return reopened
        return self._commit_and_open(
            repo=repo,
            number=number,
            title=title,
            body=body,
            instruction=instruction,
            installation_id=installation_id,
            base_branch=base_branch,
            head_branch=None,
            new_branch=branch,
            open_pr=True,
        )

    def _pull_from_existing_branch(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        installation_id: int | None,
        base_branch: str,
        branch: str,
    ) -> FixResult | None:
        tip = self._branch_tip(repo, branch, installation_id)
        if not tip:
            return None
        try:
            base_tip = self.github.get_branch_sha(repo, base_branch, installation_id)
        except GitHubError as exc:
            raise FixError(_access_message(exc)) from exc
        if tip == base_tip:
            return None
        summary = "The branch already had this fix. I opened another pull request and did not rewind it."
        try:
            pr = self.github.create_pull(
                repo,
                title=clip(f"Fix #{number}: {title}", 120),
                head=branch,
                base=base_branch,
                body=pull_request_body(number, summary, []),
                installation_id=installation_id,
            )
        except GitHubError as exc:
            raise FixError(_access_message(exc), branch=branch, pushed=True) from exc
        return FixResult(pr["html_url"], branch, summary, int(pr.get("number") or 0), [])

    def _branch_tip(self, repo: str, branch: str, installation_id: int | None) -> str:
        try:
            return self.github.get_branch_sha(repo, branch, installation_id)
        except GitHubError as exc:
            if "404" in str(exc):
                return ""
            raise FixError(_access_message(exc)) from exc

    def revise(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str,
        html_url: str,
    ) -> FixResult:
        if not instruction.strip():
            raise FixError("Tell me what to change. Example: `/caya revise say Save instead of Submit`.")
        result = self._commit_and_open(
            repo=repo,
            number=number,
            title=title,
            body=body,
            instruction=instruction,
            installation_id=installation_id,
            base_branch=base_branch,
            head_branch=head_branch,
            new_branch=head_branch,
            open_pr=False,
        )
        return FixResult(
            html_url or result.url,
            result.branch,
            result.summary,
            number,
            result.files,
            created=False,
            review_note=result.review_note,
        )

    def explain(
        self,
        *,
        repo: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str,
    ) -> str:
        if self._uses_api(repo):
            try:
                compared = self.github.compare(repo, base_branch, head_branch, installation_id)
            except GitHubError as exc:
                raise FixError(_access_message(exc)) from exc
            parts = []
            for item in compared.get("files") or []:
                name = item.get("filename") or "file"
                patch = item.get("patch") or ""
                if patch and not is_secret_path(name):
                    parts.append(f"--- {name} ---\n{patch}")
            diff = "\n\n".join(parts)
        else:
            with self._workdir() as dest:
                token = self.github.token_for(installation_id)
                self._checkout(dest, repo, base_branch, head_branch, token)
                diff = self.git.diff_against_base(dest, base_branch)
        if not diff.strip():
            return "This branch has no difference from the base branch."
        system = (
            "You are CaYaDraft. Explain this git diff in English, in under 120 words. "
            "Do not invent changes that are not in the diff."
        )
        try:
            return self.ollama.complete(
                system=system,
                user=clip(diff, 6000),
                temperature=0.2,
                num_predict=400,
            )
        except OllamaError as exc:
            raise FixError(str(exc)) from exc

    def revert(
        self,
        *,
        repo: str,
        number: int,
        installation_id: int | None,
        base_branch: str,
        head_branch: str,
        last_only: bool,
    ) -> str:
        if self._uses_api(repo):
            return self._revert_on_github(
                repo=repo,
                installation_id=installation_id,
                base_branch=base_branch,
                head_branch=head_branch,
                last_only=last_only,
            )
        with self._workdir() as dest:
            token = self.github.token_for(installation_id)
            self._checkout(dest, repo, base_branch, head_branch, token)
            subjects = self.git.log_subjects(dest, base_branch)
            if last_only:
                if not subjects:
                    raise FixError("This branch has no commits to revert.")
                sha, subject = subjects[0]
                if not subject.startswith(COMMIT_PREFIX):
                    raise FixError("The latest commit was not made by CaYaDraft. I did not revert it.")
                self.git.revert_commit(dest, sha)
                message = f"{COMMIT_PREFIX} revert last change\n\nReverts {sha[:7]}."
            else:
                self.git.restore_to_base(dest, base_branch)
                if not self.git.changed_names(dest):
                    raise FixError("The branch already matches the base branch.")
                message = f"{COMMIT_PREFIX} revert branch to base"
            for name in self.git.changed_names(dest):
                if is_secret_path(name):
                    raise FixError(f"The revert would touch `{name}`. I stopped.")
            self.git.commit_all(dest, message)
            self.git.push(dest, head_branch, token)
        action = "the latest bot commit" if last_only else "every change on this branch"
        return f"I added a commit that reverts {action}. The history is still there. I did not force-push."

    def _commit_and_open(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str | None,
        new_branch: str,
        open_pr: bool,
    ) -> FixResult:
        if self._uses_api(repo):
            return self._commit_on_github(
                repo=repo,
                number=number,
                title=title,
                body=body,
                instruction=instruction,
                installation_id=installation_id,
                base_branch=base_branch,
                head_branch=head_branch,
                new_branch=new_branch,
                open_pr=open_pr,
            )
        pushed = False
        with self._workdir() as dest:
            token = self.github.token_for(installation_id)
            self._checkout(dest, repo, base_branch, head_branch, token)
            if head_branch is None:
                self.git.checkout_new(dest, new_branch)
            head = self.git.head_sha(dest)
            proposal = self._proposal_for(dest, repo, number, title, body, instruction, head)
            if proposal.cannot:
                raise FixError(proposal.cannot)
            try:
                files = apply_edits(
                    dest,
                    proposal.edits,
                    max_files=self.config.max_files,
                    max_edits=self.config.max_edits,
                )
            except EditError as exc:
                self.plans.delete(self._key(repo, number))
                raise FixError(str(exc)) from exc
            try:
                files, review_note = self._ensure_safe(dest)
                summary = proposal.summary or f"simple fix for issue #{number}"
                self.git.commit_all(dest, f"{COMMIT_PREFIX} {summary}\n\nCaYaDraft-Issue: {number}")
                self.git.push(dest, new_branch, token)
                pushed = True
            except GitError as exc:
                raise FixError(str(exc)) from exc
        if not open_pr:
            return FixResult("", new_branch, proposal.summary, number, files, created=False, review_note=review_note)
        pr_title = clip(f"Fix #{number}: {proposal.summary or title}", 120)
        try:
            pr = self.github.create_pull(
                repo,
                title=pr_title,
                head=new_branch,
                base=base_branch,
                body=pull_request_body(number, proposal.summary, files, review_note),
                installation_id=installation_id,
            )
        except GitHubError as exc:
            raise FixError(str(exc), branch=new_branch, pushed=pushed) from exc
        return FixResult(
            pr["html_url"], new_branch, proposal.summary, int(pr.get("number") or 0), files, review_note=review_note
        )

    def _proposal_for(self, dest, repo, number, title, body, instruction, head) -> Proposal:
        saved = self.plans.get(self._key(repo, number))
        saved_instruction = (saved or {}).get("instruction") or ""
        reuse = bool(saved and saved.get("head_sha") == head and saved.get("raw"))
        if reuse and (not (instruction or "").strip() or saved_instruction == (instruction or "")):
            proposal = parse_proposal(saved["raw"])
            if proposal.edits:
                log.info("using saved plan for %s#%s", repo, number)
                return proposal
        proposal, _raw = self._propose(dest, repo, number, title, body, instruction, head)
        return proposal

    def _propose(self, dest, repo, number, title, body, instruction, head) -> tuple[Proposal, str]:
        snippets = gather_context(dest, title, body, instruction)
        if not snippets.strip():
            raise FixError("I could not find a file to edit. Name the file in the command, for example `/caya fix path/to/file`.")
        try:
            raw = self.ollama.complete(
                system=EDIT_SYSTEM,
                user=edit_user(title, body, instruction, snippets),
                temperature=0.1,
                num_predict=self.config.edit_tokens,
            )
        except OllamaError as exc:
            raise FixError(str(exc)) from exc
        proposal = parse_proposal(raw)
        if proposal.edits:
            self.plans.put(
                self._key(repo, number),
                {"head_sha": head, "raw": raw, "instruction": instruction or ""},
            )
        return proposal, raw

    def _commit_on_github(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str | None,
        new_branch: str,
        open_pr: bool,
    ) -> FixResult:
        branch = head_branch or base_branch
        with self._workdir() as dest:
            parent_sha, blobs, missing = self._fetch_named(dest, repo, branch, title, body, instruction, installation_id)
            if not blobs:
                return self._create_missing(
                    repo=repo,
                    number=number,
                    title=title,
                    body=body,
                    instruction=instruction,
                    installation_id=installation_id,
                    base_branch=base_branch,
                    head_branch=head_branch,
                    new_branch=new_branch,
                    open_pr=open_pr,
                    parent_sha=parent_sha,
                    missing=missing,
                )
            try:
                proposal = self._proposal_for(dest, repo, number, title, body, instruction, parent_sha)
            except FixError:
                proposal = Proposal("", [], "The model did not return an edit.")
            before = _snapshot(dest, list(blobs))
            updated, created, summary_override = self._realize(
                dest, proposal, blobs, title, body, instruction, repo, number
            )
            files = _uniq(updated + created)
            changed = _changed_lines(before, dest, updated)
            added = sum(_line_count(dest / path) for path in created)
            review_note = _review_note(changed + added, self.config.max_diff_lines)
            updated = [
                path
                for path in updated
                if (dest / path).is_file() and (dest / path).read_bytes() != before.get(path, b"")
            ]
            files = _uniq(updated + created)
            if not files:
                raise FixError("The edit did not change any file.")
            target = head_branch or new_branch
            tracked = _tracked_issue(number, target)
            summary = summary_override or proposal.summary or f"simple fix for issue #{tracked}"
            if summary.upper().startswith("CANNOT"):
                summary = summary_override or f"simple fix for issue #{tracked}"
            message = f"{COMMIT_PREFIX} {summary}\n\nCaYaDraft-Issue: {tracked}"
            try:
                if head_branch is None:
                    self._publish_branch(repo, new_branch, parent_sha, installation_id, create=True)
                for path in updated:
                    real = _blob_for(path, blobs) or path
                    self.github.update_file(
                        repo,
                        real,
                        message,
                        (dest / real).read_bytes(),
                        blobs[real],
                        target,
                        installation_id,
                    )
                for path in created:
                    self.github.create_file(
                        repo,
                        path,
                        message,
                        (dest / path).read_bytes(),
                        target,
                        installation_id,
                    )
            except GitHubError as exc:
                raise FixError(_access_message(exc), branch=target, pushed=head_branch is None) from exc
        if not open_pr:
            return FixResult(
                "", new_branch, summary, number, files, created=False, review_note=review_note
            )
        pr_title = clip(f"Fix #{number}: {summary or title}", 120)
        try:
            pr = self.github.create_pull(
                repo,
                title=pr_title,
                head=new_branch,
                base=base_branch,
                body=pull_request_body(number, summary, files, review_note),
                installation_id=installation_id,
            )
        except GitHubError as exc:
            raise FixError(_access_message(exc), branch=new_branch, pushed=True) from exc
        return FixResult(
            pr["html_url"], new_branch, summary, int(pr.get("number") or 0), files, review_note=review_note
        )

    def _revert_on_github(
        self,
        *,
        repo: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str,
        last_only: bool,
    ) -> str:
        try:
            head_sha = self.github.get_branch_sha(repo, head_branch, installation_id)
            head = self.github.get_git_commit(repo, head_sha, installation_id)
            base_sha = self.github.get_branch_sha(repo, base_branch, installation_id)
            base = self.github.get_git_commit(repo, base_sha, installation_id)
            if last_only:
                compared = self.github.compare(repo, base_branch, head_branch, installation_id)
                commits = compared.get("commits") or []
                if not commits:
                    raise FixError("This branch has no commits to revert.")
                last = commits[-1]
                message = ((last.get("commit") or {}).get("message")) or ""
                if not message.startswith(COMMIT_PREFIX):
                    raise FixError("The latest commit was not made by CaYaDraft. I did not revert it.")
                parents = last.get("parents") or []
                if not parents:
                    raise FixError("The latest commit has no parent. I did not revert it.")
                previous = self.github.get_git_commit(repo, parents[0]["sha"], installation_id)
                tree = previous["tree"]["sha"]
                text = f"{COMMIT_PREFIX} revert last change\n\nReverts {str(last.get('sha') or '')[:7]}."
            else:
                tree = base["tree"]["sha"]
                if tree == head["tree"]["sha"]:
                    raise FixError("The branch already matches the base branch.")
                text = f"{COMMIT_PREFIX} revert branch to base"
            self._reject_secret_tree_change(repo, head["tree"]["sha"], tree, installation_id)
            commit = self.github.create_git_commit(repo, text, tree, [head_sha], installation_id)
            self.github.update_ref(repo, head_branch, commit, installation_id)
        except GitHubError as exc:
            raise FixError(_access_message(exc)) from exc
        action = "the latest bot commit" if last_only else "every change on this branch"
        return f"I added a commit that reverts {action}. The history is still there. I did not force-push."

    def _fetch_named(
        self,
        dest: Path,
        repo: str,
        branch: str,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
    ) -> tuple[str, dict[str, str]]:
        paths = _candidate_paths(title, body, instruction)
        if not paths:
            raise FixError("Name the file in the command, for example `/caya fix path/to/file`.")
        try:
            sha = self.github.get_branch_sha(repo, branch, installation_id)
        except GitHubError as exc:
            raise FixError(_access_message(exc)) from exc
        found: dict[str, str] = {}
        missing: list[str] = []
        dest.mkdir(parents=True, exist_ok=True)
        for path in paths:
            try:
                item = self.github.get_file(repo, path, branch, installation_id)
            except GitHubError as exc:
                if "404" in str(exc):
                    missing.append(path)
                    continue
                raise FixError(_access_message(exc)) from exc
            full = dest / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(item["bytes"])
            found[path] = item["sha"]
        return sha, found, missing

    def _create_missing(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        instruction: str,
        installation_id: int | None,
        base_branch: str,
        head_branch: str | None,
        new_branch: str,
        open_pr: bool,
        parent_sha: str,
        missing: list[str],
    ) -> FixResult:
        path = missing[0]
        try:
            raw = self.ollama.complete(
                system=CREATE_SYSTEM,
                user=create_user(title, body, instruction, path),
                temperature=0.2,
                num_predict=self.config.edit_tokens,
            )
        except OllamaError:
            raw = ""
        proposal = parse_proposal(raw)
        content = ""
        summary = proposal.summary
        if proposal.created:
            _created_path, content = proposal.created[0]
        elif proposal.edits and proposal.edits[0].replace.strip():
            content = proposal.edits[0].replace
            summary = summary or f"add {path} for issue #{number}"
        terms = _payload_terms(title, body, instruction)
        missing_terms = _missing_terms(terms, content)
        none_present = bool(terms) and len(missing_terms) == len(terms)
        if not content.strip() or (none_present and len(content.splitlines()) < 15):
            content = _fallback_new_file(title, body, instruction)
            summary = f"add {path} for issue #{number}"
        content = strip_fences(content).strip("\n") + "\n"
        self._check_new_file(path, content)
        review_note = _review_note(len(content.splitlines()), self.config.max_diff_lines)
        summary = summary or f"add {path} for issue #{number}"
        target = head_branch or new_branch
        tracked = _tracked_issue(number, target)
        message = f"{COMMIT_PREFIX} {summary}\n\nCaYaDraft-Issue: {tracked}"
        try:
            if head_branch is None:
                self._publish_branch(repo, new_branch, parent_sha, installation_id, create=True)
            self.github.create_file(
                repo,
                path,
                message,
                content.encode("utf-8"),
                target,
                installation_id,
            )
        except GitHubError as exc:
            raise FixError(_access_message(exc), branch=target, pushed=head_branch is None) from exc
        if not open_pr:
            return FixResult("", new_branch, summary, number, [path], created=False, review_note=review_note)
        pr_title = clip(f"Fix #{number}: {summary or title}", 120)
        try:
            pr = self.github.create_pull(
                repo,
                title=pr_title,
                head=new_branch,
                base=base_branch,
                body=pull_request_body(number, summary, [path], review_note),
                installation_id=installation_id,
            )
        except GitHubError as exc:
            raise FixError(_access_message(exc), branch=new_branch, pushed=True) from exc
        return FixResult(
            pr["html_url"], new_branch, summary, int(pr.get("number") or 0), [path], review_note=review_note
        )

    def _realize(
        self,
        dest: Path,
        proposal: Proposal,
        blobs: dict[str, str],
        title: str,
        body: str,
        instruction: str,
        repo: str,
        number: int,
    ) -> tuple[list[str], list[str], str]:
        """Apply a model proposal. If it misses the issue text, write that text instead."""
        original = {
            path: (dest / path).read_bytes()
            for path in blobs
            if (dest / path).is_file()
        }
        updated: list[str] = []
        created: list[str] = []
        for path, content in proposal.created:
            self._place_content(dest, path, content, blobs, updated, created)
        edits: list[Edit] = []
        for edit in proposal.edits:
            rel = clean_rel(edit.path)
            real = _blob_for(rel, blobs)
            if real:
                edits.append(Edit(real, edit.find, edit.replace))
                continue
            content = (edit.replace or "").strip()
            if content:
                self._place_content(dest, rel, content, blobs, updated, created)
        if edits:
            try:
                updated.extend(
                    apply_edits(
                        dest,
                        edits,
                        max_files=self.config.max_files,
                        max_edits=self.config.max_edits,
                    )
                )
            except EditError:
                self.plans.delete(self._key(repo, number))
        updated = _uniq(updated)
        created = _uniq(created)
        if updated or created:
            updated, created, summary = self._require_payload(
                dest, original, blobs, updated, created, title, body, instruction
            )
        else:
            target = _best_existing(blobs, title, body, instruction)
            if not target and not _wants_mit(instruction, body):
                raise FixError("I could not tell which file to change. Name the file in the command.")
            summary = "Add the text requested in this issue."
            updated, created = [], []
            if target:
                previous = (dest / target).read_bytes() if (dest / target).is_file() else b""
                _append_request(dest / target, title, body, instruction)
                if (dest / target).read_bytes() != previous:
                    updated = [target]
        updated, created = _attach_mit(dest, blobs, instruction, body, updated, created)
        if not updated and not created:
            raise FixError("The edit did not change any file.")
        return updated, created, summary

    def _place_content(
        self,
        dest: Path,
        path: str,
        content: str,
        blobs: dict[str, str],
        updated: list[str],
        created: list[str],
    ) -> None:
        text = strip_fences(content).strip("\n")
        if not text:
            return
        rel = clean_rel(path)
        real = _blob_for(rel, blobs)
        if real:
            current = (dest / real).read_text(encoding="utf-8", errors="replace")
            if current.strip() and current.strip() not in text:
                _append_request(dest / real, "", text, "")
            else:
                _write_text(dest / real, text + "\n")
            updated.append(real)
            return
        self._check_new_file(rel, text + "\n")
        _write_text(dest / rel, text + "\n")
        created.append(rel)

    def _require_payload(
        self,
        dest: Path,
        original: dict[str, bytes],
        blobs: dict[str, str],
        updated: list[str],
        created: list[str],
        title: str,
        body: str,
        instruction: str,
    ) -> tuple[list[str], list[str], str]:
        """Drop a model edit that ignores the issue and write the issue text instead."""
        original_text = "\n".join(raw.decode("utf-8", "replace") for raw in original.values())
        task = _task_source(body, instruction)
        date_line = _date_line(instruction, body)
        needed = [
            term
            for term in _payload_terms(title, body, instruction)
            if term.casefold() not in original_text.casefold()
        ]
        focus = _focus_paths(updated, created, title, body, instruction)
        focus_text = "\n".join(
            (dest / path).read_text(encoding="utf-8", errors="replace")
            for path in focus
            if (dest / path).is_file()
        )
        copied_bot_text = _is_bot_text(focus_text) and not _is_bot_text(original_text)
        date_missing = bool(date_line) and date_line not in focus_text
        if not needed and not copied_bot_text and not date_missing:
            return updated, created, ""
        if needed and not _missing_terms(needed, focus_text) and not _messy_addition(original_text, focus_text, needed) and not copied_bot_text and not date_missing:
            return updated, created, ""
        for path, raw in original.items():
            if path in updated or path in focus:
                (dest / path).write_bytes(raw)
        for path in created:
            full = dest / path
            if full.is_file() and path not in original:
                full.unlink()
        target = _best_existing(blobs, title, body, instruction) or next(iter(original), "")
        if not target:
            raise FixError("I could not tell which file to change. Name the file in the command.")
        _append_request(dest / target, title, body, instruction)
        return [target], [], "Add the text requested in this issue."

    def _check_new_file(self, path: str, content: str) -> None:
        try:
            validate_rel(path)
        except EditError as exc:
            raise FixError(str(exc)) from exc
        if is_secret_path(path):
            raise FixError(f"I will not add `{path}`.")
        if "\x00" in content:
            raise FixError(f"`{path}` looks binary. I did not add it.")
        if not content.strip():
            raise FixError(f"`{path}` was empty. I did not add it.")

    def _materialize(
        self,
        dest: Path,
        repo: str,
        branch: str,
        installation_id: int | None,
        known_sha: str | None = None,
    ) -> str:
        try:
            sha = known_sha or self.github.get_branch_sha(repo, branch, installation_id)
            commit = self.github.get_git_commit(repo, sha, installation_id)
            tree, truncated = self.github.get_tree(repo, commit["tree"]["sha"], installation_id)
        except GitHubError as exc:
            raise FixError(_access_message(exc)) from exc
        if truncated:
            raise FixError("The repository tree is too large to scan. Name the file in the command.")
        chosen = _choose_files(tree)
        dest.mkdir(parents=True, exist_ok=True)
        for path, blob_sha in chosen:
            try:
                raw = self.github.get_blob(repo, blob_sha, installation_id)
            except GitHubError as exc:
                raise FixError(_access_message(exc)) from exc
            full = dest / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(raw)
        return sha

    def _publish_branch(self, repo: str, branch: str, sha: str, installation_id: int | None, *, create: bool) -> None:
        try:
            if create:
                self.github.create_ref(repo, branch, sha, installation_id)
                return
            self.github.update_ref(repo, branch, sha, installation_id)
        except GitHubError as exc:
            text = str(exc).lower()
            if create and "already exists" in text:
                raise FixError(f"The branch `{branch}` already exists. I did not rewind it.")
            raise

    def _reject_secret_tree_change(self, repo: str, old_tree: str, new_tree: str, installation_id: int | None) -> None:
        before, _truncated = self.github.get_tree(repo, old_tree, installation_id)
        after, _truncated_after = self.github.get_tree(repo, new_tree, installation_id)
        old = {item.get("path"): item.get("sha") for item in before if item.get("type") == "blob"}
        new = {item.get("path"): item.get("sha") for item in after if item.get("type") == "blob"}
        for path in set(old) | set(new):
            if path and old.get(path) != new.get(path) and is_secret_path(str(path)):
                raise FixError(f"The revert would touch `{path}`. I stopped.")

    def _checkout(self, dest: Path, repo: str, base_branch: str, head_branch: str | None, token: str) -> None:
        try:
            self.git.clone(self.url_for(repo), dest, base_branch, token)
            if head_branch:
                self.git.fetch_branch(dest, head_branch, token)
        except GitError as exc:
            raise FixError(str(exc)) from exc

    def _ensure_safe(self, dest: Path) -> tuple[list[str], str]:
        try:
            files, total = review_diff(self.git, dest)
        except GitError as exc:
            raise FixError(str(exc)) from exc
        return files, _review_note(total, self.config.max_diff_lines)

    def _workdir(self):
        root = Path(self.config.work_root) if self.config.work_root else None
        if root:
            root.mkdir(parents=True, exist_ok=True)
        return _TempRepo(tempfile.TemporaryDirectory(prefix="cayadraft-", dir=str(root) if root else None))

    @staticmethod
    def _key(repo: str, number: int) -> str:
        return f"{repo}#{number}"


def _fallback_new_file(title: str, body: str, instruction: str) -> str:
    parts = [f"# {title.strip() or 'New content'}", ""]
    if (body or "").strip():
        parts.append(body.strip())
    if (instruction or "").strip():
        parts.extend(["", instruction.strip()])
    return "\n".join(parts).strip() + "\n"


def _missing_file_message(paths: list[str]) -> str:
    if not paths:
        return "Name the file in the command, for example `/caya fix path/to/file`."
    shown = ", ".join(f"`{path}`" for path in paths[:3])
    return f"I could not find {shown}."


def _access_message(exc: GitHubError) -> str:
    text = str(exc)
    lowered = text.lower()
    if "403" in text or "resource not accessible" in lowered or "not accessible by integration" in lowered:
        return (
            f"{text}\n\nThe GitHub App needs Contents: Read and write. "
            "Add it under the app permissions, then accept the update on the installation."
        )
    return text


def _candidate_paths(title: str, body: str, instruction: str) -> list[str]:
    text = "\n".join([title or "", body or "", instruction or ""])
    paths = extract_paths(text)
    if re.search(r"readme", text, re.IGNORECASE):
        for name in ("README.md", "README.txt", "README"):
            if name not in paths:
                paths.append(name)
    if _wants_mit(instruction, body) and "LICENSE" not in paths:
        paths.append("LICENSE")
    if not paths:
        added = _new_file_for(text)
        if added:
            paths.append(added)
    return paths[:8]


_PAYLOAD_STOP = {
    "add", "more", "content", "contents", "readme", "file", "files", "this", "that",
    "with", "from", "your", "have", "need", "want", "please", "should", "would",
    "could", "into", "onto", "main", "branch", "code", "example", "examples", "note",
    "notes", "spelling", "misspelled", "misspell", "typo", "typos", "word", "words",
    "change", "correct", "correction", "fix", "fixed", "update", "write", "create",
    "dosya", "dosyası", "dosyasına", "dosyaya", "ekle", "eklenmeli", "eklensin",
    "yazar", "için", "olan", "olarak", "veya", "gibi", "bunu", "issue", "private",
    "repository", "trying", "only", "there", "they", "them", "then", "than", "when",
    "what", "where", "which", "while", "about", "after", "before", "again", "also",
    "just", "like", "make", "made", "does", "dont", "isn't", "isnt", "will", "shall",
    "must", "each", "other", "some", "such", "very", "into", "over", "under",
}


def _payload_terms(title: str, body: str, instruction: str) -> list[str]:
    """Words the issue itself asks for. File text and command filler are not included."""
    del title
    if _date_line(instruction, body):
        return []
    text = _task_source(body, instruction)
    found: list[str] = []
    for token in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşüİı]{4,}", text):
        if token.casefold() in _PAYLOAD_STOP:
            continue
        if token not in found:
            found.append(token)
        if len(found) >= 12:
            break
    return found


def _missing_terms(terms: list[str], text: str) -> list[str]:
    haystack = text.casefold()
    return [term for term in terms if term.casefold() not in haystack]


def _missing_payload(title: str, body: str, instruction: str, text: str) -> list[str]:
    return _missing_terms(_payload_terms(title, body, instruction), text)


def _focus_paths(updated: list[str], created: list[str], title: str, body: str, instruction: str) -> list[str]:
    touched = _uniq(updated + created)
    text = "\n".join([title or "", body or "", instruction or ""])
    if re.search(r"readme", text, re.IGNORECASE):
        return [path for path in touched if Path(path).name.lower().startswith("readme")]
    return touched


def _new_file_for(text: str) -> str:
    """Pick a new file from this issue only. Never fall back to another issue's example."""
    lowered = text.lower()
    if re.search(r"\bpython\b|\.py\b", lowered):
        if re.search(r"\bsnake\b", lowered):
            return "snake.py"
        return "main.py"
    if re.search(r"readme", lowered):
        return ""
    if not re.search(r"\b(add|more|create|new|content|example|write)\b", lowered):
        return ""
    if re.search(r"\b(game|games|snake|mario)\b", lowered):
        return "games.md"
    return "content.md"


def _blob_for(path: str, blobs: dict[str, str]) -> str | None:
    if path in blobs:
        return path
    wanted = path.lower()
    for key in blobs:
        if key.lower() == wanted:
            return key
    return None


def _best_existing(blobs: dict[str, str], title: str, body: str, instruction: str) -> str:
    text = "\n".join([title or "", body or "", instruction or ""])
    if re.search(r"readme", text, re.IGNORECASE):
        for key in blobs:
            if Path(key).name.lower().startswith("readme"):
                return key
    return next(iter(blobs), "")


def _append_request(full: Path, title: str, body: str, instruction: str) -> None:
    raw = full.read_bytes() if full.is_file() else b""
    existing = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    addition = _request_text(title, body, instruction)
    if not addition:
        raise FixError("The request did not say what to add.")
    if addition in existing:
        return
    sep = "" if not existing or existing.endswith("\n") else "\n"
    _write_text(full, existing + sep + "\n" + addition + "\n")


def _write_text(path: Path, text: str) -> None:
    """Write repository text without changing an LF file into CRLF."""
    raw = path.read_bytes() if path.is_file() else b""
    style = "\r\n" if b"\r\n" in raw else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    payload = normalized.replace("\n", style).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


_FILLER = re.compile(
    r"^(add it|ekle|fix|fix it|düzelt|duzelt|please|lütfen|lutfen)$",
    re.IGNORECASE,
)


def _is_bot_text(text: str) -> bool:
    sample = text or ""
    return "<!-- cayadraft -->" in sample or "This pull request stays open" in sample or "CaYaDraft commands" in sample


def _task_source(body: str, instruction: str) -> str:
    """Issue text plus the owner's instruction. A pull request description is not issue text."""
    order = (instruction or "").strip()
    source = (body or "").strip()
    if _is_bot_text(source):
        source = ""
    if order and not _FILLER.match(order):
        return "\n".join(part for part in (source, order) if part)
    return source or order


def _date_line(instruction: str, body: str = "") -> str:
    task = (instruction or "").strip()
    if not task or _FILLER.match(task):
        task = "" if _is_bot_text(body or "") else (body or "")
    folded = task.casefold()
    if re.search(r"tarih|date", folded) and re.search(r"sona|sonuna|sonun|\bend\b", folded):
        return datetime.date.today().isoformat()
    return ""


def _request_text(title: str, body: str, instruction: str) -> str:
    """Text to add when the model edit cannot be used. Never paste the bot's own pull request."""
    dated = _date_line(instruction, body)
    if dated:
        return dated
    order = (instruction or "").strip()
    real_order = bool(order) and not _FILLER.match(order) and not _is_bot_text(order)
    if _is_bot_text(body or ""):
        if not real_order:
            return ""
        terms = _payload_terms(title, body, instruction)
        if terms and all(_name_token(term) for term in terms):
            return "Author: " + " ".join(terms)
        return _owner_addition(order) or order
    terms = _payload_terms(title, body, instruction)
    if terms and all(_name_token(term) for term in terms):
        return "Author: " + " ".join(terms)
    return (body or "").strip() or order or (title or "").strip()


def _wants_mit(instruction: str, body: str = "") -> bool:
    order = (instruction or "").strip()
    task = order if order and not _FILLER.match(order) and not _is_bot_text(order) else ""
    if not task:
        task = "" if _is_bot_text(body or "") else (body or "")
    return bool(re.search(r"\bmit\b", task, re.IGNORECASE) and re.search(r"licen[cs]e", task, re.IGNORECASE))


def _owner_addition(instruction: str) -> str:
    """Keep the lines the owner wants in the file. Drop the command around them."""
    kept: list[str] = []
    for line in (instruction or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "*")):
            kept.append(stripped)
            continue
        if _wants_mit(stripped) and len(stripped.split()) <= 6:
            continue
        if re.match(r"^(add|ekle|create|write|put)\b", stripped, re.IGNORECASE) and re.search(
            r"\b(footer|readme|file|licence|license|mit)\b", stripped, re.IGNORECASE
        ):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def _copyright_holder(instruction: str) -> str:
    for line in (instruction or "").splitlines():
        match = re.match(r"^[-*]\s+(.+)$", line.strip())
        if not match:
            continue
        holder = match.group(1).split(" - ")[0].strip()
        if holder:
            return holder
    for token in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşüİı]{3,}", instruction or ""):
        if _name_token(token):
            return token
    return ""


def _mit_license(year: int, holder: str) -> str:
    name = holder or "the copyright holder"
    return (
        "MIT License\n\n"
        f"Copyright (c) {year} {name}\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        'of this software and associated documentation files (the "Software"), to deal\n'
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in all\n"
        "copies or substantial portions of the Software.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
        "SOFTWARE.\n"
    )


def _attach_mit(
    dest: Path,
    blobs: dict[str, str],
    instruction: str,
    body: str,
    updated: list[str],
    created: list[str],
) -> tuple[list[str], list[str]]:
    if not _wants_mit(instruction, body):
        return updated, created
    path = dest / "LICENSE"
    current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if "Permission is hereby granted" not in current:
        _write_text(path, _mit_license(datetime.date.today().year, _copyright_holder(instruction)))
    if _blob_for("LICENSE", blobs):
        return _uniq(updated + ["LICENSE"]), created
    return updated, _uniq(created + ["LICENSE"])


_MARKER_LINE = re.compile(r"(?m)^---\s+\S.*\s+---\s*$")


def _name_token(token: str) -> bool:
    letters = [char for char in token if char.isalpha()]
    if len(letters) < 3:
        return False
    if token.isupper():
        return True
    if letters[0].isupper() and any(char in token for char in "ÇĞİÖŞÜçğıöşü"):
        return True
    return any(char.isupper() for char in letters[1:])


def _messy_addition(original: str, updated: str, terms: list[str]) -> bool:
    """A copied snippet label, or the same requested word added more than once."""
    if _MARKER_LINE.search(updated) and not _MARKER_LINE.search(original):
        return True
    for term in terms:
        before = original.casefold().count(term.casefold())
        after = updated.casefold().count(term.casefold())
        if after > before + 1:
            return True
    return False


def _tracked_issue(number: int, branch: str) -> int:
    match = re.fullmatch(r"cayadraft/issue-(\d+)", branch or "")
    if match:
        return int(match.group(1))
    return number


def _review_note(total: int, limit: int) -> str:
    if total <= limit:
        return ""
    return (
        f"This change is {total} lines, past the {limit}-line review mark. "
        "Please read it before you merge. I still opened the pull request."
    )


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _uniq(items: list[str]) -> list[str]:
    found: list[str] = []
    for item in items:
        if item not in found:
            found.append(item)
    return found


def _choose_files(tree: list[dict]) -> list[tuple[str, str]]:
    ranked: list[tuple[int, str, str]] = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        sha = str(item.get("sha") or "")
        if not path or not sha or is_secret_path(path):
            continue
        if any(part in SKIP_DIRS for part in path.split("/")):
            continue
        if int(item.get("size") or 0) > 100_000:
            continue
        if not is_text_candidate(Path(path)):
            continue
        score = 5 if Path(path).name.lower().startswith("readme") else 0
        ranked.append((score, path, sha))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(path, sha) for _score, path, sha in ranked[:40]]


def _snapshot(dest: Path, paths: list[str]) -> dict[str, bytes]:
    saved: dict[str, bytes] = {}
    for path in paths:
        full = dest / path
        if full.is_file():
            saved[path.replace("\\", "/")] = full.read_bytes()
    return saved


def _changed_lines(before: dict[str, bytes], dest: Path, files: list[str]) -> int:
    total = 0
    for path in files:
        old = before.get(path, b"").decode("utf-8", "replace").splitlines()
        new = (dest / path).read_bytes().decode("utf-8", "replace").splitlines()
        for line in difflib.unified_diff(old, new, n=0):
            if line.startswith(("+++", "---", "@@")):
                continue
            if line.startswith(("+", "-")):
                total += 1
    return total


class _TempRepo:
    def __init__(self, temporary: tempfile.TemporaryDirectory) -> None:
        self._temporary = temporary

    def __enter__(self) -> Path:
        return Path(self._temporary.__enter__()) / "repo"

    def __exit__(self, exc_type, exc, tb) -> None:
        self._temporary.__exit__(exc_type, exc, tb)
