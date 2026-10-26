"""Lightweight Roblox document format detector for the preview widgets."""
import gzip

def classify_roblox_document(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        return "unknown"
    raw = bytes(data)
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except Exception:
            return "unknown"
    stripped = raw.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if raw.startswith(b"<roblox!\x89\xff\r\n\x1a\n"):
        return "rbxm"
    if stripped.startswith(b"<roblox") or stripped.startswith(b"<?xml"):
        return "rbxmx"
    return "unknown"
