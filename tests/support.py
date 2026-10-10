"""Shared fixtures for unit tests."""

from __future__ import annotations

from cayadraft.config import Config
from cayadraft.fixer import FixResult


def make_config(**overrides) -> Config:
    data = dict(
        owner_logins=("CaYatur",),
        mention_login="CaYatur",
        ollama_host="http://127.0.0.1:11434",
        ollama_model="phi4-mini",
        ollama_timeout=5,
        ollama_num_ctx=2048,
        analysis_tokens=200,
        edit_tokens=400,
        webhook_secret="test-secret",
        webhook_path="/webhook",
        public_prefix="/CaYaDraftBot",
        public_url="https://app.cayadev.com/CaYaDraftBot",
        host="127.0.0.1",
        port=8787,
        app_id="",
        private_key="",
        private_key_path="",
        github_token="test-token",
        git_name="CaYaDraft",
        git_email="cayadraft@example.com",
        work_root="",
        allowlist=(),
        max_files=5,
        max_edits=8,
        max_diff_lines=300,
    )
    data.update(overrides)
    return Config(**data)


class FakeOllama:
    def __init__(self, response: str = "", tool_rounds: list | None = None) -> None:
        self.response = response
        self.calls: list[str] = []
        self.tool_rounds = list(tool_rounds or [])
        self.chat_calls: list[dict] = []

    def complete(self, *, system: str, user: str, temperature: float, num_predict: int) -> str:
        self.calls.append(user)
        return self.response

    def chat(self, *, system: str, messages: list, tools: list, temperature: float, num_predict: int) -> dict:
        self.chat_calls.append(
            {
                "system": system,
                "messages": [dict(item) for item in messages],
                "tools": [item["function"]["name"] for item in tools],
            }
        )
        if self.tool_rounds:
            return self.tool_rounds.pop(0)
        return {"content": self.response, "tool_calls": []}


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.labels = ["bug", "docs"]
        self.added: list[list[str]] = []
        self.titles: list[str] = []
        self.pulls: list[dict] = []
        self.files: list[dict] = []
        self.readme = b""

    def post_comment(self, repo, number, body, installation_id=None) -> None:
        self.comments.append((repo, number, body))

    def list_comments(self, repo, number, installation_id=None):
        return [{"body": body} for name, issue, body in self.comments if name == repo and issue == number]

    def list_labels(self, repo, installation_id=None):
        return list(self.labels)

    def add_labels(self, repo, number, labels, installation_id=None) -> None:
        self.added.append(labels)

    def set_title(self, repo, number, title, installation_id=None) -> None:
        self.titles.append(title)

    def token_for(self, installation_id=None) -> str:
        return "test-token"

    def find_open_pull(self, repo, branch, installation_id=None):
        return None

    def list_pull_files(self, repo, number, installation_id=None):
        return list(self.files)

    def get_file(self, repo, path, ref, installation_id=None):
        from cayadraft.github_client import GitHubError

        if path == "README.md" and self.readme:
            return {"sha": "readme", "bytes": self.readme}
        raise GitHubError("missing")

    def get_issue(self, repo, number, installation_id=None):
        return {"number": number, "title": f"Issue {number}", "body": "Written details."}

    def get_pull(self, repo, number, installation_id=None):
        return {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "head": {"ref": f"cayadraft/issue-{number}"},
            "base": {"ref": "main"},
        }

    def create_pull(self, repo, *, title, head, base, body, installation_id=None):
        self.pulls.append({"repo": repo, "title": title, "head": head, "base": base, "body": body})
        return {"html_url": f"https://github.com/{repo}/pull/9", "number": 9}


class FakeFixer:
    def __init__(self) -> None:
        self.fixes: list[dict] = []
        self.plans: list[dict] = []
        self.revises: list[dict] = []
        self.reverts: list[dict] = []
        self.explains: list[dict] = []

    def fix(self, **kwargs) -> FixResult:
        self.fixes.append(kwargs)
        return FixResult("https://github.com/acme/app/pull/9", "cayadraft/issue-3", "Fixed the typo.", 9, ["README.md"])

    def plan(self, **kwargs) -> str:
        self.plans.append(kwargs)
        return "**Proposed edit (not applied)**\n\nREADME.md"

    def revise(self, **kwargs) -> FixResult:
        self.revises.append(kwargs)
        return FixResult(kwargs.get("html_url") or "", kwargs["head_branch"], "Updated the label.", 3, ["README.md"], False)

    def explain(self, **kwargs) -> str:
        self.explains.append(kwargs)
        return "The diff changes one word in the README."

    def revert(self, **kwargs) -> str:
        self.reverts.append(kwargs)
        return "I added a commit that reverts the latest bot commit."
