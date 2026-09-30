import unittest

from backend.diff.pdf import Token, compare_tokens


def tokens(words):
    return [
        Token(word, word.casefold(), 1, 1, 0.0, 10.0, " ".join(words),
              sum(len(w) + 1 for w in words[:index]),
              sum(len(w) + 1 for w in words[:index]) + len(word))
        for index, word in enumerate(words)
    ]


class TokenDiffTests(unittest.TestCase):
    def test_insertion_does_not_shift_unchanged_suffix(self):
        old = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split()
        new = old[:8] + ["added", "sentence"] + old[8:]
        findings = compare_tokens(tokens(old), tokens(new))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "added")
        self.assertEqual(findings[0]["new"]["text"], "added sentence")

    def test_small_number_change_is_visible(self):
        old = "the commission requires 4,000 MW by 2032 and shall review each filing promptly".split()
        new = old.copy()
        new[3] = "2,000"
        findings = compare_tokens(tokens(old), tokens(new))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["old"]["text"], "4,000")
        self.assertEqual(findings[0]["new"]["text"], "2,000")
        self.assertIn("number_changed", findings[0]["priority_reasons"])

    def test_deletion_is_reported(self):
        old = "a b c d e f g h removable words i j k l m n o p".split()
        new = [word for word in old if word not in {"removable", "words"}]
        findings = compare_tokens(tokens(old), tokens(new))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "deleted")
        self.assertEqual(findings[0]["old"]["text"], "removable words")


if __name__ == "__main__":
    unittest.main()
