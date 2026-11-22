# Blast Radius selector. The coverage map in map/ is built ahead of time
# (tools/make_map.sh) and committed. This build downloads sqlglot at the pinned
# commit (the selector reads the files a patch touches, and runs a few cheap
# tests against the patched tree) and installs the packages the tests were
# labelled with, on the image they were labelled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY kit/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app \
 && tar -xzf /tmp/sqlglot.tar.gz -C /app \
 && mv /app/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /app/sqlglot \
 && rm /tmp/sqlglot.tar.gz \
 && cd /app/sqlglot && python -m compileall -q sqlglot tests || true

COPY selector.py runner.py /app/
COPY map /app/map
RUN chmod -R a+rX /app

# The disk is read-only at run time: bytecode was compiled above.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 BR_DATA=/data BR_MAP=/app/map BR_SRC=/app/sqlglot SKIP_INTEGRATION=0 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
CMD ["python", "-s", "/app/selector.py"]
