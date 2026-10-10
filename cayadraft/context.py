"""Pick small file snippets so a local model can propose an exact edit."""

from __future__ import annotations

import os
import re
from pathlib import Path

from cayadraft.textutil import clip, is_secret_path

SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "bin",
    "obj",
    "target",
    "coverage",
    ".pytest_cache",
}
TEXT_EXT = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yml",
    ".yaml",
    ".css",
    ".html",
    ".htm",
    ".cs",
    ".toml",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".xml",
    ".svg",
    ".ini",
    ".cfg",
    ".rst",
    ".adoc",
    ".php",
    ".rb",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".vue",
    ".scss",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".example",
}
SPECIAL_NAMES = {"license", "copying", "notice", "dockerfile", "makefile", "readme"}
STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "will",
    "your",
    "about",
    "there",
    "their",
    "would",
    "could",
    "should",
    "fix",
    "fixed",
    "change",
    "update",
    "please",
    "issue",
    "readme",
    "cayadraft",
    "caya",
}
_PATH_RE = re.compile(
    r"(?i)(?:^|[\s`'\"(])((?:[\w.-]+/)*[\w.-]+\.(?:md|txt|py|js|ts|tsx|jsx|json|yml|yaml|css|html|cs|toml|rs|go|java|kt|xml|php|rb|vue|scss|sql|sh|ps1))"
)
_QUOTE_RE = re.compile(r"[\"'`]([^\"'`]{4,120})[\"'`]")


def gather_context(root: Path, title: str, body: str, instruction: str, limit_chars: int = 7000) -> str:
    haystack = "\n".join([title or "", body or "", instruction or ""])
    chunks: list[str] = []
    seen: set[str] = set()

    def add_file(rel: str, focus: str | None = None) -> None:
        rel = rel.replace("\\", "/").lstrip("./")
        if not rel or rel in seen or is_secret_path(rel):
            return
        text = read_text_file(root, rel)
        if text is None:
            return
        seen.add(rel)
        if focus and focus.lower() in text.lower():
            chunks.append(window_around(rel, text, focus))
        else:
            chunks.append(head_chunk(rel, text))

    for path in extract_paths(haystack):
        add_file(path)
    if re.search(r"readme", haystack, re.I):
        for path in find_named(root, "readme"):
            add_file(path)

    if len(chunks) < 4:
        phrases = extract_phrases(instruction) + extract_phrases(body) + keywords(title)
        for phrase in phrases:
            if len(chunks) >= 6:
                break
            for rel in search_phrase(root, phrase):
                add_file(rel, focus=phrase)
                if len(chunks) >= 6:
                    break
    return clip("\n\n".join(chunks), limit_chars)


def extract_paths(text: str) -> list[str]:
    found: list[str] = []
    for match in _PATH_RE.finditer(text or ""):
        path = match.group(1).replace("\\", "/").lstrip("./")
        if path not in found and not is_secret_path(path):
            found.append(path)
        if len(found) >= 8:
            break
    return found


def extract_phrases(text: str) -> list[str]:
    found: list[str] = []
    for match in _QUOTE_RE.finditer(text or ""):
        phrase = " ".join(match.group(1).split())
        if phrase.lower() in STOPWORDS or phrase in found:
            continue
        found.append(phrase)
        if len(found) >= 6:
            break
    return found


def keywords(title: str) -> list[str]:
    words = []
    for word in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9_]{5,}", title or ""):
        if word.lower() in STOPWORDS:
            continue
        if word not in words:
            words.append(word)
        if len(words) >= 3:
            break
    return words


def find_named(root: Path, prefix: str) -> list[str]:
    matches = []
    for path in walk_files(root):
        if path.name.lower().startswith(prefix):
            matches.append(path.relative_to(root).as_posix())
        if len(matches) >= 3:
            break
    return matches


def search_phrase(root: Path, phrase: str) -> list[str]:
    if len(phrase) < 4:
        return []
    needle = phrase.lower()
    hits = []
    for path in walk_files(root):
        if not is_text_candidate(path) or is_secret_path(path.name):
            continue
        text = read_text_file(root, path.relative_to(root).as_posix())
        if text and needle in text.lower():
            hits.append(path.relative_to(root).as_posix())
        if len(hits) >= 3:
            break
    return hits


def walk_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            count += 1
            if count > 2500:
                return
            yield Path(dirpath) / name


def is_text_candidate(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXT:
        return True
    return path.name.lower() in SPECIAL_NAMES or path.name.lower().startswith("readme")


def read_text_file(root: Path, rel: str) -> str | None:
    full = (root / rel).resolve()
    try:
        if not full.is_file() or not full.is_relative_to(root.resolve()):
            return None
        if full.stat().st_size > 100_000 or not is_text_candidate(full):
            return None
    except OSError:
        return None
    raw = full.read_bytes()
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return None


def head_chunk(rel: str, text: str, lines: int = 40) -> str:
    body = "\n".join(text.splitlines()[:lines])
    return f"<file path=\"{rel}\">\n{body}\n</file>"


def window_around(rel: str, text: str, focus: str) -> str:
    lines = text.splitlines()
    index = 0
    lowered = focus.lower()
    for i, line in enumerate(lines):
        if lowered in line.lower():
            index = i
            break
    start = max(0, index - 3)
    end = min(len(lines), index + 4)
    body = "\n".join(lines[start:end])
    return f"<file path=\"{rel}\">\n{body}\n</file>"
