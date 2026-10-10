import unittest

from cayadraft.comments import ground_read


class GroundReadTests(unittest.TestCase):
    def test_issue_23_stays_in_english_and_keeps_the_exact_names(self) -> None:
        request, analysis, draft = ground_read(
            "yeni users.md dosyası oluşturulmalı.",
            "yeni user.md dosyası oluşturularak bu depoya katkı sağlayanların adları eklenmeli:\nÇAĞAN TURGUT\nCaYatur",
            "- Yeni bir users.md dosyası oluşturulmalı ve yeni katkıda bulunan kullanıcıların adları eklenmelidir.",
            "- Dosya adı doğru yazılmış, ancak dosya içeriği henüz mevcut değil.",
            "- users.md dosyasını oluşturun ve CAYAN TURGUT, CaYatur gibi adları ekleyin.",
        )
        text = "\n".join([request, analysis, draft])
        self.assertIn("ÇAĞAN TURGUT", text)
        self.assertIn("CaYatur", text)
        self.assertNotIn("CAYAN", text)
        self.assertIn("`users.md`", text)
        self.assertIn("`user.md`", text)
        self.assertNotIn("oluşturulmalı", text)
        self.assertNotIn("dosyasını", text)

    def test_issue_with_an_image_is_accepted_as_written(self) -> None:
        body = (
            '<img width="285" height="102" alt="Image" '
            'src="https://github.com/user-attachments/assets/df5931a4-4f99-48ed-9f2f-0576a448959b" />\n'
            "error in application."
        )
        request, analysis, draft = ground_read(
            "Error",
            body,
            "The reporter reports a generic error without specifying the file.",
            "It is unclear which part of the code failed.",
            "Could you please provide the specific error message and steps to reproduce the issue?",
            picture="text",
        )
        text = "\n".join([request, analysis, draft])
        self.assertIn("Error", request)
        self.assertIn("error in application.", request)
        self.assertIn("was not read", analysis)
        self.assertNotIn("provide", text.lower())
        self.assertNotIn("reproduce", text.lower())
        self.assertNotIn("unclear", text.lower())

    def test_image_issue_quotes_the_title_and_the_written_body(self) -> None:
        request, analysis, draft = ground_read(
            "my app not work",
            '<img src="https://github.com/user-attachments/assets/aa" />\nerror in windows app',
            "The reporter states the Windows application is not working and references an attached image showing the error.",
            'The title is vague ("my app not work") and lacks specific error details. The image cannot be viewed.',
            "Use the written report as it stands.",
            picture="text",
        )
        text = "\n".join([request, analysis, draft])
        self.assertIn("my app not work", request)
        self.assertIn("error in windows app", request)
        self.assertEqual(draft, request)
        self.assertEqual(analysis, "An image is attached and was not read.")
        self.assertNotIn("vague", text.lower())
        self.assertNotIn("lacks", text.lower())
        self.assertNotIn("showing the error", text.lower())
        self.assertNotIn("as it stands", text.lower())

    def test_repo_question_is_answered_from_the_readme(self) -> None:
        request, analysis, draft = ground_read(
            "help",
            "what is this reposity",
            "The reporter is asking for a description of the repository's purpose.",
            "The issue contains no specific details. Without context, it is unclear which part they want.",
            "Could you clarify what aspect of the repository you would like to know about?",
            readme="# GithubBotTest-1\n\nThis private repository is only for trying CaYaDraft.\n\nPlease receive this note. The spelling should be receive.\n",
        )
        text = "\n".join([request, analysis, draft])
        self.assertIn("trying CaYaDraft", draft)
        self.assertNotIn("clarify", text.lower())
        self.assertNotIn("unclear", text.lower())
        self.assertNotIn("receive", text.lower())


if __name__ == "__main__":
    unittest.main()
