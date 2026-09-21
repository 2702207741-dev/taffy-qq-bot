from __future__ import annotations

import unittest

from src.core.messages import split_text, text_message, text_with_at


class SafeMessageTests(unittest.TestCase):
    def test_cq_payload_is_kept_as_text(self) -> None:
        message = text_message("[CQ:image,file=http://127.0.0.1/private]")

        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].type, "text")
        self.assertIn("[CQ:image", message[0].data["text"])

    def test_structured_mention_does_not_parse_following_text(self) -> None:
        message = text_with_at(12345, "[CQ:at,qq=all]")

        self.assertEqual([segment.type for segment in message], ["at", "text"])
        self.assertEqual(message[0].data["qq"], "12345")
        self.assertIn("[CQ:at,qq=all]", message[1].data["text"])

    def test_split_text_round_trips_content(self) -> None:
        value = "a" * 500 + "\n" + "b" * 500
        parts = split_text(value, limit=800)

        self.assertTrue(all(len(part) <= 800 for part in parts))
        self.assertEqual("\n".join(parts), value)


if __name__ == "__main__":
    unittest.main()
