# Architecture

This document explains the architecture of the FIBRE Monitoring system, its components, and how they integrate to provide real-time observability of Bitcoin block propagation.

## Overview

The FIBRE Monitoring system is a metrics and logging pipeline that captures block relay performance data from a Bitcoin node and visualizes it through Grafana dashboards. It compares two block propagation mechanisms:

- **FIBRE/UDP**: Fast Internet Bitcoin Relay Engine - a UDP-based protocol for low-latency block propagation
- **BIP152 Compact Blocks**: The standard Bitcoin peer-to-peer compact block relay

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              HOST MACHINE                                   │
│                                                                             │
│  ┌─────────────────────┐                                                    │
│  │      bitcoind       │                                                    │
│  │  (USDT tracepoints) │                                                    │
│  └──────────┬──────────┘                                                    │
│             │ eBPF hooks                                                    │
│             ▼                                                               │
│  ┌─────────────────────┐         ┌─────────────────────────────────────┐    │
│  │  fibre_exporter.py  │◄────────│     Prometheus scrapes /metrics     │    │
│  │     (port 9435)     │         │            every 10s                │    │
│  └─────────────────────┘         └─────────────────────────────────────┘    │
│                                                     │                       │
│  ┌─────────────────────┐                            │                       │
│  │ ~/.bitcoin/debug.log│                            │                       │
│  └──────────┬──────────┘                            │                       │
│             │                                       │                       │
└─────────────┼───────────────────────────────────────┼───────────────────────┘
              │                                       │
              │ ┌─────────────────────────────────────┼─────────────────────┐
              │ │              DOCKER NETWORK         │                     │
              │ │                                     │                     │
              │ │  ┌──────────────┐                   │                     │
              │ │  │   Promtail   │                   │                     │
              └─┼──►  (log agent) │                   │                     │
                │  └──────┬───────┘                   │                     │
                │         │                           │                     │
                │         ▼                           ▼                     │
                │  ┌──────────────┐           ┌──────────────┐              │
                │  │     Loki     │           │  Prometheus  │              │
                │  │ (port 3100)  │           │ (port 9090)  │              │
                │  └──────┬───────┘           └──────┬───────┘              │
                │         │                          │                      │
                │         │    ┌─────────────────┐   │                      │
                │         └───►│     Grafana     │◄──┘                      │
                │              │   (port 3000)   │                          │
                │              └─────────────────┘                          │
                │                                                           │
                └───────────────────────────────────────────────────────────┘
```

## Components

### 1. bitcoind (Bitcoin Node)

The Bitcoin daemon with FIBRE patches and USDT (Userland Statically Defined Tracing) tracepoints compiled in.

**Role**: Source of all block relay events

**USDT Tracepoints exposed**:
| Tracepoint | Description |
|------------|-------------|
| `block_reconstructed` | Fired when a block is successfully reconstructed via FIBRE/UDP |
| `block_send_start` | Fired when the node starts sending a block to peers |
| `block_race_winner` | Fired when a block race is decided (FIBRE vs Compact Blocks) |
| `block_race_time` | Fired with timing data for both propagation mechanisms |

**Requirements**:
- Compiled with `--enable-usdt` configure flag
- Running on Linux kernel 4.4+ (for eBPF support)

---

### 2. FIBRE Exporter (`fibre_exporter.py`)

A Python application that captures tracepoint events using eBPF and exposes them as Prometheus metrics.

**Role**: Bridge between bitcoind tracepoints and Prometheus

**How it works**:

```
┌─────────────────────────────────────────────────────────────────┐
│                      fibre_exporter.py                          │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │  BCC/eBPF   │───►│   Event     │───►│ Prometheus Metrics  │  │
│  │   Probes    │    │  Handler    │    │  (Counter, Gauge,   │  │
│  └─────────────┘    └─────────────┘    │   Histogram)        │  │
│        ▲                               └──────────┬──────────┘  │
│        │                                          │             │
│        │ USDT                                     ▼             │
│        │ attach                          ┌───────────────┐      │
│  ┌─────┴─────┐                           │ HTTP Server   │      │
│  │ bitcoind  │                           │ /metrics      │      │
│  │  process  │                           │ (port 9435)   │      │
│  └───────────┘                           └───────────────┘      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Technology Stack**:

| Component | Purpose |
|-----------|---------|
| **BCC (BPF Compiler Collection)** | Python bindings for eBPF, used to attach to USDT tracepoints |
| **eBPF** | Linux kernel technology for safe, efficient tracing without kernel modules |
| **prometheus_client** | Python library to expose metrics in Prometheus format |

**Endpoints**:
- `GET /metrics` (port 9435) - Prometheus metrics endpoint (supports basic auth)
- `GET /health` (port 9436) - Health check endpoint

