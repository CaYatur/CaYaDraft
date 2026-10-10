"""GitHub REST client. Supports a GitHub App or a personal access token."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from cayadraft.config import Config
from cayadraft.textutil import scrub


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = config.api_base.rstrip("/")
        self._tokens: dict[int, tuple[str, float]] = {}
        self._key_cache: str | None = None

    def token_for(self, installation_id: int | None) -> str:
        if not self.config.app_mode:
            if not self.config.github_token:
                raise GitHubError("GITHUB_TOKEN is empty.")
            return self.config.github_token
        if not installation_id:
            raise GitHubError("The webhook did not include a GitHub App installation id.")
        cached = self._tokens.get(installation_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        data = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._app_jwt(),
        )
        token = str(data["token"])
        self._tokens[installation_id] = (token, time.time() + 8 * 60)
        return token

    def post_comment(self, repo: str, number: int, body: str, installation_id: int | None) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            token=self.token_for(installation_id),
            json_body={"body": body},
        )

    def installation_for_repo(self, repo: str) -> int:
        data = self._request("GET", f"/repos/{repo}/installation", token=self._app_jwt())
        if not isinstance(data, dict) or not data.get("id"):
            raise GitHubError(f"The GitHub App is not installed on {repo}.")
        return int(data["id"])

    def get_repo(self, repo: str, installation_id: int | None) -> dict:
        data = self._request("GET", f"/repos/{repo}", token=self.token_for(installation_id))
        if not isinstance(data, dict):
            raise GitHubError("GitHub did not return the repository.")
        return data

    def list_issues(self, repo: str, installation_id: int | None) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{repo}/issues",
            token=self.token_for(installation_id),
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": "30"},
        )
        return data if isinstance(data, list) else []

    def list_recent_comments(self, repo: str, installation_id: int | None) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{repo}/issues/comments",
            token=self.token_for(installation_id),
            params={"sort": "created", "direction": "desc", "per_page": "50"},
        )
        return data if isinstance(data, list) else []

    def list_review_comments(self, repo: str, installation_id: int | None) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{repo}/pulls/comments",
            token=self.token_for(installation_id),
            params={"sort": "created", "direction": "desc", "per_page": "30"},
        )
        return data if isinstance(data, list) else []

    def get_issue(self, repo: str, number: int, installation_id: int | None) -> dict:
        data = self._request(
            "GET",
            f"/repos/{repo}/issues/{number}",
            token=self.token_for(installation_id),
        )
        if not isinstance(data, dict):
            raise GitHubError("GitHub did not return the issue.")
        return data

    def list_comments(self, repo: str, number: int, installation_id: int | None) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{repo}/issues/{number}/comments",
            token=self.token_for(installation_id),
            params={"per_page": "30"},
        )
        return data if isinstance(data, list) else []

    def list_labels(self, repo: str, installation_id: int | None) -> list[str]:
        data = self._request(
            "GET",
            f"/repos/{repo}/labels",
            token=self.token_for(installation_id),
            params={"per_page": "100"},
        )
        if not isinstance(data, list):
            return []
        return [str(item.get("name")) for item in data if item.get("name")]

    def add_labels(self, repo: str, number: int, labels: list[str], installation_id: int | None) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            token=self.token_for(installation_id),
            json_body={"labels": labels},
        )

    def set_title(self, repo: str, number: int, title: str, installation_id: int | None) -> None:
        self._request(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            token=self.token_for(installation_id),
            json_body={"title": title},
        )

    def find_open_pull(self, repo: str, branch: str, installation_id: int | None) -> dict | None:
        owner = repo.split("/")[0]
        data = self._request(
            "GET",
            f"/repos/{repo}/pulls",
            token=self.token_for(installation_id),
            params={"head": f"{owner}:{branch}", "state": "open", "per_page": "5"},
        )
        if isinstance(data, list) and data:
            return data[0]
        return None

    def list_pull_files(self, repo: str, number: int, installation_id: int | None) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}/files",
            token=self.token_for(installation_id),
            params={"per_page": "20"},
        )
        return data if isinstance(data, list) else []

    def get_file(self, repo: str, path: str, ref: str, installation_id: int | None) -> dict:
        quoted = quote(path, safe="/")
        data = self._request(
            "GET",
            f"/repos/{repo}/contents/{quoted}",
            token=self.token_for(installation_id),
            params={"ref": ref},
        )
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise GitHubError(f"`{path}` is not a small text file.")
        try:
            raw = base64.b64decode(data.get("content") or "")
        except ValueError as exc:
            raise GitHubError(f"`{path}` could not be decoded.") from exc
        sha = str(data.get("sha") or "")
        if not sha:
            raise GitHubError(f"GitHub did not return a version for `{path}`.")
        return {"sha": sha, "bytes": raw}

    def create_file(
        self,
        repo: str,
        path: str,
        message: str,
        content: bytes,
        branch: str,
        installation_id: int | None,
    ) -> None:
        quoted = quote(path, safe="/")
        self._request(
            "PUT",
            f"/repos/{repo}/contents/{quoted}",
            token=self.token_for(installation_id),
            json_body={
                "message": message,
                "content": base64.b64encode(content).decode("ascii"),
                "branch": branch,
            },
        )

    def update_file(
        self,
        repo: str,
        path: str,
        message: str,
        content: bytes,
        blob_sha: str,
        branch: str,
        installation_id: int | None,
    ) -> None:
        quoted = quote(path, safe="/")
        self._request(
            "PUT",
            f"/repos/{repo}/contents/{quoted}",
            token=self.token_for(installation_id),
            json_body={
                "message": message,
                "content": base64.b64encode(content).decode("ascii"),
                "sha": blob_sha,
                "branch": branch,
            },
        )

    def get_branch_sha(self, repo: str, branch: str, installation_id: int | None) -> str:
        ref = quote(f"heads/{branch}", safe="/")
        data = self._request("GET", f"/repos/{repo}/git/ref/{ref}", token=self.token_for(installation_id))
        sha = ((data.get("object") or {}).get("sha")) if isinstance(data, dict) else ""
        if not sha:
            raise GitHubError(f"GitHub did not return a commit for {branch}.")
        return str(sha)

    def get_git_commit(self, repo: str, sha: str, installation_id: int | None) -> dict:
        data = self._request("GET", f"/repos/{repo}/git/commits/{sha}", token=self.token_for(installation_id))
        if not isinstance(data, dict) or not (data.get("tree") or {}).get("sha"):
            raise GitHubError("GitHub did not return the commit.")
        return data

    def get_tree(self, repo: str, sha: str, installation_id: int | None) -> tuple[list[dict], bool]:
        data = self._request(
            "GET",
            f"/repos/{repo}/git/trees/{sha}",
            token=self.token_for(installation_id),
            params={"recursive": "1"},
        )
        if not isinstance(data, dict):
            raise GitHubError("GitHub did not return the tree.")
        tree = data.get("tree") if isinstance(data.get("tree"), list) else []
        return tree, bool(data.get("truncated"))

    def get_blob(self, repo: str, sha: str, installation_id: int | None) -> bytes:
        data = self._request("GET", f"/repos/{repo}/git/blobs/{sha}", token=self.token_for(installation_id))
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise GitHubError("GitHub did not return the file contents.")
        try:
            return base64.b64decode(data.get("content") or "")
        except ValueError as exc:
            raise GitHubError("GitHub returned a file that could not be decoded.") from exc

    def create_blob(self, repo: str, content: bytes, installation_id: int | None) -> str:
        data = self._request(
            "POST",
            f"/repos/{repo}/git/blobs",
            token=self.token_for(installation_id),
            json_body={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        sha = data.get("sha") if isinstance(data, dict) else ""
        if not sha:
            raise GitHubError("GitHub did not return a blob.")
        return str(sha)

    def create_tree(self, repo: str, base_tree: str, updates: list[tuple[str, str]], installation_id: int | None) -> str:
        data = self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            token=self.token_for(installation_id),
            json_body={
                "base_tree": base_tree,
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "sha": sha}
                    for path, sha in updates
                ],
            },
        )
        sha = data.get("sha") if isinstance(data, dict) else ""
        if not sha:
            raise GitHubError("GitHub did not return a tree.")
        return str(sha)

    def create_git_commit(
        self,
        repo: str,
        message: str,
        tree: str,
        parents: list[str],
        installation_id: int | None,
    ) -> str:
        data = self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            token=self.token_for(installation_id),
            json_body={"message": message, "tree": tree, "parents": parents},
        )
        sha = data.get("sha") if isinstance(data, dict) else ""
        if not sha:
            raise GitHubError("GitHub did not return a commit.")
        return str(sha)

    def create_ref(self, repo: str, branch: str, sha: str, installation_id: int | None) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            token=self.token_for(installation_id),
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def update_ref(self, repo: str, branch: str, sha: str, installation_id: int | None) -> None:
        ref = quote(f"heads/{branch}", safe="/")
        self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/{ref}",
            token=self.token_for(installation_id),
            json_body={"sha": sha, "force": False},
        )

    def compare(self, repo: str, base: str, head: str, installation_id: int | None) -> dict:
        spec = quote(f"{base}...{head}", safe="/.")
        data = self._request("GET", f"/repos/{repo}/compare/{spec}", token=self.token_for(installation_id))
        if not isinstance(data, dict):
            raise GitHubError("GitHub did not return the comparison.")
        return data

    def get_pull(self, repo: str, number: int, installation_id: int | None) -> dict:
        data = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            token=self.token_for(installation_id),
        )
        if not isinstance(data, dict):
            raise GitHubError("GitHub did not return the pull request.")
        return data

    def create_pull(
        self,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
        installation_id: int | None,
    ) -> dict:
        try:
            return self._create_pull_once(repo, title, head, base, body, installation_id)
        except GitHubError as first:
            message = str(first).lower()
            if "invalid" not in message and "not found" not in message:
                raise
            time.sleep(2)
            return self._create_pull_once(repo, title, head, base, body, installation_id)

    def _create_pull_once(self, repo, title, head, base, body, installation_id) -> dict:
        data = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            token=self.token_for(installation_id),
            json_body={"title": title, "head": head, "base": base, "body": body},
        )
        if not isinstance(data, dict) or not data.get("html_url"):
            raise GitHubError("GitHub did not return a pull request URL.")
        return data

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.config.app_id}
        token = jwt.encode(payload, self._private_key(), algorithm="RS256")
        if isinstance(token, bytes):
            return token.decode("ascii")
        return token

    def _private_key(self) -> str:
        if self._key_cache:
            return self._key_cache
        if self.config.private_key:
            self._key_cache = self.config.private_key.replace("\\n", "\n")
            return self._key_cache
        path = Path(self.config.private_key_path)
        if not path.is_file():
            raise GitHubError(f"Private key file not found: {path}")
        self._key_cache = path.read_text(encoding="utf-8")
        return self._key_cache

    def _request(self, method: str, path: str, *, token: str, json_body=None, params=None):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "CaYaDraft",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = httpx.request(
                method,
                f"{self.api}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise GitHubError("The GitHub request failed before a response arrived.") from exc
        if response.status_code >= 400:
            message = _error_message(response)
            raise GitHubError(scrub(f"GitHub HTTP {response.status_code}: {message}", [token]))
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "unknown error")[:400]
    if isinstance(payload, dict):
        message = str(payload.get("message") or "unknown error")
        errors = payload.get("errors")
        if errors:
            message = f"{message} {errors}"
        return message[:500]
    return "unknown error"
