import tempfile
import unittest
from pathlib import Path

from cayadraft.poll import PollState, Poller


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.comments: list[dict] = []
        self.reviews: list[dict] = []

    def get_repo(self, repo, installation_id=None):
        return {"default_branch": "main"}

    def list_issues(self, repo, installation_id=None):
        return list(self.issues)

    def list_recent_comments(self, repo, installation_id=None):
        return list(self.comments)

    def list_review_comments(self, repo, installation_id=None):
        return list(self.reviews)

    def get_issue(self, repo, number, installation_id=None):
        return {"number": number, "title": "x", "body": "", "user": {"login": "CaYatur", "type": "User"}}

    def get_pull(self, repo, number, installation_id=None):
        return {"number": number, "title": "pr", "body": "", "head": {"ref": "branch"}, "base": {"ref": "main"}}


class FakeHandler:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def handle(self, event, payload) -> None:
        self.events.append((event, payload))


class PollTests(unittest.TestCase):
    def test_existing_activity_is_not_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            github = FakeGitHub()
            github.issues = [_issue(1), _pull(2)]
            github.comments = [_comment(9, 1)]
            handler = FakeHandler()
            poller = Poller(handler, github, "CaYatur/GithubBotTest-1", PollState(Path(tmp) / "state.json"))
            self.assertEqual(poller.once(), 0)
            self.assertEqual(handler.events, [])
            github.issues.append(_issue(3, title="Typo"))
            github.comments.append(_comment(10, 3, "/caya summarize"))
            self.assertEqual(poller.once(), 2)
            self.assertEqual([event for event, _ in handler.events], ["issues", "issue_comment"])
            self.assertEqual(handler.events[0][1]["issue"]["title"], "Typo")
            self.assertEqual(poller.once(), 0)


def _issue(number: int, title: str = "Old") -> dict:
    return {
        "number": number,
        "title": title,
        "body": "body",
        "created_at": f"2026-10-10T00:00:{number:02d}Z",
        "user": {"login": "CaYatur", "type": "User"},
    }


def _pull(number: int) -> dict:
    return {**_issue(number), "pull_request": {"url": "https://example.test"}}


def _comment(comment_id: int, number: int, body: str = "note") -> dict:
    return {
        "id": comment_id,
        "body": body,
        "created_at": f"2026-10-10T01:00:{comment_id:02d}Z",
        "issue_url": f"https://api.github.com/repos/CaYatur/GithubBotTest-1/issues/{number}",
        "user": {"login": "CaYatur", "type": "User"},
    }


if __name__ == "__main__":
    unittest.main()
