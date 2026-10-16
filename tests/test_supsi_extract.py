"""Guard the symbol handling that affects scientific interpretation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supsi_extract as extract  # noqa: E402


class SymbolTests(unittest.TestCase):
    def test_standalone_dash_is_neither_zero_nor_negative(self):
        self.assertEqual(extract.clean_token("-"), ("", "", "dash_uninterpreted", ""))
        self.assertEqual(extract.clean_token(""), ("", "", "blank", ""))

    def test_numeric_suffix_is_retained_separately(self):
        self.assertEqual(extract.clean_token("330.0+"), ("330.0", "+", "numeric", ""))
        self.assertEqual(extract.clean_token("1.8-"), ("1.8", "-", "numeric", ""))
        self.assertEqual(extract.clean_token("n/a"), ("", "", "other_symbol", "n/a"))


if __name__ == "__main__":
    unittest.main()