**Metrics exposed**:
```
# Block reconstruction
fibre_blocks_reconstructed_total{node="mynode"} 42
fibre_block_reconstruction_duration_seconds_bucket{...}
fibre_block_chunks_used_bucket{...}

# Race results
fibre_block_race_wins_total{node="mynode",mechanism="fibre_udp"} 35
fibre_block_race_wins_total{node="mynode",mechanism="bip152_cmpct"} 7
fibre_block_race_latency_seconds_bucket{...}

# Current state
fibre_last_block_height{node="mynode"} 876543
```

---

### 3. Prometheus

A time-series database that scrapes and stores metrics from the exporter.

**Role**: Metrics collection and storage

**How it works**:
```
┌────────────────────────────────────────────────────┐
│                    Prometheus                      │
│                                                    │
│  ┌────────────┐    ┌────────────┐    ┌──────────┐  │
│  │  Scraper   │───►│   TSDB     │◄───│  PromQL  │  │
│  │ (pull)     │    │ (storage)  │    │ (query)  │  │
│  └─────┬──────┘    └────────────┘    └────┬─────┘  │
│        │                                  │        │
│        │ HTTP GET /metrics                │        │
│        │ every 10s                        │        │
│        ▼                                  ▼        │
│  ┌───────────────┐                 ┌───────────┐   │
│  │ fibre_exporter│                 │  Grafana  │   │
│  │ :9435         │                 │  queries  │   │
│  └───────────────┘                 └───────────┘   │
│                                                    │
└────────────────────────────────────────────────────┘
```

**Key Concepts**:
- **Pull-based**: Prometheus actively fetches metrics from targets (vs push-based systems)
- **Scrape interval**: Configured to 10 seconds for FIBRE metrics
- **Retention**: 30 days of historical data (configurable)
- **PromQL**: Query language used by Grafana to retrieve and aggregate metrics

**Configuration** (`prometheus.yml`):
```yaml
scrape_configs:
  - job_name: 'fibre'
    scrape_interval: 10s
    basic_auth:
      username: 'prometheus'
      password: 'secret'
    static_configs:
      - targets: ['host.docker.internal:9435']
        labels:
          node: 'mynode'
```

---

### 4. Loki

A log aggregation system designed for efficiency and ease of use.

**Role**: Centralized log storage and querying

**How it works**:
```
┌─────────────────────────────────────────────────────┐
│                       Loki                          │
│                                                     │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────┐  │
│  │   Ingester  │───►│   Storage   │◄───│ LogQL   │  │
│  │             │    │  (chunks)   │    │ (query) │  │
│  └──────▲──────┘    └─────────────┘    └────┬────┘  │
│         │                                   │       │
│         │ push logs                         │       │
│         │                                   ▼       │
│  ┌──────┴──────┐                      ┌─────────┐   │
│  │  Promtail   │                      │ Grafana │   │
│  └─────────────┘                      └─────────┘   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**Key Concepts**:
- **Push-based**: Unlike Prometheus, Loki receives logs pushed by agents (Promtail)
- **Labels**: Logs are indexed by labels (e.g., `{job="bitcoin", filename="/var/log/bitcoin/debug.log"}`)
- **LogQL**: Query language similar to PromQL but for logs

---

### 5. Promtail

A log shipping agent that sends logs to Loki.

**Role**: Collect and forward Bitcoin debug logs

**How it works**:
```
┌─────────────────────────────────────────────────────────────┐
│                        Promtail                             │
│                                                             │
│  ┌─────────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  File Discovery │───►│   Tailer    │───►│   Pusher    │  │
│  │  (debug.log)    │    │  (follow)   │    │  (to Loki)  │  │
│  └─────────────────┘    └─────────────┘    └─────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Configuration** (`promtail.yml`):
```yaml
scrape_configs:
  - job_name: bitcoin
    static_configs:
      - targets: [localhost]
        labels:
          job: bitcoin
          __path__: /var/log/bitcoin/debug.log
```

---

### 6. Grafana

A visualization platform for metrics and logs.

**Role**: Dashboards, alerting, and data exploration

**Data Sources**:
| Source | Type | Purpose |
|--------|------|---------|
| Prometheus | Metrics | FIBRE performance metrics, race statistics |
| Loki | Logs | Bitcoin debug log exploration |

**Dashboard Features**:
- Block race win rates (FIBRE vs Compact Blocks)
- Reconstruction time histograms
- Latency percentiles (p50, p95, p99)
- Chunk efficiency metrics
- Real-time block height tracking

---

## Data Flow

### Metrics Flow (Real-time Performance Data)

