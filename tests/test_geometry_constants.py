"""Every pixel-sized literal must be derived, not stored (issue #42).

The scan is deliberately blunt: any numeric literal of magnitude 16 or more in
the package is suspicious until it is justified. A timeout in seconds, a byte
preview, a luminance scale and an analysis-grid resolution are not device
pixels, so they are allowlisted with the reason they cannot be derived from the
frame. A new coordinate or swipe distance fails this test until its author
either derives it from the current frame/profile or adds a documented reason.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src' / 'aliexpress_coins'
MIN_SUSPICIOUS = 16


@dataclass(frozen=True, slots=True)
class _Allowed:
    count: int
    reason: str


# (relative file, literal) -> how many occurrences are justified and why.
ALLOWED: dict[tuple[str, int | float], _Allowed] = {
    ('control/controller.py', 300): _Allowed(1, 'default swipe duration in milliseconds, not pixels'),
    ('control/device.py', 200): _Allowed(1, 'adb error-preview byte limit'),
    ('control/popups.py', 30.0): _Allowed(1, 'dismissal wall-clock budget in seconds'),
    ('doctor.py', 20): _Allowed(1, 'OCR probe image draw coordinate, not a device coordinate'),
    ('doctor.py', 25): _Allowed(1, 'OCR probe image draw coordinate, not a device coordinate'),
    ('doctor.py', 56): _Allowed(1, 'OCR probe font size, not a device pixel constant'),
    ('doctor.py', 120): _Allowed(1, 'OCR probe canvas height, not a device geometry'),
    ('doctor.py', 320): _Allowed(1, 'OCR probe canvas width, not a device geometry'),
    ('perception/capture.py', 30.0): _Allowed(1, 'adb capture timeout in seconds'),
    ('perception/capture.py', 200): _Allowed(1, 'adb error-preview byte limit'),
    ('perception/geometry.py', 16): _Allowed(1, 'profile-id hash prefix length, not pixels'),
    ('perception/scroll.py', 255.0): _Allowed(1, '8-bit luminance denominator, not a pixel size'),
    ('perception/templates.py', 17): _Allowed(1, 'number of vertical scale steps, not pixels'),
    ('perception/state.py', 99.0): _Allowed(1, 'luminance percentile, not pixels'),
    ('perception/state.py', 100): _Allowed(1, 'popup-analysis grid resolution in cells, not pixels'),
    ('perception/state.py', 255.0): _Allowed(5, '8-bit luminance range, not a pixel size'),
    ('perception/state.py', 256): _Allowed(4, 'histogram bin count, not a pixel size'),
    ('perception/waits.py', 32): _Allowed(1, 'downscaled stability signature resolution in cells, not pixels'),
    ('perception/waits.py', 255.0): _Allowed(1, '8-bit luminance denominator, not a pixel size'),
    ('runner/config.py', 23): _Allowed(1, 'clock hour range, not pixels'),
    ('runner/config.py', 30): _Allowed(2, 'run-retention default in runs, not pixels'),
    ('runner/config.py', 59): _Allowed(1, 'clock minute range, not pixels'),
    ('runner/config.py', 180): _Allowed(1, 'schedule jitter in seconds, not pixels'),
    ('runner/config.py', 1200.0): _Allowed(1, 'run wall-clock budget in seconds'),
    ('runner/lock.py', 64): _Allowed(1, 'owner-record read limit factor, not a pixel size'),
    ('runner/lock.py', 384): _Allowed(1, 'owner file mode 0o600 as a decimal, not pixels'),
    ('runner/lock.py', 1024): _Allowed(1, 'owner-record read limit factor, not a pixel size'),
    ('runner/recording.py', 30.0): _Allowed(1, 'failure-recording rolling window in seconds, not pixels'),
    ('runner/recording.py', 120): _Allowed(1, 'failure-recording frame cap, not pixels'),
    ('runner/recording.py', 1024): _Allowed(2, 'MiB conversion for the failure-recording byte cap, not pixels'),
    ('runner/schedule.py', 300.0): _Allowed(1, 'run-lock wait grace after the budget, in seconds'),
    ('runner/device.py', 20.0): _Allowed(1, 'adb command timeout in seconds'),
    ('runner/device.py', 200): _Allowed(2, 'adb error-preview byte limit'),
    ('tasks/browse.py', 20.0): _Allowed(1, 'navigation timeout in seconds'),
    ('tasks/discovery.py', 30.0): _Allowed(1, 'boost-page timeout in seconds'),
    ('tasks/notifications.py', 20.0): _Allowed(1, 'navigation timeout in seconds'),
    ('tasks/quiz.py', 20.0): _Allowed(1, 'navigation timeout in seconds'),
    ('tasks/search.py', 20.0): _Allowed(1, 'navigation timeout in seconds'),
    ('tasks/surprise.py', 20.0): _Allowed(1, 'navigation timeout in seconds'),
    ('tools/capture_frames.py', 35): _Allowed(1, 'manifest-only default SDK, not a pixel coordinate'),
    ('tools/capture_frames.py', 240): _Allowed(1, 'manifest-only default density, not a pixel coordinate'),
}


def _numeric_literals(path: Path) -> Counter[int | float]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    values: Counter[int | float] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        if not isinstance(node.value, (int, float)):
            continue
        if abs(node.value) >= MIN_SUSPICIOUS:
            values[node.value] += 1
    return values


def test_no_absolute_pixel_constant_outside_the_justified_allowlist() -> None:
    unexpected: list[str] = []
    for path in sorted(PACKAGE.rglob('*.py')):
        relative = path.relative_to(PACKAGE).as_posix()
        for value, count in sorted(_numeric_literals(path).items()):
            allowed = ALLOWED.get((relative, value))
            if allowed is None:
                unexpected.append(f'{relative}: {value!r} x{count} is not justified')
            elif count > allowed.count:
                unexpected.append(
                    f'{relative}: {value!r} x{count} exceeds the {allowed.count} justified '
                    f'occurrence(s) ({allowed.reason})',
                )

    assert not unexpected, (
        'pixel-sized constants must be derived from the frame or a device profile; '
        'if a literal cannot be derived, add it to ALLOWED with a reason:\n' + '\n'.join(unexpected)
    )
