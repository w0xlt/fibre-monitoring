# FIBRE Monitoring

Real-time monitoring solution for FIBRE block relay performance in Bitcoin nodes. Tracks FIBRE/UDP block propagation using eBPF tracing and Prometheus/Grafana visualization.

## Overview

This project captures and visualizes:
- Block reconstruction performance (timing, chunk efficiency)
- Block delivery tracking by mechanism and peer
- Block connection metrics (connection time, transaction count)
- Blocks sent via FIBRE/UDP

## Architecture

```
bitcoind (with USDT probes)
    ↓ [eBPF hooks]
fibre_exporter.py (:9435)
    ↓ [Prometheus scrapes]
Prometheus (:9090) → Grafana (:3000)
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
sudo apt install bpfcc-tools python3-bpfcc python3-venv bpftrace
```

### 2. Set up Python environment

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install prometheus_client pyyaml
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
| `--bitcoind`, `-b` | Yes* | - | Path to bitcoind binary |
| `--pid`, `-p` | No | Auto-detect | PID of running bitcoind |
| `--node-name`, `-n` | No | localhost | Label for this node in metrics |
| `--port` | No | 9435 | Prometheus metrics port |
| `--health-port` | No | 9436 | Health check endpoint port |
| `--config`, `-c` | No | - | Path to YAML config file |
| `--verbose`, `-v` | No | false | Log individual events |
| `--log-level` | No | INFO | Log level (DEBUG, INFO, WARNING, ERROR) |
| `--log-file` | No | - | Path to log file (logs to both stdout and file) |
| `--metrics-auth-username` | No | - | Basic auth username for /metrics endpoint |
| `--metrics-auth-password` | No | - | Basic auth password for /metrics endpoint |
| `--tls-cert` | No | - | Path to TLS certificate file (enables HTTPS) |
| `--tls-key` | No | - | Path to TLS private key file |
| `--version` | No | - | Show version and exit |

*Required unless provided via config file or environment variable.

### Configuration File

Instead of command-line arguments, you can use a YAML config file:

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your settings
sudo .venv/bin/python3 fibre_exporter.py --config config.yaml
```

Example `config.yaml`:
```yaml
bitcoind_path: /usr/local/bin/bitcoind
node_name: mynode
metrics_port: 9435
health_port: 9436
verbose: false
log_level: INFO

# Optional: Enable basic auth for /metrics endpoint
metrics_auth_username: prometheus
metrics_auth_password: changeme

# Optional: Enable TLS (see "TLS Encryption" section below)
tls_cert: /etc/fibre-exporter/node.crt
tls_key: /etc/fibre-exporter/node.key
```

### Environment Variables

All settings can also be configured via environment variables:

| Variable | Description |
|----------|-------------|
| `FIBRE_BITCOIND_PATH` | Path to bitcoind binary |
| `FIBRE_PID` | PID of running bitcoind |
| `FIBRE_NODE_NAME` | Node name label |
| `FIBRE_METRICS_PORT` | Prometheus metrics port |
| `FIBRE_HEALTH_PORT` | Health check port |
| `FIBRE_VERBOSE` | Enable verbose logging (true/false) |
| `FIBRE_LOG_LEVEL` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `FIBRE_METRICS_AUTH_USERNAME` | Basic auth username for /metrics endpoint |
| `FIBRE_METRICS_AUTH_PASSWORD` | Basic auth password for /metrics endpoint |
| `FIBRE_TLS_CERT` | Path to TLS certificate file |
| `FIBRE_TLS_KEY` | Path to TLS private key file |

Configuration priority (highest to lowest):
1. Command-line arguments
2. Environment variables
3. Config file
4. Defaults

### Verifying the Exporter

Once running, you should see output like:
```
2024-01-15 10:30:00 [INFO] fibre_exporter: FIBRE USDT Metrics Exporter v1.2.0
2024-01-15 10:30:00 [INFO] fibre_exporter: Configuration: bitcoind=/path/to/bitcoind node=mynode port=9435
2024-01-15 10:30:00 [INFO] fibre_exporter: Attached probe: udp:block_reconstructed
2024-01-15 10:30:00 [INFO] fibre_exporter: Attached probe: udp:block_send_start
2024-01-15 10:30:00 [INFO] fibre_exporter: Attached probe: udp:block_race_winner
2024-01-15 10:30:00 [INFO] fibre_exporter: Attached probe: validation:block_connected
2024-01-15 10:30:00 [INFO] fibre_exporter: Attached 4/4 probes successfully
2024-01-15 10:30:00 [INFO] fibre_exporter: Prometheus metrics: http://0.0.0.0:9435/metrics
2024-01-15 10:30:00 [INFO] fibre_exporter: Health check: http://0.0.0.0:9436/health
2024-01-15 10:30:00 [INFO] fibre_exporter: Waiting for FIBRE events...
```

With `--verbose` mode, individual events are also logged:
```
2024-01-15 10:35:12 [INFO] fibre_exporter: Block delivery: height=876543 mechanism=fibre_udp peer=1.2.3.4:8333
2024-01-15 10:35:12 [INFO] fibre_exporter: Block connected: height=876543 tx_count=2847 connection_time=12.3ms
```

Test endpoints:
```bash
# Metrics endpoint (without auth)
curl http://localhost:9435/metrics

