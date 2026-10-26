"""Compatibility helpers used by the embedded Fleasion preview components."""
import logging
from pathlib import Path

_logger = logging.getLogger("CacheSwap.Fleasion")
class _LogBuffer:
    @staticmethod
    def log(component, message):
        _logger.info("[%s] %s", component, message)
log_buffer = _LogBuffer()
def format_count(value):
    return f"{int(value):,}"
def open_folder(path):
    return Path(path)
