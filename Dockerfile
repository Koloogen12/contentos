# Pinned to bookworm (Debian 12), NOT the floating slim tag.
# Playwright 1.49's `--with-deps` script tries to apt-get
# `ttf-ubuntu-font-family` + `ttf-unifont` which only exist in Debian
# 12 and earlier. Trixie (Debian 13, which is what plain `python:3.11-slim`
# now resolves to) renames those packages and the script fails with
# "Package not available". Pin and forget.
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Playwright stores Chromium under /root/.cache/ms-playwright by default;
    # pinning the path keeps it explicit and survives the COPY . . step
    # (which would otherwise mask /root if a host-mounted home dir is used).
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Зеркало apt. Пусто — берём deb.debian.org, как обычно.
#
# Прод-сервер (Selectel, Москва) не достаёт CDN Debian: DNS резолвится,
# соединение висит и apt-get update отдаёт Err по всем спискам, после чего
# install падает с «Unable to locate package». При этом обычные зеркала с
# того же сервера открываются. Поэтому источник вынесен в аргумент сборки:
# локально и в CI ничего не меняется, а деплой передаёт рабочее зеркало.
ARG APT_MIRROR=""

RUN if [ -n "$APT_MIRROR" ]; then \
        rm -f /etc/apt/sources.list.d/*; \
        { \
          echo "deb http://$APT_MIRROR bookworm main"; \
          echo "deb http://$APT_MIRROR bookworm-updates main"; \
        } > /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Install Chromium + the OS libs Playwright needs to run it headless.
# `--with-deps` pulls in the dozen apt packages (libnss3, libatk*, libcups2,
# libxkbcommon0, libdrm2, libxcomposite1, libxdamage1, libxrandr2, libgbm1,
# libpango-1.0-0, libcairo2, libasound2, fonts-liberation, etc.) — that's
# the whole reason we need it: without `--with-deps` the binary launches
# but crashes on the first `--no-sandbox` invocation with a missing-lib
# error that's nearly impossible to debug from the worker logs.
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
