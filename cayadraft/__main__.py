"""Run CaYaDraft: serve webhooks, check setup, or preview one issue."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cayadraft import __version__
from cayadraft.comments import opening_comment, parse_analysis
from cayadraft.config import Config
from cayadraft.fixer import Fixer
from cayadraft.gitwork import GitWorkspace
from cayadraft.github_client import GitHubClient
from cayadraft.handler import Handler
from cayadraft.ollama_client import OllamaClient, OllamaError
from cayadraft.connect_app import connect_app
from cayadraft.poll import Poller, PollState
from cayadraft.prompts import ANALYSIS_SYSTEM, analysis_user
from cayadraft.server import WebhookApp, make_server
from cayadraft.store import DeliveryStore, PlanStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cayadraft", description="CaYaDraft local GitHub bot")
    parser.add_argument("--env", default="", help="Path to an env file. Defaults to .env in the working directory.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Listen for GitHub webhooks")
    sub.add_parser("doctor", help="Check git, Ollama, and GitHub settings")
    watch = sub.add_parser("poll", help="Watch one repository through the GitHub API")
    watch.add_argument("--repo", default="CaYatur/GithubBotTest-1")
    watch.add_argument("--interval", type=int, default=20)
    sub.add_parser("connect-app", help="Register a GitHub App so comments come from CaYaDraft")
    preview = sub.add_parser("preview", help="Print a first-read comment without posting it")
    preview.add_argument("--title", required=True)
    preview.add_argument("--body", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    env_file = Path(args.env) if args.env else None
    config = Config.load(env_file)

    if args.command == "doctor":
        return doctor(config)
    if args.command == "preview":
        return preview_issue(config, args.title, args.body)
    if args.command == "poll":
        return poll(config, args.repo, args.interval)
    if args.command == "connect-app":
        return connect_app(Path.cwd())
    return serve(config)


def serve(config: Config) -> int:
    problems = config.auth_problems()
    if not config.webhook_secret:
        problems.append("Set WEBHOOK_SECRET.")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    root = Path.cwd() / "data"
    github = GitHubClient(config)
    ollama = OllamaClient(config.ollama_host, config.ollama_model, config.ollama_timeout, config.ollama_num_ctx)
    workspace = GitWorkspace(config.git_name, config.git_email)
    fixer = Fixer(config, github, ollama, workspace, PlanStore(root / "plans.json"))
    handler = Handler(config, github, ollama, fixer)
    app = WebhookApp(
        handler,
        config.webhook_secret,
        config.webhook_path,
        DeliveryStore(root / "deliveries.json"),
        prefix=config.public_prefix,
    )
    httpd = make_server(config.host, config.port, app)
    host, port = httpd.server_address[:2]
    print(f"CaYaDraft {__version__} listening on http://{host}:{port}")
    print(f"Public webhook: {config.public_url}/webhook")
    print(f"Owner: {', '.join(config.owner_logins)}  Mention: @{config.mention_login}")
    print(f"Model: {config.ollama_model} at {config.ollama_host}")
    print("Auth:", "GitHub App" if config.app_mode else "personal access token")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        httpd.server_close()
    return 0


def poll(config: Config, repo: str, interval: int) -> int:
    config = dataclasses.replace(config, allowlist=(repo.lower(),))
    github = GitHubClient(config)
    installation_id = None
    if config.app_mode:
        try:
            installation_id = github.installation_for_repo(repo)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            print("Install CaYaDraft on this repository, then run poll again.", file=sys.stderr)
            return 1
        print("Auth: GitHub App (comments appear as the bot)")
    else:
        token = config.github_token or _gh_token()
        if not token:
            print("Set GITHUB_TOKEN, or run: python -m cayadraft connect-app", file=sys.stderr)
            return 1
        if not config.github_token:
            print("Using the existing gh login. Comments appear as that user.")
        else:
            print("Auth: personal token (comments appear as your account)")
        config = dataclasses.replace(config, github_token=token, allowlist=(repo.lower(),))
        github = GitHubClient(config)
    root = Path.cwd() / "data"
    ollama = OllamaClient(config.ollama_host, config.ollama_model, config.ollama_timeout, config.ollama_num_ctx)
    workspace = GitWorkspace(config.git_name, config.git_email)
    fixer = Fixer(config, github, ollama, workspace, PlanStore(root / "plans.json"))
    handler = Handler(config, github, ollama, fixer)
    poller = Poller(handler, github, repo, PollState(root / "poll-state.json"), installation_id)
    print(f"CaYaDraft {__version__} watching {repo} every {interval}s")
    print("No webhook and no third-party relay. Open a new issue after this line appears.")
    try:
        while True:
            try:
                count = poller.once()
            except Exception as exc:
                print(f"Poll failed: {exc}", file=sys.stderr)
            else:
                if count:
                    print(f"Handled {count} event(s).")
            time.sleep(max(5, interval))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


def _gh_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def doctor(config: Config) -> int:
    print(f"CaYaDraft {__version__}")
    problems = 0
    git = shutil.which("git")
    if git:
        print(f"git: {git}")
    else:
        print("git: not found on PATH")
        problems += 1
    ollama = OllamaClient(config.ollama_host, config.ollama_model, config.ollama_timeout, config.ollama_num_ctx)
    ok, message = ollama.status()
    print(f"ollama: {message}")
    if not ok:
        problems += 1
    auth = config.auth_problems()
    if auth:
        problems += len(auth)
        for item in auth:
            print(f"github: {item}")
    else:
        print("github:", "GitHub App" if config.app_mode else "personal access token")
    if config.webhook_secret:
        print("webhook: secret is set")
    else:
        print("webhook: WEBHOOK_SECRET is empty")
        problems += 1
    print("owner:", ", ".join(config.owner_logins))
    print("mention:", f"@{config.mention_login}")
    print("listen:", f"http://{config.host}:{config.port}")
    print("public webhook:", f"{config.public_url}/webhook")
    if config.allowlist:
        print("repos:", ", ".join(config.allowlist))
    return problems


def preview_issue(config: Config, title: str, body: str) -> int:
    ollama = OllamaClient(config.ollama_host, config.ollama_model, config.ollama_timeout, config.ollama_num_ctx)
    try:
        raw = ollama.complete(
            system=ANALYSIS_SYSTEM,
            user=analysis_user(title, body),
            temperature=0.2,
            num_predict=config.analysis_tokens,
        )
    except OllamaError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    request, analysis, draft = parse_analysis(raw)
    print(opening_comment(config.mention_login, request or title, analysis or raw, draft))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
