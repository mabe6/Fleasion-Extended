import struct
import unittest
import core
import preview

class ExtendedFormatTests(unittest.TestCase):
    def test_common_image_signatures(self):
        cases = [
            (b"BM" + b"\x00" * 14, "bmp"),
            (b"GIF89a" + b"\x00" * 8, "gif"),
            (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 4, "webp"),
            (b"II*\x00" + b"\x00" * 8, "tiff"),
            (b"\x00\x00\x01\x00" + b"\x00" * 8, "ico"),
            (b"DDS " + b"\x00" * 20, "dds"),
            (b"8BPS" + b"\x00" * 8, "psd"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(preview.image_kind(data), expected)
                self.assertEqual(core.kind_of(data), expected)

    def test_texturepack_ids_and_fixed_slots(self):
        data = b"<TexturePack><Color>123</Color><NormalMap>456</NormalMap><Roughness>789</Roughness><Height>0</Height></TexturePack>"
        maps = preview.parse_texturepack(data)
        self.assertEqual([(m["asset_id"], m["slot"]) for m in maps], [("123", 0), ("456", 1), ("789", 3)])

    def test_ktx1_uncompressed_rgba(self):
        magic = b"\xabKTX 11\xbb\r\n\x1a\n"
        # endianness, type, type-size, format, internal, base, width, height,
        # depth, array elements, faces, mip levels, key/value bytes
        header = struct.pack("<13I", 0x04030201, 0x1401, 1, 0x1908, 0x8058, 0x1908,
                             1, 1, 0, 0, 1, 1, 0)
        pixels = struct.pack("<I", 4) + bytes([10, 20, 30, 255])
        arr = preview.decode_ktx1_uncompressed(magic + header + pixels)
        self.assertIsNotNone(arr)
        self.assertEqual(arr.shape, (1, 1, 4))
        self.assertEqual(arr[0, 0].tolist(), [10, 20, 30, 255])

if __name__ == "__main__":
    unittest.main()
