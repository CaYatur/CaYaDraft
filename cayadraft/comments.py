"""English comments posted back to GitHub."""

from __future__ import annotations

import re

from cayadraft.context import extract_paths
from cayadraft.textutil import asks_about_repo, clip, written_text

MARKER = "<!-- cayadraft -->"
OPENING_TAG = "CaYaDraft — first read"
UPDATE_TAG = "CaYaDraft — update"
PR_TAG = "CaYaDraft — pull request"

HELP = """**CaYaDraft commands**

Only the repository owner can run these. Write a new comment. I reply in English.

**On an issue**
- `/caya analyze` — read the issue again
- `/caya summarize`
- `/caya checklist`
- `/caya draft` — a suggestion only, no code change
- `/caya reply` — a reply you can copy
- `/caya labels` — suggest labels that already exist
- `/caya labels apply` — add those labels
- `/caya labels apply bug, docs` — add specific existing labels
- `/caya title` — suggest a title
- `/caya title apply` — rename the issue (or `/caya title apply New title`)
- `/caya translate en` or `/caya translate tr`
- `/caya ask <question>`
- `/caya plan` — show a small edit and wait
- `/caya fix` — open a pull request with that edit
- `/caya fix <instruction>` — open a pull request from your instruction
- `/caya wait` — stay idle
- `@cayadraft <instruction>` — I pick the tools. One comment can read the issue, open a pull request, and apply the fix.

**On the pull request**
- `/caya explain` — describe the current diff
- `/caya plan` — preview the next edit
- `/caya revise <instruction>` — add another commit
- `@cayadraft <instruction>` — I pick the tools, for example a new commit, or an explanation and a commit
- `/caya revert last` — undo the latest CaYaDraft commit
- `/caya revert` — make the branch match the base branch again

Turkish aliases work too, for example `/caya düzelt`, `/caya geri son`, and `/caya özet`.

I never merge. I never run the repository's scripts or tests. A fix is only a pull request for you to accept, revise, or revert.
"""


def marked(body: str) -> str:
    text = (body or "").strip()
    if MARKER in text:
        return text
    return f"{text}\n\n{MARKER}"


_TURKISH_WORDS = {
    "dosya", "dosyası", "dosyasını", "oluşturulmalı", "oluşturularak", "eklenmeli",
    "eklenmelidir", "yeni", "adları", "kullanıcı", "kullanıcıların", "ancak",
    "belirtilmiş", "yazılmış", "doğru", "olan", "için", "gerekir",
}


def ground_read(
    title: str,
    body: str,
    request: str,
    analysis: str,
    draft: str,
    picture: str = "",
    readme: str = "",
) -> tuple[str, str, str]:
    """Keep the first read in English and preserve names and filenames from the issue."""
    if picture in {"title", "text"}:
        return _accept_picture(title, body, picture, request, analysis, draft)
    if asks_about_repo(title, body):
        return _accept_repo_question(request, analysis, draft, readme)
    names = _exact_names(f"{title}\n{body}")
    title_paths = extract_paths(title)
    body_paths = extract_paths(body)
    if _is_turkish(request) or _is_turkish(analysis):
        request = _english_request(names, title_paths, body_paths)
        analysis = ""
        draft = "Add the names exactly as written in the issue."
    notes: list[str] = []
    if title_paths and body_paths and title_paths[0] != body_paths[0]:
        notes.append(f"The title names `{title_paths[0]}` and the body names `{body_paths[0]}`. They are not the same file.")
    written = "\n".join([request, analysis, draft])
    missing = [name for name in names if name not in written]
    if missing:
        notes.append("Keep these exactly: " + ", ".join(f"`{name}`" for name in missing))
    if notes:
        analysis = "\n".join([analysis.strip(), *[f"- {note}" for note in notes]]).strip()
    return request, analysis or "The issue text is the request.", draft


_ASK = re.compile(
    r"\b(provide|specify|specifying|reproduction|reproduce|unclear|missing|error message|more details|could you|please)\b",
    re.IGNORECASE,
)


