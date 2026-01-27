# fibre-monitoring

### Install BCC and Python bindings

```bash
sudo apt update
sudo apt install bpfcc-tools python3-bpfcc
```

### Install virtual env
```bash
sudo apt install python3-venv
```

### Install virtual env (with access to system packages)
```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install prometheus_client
```

### Run metrics exporter
```bash
sudo .venv/bin/python3 fibre_exporter.py --bitcoind <bitcoind_path> --pid $(pgrep bitcoind) --node-name <node_name> --port 9435
```

  Required arguments:
  - --bitcoind / -b: Path to your bitcoind binary
  - --pid / -p: PID of running bitcoind (optional)
  - --node-name / -n: Label for this node (default: localhost)
  - --port: Prometheus metrics port (default: 9435)

### Run Grafana / Prometheus

First, configure environment variables:
```bash
cd docker
cp .env.example .env
# Edit .env and set GRAFANA_ADMIN_PASSWORD
```

Required environment variables:
- `GRAFANA_ADMIN_PASSWORD`: Grafana admin password (required)
- `GRAFANA_ADMIN_USER`: Grafana admin username (default: admin)
- `BITCOIN_DATA_DIR`: Path to bitcoin data directory (default: /home/node/.bitcoin)

Then start the stack:
```bash
docker compose up -d
```

  Access:
  - Grafana: http://localhost:3000
  - Prometheus: http://localhost:9090