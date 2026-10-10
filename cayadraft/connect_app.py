"""One-time GitHub App registration. The browser confirms it; secrets stay in local files."""

from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

PORT = 8788


def connect_app(root: Path) -> int:
    state = secrets.token_urlsafe(24)
    manifest = {
        "name": "CaYaDraft",
        "url": "https://cayadev.com",
        "description": "Drafts a note on a new issue and opens a pull request only when the owner asks.",
        "public": False,
        "request_oauth_on_install": False,
        "redirect_url": f"http://127.0.0.1:{PORT}/callback",
        "callback_urls": [f"http://127.0.0.1:{PORT}/callback"],
        "hook_attributes": {"url": "https://app.cayadev.com/CaYaDraftBot/webhook", "active": True},
        "default_permissions": {"contents": "write", "issues": "write", "pull_requests": "write", "metadata": "read"},
        "default_events": ["issues", "issue_comment", "pull_request_review_comment"],
    }
    done: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._html(_page(manifest, state))
                return
            if path != "/callback":
                self._html("<p>Not found.</p>", 404)
                return
            query = parse_qs(urlparse(self.path).query)
            if (query.get("state") or [""])[0] != state:
                self._html("<p>State did not match. Close this tab and run connect-app again.</p>", 400)
                return
            code = (query.get("code") or [""])[0]
            if not code:
                self._html("<p>GitHub did not return a code.</p>", 400)
                return
            try:
                saved = exchange(root, code)
            except Exception as exc:
                self._html(f"<p>Could not save the app. {exc}</p>", 502)
                return
            done.update(saved)
            self._html(
                "<p>CaYaDraft is registered. You can close this tab.</p>"
                f"<p>Next, install it only on GithubBotTest-1: "
                f"<a href=\"{saved['install_url']}\">{saved['install_url']}</a></p>"
            )
            threading_shutdown(self.server)

        def log_message(self, fmt: str, *args) -> None:
            return

        def _html(self, body: str, status: int = 200) -> None:
            raw = f"<!DOCTYPE html><html><body>{body}</body></html>".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Open this page while you are logged in as CaYatur: {url}")
    print("Click Create, then install the app only on GithubBotTest-1.")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        httpd.server_close()
    if not done:
        return 1
    print(f"App id {done['id']} saved. Slug: {done['slug']}")
    print(f"Install it here if you have not yet: {done['install_url']}")
    print(f"Upload assets/avatar.png at https://github.com/settings/apps/{done['slug']}")
    return 0


def exchange(root: Path, code: str) -> dict[str, str]:
    response = httpx.post(
        f"https://api.github.com/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CaYaDraft",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub returned HTTP {response.status_code}.")
    data = response.json()
    pem = data.get("pem") or ""
    app_id = str(data.get("id") or "")
    slug = str(data.get("slug") or "cayadraft")
    if not pem or not app_id:
        raise RuntimeError("GitHub did not return the app id and key.")
    key_path = root / "cayadraft.pem"
    key_path.write_text(pem if pem.endswith("\n") else pem + "\n", encoding="utf-8")
    bot_name, bot_email = "CaYaDraft", "cayadraft@cayadev.com"
    upsert_env(
        root / ".env",
        {
            "GITHUB_APP_ID": app_id,
            "GITHUB_PRIVATE_KEY_PATH": "cayadraft.pem",
            "GITHUB_APP_SLUG": slug,
            "WEBHOOK_SECRET": str(data.get("webhook_secret") or ""),
            "GIT_AUTHOR_NAME": bot_name,
            "GIT_AUTHOR_EMAIL": bot_email,
        },
    )
    return {"id": app_id, "slug": slug, "install_url": f"https://github.com/apps/{slug}/installations/new"}


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    rewritten: list[str] = []
    for line in lines:
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            rewritten.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            rewritten.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def _page(manifest: dict, state: str) -> str:
    payload = json.dumps(json.dumps(manifest).replace("<", "\\u003c"))
    return (
        "<p>This creates a GitHub App named CaYaDraft on the account you are logged into.</p>"
        f'<form action="https://github.com/settings/apps/new?state={state}" method="post">'
        '<input type="hidden" name="manifest" id="manifest">'
        '<button type="submit">Create CaYaDraft</button></form>'
        f'<script>document.getElementById("manifest").value = {payload};</script>'
    )


def threading_shutdown(server: ThreadingHTTPServer) -> None:
    import threading

    threading.Thread(target=server.shutdown, daemon=True).start()
