"""One scripted adb transport for every test (issue #36).

`ScriptedAdbRunner` implements the production `AdbRunner` seam: real clients
(`AdbSnapshotter`, `AdbInputDevice`, `AdbDevice`, the notification probe) are
constructed around it, so their argv construction and parsing run unchanged
while no process is ever spawned. There is deliberately no fallback to the real
transport: an unscripted call raises `AssertionError` naming the argv instead of
quietly reaching a phone.

Three ways to script a run:

* a queue of results, one per call;
* a responder, a callable from argv to a result (or `None` to fall through);
* a transcript recorded from a real session with `RecordingAdbRunner`, replayed
  with `ScriptedAdbRunner.from_transcript()`. The transcript records argv and
  exit status, so a replay also checks that the client still asks for the same
  command.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aliexpress_coins.transport import AdbResult, AdbRunner

__all__ = [
    'RecordingAdbRunner',
    'Responder',
    'ScriptedAdbRunner',
    'fail',
    'ok',
]

Responder = Callable[[tuple[str, ...]], AdbResult | Exception | None]
"""Answer one argv, or return ``None`` to fall through to the queued result."""


def ok(text: str = '') -> AdbResult:
    """A successful adb result whose stdout is ``text``."""
    return AdbResult(0, text.encode(), b'')


def fail(text: str = 'failed', *, returncode: int = 1) -> AdbResult:
    """A failed adb result whose stderr is ``text``."""
    return AdbResult(returncode, b'', text.encode())


@dataclass(frozen=True, slots=True)
class _Entry:
    """One queued result, optionally pinned to the argv that produced it."""

    result: AdbResult | Exception
    argv: tuple[str, ...] | None = None


class ScriptedAdbRunner:
    """The test-side implementation of the shared adb transport seam."""

    def __init__(
        self,
        *results: AdbResult | Exception,
        responder: Responder | None = None,
        entries: Iterable[_Entry] | None = None,
    ) -> None:
        self._queue = list(entries) if entries is not None else [_Entry(result) for result in results]
        self._responder = responder
        self.calls: list[tuple[str, ...]] = []
        """Every argv received, in order, for assertions about the real client."""

        self.timeouts: list[float] = []
        """Every timeout received, so a client's bound stays asserted."""

    @classmethod
    def responding(cls, responder: Responder) -> ScriptedAdbRunner:
        """Answer calls through a predicate instead of a positional queue."""
        return cls(responder=responder)

    @classmethod
    def from_transcript(cls, path: Path | str) -> ScriptedAdbRunner:
        """Load a transcript written by :class:`RecordingAdbRunner`."""
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        entries = [
            _Entry(
                AdbResult(
                    returncode=int(call['returncode']),
                    stdout=_decode(call, 'stdout'),
                    stderr=_decode(call, 'stderr'),
                ),
                argv=tuple(str(part) for part in call['argv']),
            )
            for call in payload['calls']
        ]
        return cls(entries=entries)

    @property
    def remaining(self) -> int:
        """Scripted calls that have not been consumed."""
        return len(self._queue)

    def __call__(self, args: Sequence[str], timeout: float) -> AdbResult:
        argv = tuple(args)
        if timeout <= 0:
            raise AssertionError(f'the scripted adb runner was given a non-positive timeout: {timeout!r}')
        self.calls.append(argv)
        self.timeouts.append(timeout)

        result = self._responder(argv) if self._responder is not None else None
        if result is None:
            result = self._next(argv)

        if isinstance(result, Exception):
            raise result
        return result

    def _next(self, argv: tuple[str, ...]) -> AdbResult | Exception:
        if not self._queue:
            raise AssertionError(
                f'the scripted adb runner has no response for {argv!r}; '
                f'{len(self.calls)} calls were made and the transcript is exhausted. '
                'A test must never fall through to a real device.',
            )
        entry = self._queue.pop(0)
        if entry.argv is not None and entry.argv != argv:
            raise AssertionError(
                f'the transcript expected {entry.argv!r} but the client sent {argv!r}',
            )
        return entry.result


class RecordingAdbRunner:
    """Wrap a real runner and write every call to a replayable transcript.

    Recording is deliberately a separate object: a test that constructs the
    ``ScriptedAdbRunner`` cannot accidentally have a real runner behind it.
    """

    def __init__(self, inner: AdbRunner, path: Path | str) -> None:
        self._inner = inner
        self._path = Path(path)
        self._calls: list[dict[str, object]] = []

    @property
    def path(self) -> Path:
        """The transcript path written after every call."""
        return self._path

    def __call__(self, args: Sequence[str], timeout: float) -> AdbResult:
        result = self._inner(args, timeout)
        self._calls.append(
            {
                'argv': list(args),
                'returncode': result.returncode,
                **_encode('stdout', result.stdout),
                **_encode('stderr', result.stderr),
            },
        )
        self._write()
        return result

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': 1, 'calls': self._calls}
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _encode(field: str, value: bytes) -> dict[str, str]:
    try:
        return {field: value.decode('utf-8')}
    except UnicodeDecodeError:
        return {f'{field}_b64': base64.b64encode(value).decode('ascii')}


def _decode(call: dict[str, object], field: str) -> bytes:
    encoded = call.get(f'{field}_b64')
    if encoded is not None:
        return base64.b64decode(str(encoded))
    return str(call.get(field, '')).encode()