```
1. Block Event Occurs
   bitcoind receives/sends a block
         │
         ▼
2. USDT Tracepoint Fires
   Kernel triggers the tracepoint with event data
         │
         ▼
3. eBPF Program Captures Event
   BPF program in kernel space copies data to perf buffer
         │
         ▼
4. Exporter Processes Event
   Python callback updates Prometheus metrics (counters, histograms)
         │
         ▼
5. Prometheus Scrapes Metrics
   HTTP GET /metrics every 10 seconds
         │
         ▼
6. Grafana Queries Prometheus
   PromQL queries aggregate and display data
         │
         ▼
7. Dashboard Renders Visualization
   Graphs, gauges, and tables update in real-time
```

### Logs Flow (Debug Information)

```
1. bitcoind Writes Log
   Debug message written to ~/.bitcoin/debug.log
         │
         ▼
2. Promtail Tails File
   Detects new lines, parses timestamps
         │
         ▼
3. Promtail Pushes to Loki
   Batched log entries with labels
         │
         ▼
4. Loki Indexes and Stores
   Chunks stored, labels indexed
         │
         ▼
5. Grafana Queries Loki
   LogQL queries filter and display logs
```

---

## Network Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Docker Network                              │
│                       (fibre-network)                               │
│                                                                     │
│  ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐      │
│  │ prometheus│   │   loki    │   │  promtail │   │  grafana  │      │
│  │   :9090   │   │   :3100   │   │   :9080   │   │   :3000   │      │
│  └─────┬─────┘   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘      │
│        │               │               │               │            │
│        └───────────────┴───────────────┴───────────────┘            │
│                              │                                      │
└──────────────────────────────┼──────────────────────────────────────┘
                               │
                    host.docker.internal
                               │
┌──────────────────────────────┼──────────────────────────────────────┐
│                         HOST MACHINE                                │
│                               │                                     │
│     ┌─────────────────────────┼─────────────────────────┐           │
│     │                         │                         │           │
│     ▼                         ▼                         ▼           │
│ ┌─────────┐           ┌──────────────┐          ┌─────────────┐     │
│ │bitcoind │           │fibre_exporter│          │ debug.log   │     │
│ │         │◄──────────│    :9435     │          │ (mounted)   │     │
│ └─────────┘   eBPF    └──────────────┘          └─────────────┘     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Port Summary**:
| Port | Service | Access |
|------|---------|--------|
| 3000 | Grafana | External (browser) |
| 9090 | Prometheus | External (optional) |
| 3100 | Loki | Internal only |
| 9435 | FIBRE Exporter | Host + Docker |
| 9436 | Exporter Health | Host only |

---

## Security Considerations

### Metrics Endpoint Authentication

The exporter supports HTTP Basic Authentication to prevent unauthorized access:

```
                    ┌─────────────────────┐
                    │     Prometheus      │
                    │                     │
                    │  Authorization:     │
                    │  Basic base64(u:p)  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   fibre_exporter    │
                    │                     │
                    │  ┌───────────────┐  │
                    │  │ Auth Check    │  │
                    │  │ (hmac.compare │  │
                    │  │  _digest)     │  │
                    │  └───────┬───────┘  │
                    │          │          │
                    │    ┌─────┴─────┐    │
                    │    ▼           ▼    │
                    │  200 OK     401     │
                    │  metrics   Unauth   │
                    └─────────────────────┘
```

### eBPF Security

- Requires root privileges (CAP_SYS_ADMIN or CAP_BPF)
- eBPF programs are verified by the kernel before execution
- No kernel module installation required

---

## Scaling Considerations

### Multi-Node Monitoring

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   Node A    │  │   Node B    │  │   Node C    │
│  bitcoind   │  │  bitcoind   │  │  bitcoind   │
│  exporter   │  │  exporter   │  │  exporter   │
│   :9435     │  │   :9435     │  │   :9435     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        │
                        ▼
              ┌─────────────────┐
              │   Prometheus    │
              │                 │
              │  scrape_configs:│
              │  - node_a:9435  │
              │  - node_b:9435  │
              │  - node_c:9435  │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │     Grafana     │
              │                 │
              │  Dashboard with │
              │  node selector  │
              └─────────────────┘
```

Each node runs its own exporter, and a central Prometheus instance scrapes all of them. The `node` label differentiates metrics in queries.

---

## Technology Summary

| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.8+ | Exporter runtime |
| BCC | Latest | eBPF Python bindings |
| eBPF | Kernel 4.4+ | Efficient kernel tracing |
| Prometheus | v3.x | Metrics storage |
| Grafana | v12.x | Visualization |
| Loki | v3.x | Log aggregation |
| Promtail | v3.x | Log shipping |
| Docker | 20.10+ | Container orchestration |
