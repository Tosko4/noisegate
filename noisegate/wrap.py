from __future__ import annotations

import os
import selectors
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Any

from .engine import JsonValue, NoisegateOptions, reduce_text

DEFAULT_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
CAPTURE_TRUNCATED_SUFFIX = "\n[noisegate: capture truncated]\n"
PIPE_DRAIN_GRACE_SECONDS = 0.1
PROCESS_TERMINATION_GRACE_SECONDS = 0.5


class WrappedCommandInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"wrapped command interrupted by signal {signum}")


@dataclass(frozen=True, slots=True)
class WrappedCommandResult:
    text: str
    exit_code: int
    stdout: str
    stderr: str
    capture_truncated: bool
    metadata: dict[str, JsonValue]


@dataclass(slots=True)
class _CapturedStream:
    max_bytes: int
    text: str = ""
    bytes_seen: int = 0
    truncated: bool = False

    def append(self, chunk: bytes) -> str:
        if self.truncated:
            return ""
        if self.max_bytes <= 0:
            self.text = CAPTURE_TRUNCATED_SUFFIX
            self.truncated = True
            return CAPTURE_TRUNCATED_SUFFIX

        remaining = self.max_bytes - self.bytes_seen
        if remaining <= 0:
            self.text += CAPTURE_TRUNCATED_SUFFIX
            self.truncated = True
            return CAPTURE_TRUNCATED_SUFFIX
        if len(chunk) <= remaining:
            fragment = chunk.decode("utf-8", errors="replace")
            self.text += fragment
            self.bytes_seen += len(chunk)
            return fragment

        fragment = chunk[:remaining].decode("utf-8", errors="replace") + CAPTURE_TRUNCATED_SUFFIX
        self.text += fragment
        self.bytes_seen = self.max_bytes
        self.truncated = True
        return fragment


def run_wrapped_command(
    argv: list[str],
    *,
    command: str | None = None,
    source: str = "wrap",
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    raw: bool = False,
    options: NoisegateOptions | None = None,
) -> WrappedCommandResult:
    if not argv:
        raise ValueError("wrap requires a command after --")

    stdout, stderr, raw_text, exit_code = _capture_command(
        argv,
        max_capture_bytes=max_capture_bytes,
    )
    capture_truncated = stdout.truncated or stderr.truncated
    if raw:
        return WrappedCommandResult(
            text=raw_text,
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            capture_truncated=capture_truncated,
            metadata={
                "compacted": False,
                "mode": "raw",
                "capture_truncated": capture_truncated,
            },
        )

    reduce_options = options or NoisegateOptions.from_env()
    try:
        reduced = reduce_text(
            raw_text,
            command=command or shlex.join(argv),
            tool_name="terminal",
            source=source,
            exit_code=exit_code,
            options=reduce_options,
        )
        metadata = dict(reduced.metadata)
        metadata["capture_truncated"] = capture_truncated
        return WrappedCommandResult(
            text=reduced.text,
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            capture_truncated=capture_truncated,
            metadata=metadata,
        )
    except Exception:
        return WrappedCommandResult(
            text=raw_text,
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            capture_truncated=capture_truncated,
            metadata={
                "compacted": False,
                "mode": "fail_open",
                "capture_truncated": capture_truncated,
            },
        )


def _capture_command(
    argv: list[str],
    *,
    max_capture_bytes: int,
) -> tuple[_CapturedStream, _CapturedStream, str, int]:
    process = subprocess.Popen(
        argv,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=(os.name == "posix"),
    )
    # Use one OS pipe for stdout+stderr so wrapped output follows the order a
    # terminal-like combined stream would see. Separate stdout/stderr fields are
    # retained for API shape, but stderr is intentionally empty in wrap mode.
    stdout = _CapturedStream(max(0, max_capture_bytes))
    stderr = _CapturedStream(max(0, max_capture_bytes))
    combined_parts: list[str] = []
    old_handlers = _install_interrupt_handlers(process)
    try:
        exit_code = _drain_process_pipes(
            process,
            stdout=stdout,
            stderr=stderr,
            combined_parts=combined_parts,
        )
    except (KeyboardInterrupt, WrappedCommandInterrupted):
        _terminate_wrapped_process(process)
        raise
    finally:
        _restore_interrupt_handlers(old_handlers)

    return stdout, stderr, "".join(combined_parts), exit_code


def _install_interrupt_handlers(process: subprocess.Popen[bytes]) -> dict[int, Any]:
    old_handlers: dict[int, Any] = {}

    def handle(signum: int, _frame: object) -> None:
        _terminate_wrapped_process(process)
        raise WrappedCommandInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (ValueError, OSError):
            old_handlers.pop(signum, None)
    return old_handlers


def _restore_interrupt_handlers(old_handlers: dict[int, Any]) -> None:
    for signum, handler in old_handlers.items():
        with suppress(ValueError, OSError):
            signal.signal(signum, handler)


def _terminate_wrapped_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.poll() is None:
        with suppress(OSError):
            process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    if os.name == "posix":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        with suppress(OSError):
            process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)


def _drain_process_pipes(
    process: subprocess.Popen[bytes],
    *,
    stdout: _CapturedStream,
    stderr: _CapturedStream,
    combined_parts: list[str],
) -> int:
    selector = selectors.DefaultSelector()
    streams: dict[int, IO[Any]] = {}
    captures: dict[int, _CapturedStream] = {}
    for stream, captured in ((process.stdout, stdout), (process.stderr, stderr)):
        if stream is None:
            continue
        fd = stream.fileno()
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
        streams[fd] = stream
        captures[fd] = captured

    exit_code: int | None = None
    drain_deadline: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if exit_code is None:
                polled = process.poll()
                if polled is not None:
                    exit_code = polled
                    drain_deadline = now + PIPE_DRAIN_GRACE_SECONDS
            elif drain_deadline is not None and now >= drain_deadline:
                break

            timeout = 0.1
            if drain_deadline is not None:
                timeout = max(0.0, min(timeout, drain_deadline - now))
            events = selector.select(timeout)
            for key, _mask in events:
                fd = key.fd
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                except OSError:
                    _unregister_and_close(selector, streams, fd)
                    continue
                if not chunk:
                    _unregister_and_close(selector, streams, fd)
                    continue
                fragment = captures[fd].append(chunk)
                if fragment:
                    combined_parts.append(fragment)
        if exit_code is None:
            exit_code = process.wait()
        return exit_code
    finally:
        for fd in list(streams):
            _unregister_and_close(selector, streams, fd)
        selector.close()


def _unregister_and_close(
    selector: selectors.BaseSelector,
    streams: dict[int, IO[Any]],
    fd: int,
) -> None:
    with suppress(KeyError, OSError, ValueError):
        selector.unregister(fd)
    stream = streams.pop(fd, None)
    if stream is not None and hasattr(stream, "close"):
        stream.close()
