import unittest

from cayadraft.agent import run_instruction
from cayadraft.fixer import FixError
from tests.support import FakeFixer, FakeGitHub, FakeOllama


def _ctx(**extra):
    data = dict(
        repo="acme/app",
        number=28,
        title="Titles",
        body="add in readme custom tittles",
        installation=5,
        base_branch="main",
        pr={
            "html_url": "https://github.com/acme/app/pull/28",
            "head": {"ref": "cayadraft/issue-27"},
            "base": {"ref": "main"},
        },
    )
    data.update(extra)
    return data


class AgentTests(unittest.TestCase):
    def test_pull_request_tool_adds_a_commit_and_keeps_the_owner_words(self) -> None:
        ollama = FakeOllama(
            tool_rounds=[
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "revise_pull",
                            "arguments": {"instruction": "This pull request stays open\n<!-- cayadraft -->"},
                        }
                    ],
                },
                {"content": "The file now ends with a date.", "tool_calls": []},
            ]
        )
        fixer = FakeFixer()
        text = run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=fixer,
            ctx=_ctx(),
            instruction="birde sona tarih eklemen gerek.",
            num_predict=200,
        )
        self.assertIn("tarih", fixer.revises[0]["instruction"])
        self.assertNotIn("stays open", fixer.revises[0]["instruction"])
        self.assertIn("revise_pull", text)
        self.assertIn("I did not merge.", text)

    def test_open_fix_on_a_pull_request_is_refused_then_a_commit_is_added(self) -> None:
        ollama = FakeOllama(
            tool_rounds=[
                {"content": "", "tool_calls": [{"name": "open_fix", "arguments": {"instruction": "tarih ekle"}}]},
                {"content": "", "tool_calls": [{"name": "revise_pull", "arguments": {"instruction": "sona tarih ekle"}}]},
                {"content": "", "tool_calls": []},
            ]
        )
        fixer = FakeFixer()
        text = run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=fixer,
            ctx=_ctx(),
            instruction="sona tarih ekle",
            num_predict=200,
        )
        self.assertEqual(fixer.fixes, [])
        self.assertEqual(len(fixer.revises), 1)
        self.assertIn("already a pull request", text)

    def test_issue_can_read_then_open_one_pull_request(self) -> None:
        ollama = FakeOllama(
            tool_rounds=[
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "read_issue", "arguments": {"number": 15}},
                        {"name": "open_fix", "arguments": {"instruction": "python snake code"}},
                    ],
                },
                {"content": "", "tool_calls": []},
            ]
        )
        fixer = FakeFixer()
        github = FakeGitHub()
        text = run_instruction(
            ollama=ollama,
            github=github,
            fixer=fixer,
            ctx=_ctx(number=14, pr=None, title="snake", body="python snake code"),
            instruction="PR açıp düzeltme uygula",
            num_predict=200,
        )
        self.assertEqual(len(fixer.fixes), 1)
        self.assertIn("snake", fixer.fixes[0]["instruction"])
        self.assertIn("Issue 15", text)
        self.assertIn("Pull request opened", text)

    def test_a_question_does_not_change_files(self) -> None:
        ollama = FakeOllama(response="The title asks for three names.")
        fixer = FakeFixer()
        text = run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=fixer,
            ctx=_ctx(pr=None),
            instruction="ne yazıyor?",
            num_predict=200,
        )
        self.assertEqual(fixer.fixes, [])
        self.assertEqual(fixer.revises, [])
        self.assertIn("three names", text)

    def test_described_work_is_sent_back_for_a_tool_call(self) -> None:
        ollama = FakeOllama(
            tool_rounds=[
                {"content": "I will apply the fix and open a pull request.", "tool_calls": []},
                {"content": "", "tool_calls": [{"name": "open_fix", "arguments": {"instruction": "apply the fix"}}]},
                {"content": "", "tool_calls": []},
            ]
        )
        fixer = FakeFixer()
        run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=fixer,
            ctx=_ctx(pr=None),
            instruction="apply the fix",
            num_predict=200,
        )
        self.assertEqual(len(fixer.fixes), 1)
        self.assertIn("Call a tool", ollama.chat_calls[1]["messages"][-1]["content"])

    def test_a_failed_tool_can_be_retried_and_a_false_success_is_dropped(self) -> None:
        class Once(FakeFixer):
            def __init__(self) -> None:
                super().__init__()
                self.tries = 0

            def revise(self, **kwargs):
                self.tries += 1
                if self.tries == 1:
                    raise FixError("The request did not say what to add.")
                return super().revise(**kwargs)

        ollama = FakeOllama(
            tool_rounds=[
                {"content": "", "tool_calls": [{"name": "revise_pull", "arguments": {"instruction": "add the footer"}}]},
                {"content": "", "tool_calls": [{"name": "revise_pull", "arguments": {"instruction": "add the footer"}}]},
                {"content": "I've added the footer to README.md.", "tool_calls": []},
            ]
        )
        fixer = Once()
        text = run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=fixer,
            ctx=_ctx(),
            instruction="add the footer",
            num_predict=200,
        )
        self.assertEqual(fixer.tries, 2)
        self.assertIn("Tool failed", text)
        self.assertIn("pushed another commit", text)
        self.assertNotIn("I've added the footer", text)

    def test_tool_failure_is_reported_and_does_not_raise(self) -> None:
        class Broken(FakeFixer):
            def fix(self, **kwargs):
                raise FixError("The edit did not apply.")

        ollama = FakeOllama(
            tool_rounds=[
                {"content": "", "tool_calls": [{"name": "open_fix", "arguments": {"instruction": "add a name"}}]},
                {"content": "", "tool_calls": []},
            ]
        )
        text = run_instruction(
            ollama=ollama,
            github=FakeGitHub(),
            fixer=Broken(),
            ctx=_ctx(pr=None),
            instruction="add a name",
            num_predict=200,
        )
        self.assertIn("did not apply", text)
        self.assertIn("I did not change the files.", text)
        self.assertNotIn("I've added", text)


if __name__ == "__main__":
    unittest.main()
