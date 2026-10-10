import tempfile
import unittest
from pathlib import Path

from cayadraft.edits import Edit, EditError, apply_edits, parse_proposal
from cayadraft.ollama_client import _stream_piece


SAMPLE = """SUMMARY:
Fix the spelling.
FILE: README.md
FIND:
recieve
REPLACE:
receive
---
FILE: docs/guide.md
FIND:
old label
REPLACE:
new label
---
"""


class EditTests(unittest.TestCase):
    def test_parse_blocks(self) -> None:
        proposal = parse_proposal("```\n" + SAMPLE + "```")
        self.assertEqual(proposal.summary, "Fix the spelling.")
        self.assertEqual(len(proposal.edits), 2)
        self.assertEqual(proposal.edits[0].path, "README.md")
        self.assertIn("recieve", proposal.edits[0].find)

    def test_stream_piece_joins_token_lines(self) -> None:
        self.assertEqual(_stream_piece('{"message":{"content":"Çağan"}}'), "Çağan")
        self.assertEqual(_stream_piece(""), "")
        self.assertEqual(_stream_piece("not-json"), "")

    def test_parse_new_file_content(self) -> None:
        proposal = parse_proposal("SUMMARY:\nAdd a welcome page.\nFILE: welcome.html\nCONTENT:\n<h1>Welcome</h1>\n---\n")
        self.assertEqual(proposal.edits, [])
        self.assertEqual(proposal.created, (("welcome.html", "<h1>Welcome</h1>"),))

    def test_parse_inline_find_and_replace(self) -> None:
        proposal = parse_proposal("SUMMARY:\nFILE: README.md\nFIND: receivee\nREPLACE: receive\n---\n")
        self.assertEqual(proposal.edits[0].find, "receivee")
        self.assertEqual(proposal.edits[0].replace, "receive")

    def test_cannot(self) -> None:
        proposal = parse_proposal("CANNOT:\nThe file was not in the snippets.")
        self.assertTrue(proposal.cannot)
        self.assertEqual(proposal.edits, [])

    def test_apply_unique_and_keep_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "README.md"
            target.write_bytes(b"Please recieve this\r\nNext line\r\n")
            changed = apply_edits(root, [Edit("README.md", "recieve", "receive")], max_files=5, max_edits=8)
            self.assertEqual(changed, ["README.md"])
            self.assertEqual(target.read_bytes(), b"Please receive this\r\nNext line\r\n")

    def test_expands_a_unique_short_fragment_to_the_word(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "README.md"
            target.write_text("Please recieve this note. The spelling should be receivee.\n", encoding="utf-8")
            changed = apply_edits(root, [Edit("README.md", "ee", "e")], max_files=5, max_edits=8)
            self.assertEqual(changed, ["README.md"])
            self.assertEqual(target.read_text(encoding="utf-8"), "Please recieve this note. The spelling should be receive.\n")

    def test_rejects_ambiguous_secret_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("recieve recieve\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
            with self.assertRaises(EditError):
                apply_edits(root, [Edit("README.md", "recieve", "receive")], max_files=5, max_edits=8)
            with self.assertRaises(EditError):
                apply_edits(root, [Edit(".env", "TOKEN", "NOPE")], max_files=5, max_edits=8)
            with self.assertRaises(EditError):
                apply_edits(root, [Edit("../README.md", "recieve", "receive")], max_files=5, max_edits=8)
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "TOKEN=1\n")


if __name__ == "__main__":
    unittest.main()
