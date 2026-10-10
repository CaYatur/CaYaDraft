"""Parse owner commands from issue and pull request comments."""

from __future__ import annotations

from dataclasses import dataclass

from cayadraft.comments import MARKER
from cayadraft.textutil import norm

TRIGGERS = ("/cayadraft", "/caya", "@cayadraft")

PHRASE_MAP = {
    "revert last": "revert_last",
    "geri son": "revert_last",
    "labels apply": "labels_apply",
    "label apply": "labels_apply",
    "etiket uygula": "labels_apply",
    "title apply": "title_apply",
    "baslik uygula": "title_apply",
}

WORD_MAP = {
    "help": "help",
    "yardim": "help",
    "commands": "help",
    "analyze": "analyze",
    "analyse": "analyze",
    "analysis": "analyze",
    "analiz": "analyze",
    "summarize": "summarize",
    "summary": "summarize",
    "ozet": "summarize",
    "checklist": "checklist",
    "liste": "checklist",
    "labels": "labels",
    "label": "labels",
    "etiket": "labels",
    "ask": "ask",
    "sor": "ask",
    "title": "title",
    "baslik": "title",
    "translate": "translate",
    "cevir": "translate",
    "reply": "reply",
    "cevap": "reply",
    "draft": "draft",
    "taslak": "draft",
    "plan": "plan",
    "preview": "plan",
    "onizle": "plan",
    "fix": "fix",
    "approve": "fix",
    "duzelt": "fix",
    "revise": "revise",
    "update": "revise",
    "ek": "revise",
    "revert": "revert",
    "geri": "revert",
    "undo": "revert_last",
    "wait": "wait",
    "bekle": "wait",
    "dur": "wait",
    "explain": "explain",
    "acikla": "explain",
}


@dataclass(frozen=True)
class Command:
    name: str
    arg: str


def _split_trigger(line: str) -> str | None:
    raw = line.strip()
    lower = raw.lower()
    for trigger in TRIGGERS:
        if not lower.startswith(trigger):
            continue
        rest = raw[len(trigger) :]
        if rest and rest[0] not in " \t:-,":
            continue
        return rest.lstrip(" \t:-,")
    return None


def _parse_rest(rest: str) -> Command:
    if not rest.strip():
        return Command("help", "")
    tokens = rest.split()
    folded = [norm(token) for token in tokens]
    for size in (2, 1):
        if len(folded) < size:
            continue
        key = " ".join(folded[:size])
        name = PHRASE_MAP.get(key) if size == 2 else WORD_MAP.get(key)
        if size == 2 and key not in PHRASE_MAP:
            continue
        if name:
            return Command(name, " ".join(tokens[size:]).strip())
    return Command("unknown", rest.strip())


def parse_command(body: str | None) -> Command | None:
    """Return the first command, or None when the comment is not a command.

    Comments that contain the bot marker are ignored so the bot never answers itself.
    Lines after the command are part of the instruction.
    """
    if not body or MARKER in body:
        return None
    lines = body.splitlines() or [body]
    for index, line in enumerate(lines):
        rest = _split_trigger(line)
        if rest is None:
            continue
        command = _parse_rest(rest)
        extra = "\n".join(lines[index + 1 :]).strip()
        if extra:
            combined = f"{command.arg}\n{extra}".strip() if command.arg else extra
            command = Command(command.name, combined)
        if command.name == "unknown" and line.lower().lstrip().startswith("@cayadraft") and command.arg.strip():
            return Command("instruct", command.arg)
        return command
    return None
