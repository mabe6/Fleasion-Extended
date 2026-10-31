"""Thread-safe application logging, persistence and redaction."""

from __future__ import annotations

import atexit
import logging
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any

from .paths import LOG_FILE, LOGS_DIR

MAX_LOG_FILE_BYTES = 1 * 1024 * 1024
MAX_LOG_ARCHIVES = 5
MAX_LIVE_LOG_ENTRIES = 5000
MAX_WARNING_DEDUP_KEYS = 512
WARNING_DEDUP_WINDOW_SECONDS = 30.0
_REDACTED = '[REDACTED]'

# Header values are deliberately redacted as a whole: cookies often contain
# several semicolon-separated values, not just the Roblox session cookie.
_RAW_SECRET_HEADER_RE = re.compile(
    r'(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*'
)
_QUOTED_SECRET_HEADER_RE = re.compile(
    r"""(?i)(["']?(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)["']?\s*[:=]\s*)(["'])(.*?)\2"""
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?i)(["']?(?:roblosecurity|access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|secret|token|signature|sig|auth)["']?\s*[:=]\s*)(["']?)([^"'&,\s;}\]]+)"""
)
_ROBLOSECURITY_RE = re.compile(
    r'(?i)(\.ROBLOSECURITY\s*=\s*)[^;\s,}"\']+'
)
_BEARER_RE = re.compile(
    r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+'
)
_URL_CREDENTIALS_RE = re.compile(
    r'(?i)(https?://)[^/@\s:]+(?::[^/@\s]*)?@'
)
_SIGNED_QUERY_RE = re.compile(
    r'(?i)([?&](?:token|sig|signature|auth|expires|access_token|refresh_token|api_key|'
    r'client_secret|x-amz-signature|x-amz-credential|x-amz-security-token|'
    r'x-goog-signature|key-pair-id|policy)=)[^&#\s"\']+'
)


def redact_sensitive_data(value: object) -> str:
    """Remove credentials and signed URL parameters before display or disk I/O."""
    text = str(value)
    text = _RAW_SECRET_HEADER_RE.sub(r'\1' + _REDACTED, text)
    text = _QUOTED_SECRET_HEADER_RE.sub(
        lambda match: match.group(1) + match.group(2) + _REDACTED + match.group(2),
        text,
    )
    text = _ROBLOSECURITY_RE.sub(r'\1' + _REDACTED, text)
    text = _BEARER_RE.sub(lambda match: match.group(1) + ' ' + _REDACTED, text)
    text = _URL_CREDENTIALS_RE.sub(r'\1' + _REDACTED + '@', text)
    text = _SIGNED_QUERY_RE.sub(r'\1' + _REDACTED, text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            match.group(1)
            + match.group(2)
            + _REDACTED
            + (match.group(2) if match.group(2) else '')
        ),
        text,
    )
    return text


