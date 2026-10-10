import os
import shutil
import subprocess
import datetime
import tempfile
import unittest
from pathlib import Path

from cayadraft.gitwork import GitWorkspace
from cayadraft.github_client import GitHubError
from cayadraft.store import PlanStore
from tests.support import make_config

GIT = shutil.which("git")

PROPOSAL = """SUMMARY:
Fix the spelling of receive.
FILE: README.md
FIND:
recieve
REPLACE:
receive
---
"""


def git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "safe.directory=*",
            *args,
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


class TokenTests(unittest.TestCase):
    @unittest.skipUnless(GIT, "git is required")
    def test_token_is_not_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-b", "main")
            workspace = GitWorkspace("CaYaDraft", "cayadraft@example.com")
            workspace.run(repo, ["status"], token="super-secret-token")
            config = (repo / ".git" / "config").read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token", config)


class FlowTests(unittest.TestCase):
    @unittest.skipUnless(GIT, "git is required")
    def test_plan_fix_and_revert(self) -> None:
        from cayadraft.fixer import Fixer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "origin.git"
            seed = root / "seed"
            git(root, "init", "--bare", "-b", "main", str(bare))
            git(root, "init", "-b", "main", str(seed))
            (seed / "README.md").write_text("Please recieve this\n", encoding="utf-8")
            git(seed, "add", "README.md")
            git(seed, "commit", "-m", "init")
            git(seed, "remote", "add", "origin", bare.as_uri())
            git(seed, "push", "origin", "main")

            ollama = _ScriptedOllama(PROPOSAL)
            github = _LocalGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                ollama,
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
                url_for=lambda repo: bare.as_uri(),
            )
            preview = fixer.plan(
                repo="acme/app",
                number=4,
                title="Typo in the readme",
                body="Please recieve is misspelled.",
                instruction="",
                installation_id=None,
                base_branch="main",
            )
            self.assertIn("not applied", preview)
            self.assertIn("recieve", (seed / "README.md").read_text(encoding="utf-8"))
            result = fixer.fix(
                repo="acme/app",
                number=4,
                title="Typo in the readme",
                body="Please recieve is misspelled.",
                instruction="",
                installation_id=None,
                base_branch="main",
            )
            self.assertEqual(ollama.calls, 1)
            self.assertEqual(result.branch, "cayadraft/issue-4")
            self.assertTrue(result.created)
            self.assertEqual(github.pulls[0]["base"], "main")
            checked = root / "checked"
            git(root, "clone", "--branch", "cayadraft/issue-4", bare.as_uri(), str(checked))
            self.assertEqual((checked / "README.md").read_text(encoding="utf-8"), "Please receive this\n")

            message = fixer.revert(
                repo="acme/app",
                number=4,
                installation_id=None,
                base_branch="main",
                head_branch="cayadraft/issue-4",
                last_only=True,
            )
            self.assertIn("revert", message.lower())
            reverted = root / "reverted"
            git(root, "clone", "--branch", "cayadraft/issue-4", bare.as_uri(), str(reverted))
            self.assertIn("recieve", (reverted / "README.md").read_text(encoding="utf-8"))


class CreateFileTests(unittest.TestCase):
    def test_fix_adds_a_missing_file_without_touching_readme(self) -> None:
        from cayadraft.fixer import Fixer

        page = "SUMMARY:\nAdd the welcome page.\nFILE: welcome.html\nCONTENT:\n<h1>Welcome</h1>\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.files = {}
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=9,
                title="add welcome.html",
                body="need welcome webpage in main",
                instruction="",
                installation_id=5,
                base_branch="main",
            )
            self.assertEqual(result.files, ["welcome.html"])
            self.assertEqual(github.created[0]["path"], "welcome.html")
            self.assertIn("Welcome", github.created[0]["content"].decode("utf-8"))
            self.assertNotIn("README.md", result.files)


class ExistingBranchTests(unittest.TestCase):
    def test_fix_opens_a_pull_from_the_existing_branch(self) -> None:
        from cayadraft.fixer import Fixer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.refs["cayadraft/issue-7"] = "already"
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(PROPOSAL),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=7,
                title="fix readme",
                body="change receivee to receive",
                instruction="",
                installation_id=5,
                base_branch="main",
            )
            self.assertTrue(result.created)
            self.assertEqual(result.branch, "cayadraft/issue-7")
            self.assertEqual(github.pulls[0]["head"], "cayadraft/issue-7")
            self.assertEqual(github.updates, [])
            self.assertEqual(github.refs["cayadraft/issue-7"], "already")


