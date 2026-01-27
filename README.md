# FIBRE Monitoring

Real-time monitoring solution for FIBRE block relay performance in Bitcoin nodes. Compares FIBRE/UDP vs BIP152 compact block propagation using eBPF tracing and Prometheus/Grafana visualization.

## Overview

This project captures and visualizes:
- Block reconstruction performance metrics
- Block relay race winners (FIBRE/UDP vs Compact Blocks)
- Latency and reconstruction times
- Chunk usage efficiency

## Architecture

```
bitcoind (with USDT probes)
    ↓ [eBPF hooks]
fibre_exporter.py (:9435)
    ↓ [Prometheus scrapes]
Prometheus (:9090) → Grafana (:3000)
    ↑
Loki (:3100) ← Promtail (bitcoin debug.log)
```

## Prerequisites

- Linux with kernel 4.4+ (for eBPF support)
- bitcoind compiled with USDT tracepoint support
- Docker and Docker Compose
- Python 3.8+
- Root access (required for eBPF)

## Installation

### 1. Install BCC and Python bindings

```bash
sudo apt update
sudo apt install bpfcc-tools python3-bpfcc python3-venv
```

### 2. Set up Python environment

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install prometheus_client
```

## Running the Exporter

The exporter must run on the same machine as bitcoind with root privileges.

```bash
sudo .venv/bin/python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --pid $(pgrep bitcoind) \
  --node-name mynode \
  --port 9435
```

### Command-line Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--bitcoind`, `-b` | Yes | - | Path to bitcoind binary |
| `--pid`, `-p` | No | Auto-detect | PID of running bitcoind |
| `--node-name`, `-n` | No | localhost | Label for this node in metrics |
| `--port` | No | 9435 | Prometheus metrics port |

### Verifying the Exporter

Once running, you should see output like:
```
FIBRE USDT Metrics Exporter
  bitcoind: /path/to/bitcoind
  port: 9435
  node: mynode
  ✓ Attached: udp:block_reconstructed
  ✓ Attached: udp:block_send_start
  ✓ Attached: udp:block_race_winner
  ✓ Attached: udp:block_race_time

  4/4 probes attached successfully

Prometheus metrics available at http://0.0.0.0:9435/metrics
Waiting for FIBRE events...
```

Test metrics endpoint:
```bash
curl http://localhost:9435/metrics
```

## Running the Monitoring Stack

### 1. Configure Environment Variables

```bash
cd docker
cp .env.example .env
```

Edit `.env` and set the required variables:

```bash
# Required
GRAFANA_ADMIN_PASSWORD=your_secure_password

# Optional (with defaults)
GRAFANA_ADMIN_USER=admin
BITCOIN_DATA_DIR=/home/node/.bitcoin
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRAFANA_ADMIN_PASSWORD` | Yes | - | Grafana admin password |
| `GRAFANA_ADMIN_USER` | No | admin | Grafana admin username |
| `BITCOIN_DATA_DIR` | No | /home/node/.bitcoin | Bitcoin data directory for log collection |

### 2. Start the Stack

```bash
docker compose up -d
```

### 3. Access the Dashboard

| Service | URL | Credentials |
|---------|-----|-------------|
| Grafana | http://localhost:3000 | admin / (your password) |
| Prometheus | http://localhost:9090 | - |

The FIBRE dashboard is pre-loaded and available immediately.

## Multi-Node Monitoring

To monitor multiple FIBRE nodes, edit `docker/prometheus/prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'fibre'
    scrape_interval: 10s
    static_configs:
      - targets: ['host.docker.internal:9435']
        labels:
          node: 'local_node'
      - targets: ['192.168.1.10:9435']
        labels:
          node: 'remote_node_1'
      - targets: ['192.168.1.20:9435']
        labels:
          node: 'remote_node_2'
```

Then reload Prometheus:
```bash
docker compose restart prometheus
```

## Managing the Stack

```bash
# View logs
docker compose logs -f

# Stop
docker compose down

# Stop and remove data
docker compose down -v

# Restart
docker compose restart
```

## Troubleshooting

### Exporter fails to attach probes

```
ERROR: No USDT probes could be attached.
```

**Causes:**
- bitcoind not compiled with USDT support (requires `--enable-usdt` configure flag)
- Wrong PID specified
- Not running with root privileges

**Solutions:**
```bash
# Verify bitcoind has USDT probes
readelf -n /path/to/bitcoind | grep -A4 stapsdt

# Run with sudo
sudo .venv/bin/python3 fibre_exporter.py ...
```

### Prometheus can't reach exporter

```bash
# Test from Prometheus container
docker exec fibre-prometheus wget -qO- http://host.docker.internal:9435/metrics | head

# Check targets status
open http://localhost:9090/targets
```

### Docker stack fails to start

```
GRAFANA_ADMIN_PASSWORD must be set
```

Ensure you've created the `.env` file with required variables:
```bash
cd docker
cp .env.example .env
# Edit .env and set GRAFANA_ADMIN_PASSWORD
```

### View container logs

```bash
docker compose logs prometheus
docker compose logs grafana
docker compose logs loki
docker compose logs promtail
```

### Reset everything

```bash
docker compose down -v
docker compose up -d
```

## Metrics Reference

| Metric | Type | Description |
|--------|------|-------------|
| `fibre_blocks_reconstructed_total` | Counter | Total blocks reconstructed via FIBRE/UDP |
| `fibre_block_reconstruction_duration_seconds` | Histogram | Block reconstruction time |
| `fibre_block_chunks_used` | Histogram | Chunks used per block |
| `fibre_chunks_received_total` | Counter | Total chunks received |
| `fibre_chunks_used_total` | Counter | Total chunks used |
| `fibre_blocks_sent_total` | Counter | Total blocks sent |
| `fibre_block_race_wins_total` | Counter | Race wins by mechanism |
| `fibre_block_race_latency_seconds` | Histogram | Latency by mechanism |
| `fibre_race_margin_seconds` | Histogram | Race winning margin |
| `fibre_races_with_both_total` | Counter | Races where both mechanisms participated |
| `fibre_last_block_height` | Gauge | Height of most recently processed block |

## File Structure

```
fibre-monitoring/
├── fibre_exporter.py              # Main metrics exporter
├── README.md                       # This file
└── docker/
    ├── docker-compose.yml          # Container orchestration
    ├── .env.example                # Environment variables template
    ├── prometheus/
    │   └── prometheus.yml          # Prometheus scrape config
    ├── promtail/
    │   └── promtail.yml            # Log collection config
    └── grafana/
        └── provisioning/
            ├── datasources/
            │   ├── prometheus.yml  # Prometheus datasource
            │   └── loki.yml        # Loki datasource
            └── dashboards/
                ├── dashboard.yml   # Dashboard provisioner
                └── fibre-dashboard.json  # Pre-built dashboard
```
