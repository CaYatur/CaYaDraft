import unittest

from cayadraft.commands import parse_command
from cayadraft.comments import MARKER


class CommandTests(unittest.TestCase):
    def test_english_and_turkish_aliases(self) -> None:
        cases = {
            "/caya help": ("help", ""),
            "/caya": ("help", ""),
            "/cayadraft analyze": ("analyze", ""),
            "/caya analiz": ("analyze", ""),
            "/caya özet": ("summarize", ""),
            "/caya düzelt README.md": ("fix", "README.md"),
            "/caya fix change recieve to receive": ("fix", "change recieve to receive"),
            "/caya geri son": ("revert_last", ""),
            "/caya revert last": ("revert_last", ""),
            "/caya başlık uygula Clearer title": ("title_apply", "Clearer title"),
            "/caya labels apply bug, docs": ("labels_apply", "bug, docs"),
            "/caya çevir tr": ("translate", "tr"),
            "@cayadraft revise say Save": ("revise", "say Save"),
        }
        for body, expected in cases.items():
            command = parse_command(body)
            self.assertIsNotNone(command, body)
            self.assertEqual((command.name, command.arg), expected, body)

    def test_keeps_following_lines(self) -> None:
        command = parse_command("/caya fix\nChange the button label to Save.")
        self.assertEqual(command.name, "fix")
        self.assertIn("Save", command.arg)

    def test_ignores_bot_marker_and_plain_chat(self) -> None:
        self.assertIsNone(parse_command(f"/caya fix\n\n{MARKER}"))
        self.assertIsNone(parse_command("Looks good to me."))
        self.assertIsNone(parse_command("/cayman is not a command"))

    def test_unknown_command(self) -> None:
        command = parse_command("/caya dance")
        self.assertEqual(command.name, "unknown")

    def test_mention_prose_is_an_instruction(self) -> None:
        command = parse_command("@cayadraft birde sona tarih eklemen gerek.")
        self.assertEqual(command.name, "instruct")
        self.assertIn("tarih", command.arg)


if __name__ == "__main__":
    unittest.main()
