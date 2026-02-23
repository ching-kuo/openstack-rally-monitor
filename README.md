# Rally OpenStack Monitor

Automated OpenStack cloud testing tool using **Rally** with a read-only dashboard, Prometheus metrics exporter, and Alertmanager integration for cleanup failure monitoring.

![Architecture Diagram]

## Features

- **7 Core Services Tested**: Keystone, Nova, Neutron, Glance, Cinder, Swift, Placement
- **Automated Scheduling**: Cron-based test runs inside Docker container
- **Prometheus Metrics**: Full metrics exposure for all test results and cleanup status
- **Cleanup Monitoring**: Detects orphaned Rally resources and alerts admins
- **7-Day History**: Stores results with automatic pruning
- **Dark-Theme Dashboard**: Glassmorphism UI with green/red status timeline, Charts, auto-refresh

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
│              │  JSON output   │←──│  .sh             │  │
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

## Quick Start

### 1. Clone and Configure

```bash
cd infra-labs-openstack-monitor
cp env.sample .env
# Edit .env with your OpenStack credentials
vim .env
```

### 2. Build and Run

```bash
cd docker
docker-compose up -d --build
```

### 3. Access

| Service     | URL                          |
|-------------|------------------------------|
| Dashboard   | http://localhost:8080         |
| Metrics     | http://localhost:9101/metrics |
| Health      | http://localhost:9101/health  |
| API Results | http://localhost:9101/api/results |
| API History | http://localhost:9101/api/history |

### 4. Configure Prometheus

Add to your existing `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "rally-openstack-monitor"
    scrape_interval: 60s
    static_configs:
      - targets: ["<rally-monitor-host>:9101"]
```

Copy `prometheus/rally_alerts.yml` to your Prometheus rules directory.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OS_AUTH_URL` | — | OpenStack Keystone URL |
| `OS_USERNAME` | — | OpenStack username |
| `OS_PASSWORD` | — | OpenStack password |
| `OS_PROJECT_NAME` | — | OpenStack project |
| `OS_USER_DOMAIN_NAME` | `Default` | User domain |
| `OS_PROJECT_DOMAIN_NAME` | `Default` | Project domain |
| `OS_REGION_NAME` | `RegionOne` | Region |
| `RALLY_SCHEDULE_INTERVAL` | `60` | Minutes between runs |
| `RALLY_RESULTS_RETENTION_DAYS` | `7` | Days to keep results |
| `RALLY_NOVA_FLAVOR` | `m1.tiny` | Flavor for Nova tests |
| `RALLY_NOVA_IMAGE` | `cirros-0.6.2-x86_64-disk` | Image for Nova tests |
| `EXPORTER_PORT` | `9101` | Prometheus exporter port |
| `DASHBOARD_PORT` | `8080` | Dashboard port |

## Prometheus Alert Rules

| Alert | Severity | Description |
|-------|----------|-------------|
| `RallyCleanupFailure` | critical | Orphaned resources detected |
| `RallyOrphanedResourcesHigh` | warning | >5 orphaned resources |
| `RallyTestFailure` | warning | A scenario failed |
| `RallyServiceDown` | critical | Entire service failing |
| `RallySLABreach` | warning | SLA criteria not met |
| `RallyStaleResults` | warning | No results in >2 hours |
| `RallyOverallFailure` | critical | One or more services failing |

## Scenarios

### Keystone
- Create/delete users, projects
- List services
- Multi-service auth validation (Glance, Nova, Cinder, Neutron)

### Nova
- Boot/delete server
- Boot/list servers
- List flavors and hypervisors
- Floating IP associate/dissociate

### Neutron
- Create/delete networks, subnets, ports, routers
- Security group management

### Glance
- Create/delete/list images

### Cinder
- Create/delete/list volumes
- Volume cloning, snapshots
- QoS policy management

### Swift
- Container/object CRUD
- Object listing and download

### Placement
- Resource providers and classes availability

## Manual Test Run

```bash
docker exec rally-monitor /scripts/run_tests.sh
```

## Project Structure

```
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
│       ├── swift.yaml
│       └── placement.yaml
├── scripts/
│   ├── run_tests.sh
│   ├── cleanup_monitor.sh
│   └── entrypoint.sh
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
├── env.sample
└── README.md
```

## License

Internal use — Infrastructure Labs