# Metrics endpoint (with basic auth enabled)
curl -u prometheus:yourpassword http://localhost:9435/metrics

# Health check endpoint (no auth required)
curl http://localhost:9436/health
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
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRAFANA_ADMIN_PASSWORD` | Yes | - | Grafana admin password |
| `GRAFANA_ADMIN_USER` | No | admin | Grafana admin username |

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
    # If exporters have basic auth enabled
    basic_auth:
      username: 'prometheus'
      password: 'yourpassword'
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

## Securing the Metrics Endpoint

To prevent unauthorized access to your metrics, enable HTTP Basic Authentication on the exporter:

```bash
# Via environment variables
export FIBRE_METRICS_AUTH_USERNAME=prometheus
export FIBRE_METRICS_AUTH_PASSWORD=your_secure_password
sudo -E .venv/bin/python3 fibre_exporter.py --bitcoind /path/to/bitcoind

# Or via CLI arguments
sudo .venv/bin/python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --metrics-auth-username prometheus \
  --metrics-auth-password your_secure_password
```

Then configure Prometheus to authenticate when scraping (`docker/prometheus/prometheus.yml`):

```yaml
scrape_configs:
  - job_name: 'fibre'
    basic_auth:
      username: 'prometheus'
      password: 'your_secure_password'
    static_configs:
      - targets: ['host.docker.internal:9435']
```

**Note:** Both username and password must be set to enable authentication. The health check endpoint (`/health`) does not require authentication.

**Warning:** Basic auth without TLS sends credentials in plaintext over the wire. If Prometheus and the exporter are on different machines, enable TLS (see below) or restrict access via firewall rules.

### TLS Encryption

For multi-node setups where Prometheus scrapes exporters over the network, enable TLS to encrypt traffic (including basic auth credentials).

#### 1. Generate certificates

The included `generate-certs.sh` script creates a self-signed CA and per-node certificates with IP SANs:

```bash
# Pass all node IPs as arguments
./generate-certs.sh 192.168.1.10 192.168.1.11 192.168.1.12
```

This creates:
```
certs/
  ca.crt              # Give this to Prometheus
  ca.key              # Keep safe — only needed to sign new node certs
  192.168.1.10/
    node.crt           # Copy to that node
    node.key           # Copy to that node
  192.168.1.11/
    ...
```

The script is idempotent — re-running it reuses the existing CA and skips nodes that already have certs. To add a new node later, just run it again with the new IP:

```bash
./generate-certs.sh 192.168.1.13
```

#### 2. Distribute certificates

Copy each node's cert and key to that machine:

```bash
scp certs/192.168.1.10/node.{crt,key} user@192.168.1.10:/etc/fibre-exporter/
```

#### 3. Start the exporter with TLS

```bash
sudo .venv/bin/python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --tls-cert /etc/fibre-exporter/node.crt \
  --tls-key  /etc/fibre-exporter/node.key \
  --metrics-auth-username prometheus \
  --metrics-auth-password your_secure_password