class LogBuffer:
    """Thread-safe log buffer with bounded live history and persistent output."""

    def __init__(self):
        self._buffer: deque[str] = deque(maxlen=MAX_LIVE_LOG_ENTRIES)
        self._sequence = 0
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()
        self._pending_notifications = False
        self._batch_timer: threading.Timer | None = None
        self._closed = False
        self._minimum_level = logging.INFO
        self._warning_windows: dict[tuple[str, str], tuple[float, int]] = {}
        self._prepare_log_file()

    def set_level(self, level: int) -> None:
        """Set the minimum level written to the live buffer and persistent log."""
        with self._lock:
            self._minimum_level = int(level)

    def _archive_paths(self) -> list:
        pattern = f'{LOG_FILE.stem}-*{LOG_FILE.suffix}'
        try:
            return list(LOGS_DIR.glob(pattern))
        except OSError:
            return []

    def _prune_archives(self) -> None:
        def _mtime(path):
            try:
                return path.stat().st_mtime
            except OSError:
                return 0

        archives = sorted(self._archive_paths(), key=_mtime, reverse=True)
        for stale in archives[MAX_LOG_ARCHIVES:]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    def _prepare_log_file(self) -> None:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            self._rotate_log_file_if_needed()
            self._prune_archives()
        except OSError:
            # Logging must never prevent the application from starting.
            pass

    def _rotate_log_file_if_needed(self, incoming_bytes: int = 0) -> None:
        try:
            if not LOG_FILE.exists():
                return
            if LOG_FILE.stat().st_size + incoming_bytes <= MAX_LOG_FILE_BYTES:
                return

            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            archive = LOG_FILE.with_name(
                f'{LOG_FILE.stem}-{timestamp}{LOG_FILE.suffix}'
            )
            LOG_FILE.replace(archive)
            self._prune_archives()
        except OSError:
            # Keep the current log writable where possible; caller handles
            # any subsequent write error as a non-fatal logging failure.
            pass

    def _append_locked(self, now: datetime, category: str, message: str, level: str) -> None:
        safe_category = redact_sensitive_data(category)
        safe_message = redact_sensitive_data(message)
        timestamp = now.strftime('%H:%M:%S')
        entry = f'[{timestamp}] [{level}] [{safe_category}] {safe_message}'
        self._buffer.append(entry)
        self._sequence += 1

        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            line = f'{now:%Y-%m-%d} {entry}\n'
            self._rotate_log_file_if_needed(len(line.encode('utf-8')))
            with LOG_FILE.open('a', encoding='utf-8') as handle:
                handle.write(line)
                handle.flush()
        except OSError:
            # UI and application logic continue even when disk access fails.
            pass

    def _schedule_notifications_locked(self) -> None:
        if not self._pending_notifications and not self._closed:
            self._pending_notifications = True
            self._batch_timer = threading.Timer(0.05, self._notify_callbacks)
            self._batch_timer.daemon = True
            self._batch_timer.start()

    def _flush_expired_warnings_locked(self, now: datetime, monotonic_now: float) -> bool:
        emitted = False
        for key, (window_start, suppressed_count) in list(self._warning_windows.items()):
            if monotonic_now - window_start < WARNING_DEDUP_WINDOW_SECONDS:
                continue
            if suppressed_count:
                category, message = key
                self._append_locked(
                    now,
                    category,
                    f'Suppressed {suppressed_count} duplicate warning(s) in the previous '
                    f'{WARNING_DEDUP_WINDOW_SECONDS:g}s: {message}',
                    'WARNING',
                )
                emitted = True
            del self._warning_windows[key]
        return emitted

    def log(self, category: str, message: str, *, level: str = 'INFO') -> None:
        """Append one sanitized entry, filtering DEBUG and rate-limiting repeats."""
        safe_category = redact_sensitive_data(category)
        safe_message = redact_sensitive_data(message)
        safe_level = str(level).upper()
        if safe_level == 'INFO' and (
            safe_category.upper().startswith('DEBUG') or safe_category == 'ProxyDiag'
        ):
            safe_level = 'DEBUG'
        numeric_level = getattr(logging, safe_level, logging.INFO)
        if not isinstance(numeric_level, int):
            numeric_level = logging.INFO
        if numeric_level < self._minimum_level:
            return

        now = datetime.now()
        monotonic_now = time.monotonic()
        with self._lock:
            summary_written = self._flush_expired_warnings_locked(now, monotonic_now)

            if numeric_level >= logging.WARNING:
                key = (safe_category, safe_message)
                previous = self._warning_windows.get(key)
                if previous is not None and monotonic_now - previous[0] < WARNING_DEDUP_WINDOW_SECONDS:
                    self._warning_windows[key] = (previous[0], previous[1] + 1)
                    if summary_written:
                        self._schedule_notifications_locked()
                    return

                if key not in self._warning_windows and len(self._warning_windows) >= MAX_WARNING_DEDUP_KEYS:
                    oldest_key = next(iter(self._warning_windows))
                    old_start, old_count = self._warning_windows.pop(oldest_key)
                    if old_count:
                        old_category, old_message = oldest_key
                        self._append_locked(
                            now,
                            old_category,
                            f'Suppressed {old_count} duplicate warning(s) before dedup window '
                            f'eviction: {old_message}',
                            'WARNING',
                        )
                        summary_written = True
                self._warning_windows[key] = (monotonic_now, 0)

            self._append_locked(now, safe_category, safe_message, safe_level)
            self._schedule_notifications_locked()

    def _notify_callbacks(self) -> None:
        """Notify callbacks outside the lock to avoid callback deadlocks."""
        with self._lock:
            self._pending_notifications = False
            callbacks_copy = self._callbacks.copy()

        for callback in callbacks_copy:
            try:
                callback()
            except Exception:
                # A GUI notification callback must not break a worker thread.
                pass

    def get_all(self) -> list[str]:
        """Return the retained live entries (at most MAX_LIVE_LOG_ENTRIES)."""
        with self._lock:
            return list(self._buffer)

    def get_since(self, sequence: int) -> tuple[int, list[str], bool]:
        """Return entries after a sequence, plus whether older entries were dropped."""
        with self._lock:
            entries = list(self._buffer)
            oldest_sequence = self._sequence - len(entries)
            was_trimmed = sequence < oldest_sequence
            if was_trimmed:
                return self._sequence, entries, True
            offset = max(0, sequence - oldest_sequence)
            return self._sequence, entries[offset:], False

    def get_text(self) -> str:
        """Return the retained in-memory entries as text."""
        with self._lock:
            return '\n'.join(self._buffer) if self._buffer else 'No logs yet.'

    def add_callback(self, callback: Any) -> None:
        """Subscribe to batched notifications."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: Any) -> None:
        """Remove a notification callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def flush(self) -> None:
        """Writes are flushed and closed per entry; retained for shutdown hooks."""
        return

    def close(self) -> None:
        """Stop pending notifications during application shutdown."""
        with self._lock:
            self._closed = True
            timer = self._batch_timer
            self._batch_timer = None
        if timer is not None:
            timer.cancel()
        self.flush()


