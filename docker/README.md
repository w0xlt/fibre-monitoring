# FIBRE Monitoring Docker Setup

Complete Docker Compose setup for monitoring FIBRE block relay with Prometheus and Grafana.

## Quick Start

### 1. Make sure your FIBRE exporter is running

```bash
# On the host machine, run the Python exporter
sudo .venv/bin/python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --pid $(pgrep bitcoind) \
  --node-name mynode \
  --port 9435
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and set the required password:

```bash
GRAFANA_ADMIN_PASSWORD=your_secure_password

# Optional
GRAFANA_ADMIN_USER=admin
```

### 3. Start Prometheus and Grafana

```bash
docker compose up -d
```

### 4. Access the Dashboard

- **Grafana**: http://localhost:3000
  - Username: `admin` (or your configured user)
  - Password: (your configured password)
  - Dashboard is pre-loaded!

- **Prometheus**: http://localhost:9090

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRAFANA_ADMIN_PASSWORD` | Yes | - | Grafana admin password |
| `GRAFANA_ADMIN_USER` | No | admin | Grafana admin username |

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
├── .env.example                # Environment variables template
├── prometheus/
│   └── prometheus.yml          # Prometheus scrape config
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── prometheus.yml  # Prometheus datasource
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
      - targets: ['host.docker.internal:9435']
        labels:
          node: 'local_node'
      - targets: ['192.168.1.10:9435']
        labels:
          node: 'remote_node_1'
      - targets: ['192.168.1.11:9435']
        labels:
          node: 'remote_node_2'
```

Then reload Prometheus:
```bash
docker compose restart prometheus
```

## Troubleshooting

### Docker stack fails to start

```
GRAFANA_ADMIN_PASSWORD must be set
```

Ensure you've created the `.env` file:
```bash
cp .env.example .env
# Edit .env and set GRAFANA_ADMIN_PASSWORD
```

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
