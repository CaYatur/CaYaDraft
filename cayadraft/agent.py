"""Free-form owner instructions. The model picks tools. Slash commands stay single-purpose."""

from __future__ import annotations

import json
import re

from cayadraft.fixer import FixError
from cayadraft.ollama_client import OllamaError
from cayadraft.textutil import clip, written_text

MAX_ROUNDS = 4

_ACTION_WORDS = re.compile(
    r"pull request|commit|fix|düzelt|duzelt|tarih|date|ekle|revise|değiştir|degistir|uygula|apply",
    re.IGNORECASE,
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_issue",
            "description": "Read an issue or pull request title and written body. No file changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {
                        "type": "integer",
                        "description": "Issue or pull request number. Omit it to read the current thread.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_fix",
            "description": (
                "Edit the repository for the current issue and open one pull request. "
                "Use this when the owner asks to fix the issue, apply a change, or open a pull request. "
                "One call does both. Does not merge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "What to change, keeping the owner's names and filenames.",
                    }
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise_pull",
            "description": (
                "Add one commit on the current pull request. "
                "Use this when the thread is already a pull request and the owner asks for a file change. "
                "Does not open a second pull request and does not merge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "What to change on this branch, in the owner's words.",
                    }
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_diff",
            "description": "Explain the current pull request diff in English. No file changes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_SYSTEM = """You are CaYaDraft. The owner wrote one free-form instruction. It may be Turkish.
Call tools to do the work. A slash command is not your job.

- A file change on an issue, or a request to open a pull request and apply a fix, is one open_fix call.
- A file change on a pull request is one revise_pull call. Do not call open_fix there.
- Read first only when the owner asks what the issue says. read_issue does not edit files.
- explain_diff only describes the current pull request.
- A question with no requested change calls no tool. Answer in English.
- You may call more than one tool when the owner asked for more than one action.
- Pass the owner's own words as the instruction. Do not paste a pull request template into the instruction.
- Never merge, force-push, delete the repository, or run tests. Those tools do not exist.
- After the tools finish, add one short English sentence. Do not invent a result the tool did not return.
"""


def run_instruction(*, ollama, github, fixer, ctx: dict, instruction: str, num_predict: int) -> str:
    """Let the model choose tools, then return one English comment."""
    owner = (instruction or "").strip()
    messages = [{"role": "user", "content": _user_prompt(ctx, owner)}]
    actions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    nudged = False
    final = ""
    for _round in range(MAX_ROUNDS):
        result = ollama.chat(
            system=_SYSTEM,
            messages=messages,
            tools=_TOOLS,
            temperature=0.1,
            num_predict=max(num_predict, 800),
        )
        calls = result["tool_calls"]
        final = (result["content"] or "").strip()
        if not calls:
            if not nudged and not actions and _ACTION_WORDS.search(final):
                nudged = True
                messages.append({"role": "assistant", "content": final})
                messages.append(
                    {
                        "role": "user",
                        "content": "Call a tool for that request. Do not only describe it.",
                    }
                )
                continue
            break
        messages.append(
            {
                "role": "assistant",
                "content": final,
                "tool_calls": [
                    {"function": {"name": call["name"], "arguments": call["arguments"]}} for call in calls
                ],
            }
        )
        for call in calls[:3]:
            key = (call["name"], json.dumps(call["arguments"], sort_keys=True, ensure_ascii=False))
            if key in seen:
                outcome = "Already done. I did not call it again."
            else:
                outcome = _call_tool(call, github, fixer, ctx, owner)
                if not outcome.startswith("Tool failed"):
                    seen.add(key)
            actions.append((call["name"], outcome))
            messages.append({"role": "tool", "tool_name": call["name"], "content": outcome})
    else:
        if not final:
            final = "I stopped after several tool calls."
    return _report(actions, final)


def _user_prompt(ctx: dict, instruction: str) -> str:
    on_pull = ctx.get("pr") is not None
    kind = "pull request" if on_pull else "issue"
    body = clip(written_text(ctx.get("body") or ""), 1500) or "(empty)"
    title = clip(ctx.get("title") or "", 200)
    if on_pull:
        rule = "This thread is a pull request. A file change is revise_pull. Do not call open_fix."
    else:
        rule = "This thread is an issue. A file change or a new pull request is open_fix. Do not call revise_pull or explain_diff."
    return (
        f"Thread: {kind} #{ctx.get('number')}\n"
        f"{rule}\n"
        f"Title: {title}\n"
        f"Written body: {body}\n"
        "The written body is context. Do not copy it into a file unless the owner asked for that text.\n"
        f"Owner instruction:\n{instruction or '(empty)'}"
    )


def _call_tool(call: dict, github, fixer, ctx: dict, owner: str) -> str:
    name = call["name"]
    arguments = call["arguments"] if isinstance(call.get("arguments"), dict) else {}
    try:
        if name == "read_issue":
            return _read_issue(github, ctx, arguments.get("number"))
        if name == "open_fix":
            return _open_fix(fixer, ctx, _tool_instruction(arguments.get("instruction"), owner))
        if name == "revise_pull":
            return _revise(fixer, ctx, _tool_instruction(arguments.get("instruction"), owner))
        if name == "explain_diff":
            return _explain(fixer, ctx)
    except (FixError, OllamaError) as exc:
        return f"Tool failed: {exc}"
    except Exception as exc:
        return f"Tool failed: {exc.__class__.__name__}"
    return "That tool does not exist. Choose read_issue, open_fix, revise_pull, or explain_diff."


def _tool_instruction(given, owner: str) -> str:
    text = str(given or "").strip()
    if not text or _bot_boilerplate(text):
        return owner
    if owner and owner.casefold() not in text.casefold():
        return f"{owner}\n{text}".strip()
    return text


def _bot_boilerplate(text: str) -> bool:
    return "<!-- cayadraft -->" in text or "This pull request stays open" in text or "CaYaDraft commands" in text


def _read_issue(github, ctx: dict, number) -> str:
    repo = ctx["repo"]
    current = int(ctx["number"])
    target = current
    if number not in (None, ""):
        try:
            target = int(number)
        except (TypeError, ValueError):
            return "Give an issue number."
    if target == current:
        title = ctx.get("title") or ""
        body = written_text(ctx.get("body") or "")
        kind = "pull request" if ctx.get("pr") else "issue"
        return f"{kind} #{target}\nTitle: {title}\nWritten body: {clip(body, 1500) or '(empty)'}"
    issue = github.get_issue(repo, target, ctx.get("installation"))
    title = issue.get("title") or ""
    body = written_text(issue.get("body") or "")
    kind = "pull request" if issue.get("pull_request") else "issue"
    return f"{kind} #{target}\nTitle: {title}\nWritten body: {clip(body, 1500) or '(empty)'}"


def _open_fix(fixer, ctx: dict, instruction: str) -> str:
    if ctx.get("pr") is not None:
        return "This thread is already a pull request. Call revise_pull to add a commit. I did not open another one."
    if not instruction.strip():
        return "Say what to change."
    result = fixer.fix(
        repo=ctx["repo"],
        number=ctx["number"],
        title=ctx["title"],
        body=ctx["body"],
        instruction=instruction,
        installation_id=ctx["installation"],
        base_branch=ctx["base_branch"],
    )
    if result.created:
        lead = f"Pull request opened: {result.url}"
    else:
        lead = f"I added a commit to the existing pull request: {result.url}"
    review = f" {result.review_note}" if result.review_note else ""
    return f"{lead} Summary: {result.summary or 'Small fix.'}{review} I did not merge."


def _revise(fixer, ctx: dict, instruction: str) -> str:
    pull = ctx.get("pr")
    if pull is None:
        return "There is no pull request on this thread. Call open_fix to open one."
    if not instruction.strip():
        return "Say what to change."
    result = fixer.revise(
        repo=ctx["repo"],
        number=ctx["number"],
        title=ctx["title"],
        body=ctx["body"],
        instruction=instruction,
        installation_id=ctx["installation"],
        base_branch=ctx["base_branch"],
        head_branch=pull["head"]["ref"],
        html_url=pull.get("html_url") or "",
    )
    return (
        f"I pushed another commit to `{result.branch}`. "
        f"Summary: {result.summary or 'Updated the branch.'} I did not merge."
    )


def _explain(fixer, ctx: dict) -> str:
    pull = ctx.get("pr")
    if pull is None:
        return "There is no pull request diff on this thread."
    return fixer.explain(
        repo=ctx["repo"],
        installation_id=ctx["installation"],
        base_branch=ctx["base_branch"],
        head_branch=pull["head"]["ref"],
    )


def _report(actions: list[tuple[str, str]], final: str) -> str:
    if not actions:
        return final or "I did not change any files. Name the change in the same comment, or use `/caya fix`."
    lines = ["I chose tools for this instruction."]
    failed = any(outcome.startswith("Tool failed") for _, outcome in actions)
    changed = any("commit" in outcome.lower() or "Pull request opened" in outcome for _, outcome in actions)
    for name, outcome in actions:
        lines.append(f"- `{name}`: {outcome}")
    if final and not failed and not _ACTION_WORDS.search(final):
        lines.append(final)
    if failed and not changed:
        lines.append("I did not change the files.")
    lines.append("I did not merge.")
    return "\n".join(lines)
