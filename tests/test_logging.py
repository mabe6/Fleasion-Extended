import logging
import sys
import threading
from types import SimpleNamespace

import fleasion.utils.logging as fleasion_logging


def _make_buffer(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    log_file = logs_dir / "fleasion.log"
    monkeypatch.setattr(fleasion_logging, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(fleasion_logging, "LOG_FILE", log_file)
    return fleasion_logging.LogBuffer(), logs_dir, log_file


def test_log_file_rotates_at_one_mib_before_append(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    log_file = logs_dir / "fleasion.log"
    logs_dir.mkdir()
    log_file.write_bytes(b"x" * fleasion_logging.MAX_LOG_FILE_BYTES)

    monkeypatch.setattr(fleasion_logging, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(fleasion_logging, "LOG_FILE", log_file)

    buffer = fleasion_logging.LogBuffer()
    buffer.log("Test", "after rotation")

    archives = list(logs_dir.glob("fleasion-*.log"))
    assert len(archives) == 1
    assert archives[0].stat().st_size == fleasion_logging.MAX_LOG_FILE_BYTES
    assert log_file.stat().st_size < fleasion_logging.MAX_LOG_FILE_BYTES
    assert "after rotation" in log_file.read_text(encoding="utf-8")


def test_log_rotation_prunes_old_archives(tmp_path, monkeypatch):
    buffer, logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    monkeypatch.setattr(fleasion_logging, "MAX_LOG_ARCHIVES", 2)
    logs_dir.mkdir(exist_ok=True)
    for index in range(4):
        (logs_dir / f"fleasion-{index}.log").write_text(str(index), encoding="utf-8")
    log_file.write_bytes(b"x" * fleasion_logging.MAX_LOG_FILE_BYTES)

    buffer.log("Test", "retention")

    archives = list(logs_dir.glob("fleasion-*.log"))
    assert len(archives) == 2
    assert "retention" in log_file.read_text(encoding="utf-8")


def test_log_redacts_credentials_cookies_and_signed_url_parameters(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    buffer.log(
        "Proxy",
        "Authorization: Bearer auth-secret\n"
        "Cookie: .ROBLOSECURITY=roblox-cookie; other=value\n"
        "URL https://cdn.example.invalid/a?token=url-token&sig=url-signature&id=123",
    )

    text = log_file.read_text(encoding="utf-8")
    assert "auth-secret" not in text
    assert "roblox-cookie" not in text
    assert "other=value" not in text
    assert "url-token" not in text
    assert "url-signature" not in text
    assert "id=123" in text
    assert "[REDACTED]" in text
    assert "auth-secret" not in buffer.get_text()


def test_standard_logging_bridge_preserves_level_logger_and_traceback(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    handler = fleasion_logging.LogBufferHandler(buffer)
    try:
        try:
            raise ValueError("conversion failed")
        except ValueError:
            record = logging.LogRecord(
                name="fleasion.proxy.server",
                level=logging.ERROR,
                pathname=__file__,
                lineno=42,
                msg="OBJ conversion failed: %s",
                args=("bad geometry",),
                exc_info=sys.exc_info(),
            )
            handler.emit(record)
    finally:
        handler.close()

    text = log_file.read_text(encoding="utf-8")
    assert "[ERROR] [fleasion.proxy.server]" in text
    assert "OBJ conversion failed: bad geometry" in text
    assert "Traceback (most recent call last)" in text
    assert "ValueError: conversion failed" in text


def test_setup_logging_installs_only_one_bridge_for_the_same_buffer(tmp_path, monkeypatch):
    buffer, _logs_dir, _log_file = _make_buffer(tmp_path, monkeypatch)
    root = logging.getLogger()
    created = fleasion_logging.setup_logging(buffer)
    try:
        assert created in root.handlers
        assert fleasion_logging.setup_logging(buffer) is created
        logger = logging.getLogger("fleasion.test.setup")
        logger.warning("bridge works")
        assert "bridge works" in buffer.get_text()
        assert "[WARNING] [fleasion.test.setup]" in buffer.get_text()
    finally:
        root.removeHandler(created)
        created.close()


def test_unhandled_main_and_worker_exceptions_are_logged(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    original_sys_hook = sys.excepthook
    original_thread_hook = threading.excepthook
    original_unraisable_hook = sys.unraisablehook
    monkeypatch.setattr(fleasion_logging, "_exception_hooks_installed", False)
    monkeypatch.setattr(sys, "excepthook", lambda *_args: None)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)

    try:
        fleasion_logging.install_exception_hooks(buffer)
        try:
            raise RuntimeError("main startup failed")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        try:
            raise LookupError("worker scan failed")
        except LookupError as exc:
            args = SimpleNamespace(
                thread=SimpleNamespace(name="cache-scan"),
                exc_type=type(exc),
                exc_value=exc,
                exc_traceback=exc.__traceback__,
            )
            threading.excepthook(args)
    finally:
        sys.excepthook = original_sys_hook
        threading.excepthook = original_thread_hook
        sys.unraisablehook = original_unraisable_hook
        fleasion_logging._exception_hooks_installed = False

    text = log_file.read_text(encoding="utf-8")
    assert "[CRITICAL] [Uncaught]" in text
    assert "RuntimeError: main startup failed" in text
    assert "[CRITICAL] [WorkerException]" in text
    assert "cache-scan" in text
    assert "LookupError: worker scan failed" in text



def test_debug_entries_are_suppressed_by_default_and_enabled_on_request(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    buffer.log("Cache", "Cached Mesh: 123 (1024 bytes)", level="DEBUG")
    buffer.log("DEBUG_CDN", "Per-request diagnostic")
    assert buffer.get_all() == []
    assert not log_file.exists()

    buffer.set_level(logging.DEBUG)
    buffer.log("Cache", "Cached Mesh: 123 (1024 bytes)", level="DEBUG")
    buffer.log("DEBUG_CDN", "Per-request diagnostic")
    text = log_file.read_text(encoding="utf-8")
    assert "Cached Mesh: 123" in text
    assert "Per-request diagnostic" in text
    assert "[DEBUG]" in text


def test_live_buffer_is_bounded_and_reports_dropped_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fleasion_logging, "MAX_LIVE_LOG_ENTRIES", 3)
    buffer, _logs_dir, _log_file = _make_buffer(tmp_path, monkeypatch)
    for index in range(5):
        buffer.log("Test", f"entry-{index}")

    sequence, entries, was_trimmed = buffer.get_since(1)
    assert sequence == 5
    assert was_trimmed is True
    assert len(entries) == 3
    assert "entry-2" in entries[0]
    assert "entry-4" in entries[-1]

    next_sequence, new_entries, next_trimmed = buffer.get_since(4)
    assert next_sequence == 5
    assert next_trimmed is False
    assert len(new_entries) == 1
    assert "entry-4" in new_entries[0]


def test_repeated_warnings_are_suppressed_and_summarized(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    monkeypatch.setattr(fleasion_logging, "WARNING_DEDUP_WINDOW_SECONDS", 10.0)
    monotonic_now = [100.0]
    monkeypatch.setattr(fleasion_logging.time, "monotonic", lambda: monotonic_now[0])

    buffer.log("Proxy", "Connection refused", level="WARNING")
    monotonic_now[0] = 101.0
    buffer.log("Proxy", "Connection refused", level="WARNING")
    assert log_file.read_text(encoding="utf-8").count("Connection refused") == 1

    monotonic_now[0] = 111.0
    buffer.log("Proxy", "Connection refused", level="WARNING")
    text = log_file.read_text(encoding="utf-8")
    assert "Suppressed 1 duplicate warning(s)" in text
    assert text.count("Connection refused") == 3


def test_log_write_failure_does_not_raise(tmp_path, monkeypatch):
    buffer, _logs_dir, log_file = _make_buffer(tmp_path, monkeypatch)
    monkeypatch.setattr(fleasion_logging, "LOGS_DIR", tmp_path / "not-a-directory")
    (tmp_path / "not-a-directory").write_text("blocked", encoding="utf-8")

    buffer.log("Test", "still available in memory")
    assert "still available in memory" in buffer.get_text()
    assert not log_file.exists()
