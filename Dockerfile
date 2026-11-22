# syntax=docker/dockerfile:1
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/collector \
    TZ=UTC

# Runtime only: adb and its udev rules, tzdata for local-day boundaries, tini
# as PID 1, and the shared libraries OpenCV/onnxruntime load. No compiler or
# build headers are installed, and no distribution package is built locally.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adb \
        tini \
        tzdata \
        libgl1 \
        libglib2.0-0t64 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The RapidOCR wheel carries its PP-OCRv6 small models. --only-binary refuses
# a silent fallback to compiling a dependency from source.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --requirement requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY specs ./specs
COPY assets ./assets
# The popup corpus validates every reviewed fixture path when it loads. These
# are masked, account-data-free frames; keeping them makes the image self-check.
COPY tests/data/popups ./tests/data/popups
RUN python -m pip install --no-cache-dir --no-deps . \
    && rm -rf /app/build /app/src/*.egg-info

# Fixed uid/gid. The run and state directories exist before the named volumes
# are attached, so Docker initialises those volumes with collector ownership.
RUN groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid 10001 --home-dir /home/collector --shell /usr/sbin/nologin collector \
    && mkdir -p /app/runs /app/state \
    && chown -R 10001:10001 /app /home/collector

USER 10001:10001

ENV AECOINS_RUNS_DIR=/app/runs \
    AECOINS_STATE_DIR=/app/state

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "aliexpress_coins", "run", "--schedule"]
