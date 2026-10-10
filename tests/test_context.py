import tempfile
import unittest
from pathlib import Path

from cayadraft.context import gather_context


class ContextTests(unittest.TestCase):
    def test_reads_readme_and_skips_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Please recieve the file\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=super\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            text = gather_context(root, "Typo in the readme", "The word recieve is wrong.", "")
            self.assertIn("recieve", text)
            self.assertIn("README.md", text)
            self.assertNotIn("TOKEN", text)


if __name__ == "__main__":
    unittest.main()
