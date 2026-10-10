"""Environment configuration. Existing process variables win over the .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    owner_logins: tuple[str, ...]
    mention_login: str
    ollama_host: str
    ollama_model: str
    ollama_timeout: float
    ollama_num_ctx: int
    analysis_tokens: int
    edit_tokens: int
    webhook_secret: str
    webhook_path: str
    public_prefix: str
    public_url: str
    host: str
    port: int
    app_id: str
    private_key: str
    private_key_path: str
    github_token: str
    git_name: str
    git_email: str
    work_root: str
    allowlist: tuple[str, ...]
    max_files: int
    max_edits: int
    max_diff_lines: int
    api_base: str = "https://api.github.com"

    def owns(self, login: str | None) -> bool:
        if not login:
            return False
        allowed = {item.lower() for item in self.owner_logins}
        return login.lower() in allowed

    @property
    def app_mode(self) -> bool:
        return bool(self.app_id)

    def auth_problems(self) -> list[str]:
        problems = []
        if self.app_mode:
            if not (self.private_key or self.private_key_path):
                problems.append("Set GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH.")
            elif self.private_key_path and not self.private_key and not Path(self.private_key_path).is_file():
                problems.append(f"Private key file not found: {self.private_key_path}")
        elif not self.github_token:
            problems.append("Set GITHUB_APP_ID and a private key, or set GITHUB_TOKEN.")
        return problems

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or Path.cwd() / ".env")
        owners = _csv(os.environ.get("OWNER_LOGINS") or os.environ.get("OWNER_LOGIN") or "CaYatur")
        mention = os.environ.get("MENTION_LOGIN") or owners[0]
        return cls(
            owner_logins=owners,
            mention_login=mention,
            ollama_host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"),
            ollama_timeout=float(os.environ.get("OLLAMA_TIMEOUT", "180")),
            ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
            analysis_tokens=int(os.environ.get("OLLAMA_ANALYSIS_TOKENS", "700")),
            edit_tokens=int(os.environ.get("OLLAMA_EDIT_TOKENS", "1200")),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
            webhook_path=os.environ.get("WEBHOOK_PATH", "/webhook"),
            public_prefix=os.environ.get("PUBLIC_PREFIX", "/CaYaDraftBot").rstrip("/") or "",
            public_url=os.environ.get("PUBLIC_URL", "https://app.cayadev.com/CaYaDraftBot").rstrip("/"),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8787")),
            app_id=os.environ.get("GITHUB_APP_ID", "").strip(),
            private_key=os.environ.get("GITHUB_PRIVATE_KEY", ""),
            private_key_path=os.environ.get("GITHUB_PRIVATE_KEY_PATH", ""),
            github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
            git_name=os.environ.get("GIT_AUTHOR_NAME", "CaYaDraft"),
            git_email=os.environ.get("GIT_AUTHOR_EMAIL", "cayadraft@cayadev.com"),
            work_root=os.environ.get("WORK_ROOT", ""),
            allowlist=tuple(part.lower() for part in _csv(os.environ.get("REPO_ALLOWLIST", ""))),
            max_files=int(os.environ.get("MAX_EDIT_FILES", "5")),
            max_edits=int(os.environ.get("MAX_EDITS", "8")),
            max_diff_lines=int(os.environ.get("MAX_DIFF_LINES", "300")),
        )
