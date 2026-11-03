# Sandbox for scoring coding tasks, which runs model-written code.
#
# The image holds only the benchmark code. Suites and run directories are
# mounted at run time, so private tasks never end up in an image layer.
# The isolation itself (no network, read-only filesystem, resource limits)
# comes from the `docker run` flags, so use the helper instead of a bare
# `docker run`:
#
#   python scripts/sandbox_score.py --suite tasks/examples --run-dir results/<run>
#
# The base is pinned by digest so every scoring run uses the same Python.
FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TEMP=/tmp \
    TMP=/tmp

# The benchmark has no third-party dependencies, so drop the packaging
# toolchain (pip, setuptools, wheel and the ensurepip copies). It isn't
# needed at run time, and its vendored packages carry most of the image's
# CVEs. Model-written code also can't install anything without it.
RUN python -m pip uninstall --yes --no-cache-dir pip setuptools wheel packaging \
 && rm -rf /usr/local/lib/python3.11/ensurepip /usr/local/bin/pip* /root/.cache \
 && useradd --create-home --uid 10001 scorer
WORKDIR /app
COPY benchmark/ /app/benchmark/
USER scorer

ENTRYPOINT ["python", "-m", "benchmark"]
CMD ["--help"]
