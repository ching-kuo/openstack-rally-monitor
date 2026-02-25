# OpenStack Rally Monitor

Automated OpenStack cloud health testing using **Rally**, with a live dark-theme dashboard, Prometheus metrics, and Alertmanager integration for cleanup failure detection.

## Features

- **6 Core Services Tested** — Keystone, Nova, Neutron, Glance, Cinder, Swift
- **Lightweight Health Checks** — Read-only API probes every 15 minutes between heavy test runs
- **Prometheus Metrics** — Full metrics exposure for test results, SLA compliance, and orphaned resources
- **Orphan Detection & Cleanup** — Detects resources left behind by failed Rally cleanups (both `s_rally_*` and `c_rally_*` prefixes) and provides a manual purge tool
- **7-Day History** — Results retained with automatic pruning
- **Live Dashboard** — Dark-theme glassmorphism UI with status timelines, latency charts, and auto-refresh

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Docker: rally-monitor                    │
│                                                          │
│  ┌──────────┐  ┌───────────────┐  ┌─────────────────┐  │
│  │  Cron     │→│  run_tests.sh │→│  Rally Task      │  │
│  │  Schedule │  │  orchestrator │  │  Execution       │  │
│  └──────────┘  └──────┬────────┘  └─────────────────┘  │
│                       │                                   │
│                       ▼                                   │
│              ┌────────────────┐    ┌─────────────────┐  │
│              │  /results/     │    │  cleanup_monitor │  │
│              │  JSON files    │←──│  .sh             │  │
│              └───────┬────────┘    └─────────────────┘  │
│                      │                                    │
│            ┌─────────┴─────────┐                         │
│            ▼                   ▼                         │
│  ┌──────────────────┐  ┌──────────────┐                 │
│  │  rally_exporter   │  │  Dashboard   │                 │
│  │  :9101/metrics    │  │  :8080       │                 │
│  └────────┬─────────┘  └──────────────┘                 │
│           │                                               │
└───────────┼───────────────────────────────────────────────┘
            │
            ▼
┌──────────────────┐    ┌──────────────────┐
│  Prometheus      │───→│  Alertmanager    │
│  (your existing) │    │  (your existing) │
└──────────────────┘    └──────────────────┘
```

Three processes run inside a single container, managed by `scripts/entrypoint.sh`:

| Process | Port | Description |
|---------|------|-------------|
| `rally_exporter.py` | `9101` | Flask app; reads JSON from `/results/` and exposes Prometheus metrics |
| `http.server` (dashboard) | `8080` | Serves static HTML/JS/CSS dashboard backed by symlinked JSON files |
| Cron | — | Schedules Rally test runs and lightweight API health checks |

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/ching-kuo/openstack-rally-monitor.git
cd openstack-rally-monitor
cp env.sample .env
# Edit .env with your OpenStack credentials and Rally settings
vim .env
```

### 2. Build and Run

```bash
cd docker
docker-compose up -d --build
```

### 3. Access

| Service    | URL                           |
|------------|-------------------------------|
| Dashboard  | http://localhost:8080         |
| Metrics    | http://localhost:9101/metrics |
| Health     | http://localhost:9101/health  |

### 4. Configure Prometheus

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "rally-openstack-monitor"
    scrape_interval: 60s
    static_configs:
      - targets: ["<rally-monitor-host>:9101"]
