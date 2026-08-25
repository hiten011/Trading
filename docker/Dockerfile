# =============================================================================
# Custom alert runner
# =============================================================================
# Built ON TOP of the official PKScreener image, so we inherit everything that
# is painful to install: TA-Lib (C library + python bindings), pandas/numpy
# pinned to compatible versions, PKScreener itself and PKDevTools.
#
# Our own indicator code lives in /app/custom and is ALSO bind-mounted by
# docker-compose, so you can edit an indicator and re-run without rebuilding.
# =============================================================================
ARG PKSCREENER_IMAGE=pkjmesra/pkscreener
ARG PKSCREENER_TAG=latest
FROM ${PKSCREENER_IMAGE}:${PKSCREENER_TAG}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_PYTHON_REFRESH=quiet \
    PKSCREENER_DOCKER=1 \
    TERM=xterm \
    PYTHONPATH=/app

# Extra python packages for your own indicators (empty by default).
COPY docker/requirements-custom.txt /tmp/requirements-custom.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements-custom.txt && \
    rm -f /tmp/requirements-custom.txt

WORKDIR /app
COPY custom/ /app/custom/
# Defaults for anyone running the image without the compose bind mounts.
COPY config/ /app/config/

# PKScreener resolves .env.dev and its results/ cache relative to the working
# directory, so run from the same place the upstream image does.
WORKDIR /PKScreener-main

ENTRYPOINT ["python3", "-m", "custom.runner"]
CMD ["--schedule"]
