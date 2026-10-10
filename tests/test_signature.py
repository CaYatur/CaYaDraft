import unittest

from cayadraft.signature import sign, verify_signature


class SignatureTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        body = b'{"zen":"ok"}'
        header = sign("topsecret", body)
        self.assertTrue(verify_signature("topsecret", body, header))
        self.assertFalse(verify_signature("topsecret", body, "sha256=dead"))
        self.assertFalse(verify_signature("", body, header))
        self.assertFalse(verify_signature("topsecret", body + b" ", header))


if __name__ == "__main__":
    unittest.main()
