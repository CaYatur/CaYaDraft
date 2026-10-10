"""Parse and apply the model's find-and-replace edits. No shell, no new files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cayadraft.textutil import is_secret_path, strip_fences

_HEADER = re.compile(r"^(FILE|SUMMARY|FIND|REPLACE|CONTENT|CANNOT)\s*:\s*(.*)$")


class EditError(RuntimeError):
    pass


@dataclass(frozen=True)
class Edit:
    path: str
    find: str
    replace: str


@dataclass(frozen=True)
class Proposal:
    summary: str
    edits: list[Edit]
    cannot: str | None = None
    created: tuple[tuple[str, str], ...] = ()


def clean_rel(path: str) -> str:
    value = path.replace("\\", "/").strip().strip('"').strip("'")
    while value.startswith("./"):
        value = value[2:]
    return value


def validate_rel(path: str) -> None:
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise EditError(f"I will not use the path `{path}`.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise EditError(f"I will not use the path `{path}`.")


def parse_proposal(text: str) -> Proposal:
    raw = strip_fences(text or "")
    summary_match = re.search(r"(?m)^SUMMARY:\s*(.+)$", raw)
    summary = summary_match.group(1).strip() if summary_match else ""
    if not re.search(r"(?m)^FILE:", raw):
        if re.search(r"(?m)^CANNOT:", raw):
            return Proposal(summary, [], _cannot_reason(raw))
        return Proposal(summary, [], "The model did not return a file edit.")

    edits: list[Edit] = []
    created: list[tuple[str, str]] = []
    for block in _blocks(raw):
        edit, new_file = _parse_block(block)
        if edit:
            edits.append(edit)
        if new_file:
            created.append(new_file)
    if not edits and not created:
        return Proposal(summary, [], "The model did not return a usable edit.")
    return Proposal(summary, edits, None, tuple(created))


def apply_edits(root: Path, edits: list[Edit], *, max_files: int, max_edits: int) -> list[str]:
    if not edits:
        raise EditError("There is no edit to apply.")
    if len(edits) > max_edits:
        raise EditError(f"The edit has {len(edits)} replacements. The limit is {max_edits}.")

    order: list[str] = []
    buckets: dict[str, list[Edit]] = {}
    for edit in edits:
        path = clean_rel(edit.path)
        validate_rel(path)
        if is_secret_path(path):
            raise EditError(f"I will not edit `{path}`.")
        if path not in buckets:
            order.append(path)
            buckets[path] = []
        buckets[path].append(edit)
    if len(order) > max_files:
        raise EditError(f"The edit touches {len(order)} files. The limit is {max_files}.")

    projected: dict[str, tuple[Path, bytes]] = {}
    root_resolved = root.resolve()
    for path in order:
        full = (root / path).resolve()
        if not full.is_relative_to(root_resolved):
            raise EditError(f"I will not edit `{path}`.")
        if not full.is_file():
            raise EditError(f"`{path}` does not exist. I do not create files.")
        raw = full.read_bytes()
        if b"\0" in raw:
            raise EditError(f"`{path}` looks binary. I did not edit it.")
        style = "\r\n" if b"\r\n" in raw else "\n"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditError(f"`{path}` is not UTF-8. I did not edit it.") from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        updated = text
        for edit in buckets[path]:
            find = edit.find.replace("\r\n", "\n").strip("\n")
            replace = edit.replace.replace("\r\n", "\n").strip("\n")
            find, replace = _expand_short_find(updated, find, replace)
            if len(find) < 3:
                raise EditError(f"The text to find in `{path}` is too short.")
            if find == replace:
                raise EditError(f"The find and replace text for `{path}` are the same.")
            count = updated.count(find)
            if count != 1:
                raise EditError(
                    f"`{path}` contains {count} matches for the text to replace. I need exactly one."
                )
            updated = updated.replace(find, replace, 1)
        if updated == text:
            raise EditError(f"The edit does not change `{path}`.")
        projected[path] = (full, updated.replace("\n", style).encode("utf-8"))

    changed = []
    for path, (full, payload) in projected.items():
        full.write_bytes(payload)
        changed.append(path)
    return changed


def format_proposal(proposal: Proposal) -> str:
    if proposal.cannot:
        return proposal.cannot
    lines = ["**Proposed edit (not applied)**", "", f"**Summary** {proposal.summary or 'Small text change.'}", ""]
    for edit in proposal.edits:
        lines.append(f"`{clean_rel(edit.path)}`")
        lines.append("Find:")
        lines.append("```")
        lines.append(edit.find.strip("\n"))
        lines.append("```")
        lines.append("Replace with:")
        lines.append("```")
        lines.append(edit.replace.strip("\n"))
        lines.append("```")
        lines.append("")
    lines.append("Nothing was pushed. `/caya fix` opens the pull request from this preview when the branch has not moved.")
    return "\n".join(lines).strip()


def _expand_short_find(text: str, find: str, replace: str) -> tuple[str, str]:
    """Grow a unique fragment such as ee to the whole word receivee."""
    if len(find) >= 3 or not find or text.count(find) != 1:
        return find, replace
    start = text.index(find)
    left = start
    right = start + len(find)
    while left > 0 and text[left - 1].isalnum():
        left -= 1
    while right < len(text) and text[right].isalnum():
        right += 1
    word = text[left:right]
    if len(word) < 3 or word.count(find) != 1:
        return find, replace
    expanded = word.replace(find, replace, 1)
    if expanded == word or text.count(word) != 1:
        return find, replace
    return word, expanded


def _blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _parse_block(lines: list[str]) -> tuple[Edit | None, tuple[str, str] | None]:
    path = ""
    mode: str | None = None
    find_lines: list[str] = []
    replace_lines: list[str] = []
    content_lines: list[str] = []
    for line in lines:
        header = _HEADER.match(line.strip())
        if header and header.group(1) == "FILE":
            path = header.group(2).strip()
            mode = None
            continue
        if header and header.group(1) == "FIND":
            mode = "find"
            if header.group(2).strip():
                find_lines.append(header.group(2).strip())
            continue
        if header and header.group(1) == "REPLACE":
            mode = "replace"
            if header.group(2).strip():
                replace_lines.append(header.group(2).strip())
            continue
        if header and header.group(1) == "CONTENT":
            mode = "content"
            if header.group(2):
                content_lines.append(header.group(2))
            continue
        if line.strip().startswith("SUMMARY:") or line.strip().startswith("CANNOT:"):
            continue
        if mode == "find":
            find_lines.append(line)
        elif mode == "replace":
            replace_lines.append(line)
        elif mode == "content":
            content_lines.append(line)
    if not path:
        return None, None
    content = "\n".join(content_lines).strip("\n")
    if content and not find_lines:
        return None, (path, content)
    return Edit(path, "\n".join(find_lines), "\n".join(replace_lines)), None


def _cannot_reason(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^CANNOT:\s*(.*)$", line.strip())
        if not match:
            continue
        parts = [match.group(1)]
        for extra in lines[index + 1 :]:
            if extra.strip() == "---" or _HEADER.match(extra.strip()):
                break
            parts.append(extra)
        reason = "\n".join(parts).strip()
        return reason or "The model refused the edit."
    return "The model refused the edit."