def _accept_picture(
    title: str,
    body: str,
    picture: str,
    request: str,
    analysis: str,
    draft: str,
) -> tuple[str, str, str]:
    """Quote the title and the written body. The image itself is not a source."""
    del request, analysis, draft
    summary = _brief_written(title, written_text(body), picture)
    return summary, "An image is attached and was not read.", summary


def _brief_written(title: str, written: str, picture: str) -> str:
    heading = (title or "").strip()
    if picture == "title" or not written:
        return f"The title summarizes the report: {heading or 'the issue title'}."
    if heading:
        return f"The title is “{heading}”. The written body says “{written}”."
    return f"The written body says “{written}”."


_VAGUE = re.compile(
    r"\b(no specific|without context|not clear|unclear|clarify|which part|what aspect|additional metadata)\b",
    re.IGNORECASE,
)


def _accept_repo_question(request: str, analysis: str, draft: str, readme: str) -> tuple[str, str, str]:
    """A 'what is this repository' issue is already specific. Answer from the README."""
    request = _drop_vague(_drop_asks(request)) or "The reporter asks what this repository is."
    analysis = _drop_vague(_drop_asks(analysis))
    draft = _drop_vague(_drop_asks(draft))
    purpose = _repo_purpose(readme)
    if not draft:
        draft = purpose
    if not analysis:
        analysis = "The question is already specific. The answer comes from the README."
    return request, analysis, draft


def _repo_purpose(readme: str) -> str:
    kept: list[str] = []
    for line in (readme or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if not stripped:
            continue
        folded = stripped.casefold()
        if "spelling" in folded or "recieve" in folded or "receive this note" in folded:
            continue
        kept.append(stripped)
    if not kept:
        return "The repository README does not say what it is for."
    return clip(" ".join(kept), 500)


def _drop_vague(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    kept = [part.strip(" -") for part in parts if part.strip() and not _VAGUE.search(part)]
    return " ".join(kept).strip()


def _drop_asks(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    kept = [part.strip(" -") for part in parts if part.strip() and not _ASK.search(part)]
    return " ".join(kept).strip()


def _english_request(names: list[str], title_paths: list[str], body_paths: list[str]) -> str:
    who = ", ".join(names) if names else "the people named in the issue"
    if title_paths and body_paths and title_paths[0] != body_paths[0]:
        return (
            f"Create a new file and add these names exactly: {who}. "
            f"The title says `{title_paths[0]}` and the body says `{body_paths[0]}`."
        )
    path = (body_paths or title_paths or ["the named file"])[0]
    target = f"`{path}`" if path != "the named file" else path
    return f"Create {target} and add these names exactly: {who}."


def _is_turkish(text: str) -> bool:
    words = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşüİı]+", (text or "").casefold())
    return sum(1 for word in words if word in _TURKISH_WORDS) >= 2


def _exact_names(text: str) -> list[str]:
    found: list[str] = []
    for line in (text or "").splitlines():
        parts = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşüİı]{3,}", line)
        names = [part for part in parts if _looks_like_name(part)]
        if names and len(names) == len(parts):
            joined = " ".join(names)
            if joined not in found:
                found.append(joined)
            continue
        for part in names:
            if part not in found and not any(part in item for item in found):
                found.append(part)
    return found


def _looks_like_name(token: str) -> bool:
    letters = [char for char in token if char.isalpha()]
    if len(letters) < 3:
        return False
    if token.isupper():
        return True
    if letters[0].isupper() and any(char in token for char in "ÇĞİÖŞÜçğıöşü"):
        return True
    return any(char.isupper() for char in letters[1:])


def opening_comment(mention: str, request: str, analysis: str, draft: str, author: str = "") -> str:
    """Public first read. The summary is for everyone. The owner is addressed only at the end."""
    request = clip(request.strip() or "The issue did not include a clear request.", 1500)
    analysis = clip(analysis.strip() or "No analysis was returned.", 2500)
    draft = clip(draft.strip() or "No draft was returned.", 2500)
    opener = (author or "").strip()
    if opener and opener.lower() != (mention or "").lower():
        thanks = f"Thank you, @{opener}, for opening this issue."
    else:
        thanks = "Thank you for opening this issue."
    body = f"""{thanks}

**{OPENING_TAG}**

**Request**
{request}

**Analysis**
{analysis}

**Suggested draft**
{draft}

---
@{mention} Stopping here. I will not change code, labels, or the title until you ask.
"""
    return marked(body)


def follow_up_comment(mention: str, request: str, analysis: str, draft: str) -> str:
    request = clip(request.strip() or "The new comment did not include a clear addition.", 1500)
    analysis = clip(analysis.strip() or "No analysis was returned.", 2500)
    draft = clip(draft.strip() or "No draft was returned.", 2500)
    body = f"""@{mention}

**{UPDATE_TAG}**

**Request**
{request}

**Analysis**
{analysis}

**Suggested draft**
{draft}

---
Stopping here. I will not change code, labels, or the title until you ask.

Write `/caya help` for the command list. `/caya plan` previews a small fix. `/caya fix` opens a pull request and waits for your review.
"""
    return marked(body)


def pull_summary_comment(mention: str, summary: str, files: str, note: str) -> str:
    summary = clip(summary.strip() or "No summary was returned.", 2500)
    files = clip(files.strip() or "No file list was returned.", 2500)
    note = clip(note.strip() or "Nothing else was called out.", 1500)
    body = f"""@{mention}

**{PR_TAG}**

**Summary**
{summary}

**Files**
{files}

**Note**
{note}

---
Stopping here. I will not merge, push, or change this pull request until you ask.

`/caya explain` describes the diff. `/caya plan` previews the next edit. `/caya revise` adds a commit.
"""
    return marked(body)


def parse_fields(text: str, names: tuple[str, ...]) -> dict[str, str]:
    parts: dict[str, list[str]] = {}
    current: str | None = None
    wanted = set(names)
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Z_]+)\s*:\s*(.*)$", line.strip())
        if match and match.group(1) in wanted:
            current = match.group(1)
            parts[current] = [match.group(2)] if match.group(2) else []
        elif current:
            parts[current].append(line)
    return {name: "\n".join(parts.get(name, [])).strip() for name in names}


