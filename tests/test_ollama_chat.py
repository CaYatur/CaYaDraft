import json
import unittest

from cayadraft.ollama_client import collect_chat


def _line(message: dict) -> str:
    return json.dumps({"message": message})


class CollectChatTests(unittest.TestCase):
    def test_streamed_arguments_become_one_tool_call(self) -> None:
        lines = [
            _line({"role": "assistant", "content": ""}),
            _line({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "revise_pull", "arguments": "{\"instr"}}]}),
            _line({"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": "uction\": \"tarih\"}"}}]}),
        ]
        result = collect_chat(lines)
        self.assertEqual(result["tool_calls"][0]["name"], "revise_pull")
        self.assertEqual(result["tool_calls"][0]["arguments"]["instruction"], "tarih")

    def test_text_tool_call_is_not_left_in_the_reply(self) -> None:
        raw = '<tool_call>{"name": "open_fix", "arguments": {"instruction": "add a date"}}</tool_call>'
        result = collect_chat([_line({"role": "assistant", "content": raw})])
        self.assertEqual(result["tool_calls"][0]["name"], "open_fix")
        self.assertNotIn("tool_call", result["content"])
        self.assertEqual(result["tool_calls"][0]["arguments"]["instruction"], "add a date")

    def test_object_arguments_win_over_a_partial_string(self) -> None:
        lines = [
            _line({"role": "assistant", "tool_calls": [{"function": {"name": "read_issue", "arguments": "{\"n"}}]}),
            _line({"role": "assistant", "tool_calls": [{"function": {"name": "read_issue", "arguments": {"number": 14}}}]}),
        ]
        result = collect_chat(lines)
        self.assertEqual(result["tool_calls"], [{"name": "read_issue", "arguments": {"number": 14}}])


if __name__ == "__main__":
    unittest.main()
