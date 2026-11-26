# syntax=docker/dockerfile:1

# --- build stage -----------------------------------------------------------
# Only this stage runs pip. Nothing it downloads or caches survives into the
# shipped image except the installed packages themselves.
FROM python:3.14-alpine AS builder

WORKDIR /build
COPY app/requirements.txt .

# --only-binary=:all: means every dependency must arrive as a prebuilt wheel.
# They all ship musllinux wheels, so nothing is compiled; if a future
# dependency does not, the build fails loudly here instead of quietly needing
# gcc and headers. --prefix collects the result in one directory the runtime
# stage can copy whole.
RUN pip install --no-cache-dir --only-binary=:all: --prefix=/install -r requirements.txt

# --- osmium stage ----------------------------------------------------------
# The Map data screen builds the road graph inside the container, which needs
# osmium (app/requirements-build.txt). It has no Alpine (musllinux) wheel, so
# this stage compiles one from source. The compilers and headers stay in this
# stage: only the installed package reaches the runtime image, which adds the
# few shared libraries it links against (see below).
FROM python:3.14-alpine AS osmium

RUN apk add --no-cache build-base cmake boost-dev expat-dev bzip2-dev zlib-dev lz4-dev
WORKDIR /build
COPY app/requirements-build.txt .
# --no-deps: osmium's one dependency, requests, is only for its replication
# tools, which this app does not use. The import check proves the result runs.
RUN pip install --no-cache-dir --no-binary=osmium --no-deps --prefix=/install -r requirements-build.txt \
 && PYTHONPATH="$(echo /install/lib/python*/site-packages)" python -c "import osmium; osmium.FileProcessor; print('osmium ok')"

# --- runtime stage ---------------------------------------------------------
FROM python:3.14-alpine AS runtime

# ca-certificates: webhooks and map downloads call out over HTTPS, and
# without it that fails with an opaque certificate error.
#
# The uid/gid are fixed at 10001 rather than left to Alpine's system range.
# docker/run.sh grants this user the host's data directory, and this group
# read-write access to config.yaml and read access to secret.key; a number in the low hundreds is likely to belong to a
# real account on a Linux host, and that account would get the same access.
#
# pip is removed: nothing installs packages at runtime, and a package manager
# is one more tool for anyone who gets code execution in here.
#
# expat, bzip2, zlib, lz4 and the C++ runtime are what the osmium package
# links against: building a map reads the compressed .osm.pbf with them.
RUN apk add --no-cache ca-certificates expat libbz2 zlib lz4-libs libstdc++ \
 && addgroup -S -g 10001 app \
 && adduser -S -u 10001 -G app -h /app -s /sbin/nologin app \
 && python -m pip uninstall --yes --quiet pip

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=osmium /install /usr/local
COPY app ./app
COPY static ./static
# The language file and the defaults the app reads; never config.yaml or
# secret.key, which .dockerignore keeps out of the build context entirely.
COPY config/language.yaml config/defaults.yaml config/example_header.yaml ./config/
COPY config/api.json ./config/

# Version metadata, passed by docker/lib.sh so that
# `docker exec <name> python -m app version` says which build is running.
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown
ARG SKILL=
ARG SKILL_COMMIT=

# Three things, all only ever done here:
#
#   layout_pinned.py   the image's fixed layout -- config at /app/config/config.yaml
#                      with secret.key beside it, state in /app/data, listen
#                      on 0.0.0.0. This is what
#                      replaces ENV, and why `docker exec <name> python -m app
#                      manage ...` needs nothing extra (app/core/layout.py).
#   build_info.py      VERSION/COMMIT/BUILD_DATE for the version command, and
#                      SKILL/SKILL_COMMIT for /api/me (the image has no
#                      .skill-origin.yaml). The values arrive as arguments, so
#                      no quoting can break the generated file.
#   compileall         bytecode written now, because --read-only means the
#                      process can never write .pyc files later.
RUN cp app/core/layout_container.py app/core/layout_pinned.py \
 && python -c 'import sys; open("app/build_info.py", "w").write("VERSION = %r\nCOMMIT = %r\nBUILD_DATE = %r\nSKILL = %r\nSKILL_COMMIT = %r\n" % tuple(sys.argv[1:6]))' \
      "$VERSION" "$COMMIT" "$BUILD_DATE" "$SKILL" "$SKILL_COMMIT" \
 && python -m compileall -q app

# There is no ENV here, and no config.yaml or secret.key in the image
# (.dockerignore keeps them out -- an image layer is forever, and a key in
# one is everyone's). docker/run.sh mounts the project's config/config.yaml
# (writable) and config/secret.key (read-only) into /app/config at run time,
# so one image serves any deployment. Most settings apply while it runs --
# from the owner's Settings screen, or by editing config.yaml on the host;
# the rest need a restart, never a rebuild. /app/config stays root's, which
# is why the container rewrites config.yaml in place rather than by renaming
# a new file over it (see app/core/config_live.write_config_file).
#
# To run the shipped UI with different wording, put a language.yaml in the
# data directory (/app/data/language.yaml); it wins over the bundled one.

# Only the data directory is writable, and only by the app user. Everything
# else stays owned by root and read-only to the process -- so even a bug that
# lets someone run code in here cannot rewrite the app or the frontend.
# docker/run.sh mounts /app/data as a volume and runs the container with
# --read-only, which is what makes that guarantee real rather than a
# convention.
RUN mkdir -p /app/data && chown app:app /app/data

# Note: this ownership survives into a *named* volume, but a bind mount
# shadows it with the host's ownership. docker/run.sh uses a bind mount, so the
# database stays visible on the host for backups, and chowns that directory to
# this user before starting the container.

USER app

# No EXPOSE: the port is server.port in config.yaml, and docker/run.sh
# publishes it from there. A hardcoded EXPOSE would be stale documentation the
# first time someone changed it.
#
# `python -m app health` reads the same config.yaml for the port and asks
# /api/health, which touches nothing -- so a slow database query can never get
# a healthy container killed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "app", "health"]

# -u: unbuffered, so the first-run login link reaches `docker logs` the
# moment it is printed rather than whenever a buffer fills.
ENTRYPOINT ["python", "-u", "-m", "app"]
