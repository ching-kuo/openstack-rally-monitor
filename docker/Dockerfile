FROM python:3.14-slim

LABEL maintainer="infra-labs"
LABEL description="OpenStack Rally testing and monitoring tool"

ENV DEBIAN_FRONTEND=noninteractive
ENV RALLY_DATABASE_URL=sqlite:////rally/data/rally.db
ENV RESULTS_DIR=/results
ENV RALLY_CONFIG_DIR=/rally/config

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    curl \
    jq \
    cron \
    bash \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /rally/data /rally/config /results /exporter /scripts /dashboard

# Install Python dependencies
COPY exporter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Install Rally
RUN pip install --no-cache-dir \
    rally-openstack \
    python-openstackclient

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

EXPOSE 9101 8080

ENTRYPOINT ["/scripts/entrypoint.sh"]
