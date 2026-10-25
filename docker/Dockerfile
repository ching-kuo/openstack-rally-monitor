# Base image pinned by multi-arch index digest (tag kept for readability) so a
# rebuild of the same commit cannot silently pick up a re-pushed tag.
# Dependabot's docker ecosystem bumps the digest as a reviewable PR.
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

LABEL maintainer="infra-labs"
LABEL description="OpenStack Rally testing and monitoring tool"

ENV DEBIAN_FRONTEND=noninteractive
ENV RALLY_DATABASE_URL=sqlite:////rally/data/rally.db
ENV RESULTS_DIR=/results
ENV RALLY_CONFIG_DIR=/rally/config

# Create directories
RUN mkdir -p /rally/data /rally/config /results /exporter /scripts /dashboard
# Pin UID to 1500 so it stays stable across base image rebuilds.
# Without -u, useradd -r picks the next available system UID which can differ
# between python:3.12-slim, 3.13-slim, etc., causing Permission denied on
# persistent Docker volumes after a rebuild. 1500 sits above the system-user
# range (0-999) used by slim base images while staying low enough to avoid
# bloating sparse lastlog/faillog files.
# HOME is its own rally-owned directory (Rally keeps ~/.rally/globals there)
# so /rally itself can stay root-owned; see the ownership step at the end.
RUN useradd -r -u 1500 -s /bin/false -d /home/rally -g 0 rally

# Install system deps and Python packages in a single layer.
# docker/requirements.txt is the pip-compile lockfile generated from
# docker/requirements.in, which holds the image's direct pins.
# Every direct and transitive package is pinned and SHA-256-hashed, and
# --require-hashes makes pip refuse any unpinned package or any downloaded
# file whose hash is not in the lock. Every package is installed from a wheel
# (--only-binary=:all:, so no C toolchain is needed) except murano-pkg-check,
# whose only wheel is py2-only: it is built from its hashed sdist with
# --no-build-isolation, so its build backend (pbr + setuptools) comes from the
# same hashed lock, installed first, instead of an unhashed isolated download.
# docker/requirements.in explains why the OpenStack toolchain in particular
# must stay pinned (patch_rally.py + scenario plugin names).
COPY docker/requirements.txt /tmp/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl jq cron bash logrotate && \
    awk '/^[^ #]/ { keep = ($0 ~ /^(pbr|setuptools)==/) } keep' \
        /tmp/requirements.txt > /tmp/requirements-build.txt && \
    pip install --no-cache-dir --require-hashes --only-binary=:all: \
        -r /tmp/requirements-build.txt && \
    pip install --no-cache-dir --require-hashes --only-binary=:all: \
        --no-binary=murano-pkg-check --no-build-isolation \
        -r /tmp/requirements.txt && \
    rm -rf /var/lib/apt/lists/* /tmp/requirements.txt /tmp/requirements-build.txt

# Copy application files
COPY rally/ /rally/config/
COPY scripts/ /scripts/
COPY exporter/ /exporter/
COPY dashboard/ /dashboard/

# Patch rally openstack password generation policies
RUN python3 /scripts/patch_rally.py

# Make scripts executable
RUN chmod +x /scripts/*.sh

COPY docker/logrotate.conf /etc/logrotate.d/rally-monitor
RUN chmod 0644 /etc/logrotate.d/rally-monitor

# Vendor frontend assets (no runtime CDN dependency)
# Inter variable font — SIL Open Font License 1.1 (redistribution permitted)
# Chart.js — MIT License
# SHA-256 hashes pinned at vendoring time; verified after download to detect supply-chain tampering.
# Dependabot cannot see these two pins: bump them by hand (check npm for a
# release at least two weeks old, read the Chart.js changelog against the line
# and bar charts in dashboard/app.js and dashboard/report.js, and recompute the
# hash from the downloaded file, cross-checked against the npm tarball).
RUN mkdir -p /dashboard/vendor && \
    curl -fsSL "https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/files/inter-latin-wght-normal.woff2" \
        -o /dashboard/vendor/inter-variable.woff2 && \
    echo "3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62  /dashboard/vendor/inter-variable.woff2" | sha256sum -c - && \
    curl -fsSL "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js" \
        -o /dashboard/vendor/chart.umd.min.js && \
    echo "48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a  /dashboard/vendor/chart.umd.min.js" | sha256sum -c -

# Ownership: code and config stay root:root and not group/world-writable.
# root runs entrypoint.sh (and sources entrypoint_config.sh/os_helpers.sh) on
# every start, so a rally-writable /scripts would let the rally user escalate
# to root on the next restart. Only the state directories are rally-owned
# (group root, which is rally's primary group). /rally stays root-owned so
# root's /rally/rally_env write cannot be redirected through a planted symlink.
# Bytecode is precompiled because the runtime user cannot write __pycache__.
RUN python3 -m compileall -q /exporter /dashboard /scripts && \
    chmod -R a+rX,go-w /rally /scripts /exporter /dashboard && \
    mkdir -p /rally/logs /home/rally && \
    chown rally:root /rally/data /rally/logs /results /home/rally && \
    chmod 0770 /rally/data /rally/logs /results && \
    chmod 0750 /home/rally

EXPOSE 9101 8080

ENTRYPOINT ["/scripts/entrypoint.sh"]