def parse_analysis(text: str) -> tuple[str, str, str]:
    parts: dict[str, list[str]] = {}
    current: str | None = None
    for line in (text or "").splitlines():
        match = re.match(r"^(REQUEST|ANALYSIS|DRAFT)\s*:\s*(.*)$", line.strip())
        if match:
            current = match.group(1)
            parts[current] = [match.group(2)] if match.group(2) else []
        elif current:
            parts[current].append(line)
    if not parts:
        return "", (text or "").strip(), ""
    request = "\n".join(parts.get("REQUEST", [])).strip()
    analysis = "\n".join(parts.get("ANALYSIS", [])).strip()
    draft = "\n".join(parts.get("DRAFT", [])).strip()
    return request, analysis, draft


def pull_request_body(number: int, summary: str, files: list[str], note: str = "") -> str:
    file_lines = "\n".join(f"- `{path}`" for path in files) or "- (no file list)"
    summary = clip(summary.strip() or "Small fix requested by the owner.", 1000)
    review = f"\n**Review**\n{note.strip()}\n" if note.strip() else ""
    return f"""## CaYaDraft

Simple fix for issue #{number}.

**Summary**
{summary}

**Files**
{file_lines}
{review}

This pull request stays open until you merge it. I do not merge.

- `/caya revise <instruction>` — another commit on this branch
- `/caya revert last` — undo the latest bot commit
- `/caya revert` — put the branch back in line with the base branch

Add `Closes #{number}` yourself if merging should close the issue.

{MARKER}
"""