```

The exporter will log:
```
TLS enabled: cert=/etc/fibre-exporter/node.crt
Prometheus metrics: https://0.0.0.0:9435/metrics
```

#### 4. Configure Prometheus

Mount the CA certificate into the Prometheus container by adding it to the volumes in `docker/docker-compose.yml`:

```yaml
services:
  prometheus:
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../certs/ca.crt:/etc/prometheus/fibre-ca.crt:ro   # TLS CA cert
      - prometheus_data:/prometheus
```

Then update `docker/prometheus/prometheus.yml` to scrape over HTTPS:

```yaml
scrape_configs:
  - job_name: 'fibre'
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/fibre-ca.crt
    basic_auth:
      username: 'prometheus'
      password: 'your_secure_password'
    static_configs:
      - targets: ['192.168.1.10:9435', '192.168.1.11:9435']
```

Restart the stack to apply:

```bash
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d
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
```

### Reset everything

```bash
docker compose down -v
docker compose up -d
```

## Running as a Systemd Service

For production deployments, use the provided systemd service file:

```bash
# Copy and edit the service file
sudo cp fibre-exporter.service /etc/systemd/system/
sudo nano /etc/systemd/system/fibre-exporter.service
# Adjust paths: WorkingDirectory, ExecStart --bitcoind path

# Reload systemd and enable the service
sudo systemctl daemon-reload
sudo systemctl enable fibre-exporter
sudo systemctl start fibre-exporter

# Check status
sudo systemctl status fibre-exporter
sudo journalctl -u fibre-exporter -f
```

## Metrics Reference

### FIBRE Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `fibre_blocks_reconstructed_total` | Counter | node | Total blocks reconstructed via FIBRE/UDP |
| `fibre_block_reconstruction_duration_seconds` | Histogram | node | Block reconstruction time |
| `fibre_block_chunks_used` | Histogram | node | Chunks used per block |
| `fibre_chunks_received_total` | Counter | node | Total chunks received |
| `fibre_chunks_used_total` | Counter | node | Total chunks used |
| `fibre_blocks_sent_total` | Counter | node | Total blocks sent via FIBRE/UDP |
| `fibre_block_deliveries_total` | Counter | node, mechanism, peer | Block deliveries by mechanism and peer |
| `fibre_last_block_height` | Gauge | node | Height of most recently processed block |

### Block Connection Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `bitcoin_blocks_connected_total` | Counter | node | Total blocks connected to the chain (all delivery paths) |
| `bitcoin_block_connection_duration_seconds` | Histogram | node | Time to connect a block to the chain |
| `bitcoin_block_tx_count` | Histogram | node | Number of transactions per connected block |

### Exporter Self-Monitoring Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `fibre_exporter_up` | Gauge | Whether the exporter is running (1 = up) |
| `fibre_exporter_start_time_seconds` | Gauge | Unix timestamp when exporter started |
| `fibre_exporter_events_processed_total` | Counter | Total events processed by type |
| `fibre_exporter_errors_total` | Counter | Total errors encountered by type |
| `fibre_exporter_probes_attached` | Gauge | Number of USDT probes attached |
| `fibre_exporter_info` | Info | Exporter version and configuration |

## File Structure

```
fibre-monitoring/
├── fibre_exporter.py              # Main metrics exporter
├── fibre-exporter.service         # Systemd service file
├── generate-certs.sh              # TLS certificate generator
├── config.example.yaml            # Example configuration file
├── README.md                       # This file
├── ARCHITECTURE.md                 # Architecture documentation
└── docker/
    ├── docker-compose.yml          # Container orchestration
    ├── .env.example                # Environment variables template
    ├── prometheus/
    │   └── prometheus.yml          # Prometheus scrape config
    └── grafana/
        └── provisioning/
            ├── datasources/
            │   └── prometheus.yml  # Prometheus datasource
            └── dashboards/
                ├── dashboard.yml   # Dashboard provisioner
                └── fibre-dashboard.json  # Pre-built dashboard
```
