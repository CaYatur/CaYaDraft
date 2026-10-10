"""Small text helpers shared by the bot."""

from __future__ import annotations

import re

_SECRET_NAMES = {
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "service-account.json",
}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def scrub(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    cleaned = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "***", cleaned)
    cleaned = re.sub(r"github_pat_[A-Za-z0-9_]+", "***", cleaned)
    cleaned = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***", cleaned)
    return cleaned


def norm(text: str) -> str:
    """Lowercase and fold Turkish characters so command aliases match."""
    value = (text or "").strip().lower().replace("i\u0307", "i")
    return value.translate(str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a"}))


def fence(label: str, text: str) -> str:
    """Wrap untrusted text so it cannot close the delimiter early."""
    safe = (text or "").replace("</", "</ ")
    return f"<{label}>\n{safe}\n</{label}>"


def is_secret_path(path: str) -> bool:
    name = path.replace("\\", "/").split("/")[-1].lower()
    if name.startswith(".env") and not name.endswith(".example"):
        return True
    if name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES):
        return True
    return False


def strip_think(text: str) -> str:
    return re.sub(r"(?is)^<think>.*?</think>\s*", "", text or "").strip()


def strip_fences(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


_IMAGE = re.compile(
    r"!\[[^\]]*\]\([^)]+\)"
    r"|<img\b[^>]*>"
    r"|https://(?:github\.com/user-attachments/|private-user-images\.githubusercontent\.com/|user-images\.githubusercontent\.com/)\S+",
    re.IGNORECASE,
)


def picture_mode(*parts: str) -> str:
    """How to read an issue when images are attached.

    Returns "title" when the text is only an image, "text" when an image
    sits beside written content, and "" when there is no image.
    """
    text = "\n".join(part or "" for part in parts)
    if not _IMAGE.search(text):
        return ""
    prose = _IMAGE.sub(" ", text)
    prose = re.sub(r"<[^>]+>", " ", prose)
    letters = re.sub(r"[^0-9A-Za-zĞÜŞİÖÇğüşıöç]+", "", prose)
    if len(letters) < 12:
        return "title"
    return "text"


def mentions_bot(text: str, *names: str) -> bool:
    """True when a comment @mentions the bot. A plain sentence does not count."""
    for name in names:
        login = (name or "").strip().lower().removesuffix("[bot]")
        if not login:
            continue
        if re.search(rf"(?<![\w/])@{re.escape(login)}(?:\[bot\])?\b", text or "", re.IGNORECASE):
            return True
    return False


def asks_about_repo(title: str, body: str) -> bool:
    """True when the issue asks what the repository is, including a misspelling such as reposity."""
    text = f"{title or ''}\n{body or ''}".casefold()
    text = text.replace("reposity", "repository").replace("repositry", "repository").replace("repoistory", "repository")
    return bool(
        re.search(r"\b(what|whats|what's|nedir|ne)\b", text)
        and re.search(r"\b(repo|repository|depo|project|proje)\b", text)
    )


def written_text(text: str) -> str:
    """Issue text with images removed. The picture itself is not readable."""
    cleaned = _IMAGE.sub(" ", text or "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
