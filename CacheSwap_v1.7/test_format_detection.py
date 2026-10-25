import unittest
import core

class FormatDetectionTests(unittest.TestCase):
    def test_font_signatures(self):
        for sig in (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2"):
            self.assertEqual(core.kind_of(sig + b"test"), "font")

    def test_existing_asset_types_unchanged(self):
        self.assertEqual(core.kind_of(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(core.kind_of(b"OggS"), "ogg")
        self.assertEqual(core.kind_of(b"version 7.00"), "mesh")

if __name__ == "__main__":
    unittest.main()
