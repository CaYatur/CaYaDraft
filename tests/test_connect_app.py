import tempfile
import unittest
from pathlib import Path

from cayadraft.connect_app import upsert_env


class EnvTests(unittest.TestCase):
    def test_updates_keys_and_keeps_the_token_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GITHUB_TOKEN=secret\nOWNER_LOGIN=CaYatur\n", encoding="utf-8")
            upsert_env(path, {"GITHUB_APP_ID": "42", "OWNER_LOGIN": "CaYatur"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("GITHUB_TOKEN=secret", text)
            self.assertIn("GITHUB_APP_ID=42", text)
            self.assertEqual(text.count("OWNER_LOGIN="), 1)


if __name__ == "__main__":
    unittest.main()
