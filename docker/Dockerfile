FROM python:3.13-slim

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
RUN useradd -r -u 1500 -s /bin/false -d /rally -g 0 rally

# Install system deps and Python packages in a single layer.
# Build-only packages (gcc, libffi-dev, libssl-dev, python3-dev) are removed
# after pip install so they don't persist in the final image.
COPY exporter/requirements.txt /tmp/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev libssl-dev python3-dev \
        curl jq cron bash logrotate && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    pip install --no-cache-dir rally-openstack python-openstackclient && \
    apt-get remove --purge -y gcc libffi-dev libssl-dev python3-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Initialize Rally database
RUN rally db recreate

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
RUN mkdir -p /dashboard/vendor && \
    curl -fsSL "https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.1.1/files/inter-latin-wght-normal.woff2" \
        -o /dashboard/vendor/inter-variable.woff2 && \
    echo "f052ee44c3728dfd23aba8a4567150bc314d23903026fbb6ad089422c2df56af  /dashboard/vendor/inter-variable.woff2" | sha256sum -c - && \
    curl -fsSL "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" \
        -o /dashboard/vendor/chart.umd.min.js && \
    echo "206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e  /dashboard/vendor/chart.umd.min.js" | sha256sum -c -

RUN mkdir -p /rally/logs && chown -R rally:root /rally /results /exporter /dashboard /scripts && chmod -R g=u /rally /results /exporter /dashboard /scripts && chmod 0770 /rally/logs

EXPOSE 9101 8080

ENTRYPOINT ["/scripts/entrypoint.sh"]
