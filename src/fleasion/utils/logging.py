"""Thread-safe application logging, persistence and redaction."""

from __future__ import annotations

import atexit
import logging
import re
import sys
import threading
import traceback
from datetime import datetime
from typing import Any

from .paths import LOG_FILE, LOGS_DIR

MAX_LOG_FILE_BYTES = 1 * 1024 * 1024
MAX_LOG_ARCHIVES = 5
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
    """Thread-safe in-memory log buffer with persistent, size-limited output."""

    def __init__(self):
        self._buffer: list[str] = []
        self._callbacks: list[Any] = []
        self._lock = threading.Lock()
        self._pending_notifications = False
        self._batch_timer: threading.Timer | None = None
        self._closed = False
        self._prepare_log_file()

    def _archive_paths(self) -> list:
        pattern = f'{LOG_FILE.stem}-*{LOG_FILE.suffix}'
        try:
            return list(LOGS_DIR.glob(pattern))
        except OSError:
            return []

    def _prune_archives(self) -> None:
        archives = sorted(
            self._archive_paths(),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
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

    def log(self, category: str, message: str, *, level: str = 'INFO') -> None:
        """Append one sanitized entry and notify GUI subscribers in batches."""
        now = datetime.now()
        safe_category = redact_sensitive_data(category)
        safe_message = redact_sensitive_data(message)
        safe_level = str(level).upper()
        timestamp = now.strftime('%H:%M:%S')
        entry = f'[{timestamp}] [{safe_level}] [{safe_category}] {safe_message}'

        with self._lock:
            self._buffer.append(entry)
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

            if not self._pending_notifications and not self._closed:
                self._pending_notifications = True
                self._batch_timer = threading.Timer(0.05, self._notify_callbacks)
                self._batch_timer.daemon = True
                self._batch_timer.start()

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
        """Return a snapshot of all entries recorded in this process."""
        with self._lock:
            return self._buffer.copy()

    def get_text(self) -> str:
        """Return all in-memory entries as text."""
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
        """Log entries are flushed and closed after each write; retained for shutdown hooks."""
        # Each file write is explicitly flushed inside its context manager.
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
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    if not _logging_shutdown_registered:
        atexit.register(target.close)
        _logging_shutdown_registered = True
    return handler


# Global buffer remains the GUI's single source of log messages.
log_buffer = LogBuffer()
