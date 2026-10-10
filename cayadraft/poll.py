"""Watch one repository through the GitHub API. No public webhook and no relay."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("cayadraft.poll")


class PollState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bootstrapped = False
        self.issues: set[int] = set()
        self.comments: set[int] = set()
        self.reviews: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.bootstrapped = bool(data.get("bootstrapped"))
        self.issues = {int(item) for item in data.get("issues", [])}
        self.comments = {int(item) for item in data.get("comments", [])}
        self.reviews = {int(item) for item in data.get("reviews", [])}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bootstrapped": self.bootstrapped,
            "issues": sorted(self.issues),
            "comments": sorted(self.comments),
            "reviews": sorted(self.reviews),
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")


class Poller:
    def __init__(self, handler, github, repo: str, state: PollState, installation_id: int | None = None) -> None:
        self.handler = handler
        self.github = github
        self.repo = repo
        self.state = state
        self.installation_id = installation_id

    def once(self) -> int:
        repo_info = self.github.get_repo(self.repo, self.installation_id)
        default_branch = repo_info.get("default_branch") or "main"
        issues = [item for item in self.github.list_issues(self.repo, self.installation_id) if "pull_request" not in item]
        comments = self.github.list_recent_comments(self.repo, self.installation_id)
        reviews = self.github.list_review_comments(self.repo, self.installation_id)
        if not self.state.bootstrapped:
            self.state.issues.update(int(item["number"]) for item in issues)
            self.state.comments.update(int(item["id"]) for item in comments)
            self.state.reviews.update(int(item["id"]) for item in reviews)
            self.state.bootstrapped = True
            self.state.save()
            log.info("watching %s from now on", self.repo)
            return 0

        dispatched = 0
        known = {int(item["number"]): item for item in issues}
        for issue in sorted(issues, key=lambda item: item.get("created_at") or ""):
            number = int(issue["number"])
            if number in self.state.issues:
                continue
            self.handler.handle("issues", _issue_payload(self.repo, issue, default_branch))
            self.state.issues.add(number)
            dispatched += 1
        for comment in sorted(comments, key=lambda item: item.get("created_at") or ""):
            comment_id = int(comment["id"])
            if comment_id in self.state.comments:
                continue
            number = _issue_number(comment.get("issue_url") or "")
            issue = known.get(number) or self.github.get_issue(self.repo, number, self.installation_id)
            self.handler.handle("issue_comment", _comment_payload(self.repo, issue, comment, default_branch))
            self.state.comments.add(comment_id)
            dispatched += 1
        for review in sorted(reviews, key=lambda item: item.get("created_at") or ""):
            review_id = int(review["id"])
            if review_id in self.state.reviews:
                continue
            number = _issue_number(review.get("pull_request_url") or "")
            pull = self.github.get_pull(self.repo, number, self.installation_id)
            self.handler.handle("pull_request_review_comment", _review_payload(self.repo, pull, review, default_branch))
            self.state.reviews.add(review_id)
            dispatched += 1
        self.state.save()
        return dispatched


def _issue_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def _sender(user: dict | None) -> dict:
    user = user or {}
    return {"login": user.get("login") or "", "type": user.get("type") or "User"}


def _issue_payload(repo: str, issue: dict, default_branch: str) -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": repo, "default_branch": default_branch},
        "sender": _sender(issue.get("user")),
        "issue": issue,
    }


def _comment_payload(repo: str, issue: dict, comment: dict, default_branch: str) -> dict:
    return {
        "action": "created",
        "repository": {"full_name": repo, "default_branch": default_branch},
        "sender": _sender(comment.get("user")),
        "issue": issue,
        "comment": comment,
    }


def _review_payload(repo: str, pull: dict, comment: dict, default_branch: str) -> dict:
    return {
        "action": "created",
        "repository": {"full_name": repo, "default_branch": default_branch},
        "sender": _sender(comment.get("user")),
        "pull_request": pull,
        "comment": comment,
    }
