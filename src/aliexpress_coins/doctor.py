"""Read-only pre-flight checks for the collector (issue #34).

`doctor` reports one line per check and exits non-zero when a check that should
pass fails. It is deliberately free of device side effects: it may start the
container's own adb server and connect a network serial, but it never taps,
launches, installs or changes anything on the phone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Status = Literal['ok', 'fail', 'skipped']


@dataclass(frozen=True)
class CheckResult:
    """The verdict of one pre-flight check."""

    name: str
    status: Status
    detail: str

    def render(self) -> str:
        """Format the check as the single line doctor prints."""
        return f'[{self.status}] {self.name}: {self.detail}'


@dataclass(frozen=True)
class CommandResult:
    """The subset of a completed process that doctor needs."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], float], CommandResult]
Which = Callable[[str], str | None]
OcrLoader = Callable[[], str]


def _run_command(args: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _first_line(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return 'no output'


def check_adb(*, which: Which = shutil.which, run: CommandRunner = _run_command) -> CheckResult:
    """Check that the adb executable is present and runnable."""
    path = which('adb')
    if path is None:
        return CheckResult('adb', 'fail', 'adb is not on PATH')

    try:
        result = run([path, 'version'], 5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult('adb', 'fail', f'{path}: {exc}')

    if result.returncode != 0:
        return CheckResult('adb', 'fail', f'{path} exited {result.returncode}: {_first_line(result.stderr)}')

    return CheckResult('adb', 'ok', f'{_first_line(result.stdout, result.stderr)} ({path})')


def _is_network_serial(serial: str) -> bool:
    return re.fullmatch(r'.+:\d+', serial) is not None


def _get_state(serial: str, run: CommandRunner) -> tuple[str | None, str]:
    try:
        result = run(['adb', '-s', serial, 'get-state'], 10.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or output != 'device':
        return None, output or f'adb exited {result.returncode}'
    return output, output


def check_device(serial: str | None, *, run: CommandRunner = _run_command) -> CheckResult:
    """Check the device named by AECOINS_DEVICE; unset means skip, not fail."""
    if not serial:
        return CheckResult('device', 'skipped', 'AECOINS_DEVICE is not set')

    state, detail = _get_state(serial, run)
    if state is None and _is_network_serial(serial):
        try:
            connected = run(['adb', 'connect', serial], 10.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult('device', 'fail', f'{serial}: adb connect failed: {exc}')

        connect_detail = _first_line(connected.stdout, connected.stderr)
        state, detail = _get_state(serial, run)
        if state is None:
            return CheckResult('device', 'fail', f'{serial}: {detail}; adb connect: {connect_detail}')

    if state is None:
        return CheckResult('device', 'fail', f'{serial}: {detail}')

    return CheckResult('device', 'ok', f'{serial} ({state})')


def _load_ocr() -> str:
    """Initialise RapidOCR and read a generated probe image.

    The probe exercises detection, orientation and recognition, which is what
    forces every bundled model to load. It uses no network and no device.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from rapidocr import RapidOCR

    image = Image.new('RGB', (320, 120), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((20, 25), 'COINS', fill='black', font=ImageFont.load_default(size=56))

    engine = RapidOCR(params={'Global.log_level': 'error'})
    result = engine(np.asarray(image))
    texts = getattr(result, 'txts', None) or ()
    if not texts:
        raise RuntimeError('OCR returned no text for the built-in probe image')
    return f'RapidOCR loaded and read {texts[0]!r} from its probe image'


def check_ocr(*, load: OcrLoader = _load_ocr) -> CheckResult:
    """Check that the OCR engine starts and its bundled models load."""
    try:
        detail = load()
    except Exception as exc:  # doctor must report any engine failure, not traceback
        return CheckResult('ocr', 'fail', f'{type(exc).__name__}: {exc}')
    return CheckResult('ocr', 'ok', detail)


def run_doctor(
    environment: Mapping[str, str],
    *,
    which: Which = shutil.which,
    run: CommandRunner = _run_command,
    load_ocr: OcrLoader = _load_ocr,
) -> int:
    """Run every check, print one line each, and return the process exit code."""
    serial = environment.get('AECOINS_DEVICE', '').strip() or None
    results = (
        check_adb(which=which, run=run),
        check_device(serial, run=run),
        check_ocr(load=load_ocr),
    )
    for result in results:
        print(result.render(), flush=True)

    return 1 if any(result.status == 'fail' for result in results) else 0