class LogBufferHandler(logging.Handler):
    """Forward standard-library logging records into the application log."""

    def __init__(self, buffer: LogBuffer):
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            formatter = self.formatter or logging.Formatter()
            if record.exc_info:
                message += '\n' + formatter.formatException(record.exc_info)
            elif record.exc_text:
                message += '\n' + record.exc_text
            if record.stack_info:
                message += '\n' + formatter.formatStack(record.stack_info)
            self.buffer.log(record.name, message, level=record.levelname)
        except Exception:
            # Do not allow a broken logging record to recursively break logging.
            self.handleError(record)


_exception_hooks_installed = False
_logging_shutdown_registered = False


def install_exception_hooks(buffer: LogBuffer = None) -> None:
    """Capture uncaught main-thread, worker-thread and unraisable exceptions."""
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return
    target = buffer or log_buffer

    previous_sys_hook = sys.excepthook

    def _sys_exception_hook(exc_type, exc_value, exc_traceback):
        try:
            formatted = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            target.log('Uncaught', formatted.rstrip(), level='CRITICAL')
        except Exception:
            pass
        previous_sys_hook(exc_type, exc_value, exc_traceback)

    previous_thread_hook = threading.excepthook

    def _thread_exception_hook(args):
        try:
            thread_name = args.thread.name if args.thread is not None else 'unknown'
            formatted = ''.join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
            target.log(
                'WorkerException',
                f'Unhandled exception in thread {thread_name}\n{formatted}'.rstrip(),
                level='CRITICAL',
            )
        except Exception:
            pass
        previous_thread_hook(args)

    previous_unraisable_hook = sys.unraisablehook

    def _unraisable_exception_hook(args):
        try:
            formatted = ''.join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
            detail = f'{args.err_msg or "Unraisable exception"}\n{formatted}'
            target.log('Unraisable', detail.rstrip(), level='ERROR')
        except Exception:
            pass
        previous_unraisable_hook(args)

    sys.excepthook = _sys_exception_hook
    threading.excepthook = _thread_exception_hook
    sys.unraisablehook = _unraisable_exception_hook
    _exception_hooks_installed = True


def setup_logging(
    buffer: LogBuffer = None,
    *,
    level: int = logging.INFO,
) -> LogBufferHandler:
    """Install one root logging bridge and register orderly shutdown."""
    global _logging_shutdown_registered
    target = buffer or log_buffer
    root = logging.getLogger()

    handler = next(
        (
            item
            for item in root.handlers
            if isinstance(item, LogBufferHandler) and item.buffer is target
        ),
        None,
    )
    if handler is None:
        handler = LogBufferHandler(target)
        root.addHandler(handler)

    handler.setLevel(logging.DEBUG)
    target.set_level(level)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    if not _logging_shutdown_registered:
        atexit.register(target.close)
        _logging_shutdown_registered = True
    return handler


# Global buffer remains the GUI's single source of log messages.
log_buffer = LogBuffer()
