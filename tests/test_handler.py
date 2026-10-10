import unittest

from cayadraft.comments import MARKER, OPENING_TAG, PR_TAG, UPDATE_TAG
from cayadraft.handler import Handler
from cayadraft.textutil import picture_mode
from tests.support import FakeFixer, FakeGitHub, FakeOllama, make_config


ANALYSIS = "REQUEST:\nThe button label is wrong.\nANALYSIS:\nThe report is specific.\nDRAFT:\nChange the label to Save.\n"


def issue_payload(action="opened", **issue):
    body = {
        "action": action,
        "repository": {"full_name": "acme/app", "default_branch": "main"},
        "installation": {"id": 5},
        "sender": {"login": "CaYatur", "type": "User"},
        "issue": {"number": 3, "title": "Button label", "body": "It says Submit.", **issue},
    }
    return body


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.ollama = FakeOllama(ANALYSIS)
        self.fixer = FakeFixer()
        self.handler = Handler(make_config(), self.github, self.ollama, self.fixer)

    def test_issue_opened_mentions_owner_and_stops(self) -> None:
        self.handler.handle("issues", issue_payload())
        self.assertEqual(len(self.github.comments), 1)
        body = self.github.comments[0][2]
        self.assertTrue(body.startswith("Thank you for opening this issue."))
        self.assertLess(body.index("**Request**"), body.index("@CaYatur"))
        self.assertIn("@CaYatur Stopping here.", body)
        self.assertIn(OPENING_TAG, body)
        self.assertIn("Stopping here", body)
        self.assertNotIn("/caya help", body)
        self.assertNotIn("/caya fix", body)
        self.assertIn("Change the label to Save.", body)
        self.assertIn(MARKER, body)
        self.assertNotIn("ilk okuma", body)
        self.assertEqual(self.fixer.fixes, [])
        self.assertIn("<issue>", self.ollama.calls[0])

    def test_installed_account_is_the_admin_and_the_mention(self) -> None:
        payload = issue_payload()
        payload["installation"]["account"] = {"login": "OtherDev", "type": "User"}
        self.handler.handle("issues", payload)
        self.assertIn("@OtherDev", self.github.comments[0][2])
        self.assertNotIn("@CaYatur", self.github.comments[0][2])
        payload = issue_payload(action="created")
        payload["installation"]["account"] = {"login": "OtherDev", "type": "User"}
        payload["sender"] = {"login": "CaYatur", "type": "User"}
        payload["comment"] = {"body": "/caya help"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.github.comments), 1)
        payload["sender"] = {"login": "OtherDev", "type": "User"}
        self.handler.handle("issue_comment", payload)
        self.assertIn("CaYaDraft commands", self.github.comments[-1][2])
        self.assertIn("untrusted", self.ollama.calls[0].lower())
        self.assertNotIn("cannot see", self.ollama.calls[0])

    def test_repo_question_uses_the_readme_and_does_not_ask_for_more(self) -> None:
        self.ollama.response = (
            "REQUEST:\nThe reporter asks what this repository is.\n"
            "ANALYSIS:\nWithout context it is unclear.\n"
            "DRAFT:\nCould you clarify what aspect you want?\n"
        )
        self.github.readme = b"# GithubBotTest-1\n\nThis private repository is only for trying CaYaDraft.\n"
        payload = issue_payload(title="help", body="what is this reposity")
        self.handler.handle("issues", payload)
        body = self.github.comments[0][2]
        self.assertIn("trying CaYaDraft", body)
        self.assertNotIn("clarify", body.lower())
        payload["issue"]["user"] = {"login": "someone"}
        self.github.comments.clear()
        self.handler.handle("issues", issue_payload(title="help", body="what is this reposity", user={"login": "someone"}))
        opened = self.github.comments[-1][2]
        self.assertTrue(opened.startswith("Thank you, @someone, for opening this issue."))
        self.assertLess(opened.index("**Suggested draft**"), opened.index("@CaYatur"))
        self.assertIn("<readme>", self.ollama.calls[0])
        self.assertNotIn("/caya help", body)

    def test_second_open_does_not_duplicate(self) -> None:
        self.handler.handle("issues", issue_payload())
        self.handler.handle("issues", issue_payload())
        self.assertEqual(len(self.github.comments), 1)

    def test_ignores_pull_request_opened_as_issue(self) -> None:
        self.handler.handle("issues", issue_payload(pull_request={"url": "x"}))
        self.assertEqual(self.github.comments, [])

    def test_non_owner_and_bot_cannot_fix(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "/caya fix the typo"}
        payload["sender"] = {"login": "someone", "type": "User"}
        self.handler.handle("issue_comment", payload)
        payload["sender"] = {"login": "cayadraft[bot]", "type": "Bot"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.fixer.fixes, [])
        self.assertEqual(self.github.comments, [])

    def test_owner_fix_waits_for_command(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "I will look later."}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.fixer.fixes, [])
        payload["comment"] = {"body": "/caya fix change Submit to Save"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.fixer.fixes), 1)
        self.assertIn("Save", self.fixer.fixes[0]["instruction"])
        self.assertTrue(any("Pull request opened" in body for _, _, body in self.github.comments))

    def test_own_comment_is_not_a_command(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": f"/caya fix\n\n{MARKER}"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.fixer.fixes, [])

    def test_labels_and_title_apply_without_inventing(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "/caya labels apply bug, missing"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.github.added, [["bug"]])
        payload["comment"] = {"body": "/caya title apply Shorter title"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.github.titles, ["Shorter title"])
        self.assertEqual(self.ollama.calls, [])

    def test_translate_asks_for_turkish_in_english_bot(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "/caya çevir tr"}
        self.handler.handle("issue_comment", payload)
        self.assertIn("Turkish", self.ollama.calls[-1])
        self.assertIn(MARKER, self.github.comments[-1][2])

    def test_pr_revise_and_revert(self) -> None:
        payload = issue_payload(action="created")
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/pulls/3"}
        payload["comment"] = {"body": "/caya revise say Kaydet"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.fixer.revises), 1)
        self.assertEqual(self.fixer.fixes, [])
        payload["comment"] = {"body": "/caya revert last"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.fixer.reverts[-1]["last_only"], True)
        payload["comment"] = {"body": "/caya revert"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.fixer.reverts[-1]["last_only"], False)

    def test_owner_mention_on_a_pull_revises(self) -> None:
        self.ollama.tool_rounds.append(
            {
                "content": "",
                "tool_calls": [{"name": "revise_pull", "arguments": {"instruction": "birde sona tarih eklemen gerek."}}],
            }
        )
        self.ollama.tool_rounds.append({"content": "Added the date.", "tool_calls": []})
        payload = issue_payload(action="created")
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/pulls/28"}
        payload["comment"] = {"body": "@cayadraft birde sona tarih eklemen gerek."}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.fixer.revises), 1)
        self.assertIn("tarih", self.fixer.revises[0]["instruction"])
        self.assertEqual(self.fixer.fixes, [])
        self.assertIn("revise_pull", self.github.comments[-1][2])
        self.assertNotIn("don't know that command", self.github.comments[-1][2])

    def test_slash_fix_does_not_ask_the_model_to_pick_tools(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "/caya fix change Submit to Save"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.fixer.fixes), 1)
        self.assertEqual(self.ollama.chat_calls, [])

    def test_owner_mention_can_open_a_pull_and_apply_the_fix(self) -> None:
        self.ollama.tool_rounds.append(
            {
                "content": "",
                "tool_calls": [
                    {"name": "read_issue", "arguments": {}},
                    {"name": "open_fix", "arguments": {"instruction": "PR açıp düzeltme uygula"}},
                ],
            }
        )
        self.ollama.tool_rounds.append({"content": "Done.", "tool_calls": []})
        payload = issue_payload(action="created")
        payload["comment"] = {"body": "@cayadraft PR açıp düzeltme uygula"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.fixer.fixes), 1)
        self.assertIn("düzeltme", self.fixer.fixes[0]["instruction"])
        self.assertEqual(self.fixer.revises, [])
        self.assertIn("open_fix", self.github.comments[-1][2])
        self.assertIn("Pull request opened", self.github.comments[-1][2])

    def test_review_comment_includes_the_line(self) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "acme/app", "default_branch": "main"},
            "installation": {"id": 5},
            "sender": {"login": "CaYatur", "type": "User"},
            "pull_request": {
                "number": 9,
                "title": "Fix #3",
                "body": "draft",
                "html_url": "https://github.com/acme/app/pull/9",
                "head": {"ref": "cayadraft/issue-3"},
                "base": {"ref": "main"},
            },
            "comment": {"body": "/caya revise use Save", "path": "README.md", "line": 4, "diff_hunk": "Submit"},
        }
        self.handler.handle("pull_request_review_comment", payload)
        instruction = self.fixer.revises[0]["instruction"]
        self.assertIn("README.md", instruction)
        self.assertIn("Save", instruction)

    def test_author_followup_posts_an_update(self) -> None:
        payload = issue_payload(action="created")
        payload["issue"]["user"] = {"login": "caganturgut"}
        payload["sender"] = {"login": "caganturgut", "type": "User"}
        payload["comment"] = {"body": "@cayadraft The board is 20 by 20 and the score increases by one."}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.github.comments), 1)
        body = self.github.comments[0][2]
        self.assertIn(UPDATE_TAG, body)
        self.assertIn("@CaYatur", body)
        self.assertIn("Stopping here", body)
        self.assertIn(MARKER, body)
        self.assertEqual(self.fixer.fixes, [])
        self.assertIn("<new_comment>", self.ollama.calls[0])

    def test_later_comment_without_a_mention_stays_silent(self) -> None:
        payload = issue_payload(action="created")
        payload["issue"]["user"] = {"login": "caganturgut"}
        payload["sender"] = {"login": "caganturgut", "type": "User"}
        payload["comment"] = {"body": "thank you"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.ollama.calls, [])

    def test_other_user_comment_stays_silent(self) -> None:
        payload = issue_payload(action="created")
        payload["issue"]["user"] = {"login": "caganturgut"}
        payload["sender"] = {"login": "someone", "type": "User"}
        payload["comment"] = {"body": "I have more details about the board."}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.ollama.calls, [])

    def test_mention_from_someone_else_gets_one_update(self) -> None:
        payload = issue_payload(action="created")
        payload["issue"]["user"] = {"login": "caganturgut"}
        payload["sender"] = {"login": "someone", "type": "User"}
        payload["comment"] = {"body": "@cayadraft[bot] what does the title ask for?"}
        self.handler.handle("issue_comment", payload)
        self.assertEqual(len(self.github.comments), 1)
        self.assertIn(UPDATE_TAG, self.github.comments[0][2])

    def test_image_only_issue_is_judged_from_the_title(self) -> None:
        self.handler.handle(
            "issues",
            issue_payload(body="![screen](https://github.com/user-attachments/assets/abc)"),
        )
        prompt = self.ollama.calls[0]
        self.assertIn("only an attached image", prompt)
        self.assertIn("Summarize the title", prompt)
        self.assertIn("Do not ask questions", prompt)
        self.assertIn("Button label", prompt)

    def test_image_with_text_does_not_ask_for_gaps(self) -> None:
        body = "The save button stays disabled after a click.\n\n<img src=\"https://github.com/user-attachments/assets/abc\">"
        self.handler.handle("issues", issue_payload(body=body))
        prompt = self.ollama.calls[0]
        self.assertIn("cannot see it", prompt)
        self.assertIn("Summarize the title and the written body", prompt)
        self.assertIn("Do not ask questions", prompt)
        self.assertIn("save button stays disabled", prompt)

    def test_pull_request_opened_posts_a_summary(self) -> None:
        self.ollama.response = (
            "SUMMARY:\nIt renames the button.\nFILES:\n- README.md\nNOTE:\nNothing blocking is visible from the text.\n"
        )
        self.github.files = [
            {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 1, "patch": "-Submit\n+Save"}
        ]
        payload = {
            "action": "opened",
            "number": 4,
            "repository": {"full_name": "acme/app"},
            "installation": {"id": 5},
            "pull_request": {
                "number": 4,
                "title": "Rename the button",
                "body": "Submit becomes Save.",
                "additions": 1,
                "deletions": 1,
                "changed_files": 1,
            },
        }
        self.handler.handle("pull_request", payload)
        body = self.github.comments[0][2]
        self.assertIn(PR_TAG, body)
        self.assertIn("@CaYatur", body)
        self.assertIn("It renames the button.", body)
        self.assertIn("README.md", body)
        self.assertIn("README.md", self.ollama.calls[0])
        self.assertIn("-Submit", self.ollama.calls[0])
        self.assertEqual(self.fixer.fixes, [])
        self.handler.handle("pull_request", payload)
        self.assertEqual(len(self.github.comments), 1)

    def test_pull_with_only_an_image_uses_the_title_and_files(self) -> None:
        self.github.files = [{"filename": "game.py", "status": "added", "additions": 20, "deletions": 0, "patch": "snake"}]
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/app"},
            "installation": {"id": 5},
            "pull_request": {
                "number": 8,
                "title": "Add the snake game",
                "body": "![board](https://github.com/user-attachments/assets/zz)",
                "additions": 20,
                "deletions": 0,
                "changed_files": 1,
            },
        }
        self.handler.handle("pull_request", payload)
        prompt = self.ollama.calls[0]
        self.assertIn("only an attached image", prompt)
        self.assertIn("file notes", prompt)
        self.assertIn("game.py", prompt)

    def test_picture_mode_ignores_a_bare_title(self) -> None:
        self.assertEqual(picture_mode("Need a python snake code in main."), "")
        self.assertEqual(picture_mode("![shot](https://github.com/user-attachments/assets/aa)"), "title")
        self.assertEqual(picture_mode("The score goes up by one.\n\nhttps://user-images.githubusercontent.com/1/a.png"), "text")

    def test_allowlist_skips_other_repos(self) -> None:
        handler = Handler(make_config(allowlist=("other/repo",)), self.github, self.ollama, self.fixer)
        handler.handle("issues", issue_payload())
        self.assertEqual(self.github.comments, [])


if __name__ == "__main__":
    unittest.main()