class TopicTests(unittest.TestCase):
    def test_more_content_adds_games_not_the_readme_example(self) -> None:
        from cayadraft.fixer import Fixer

        page = "SUMMARY:\nAdd game examples.\nFILE: games.md\nCONTENT:\n- snake game\n- mario\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.files = {}
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=8,
                title="more content",
                body="more code content\nexample:\n- snake game\n- mario\n- games etc.",
                instruction="add the contents",
                installation_id=5,
                base_branch="main",
            )
            self.assertEqual(result.files, ["games.md"])
            self.assertEqual(github.created[0]["path"], "games.md")
            self.assertNotIn("recieve", github.created[0]["content"].decode("utf-8"))

    def test_refused_creation_still_writes_the_new_file(self) -> None:
        from cayadraft.fixer import Fixer

        refusal = "CANNOT:\nI cannot create a new file as requested.\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.files = {}
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(refusal),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=8,
                title="more content",
                body="more code content\nexample:\n- snake game\n- mario\n- games etc.",
                instruction="add the contents",
                installation_id=5,
                base_branch="main",
            )
            text = github.created[0]["content"].decode("utf-8")
            self.assertEqual(result.files, ["games.md"])
            self.assertIn("snake game", text)
            self.assertIn("mario", text)

    def test_readme_case_still_updates_the_real_file(self) -> None:
        from cayadraft.fixer import Fixer

        page = (
            "SUMMARY:\nAdd the author.\n"
            "FILE: readme.md\n"
            "FIND:\nPlease recieve this\n"
            "REPLACE:\nPlease recieve this\n\nAuthor: Çağan TURGUT\n"
            "---\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=15,
                title="Readme'ye ad eklemesi",
                body="readme.md dosyasına yazar Çağan TURGUT eklenmeli.",
                instruction="add it",
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            self.assertIn("Çağan TURGUT", text)
            self.assertEqual(result.files, ["README.md"])
            self.assertEqual(github.updates[0]["path"], "README.md")
            self.assertEqual(github.created, [])

    def test_failed_edit_still_adds_the_requested_name(self) -> None:
        from cayadraft.fixer import Fixer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama("CANNOT:\nThe request is unclear.\n"),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.fix(
                repo="acme/app",
                number=15,
                title="Readme'ye ad eklemesi",
                body="readme.md dosyasına yazar Çağan TURGUT eklenmeli.",
                instruction="add it",
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            self.assertIn("Çağan TURGUT", text)
            self.assertEqual(len(github.pulls), 1)

    def test_long_python_file_is_added_with_a_review_note(self) -> None:
        from cayadraft.fixer import Fixer

        body_lines = "\n".join(f"line {index}" for index in range(301))
        page = f"SUMMARY:\nAdd snake.\nFILE: games.md\nCONTENT:\n{body_lines}\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.files = {}
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=14,
                title="python snake code not in the main",
                body="python snake code not in the main branch.\nAdd python snake code.",
                instruction="write the python snake code",
                installation_id=5,
                base_branch="main",
            )
            self.assertEqual(result.files, ["snake.py"])
            self.assertEqual(github.created[0]["path"], "snake.py")
            self.assertGreater(github.created[0]["content"].decode("utf-8").count("\n"), 300)
            self.assertIn("review mark", result.review_note)
            self.assertIn("review mark", github.pulls[0]["body"])

    def test_author_request_is_not_replaced_by_the_file_note(self) -> None:
        from cayadraft.fixer import Fixer

        page = "SUMMARY:\nFix the spelling.\nFILE: README.md\nFIND:\nrecieve\nREPLACE:\nreceive\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=15,
                title="Readme'ye ad eklemesi",
                body="readme.md dosyasına yazar Çağan TURGUT eklenmeli.",
                instruction="add it",
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            self.assertIn("Çağan TURGUT", text)
            self.assertIn("recieve", text)
            self.assertIn("requested in this issue", result.summary)

    def test_content_request_is_not_replaced_by_a_spelling_edit(self) -> None:
        from cayadraft.fixer import Fixer

        page = "SUMMARY:\nUse a clearer word.\nFILE: README.md\nFIND:\nrecieve\nREPLACE:\nget\n---\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.fix(
                repo="acme/app",
                number=8,
                title="more content",
                body="more code content\nexample:\n- snake game\n- mario\n- games etc.",
                instruction="add more content in readme",
                installation_id=5,
                base_branch="main",
            )
            raw = github.files["README.md"]["bytes"]
            text = raw.decode("utf-8")
            self.assertNotIn(b"\r", raw)
            self.assertIn("snake", text)
            self.assertIn("mario", text)
            self.assertIn("games", text)
            self.assertIn("recieve", text)
            self.assertNotIn("Please get this", text)
            self.assertNotIn("add more content in readme", text)
            self.assertIn("requested in this issue", github.updates[0]["message"])

    def test_copied_snippet_labels_are_not_committed(self) -> None:
        from cayadraft.fixer import Fixer

        page = (
            "SUMMARY:\nAdd the author to the title.\n"
            "FILE: README.md\n"
            "FIND:\n# GithubBotTest-1\n"
            "REPLACE:\n"
            "# GithubBotTest-1 by Çağan TURGUT\n"
            "--- readme.md ---\n"
            "# GithubBotTest-1 by Çağan TURGUT\n"
            "--- readme.md ---\n"
            "# GithubBotTest-1\n"
            "---\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.files = {"README.md": {"sha": "blob0", "bytes": b"# GithubBotTest-1\n\nPlease recieve this note.\n"}}
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.fix(
                repo="acme/app",
                number=21,
                title="readme.md ye ad eklemesi",
                body="readme.md dosyasına yazar Çağan TURGUT eklenmeli.",
                instruction="",
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            self.assertEqual(text.count("Çağan TURGUT"), 1)
            self.assertIn("Author: Çağan TURGUT", text)
            self.assertNotIn("--- readme.md ---", text)
            self.assertEqual(text.count("# GithubBotTest-1\n"), 1)

    def test_pull_request_note_is_not_pasted_when_a_date_was_asked(self) -> None:
        from cayadraft.fixer import Fixer

        page = (
            "SUMMARY:\nAdd the text requested in this issue.\n"
            "FILE: README.md\n"
            "FIND:\nPlease recieve this\n"
            "REPLACE:\nPlease recieve this\n\nThis pull request stays open until you merge it.\n"
            "<!-- cayadraft -->\n"
            "---\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.fix(
                repo="acme/app",
                number=28,
                title="Fix #27: Add the text requested in this issue.",
                body="## CaYaDraft\n\n**Files**\n- `README.md`\n\nThis pull request stays open until you merge it.\n\n<!-- cayadraft -->\n",
                instruction="birde sona tarih eklemen gerek.",
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            self.assertIn(datetime.date.today().isoformat(), text)
            self.assertNotIn("stays open", text)
            self.assertNotIn("cayadraft -->", text)
            self.assertIn("recieve", text)

    def test_pull_request_footer_and_mit_license_are_added(self) -> None:
        from cayadraft.fixer import Fixer

        page = "SUMMARY:\nFix the spelling.\nFILE: README.md\nFIND:\nrecieve\nREPLACE:\nreceive\n---\n"
        instruction = "add footer in readme.md\n- CaYaDev - cayadev.com\nadd mit licence"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(page),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.fix(
                repo="acme/app",
                number=28,
                title="Fix #27: Add the text requested in this issue.",
                body="## CaYaDraft\n\nThis pull request stays open until you merge it.\n\n<!-- cayadraft -->\n",
                instruction=instruction,
                installation_id=5,
                base_branch="main",
            )
            text = github.files["README.md"]["bytes"].decode("utf-8")
            license_text = github.created[0]["content"].decode("utf-8")
            self.assertIn("CaYaDev - cayadev.com", text)
            self.assertIn("recieve", text)
            self.assertNotIn("stays open", text)
            self.assertNotIn("add mit licence", text)
            self.assertEqual(github.created[0]["path"], "LICENSE")
            self.assertIn("MIT License", license_text)
            self.assertIn("Copyright (c) 2026 CaYaDev", license_text)
            self.assertIn("Permission is hereby granted", license_text)


class ApiFixTests(unittest.TestCase):
    def test_fix_opens_a_pull_without_cloning(self) -> None:
        from cayadraft.fixer import Fixer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ollama = _ScriptedOllama(PROPOSAL)
            github = _MemoryGitHub()
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                ollama,
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            result = fixer.fix(
                repo="acme/app",
                number=7,
                title="Typo in the readme",
                body="Please recieve is misspelled in README.md.",
                instruction="",
                installation_id=5,
                base_branch="main",
            )
            self.assertEqual(result.branch, "cayadraft/issue-7")
            self.assertTrue(result.created)
            self.assertEqual(github.files["README.md"]["bytes"], b"Please receive this\n")
            self.assertEqual(github.refs["cayadraft/issue-7"], "base")
            self.assertEqual(len(github.updates), 1)
            self.assertEqual(github.pulls[0]["base"], "main")
            self.assertEqual(github.pulls[0]["head"], "cayadraft/issue-7")

    def test_revise_on_a_pull_keeps_the_branch_issue_number(self) -> None:
        from cayadraft.fixer import Fixer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            github = _MemoryGitHub()
            github.refs["cayadraft/issue-8"] = "base"
            config = make_config(work_root=str(root / "work"))
            fixer = Fixer(
                config,
                github,
                _ScriptedOllama(PROPOSAL),
                GitWorkspace(config.git_name, config.git_email),
                PlanStore(root / "plans.json"),
            )
            fixer.revise(
                repo="acme/app",
                number=18,
                title="more content",
                body="Please recieve is misspelled in README.md.",
                instruction="fix the spelling",
                installation_id=5,
                base_branch="main",
                head_branch="cayadraft/issue-8",
                html_url="https://github.com/acme/app/pull/18",
            )
            self.assertIn("CaYaDraft-Issue: 8", github.updates[0]["message"])
            self.assertNotIn("CaYaDraft-Issue: 18", github.updates[0]["message"])


class _ScriptedOllama:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, **kwargs) -> str:
        self.calls += 1
        return self.response


class _LocalGitHub:
    def __init__(self) -> None:
        self.pulls: list[dict] = []

    def token_for(self, installation_id=None) -> str:
        return "local-test-token"

    def get_branch_sha(self, repo, branch, installation_id=None) -> str:
        raise GitHubError("GitHub HTTP 404: Branch not found")

    def find_open_pull(self, repo, branch, installation_id=None):
        return None

    def create_pull(self, repo, *, title, head, base, body, installation_id=None):
        self.pulls.append({"title": title, "head": head, "base": base, "body": body})
        return {"html_url": "https://github.com/acme/app/pull/4", "number": 4}


class _MemoryGitHub:
    def __init__(self) -> None:
        self.pulls: list[dict] = []
        self.updates: list[dict] = []
        self.created: list[dict] = []
        self.refs = {"main": "base"}
        self.files = {"README.md": {"sha": "blob0", "bytes": b"Please recieve this\n"}}

    def token_for(self, installation_id=None) -> str:
        return "app-token"

    def find_open_pull(self, repo, branch, installation_id=None):
        return None

    def get_branch_sha(self, repo, branch, installation_id=None) -> str:
        if branch not in self.refs:
            raise GitHubError("GitHub HTTP 404: Branch not found")
        return self.refs[branch]

    def get_file(self, repo, path, ref, installation_id=None) -> dict:
        item = self.files.get(path)
        if item is None:
            raise GitHubError("GitHub HTTP 404: missing")
        return {"sha": item["sha"], "bytes": item["bytes"]}

    def create_file(self, repo, path, message, content, branch, installation_id=None) -> None:
        self.created.append({"path": path, "content": content, "branch": branch, "message": message})

    def update_file(self, repo, path, message, content, blob_sha, branch, installation_id=None) -> None:
        self.files[path] = {"sha": "blob1", "bytes": content}
        self.updates.append({"path": path, "message": message, "branch": branch, "sha": blob_sha})

    def create_ref(self, repo, branch, sha, installation_id=None) -> None:
        self.refs[branch] = sha

    def create_pull(self, repo, *, title, head, base, body, installation_id=None):
        self.pulls.append({"title": title, "head": head, "base": base, "body": body})
        return {"html_url": "https://github.com/acme/app/pull/7", "number": 7}


if __name__ == "__main__":
    unittest.main()