```

Copy `prometheus/rally_alerts.yml` to your Prometheus rules directory and include it under `rule_files:`.

## Configuration

All settings are controlled via environment variables in `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `OS_AUTH_URL` | OpenStack Keystone endpoint (e.g. `https://openstack:5000/v3`) |
| `OS_USERNAME` | OpenStack username |
| `OS_PASSWORD` | OpenStack password |
| `OS_PROJECT_NAME` | OpenStack project |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `OS_USER_DOMAIN_NAME` | `Default` | User domain |
| `OS_PROJECT_DOMAIN_NAME` | `Default` | Project domain |
| `OS_REGION_NAME` | `RegionOne` | Region |
| `RALLY_SCHEDULE_INTERVAL` | `240` | Minutes between full Rally test runs |
| `HEALTH_CHECK_INTERVAL` | `15` | Minutes between lightweight API health checks |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days to keep results before pruning |
| `RALLY_NOVA_FLAVOR` | `m1.tiny` | Flavor name for Nova scenarios |
| `RALLY_NOVA_IMAGE` | `cirros-0.6.2-x86_64-disk` | Image name for Nova scenarios |
| `RALLY_NEUTRON_NETWORK_CIDR` | `10.99.0.0/24` | CIDR for Neutron test networks |
| `EXPORTER_PORT` | `9101` | Prometheus exporter port |
| `DASHBOARD_PORT` | `8080` | Dashboard port |

## Scenarios

Each service has a dedicated YAML scenario in `rally/scenarios/`.

| Service | Tests |
|---------|-------|
| **Keystone** | Create/delete users & projects; list services; multi-service auth validation |
| **Nova** | Boot/delete servers; list flavors & hypervisors; floating IP associate/dissociate |
| **Neutron** | Create/delete networks, subnets, ports, routers; security group management |
| **Glance** | Create/delete/list images |
| **Cinder** | Create/delete/list volumes; cloning; snapshots; QoS policy management |
| **Swift** | Container/object CRUD; object listing and download |

## Alert Rules

Defined in `prometheus/rally_alerts.yml`.

| Alert | Severity | Condition |
|-------|----------|-----------|
| `RallyCleanupFailure` | critical | Orphaned resources detected |
| `RallyOrphanedResourcesHigh` | warning | >5 orphaned resources |
| `RallyTestFailure` | warning | A scenario failed |
| `RallyServiceDown` | critical | Entire service is failing |
| `RallySLABreach` | warning | SLA criteria not met |
| `RallyStaleResults` | warning | No new results in >2 hours |
| `RallyOverallFailure` | critical | One or more services failing |

## Useful Commands

```bash
# Trigger a manual test run
docker exec rally-monitor /scripts/run_tests.sh

# Run a lightweight health check (read-only, non-destructive)
docker exec rally-monitor /scripts/health_check.sh

# Run orphan detection manually (read-only, updates Prometheus metrics)
docker exec rally-monitor /scripts/cleanup_monitor.sh /results/latest_summary.json

# Dry-run purge: list all orphaned resources without deleting anything
docker exec rally-monitor /scripts/purge_orphans.sh

# Purge orphaned resources (permanently deletes s_rally_* and c_rally_* resources)
docker exec rally-monitor /scripts/purge_orphans.sh --confirm

# View live logs
docker logs -f rally-monitor
docker exec rally-monitor tail -f /var/log/rally-tests.log
docker exec rally-monitor tail -f /var/log/health-check.log
```

> **Orphan prefixes:** Rally creates resources with two naming conventions — `s_rally_*` for scenario-created resources and `c_rally_*` for context-created resources (projects, users, networks). Both are detected and purged.

## Project Structure

```
openstack-rally-monitor/
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── rally/
│   ├── deployment.yaml
│   └── scenarios/
│       ├── keystone.yaml
│       ├── nova.yaml
│       ├── neutron.yaml
│       ├── glance.yaml
│       ├── cinder.yaml
│       └── swift.yaml
├── scripts/
│   ├── entrypoint.sh
│   ├── run_tests.sh
│   ├── health_check.sh
│   ├── cleanup_monitor.sh
│   ├── purge_orphans.sh
│   └── patch_rally.py
├── exporter/
│   ├── rally_exporter.py
│   └── requirements.txt
├── prometheus/
│   ├── prometheus.yml
│   └── rally_alerts.yml
├── dashboard/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── env.sample
```

## License

MIT
