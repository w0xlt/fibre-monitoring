# FIBRE Monitoring Docker Setup

Complete Docker Compose setup for monitoring FIBRE block relay with Prometheus and Grafana.

## Quick Start

### 1. Make sure your FIBRE exporter is running

```bash
# On the host machine, run the Python exporter
sudo python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --pid $(pgrep bitcoind) \
  --node-name mynode \
  --port 9435
```

### 2. Start Prometheus and Grafana

```bash
cd docker
docker compose up -d
```

### 3. Access the Dashboard

- **Grafana**: http://localhost:3000
  - Username: `admin`
  - Password: `fibre123`
  - Dashboard is pre-loaded!

- **Prometheus**: http://localhost:9090

## Services

| Service | Port | Description |
|---------|------|-------------|
| Prometheus | 9090 | Metrics storage and queries |
| Grafana | 3000 | Visualization dashboard |
| FIBRE Exporter | 9435 | Runs on host (not in Docker) |

## File Structure

```
docker/
├── docker-compose.yml          # Container orchestration
├── prometheus/
│   └── prometheus.yml          # Prometheus scrape config
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── prometheus.yml  # Auto-configure Prometheus datasource
        └── dashboards/
            ├── dashboard.yml   # Dashboard provisioner config
            └── fibre-dashboard.json  # The actual dashboard
```

## Managing Containers

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Stop and remove volumes (reset data)
docker compose down -v

# Restart
docker compose restart
```

## Adding More Nodes

Edit `prometheus/prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'fibre'
    static_configs:
      - targets:
          - 'host.docker.internal:9435'  # Local node
          - '192.168.1.10:9435'           # Remote node 1
          - '192.168.1.11:9435'           # Remote node 2
        labels:
          network: 'mainnet'
```

Then reload Prometheus:
```bash
docker compose restart prometheus
```

## Troubleshooting

### Check if Prometheus can reach the exporter
```bash
# From inside Prometheus container
docker exec fibre-prometheus wget -qO- http://host.docker.internal:9435/metrics | head

# Check targets in Prometheus UI
open http://localhost:9090/targets
```

### View container logs
```bash
docker compose logs prometheus
docker compose logs grafana
```

### Reset everything
```bash
docker compose down -v
docker compose up -d
```
