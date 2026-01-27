#!/usr/bin/env python3
"""
FIBRE USDT Metrics Exporter for Prometheus
Captures block relay performance from bitcoind USDT tracepoints
"""

import sys
import time
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from bcc import BPF, USDT
from prometheus_client import start_http_server, Counter, Histogram, Gauge, Info

# ============================================================================
# Prometheus Metrics
# ============================================================================

BLOCKS_RECONSTRUCTED = Counter(
    'fibre_blocks_reconstructed_total',
    'Total blocks reconstructed via FIBRE/UDP',
    ['node']
)

BLOCK_RECONSTRUCTION_TIME = Histogram(
    'fibre_block_reconstruction_duration_seconds',
    'Block reconstruction time',
    ['node'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

CHUNKS_USED = Histogram(
    'fibre_block_chunks_used',
    'Number of chunks used per block',
    ['node'],
    buckets=[1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
)

CHUNKS_RECEIVED = Counter(
    'fibre_chunks_received_total',
    'Total chunks received',
    ['node']
)

CHUNKS_USED_TOTAL = Counter(
    'fibre_chunks_used_total',
    'Total chunks used',
    ['node']
)

BLOCKS_SENT = Counter(
    'fibre_blocks_sent_total',
    'Total blocks sent via FIBRE/UDP',
    ['node']
)

RACE_WINS = Counter(
    'fibre_block_race_wins_total',
    'Block race wins by mechanism',
    ['node', 'mechanism']
)

RACE_LATENCY = Histogram(
    'fibre_block_race_latency_seconds',
    'Block race latency by mechanism',
    ['node', 'mechanism'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
)

RACE_MARGIN = Histogram(
    'fibre_race_margin_seconds',
    'Race margin (how much faster winner was)',
    ['node', 'winner'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0]
)

RACES_WITH_BOTH = Counter(
    'fibre_races_with_both_total',
    'Races where both mechanisms participated',
    ['node']
)

LAST_BLOCK_HEIGHT = Gauge(
    'fibre_last_block_height',
    'Height of most recently processed block',
    ['node']
)

# ============================================================================
# Exporter Self-Monitoring Metrics
# ============================================================================

EXPORTER_UP = Gauge(
    'fibre_exporter_up',
    'Whether the FIBRE exporter is running (1 = up, 0 = down)'
)

EXPORTER_START_TIME = Gauge(
    'fibre_exporter_start_time_seconds',
    'Unix timestamp when the exporter started'
)

EXPORTER_EVENTS_TOTAL = Counter(
    'fibre_exporter_events_processed_total',
    'Total number of events processed by the exporter',
    ['event_type']
)

EXPORTER_ERRORS_TOTAL = Counter(
    'fibre_exporter_errors_total',
    'Total number of errors encountered by the exporter',
    ['error_type']
)

EXPORTER_PROBES_ATTACHED = Gauge(
    'fibre_exporter_probes_attached',
    'Number of USDT probes successfully attached'
)

EXPORTER_INFO = Info(
    'fibre_exporter',
    'Information about the FIBRE exporter'
)

# ============================================================================
# BPF Program
# ============================================================================

BPF_PROGRAM = """
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(events);

enum event_type {
    EVENT_BLOCK_RECONSTRUCTED = 1,
    EVENT_BLOCK_SEND_START = 2,
    EVENT_RACE_WINNER = 3,
    EVENT_RACE_TIME = 4,
};

struct event_t {
    u32 type;
    s64 duration_us;
    u32 chunks_used;
    u32 chunks_recvd;
    s32 height;
    s64 udp_ns;
    s64 cmpct_ns;
    char winner[24];
};

int trace_block_reconstructed(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_RECONSTRUCTED;
    
    // Args: block_hash, src, chunks_used, chunks_recvd, num_peers, duration_us
    bpf_usdt_readarg(3, ctx, &event.chunks_used);
    bpf_usdt_readarg(4, ctx, &event.chunks_recvd);
    bpf_usdt_readarg(6, ctx, &event.duration_us);
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_block_send_start(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_SEND_START;
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_race_winner(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_RACE_WINNER;
    
    // Args: block_hash, height, winner, winner_peer
    bpf_usdt_readarg(2, ctx, &event.height);
    
    u64 winner_ptr;
    bpf_usdt_readarg(3, ctx, &winner_ptr);
    bpf_probe_read_user_str(&event.winner, sizeof(event.winner), (void *)winner_ptr);
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_race_time(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_RACE_TIME;
    
    // Args: block_hash, height, udp_ns, udp_peer, cmpct_ns, cmpct_peer
    bpf_usdt_readarg(2, ctx, &event.height);
    bpf_usdt_readarg(3, ctx, &event.udp_ns);
    bpf_usdt_readarg(5, ctx, &event.cmpct_ns);
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

# ============================================================================
# Event Handler
# ============================================================================

# ============================================================================
# Health Check Server
# ============================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks."""

    def do_GET(self):
        if self.path == '/health' or self.path == '/ready':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')

    def log_message(self, format, *args):
        # Suppress default logging
        pass


def start_health_server(port):
    """Start health check server in a background thread."""
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ============================================================================
# Event Handler
# ============================================================================

EVENT_TYPE_NAMES = {
    1: 'block_reconstructed',
    2: 'block_send_start',
    3: 'race_winner',
    4: 'race_time',
}

def handle_event(cpu, data, size, node_name):
    try:
        event = bpf["events"].event(data)
        event_type_name = EVENT_TYPE_NAMES.get(event.type, 'unknown')
        EXPORTER_EVENTS_TOTAL.labels(event_type=event_type_name).inc()

        if event.type == 1:  # BLOCK_RECONSTRUCTED
            duration_sec = event.duration_us / 1_000_000.0
            BLOCKS_RECONSTRUCTED.labels(node=node_name).inc()
            BLOCK_RECONSTRUCTION_TIME.labels(node=node_name).observe(duration_sec)
            CHUNKS_USED.labels(node=node_name).observe(event.chunks_used)
            CHUNKS_USED_TOTAL.labels(node=node_name).inc(event.chunks_used)
            CHUNKS_RECEIVED.labels(node=node_name).inc(event.chunks_recvd)

        elif event.type == 2:  # BLOCK_SEND_START
            BLOCKS_SENT.labels(node=node_name).inc()

        elif event.type == 3:  # RACE_WINNER
            winner_str = event.winner.decode('utf-8', errors='ignore').rstrip('\x00')
            if winner_str.startswith('F'):
                mechanism = 'fibre_udp'
            elif winner_str.startswith('B'):
                mechanism = 'bip152_cmpct'
            else:
                mechanism = 'other'

            RACE_WINS.labels(node=node_name, mechanism=mechanism).inc()
            LAST_BLOCK_HEIGHT.labels(node=node_name).set(event.height)

        elif event.type == 4:  # RACE_TIME
            LAST_BLOCK_HEIGHT.labels(node=node_name).set(event.height)

            if event.udp_ns >= 0:
                RACE_LATENCY.labels(node=node_name, mechanism='fibre_udp').observe(event.udp_ns / 1_000_000_000.0)

            if event.cmpct_ns >= 0:
                RACE_LATENCY.labels(node=node_name, mechanism='bip152_cmpct').observe(event.cmpct_ns / 1_000_000_000.0)

            if event.udp_ns >= 0 and event.cmpct_ns >= 0:
                RACES_WITH_BOTH.labels(node=node_name).inc()
                if event.udp_ns <= event.cmpct_ns:
                    margin = (event.cmpct_ns - event.udp_ns) / 1_000_000_000.0
                    RACE_MARGIN.labels(node=node_name, winner='fibre_udp').observe(margin)
                else:
                    margin = (event.udp_ns - event.cmpct_ns) / 1_000_000_000.0
                    RACE_MARGIN.labels(node=node_name, winner='bip152_cmpct').observe(margin)
    except Exception as e:
        EXPORTER_ERRORS_TOTAL.labels(error_type='event_processing').inc()
        print(f"Error processing event: {e}")

# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='FIBRE USDT Metrics Exporter')
    parser.add_argument('--bitcoind', '-b', required=True, help='Path to bitcoind binary')
    parser.add_argument('--pid', '-p', type=int, help='PID of running bitcoind (optional)')
    parser.add_argument('--port', type=int, default=9435, help='Prometheus metrics port (default: 9435)')
    parser.add_argument('--health-port', type=int, default=9436, help='Health check port (default: 9436)')
    parser.add_argument('--node-name', '-n', default='localhost', help='Node name label')
    args = parser.parse_args()

    # Set exporter info
    EXPORTER_INFO.info({
        'version': '1.0.0',
        'node_name': args.node_name,
        'bitcoind_path': args.bitcoind,
    })
    EXPORTER_START_TIME.set(time.time())

    print(f"FIBRE USDT Metrics Exporter")
    print(f"  bitcoind: {args.bitcoind}")
    print(f"  port: {args.port}")
    print(f"  health_port: {args.health_port}")
    print(f"  node: {args.node_name}")
    
    # Set up USDT probes
    usdt = USDT(path=args.bitcoind, pid=args.pid)

    probes = [
        ("block_reconstructed", "trace_block_reconstructed"),
        ("block_send_start", "trace_block_send_start"),
        ("block_race_winner", "trace_race_winner"),
        ("block_race_time", "trace_race_time"),
    ]

    attached_count = 0
    for probe_name, fn_name in probes:
        try:
            usdt.enable_probe(probe=probe_name, fn_name=fn_name)
            print(f"  ✓ Attached: udp:{probe_name}")
            attached_count += 1
        except Exception as e:
            print(f"  ✗ Failed to attach {probe_name}: {e}")

    if attached_count == 0:
        EXPORTER_UP.set(0)
        print("\nERROR: No USDT probes could be attached.")
        print("Possible causes:")
        print("  - bitcoind was not compiled with USDT tracepoint support")
        print("  - The specified PID is not a bitcoind process")
        print("  - Insufficient permissions (try running with sudo)")
        sys.exit(1)

    EXPORTER_PROBES_ATTACHED.set(attached_count)
    print(f"\n  {attached_count}/{len(probes)} probes attached successfully")
    
    # Load BPF program
    global bpf
    bpf = BPF(text=BPF_PROGRAM, usdt_contexts=[usdt])
    
    # Set up event callback
    def _handle_event(cpu, data, size):
        handle_event(cpu, data, size, args.node_name)
    
    bpf["events"].open_perf_buffer(_handle_event)

    # Start Prometheus HTTP server
    start_http_server(args.port)

    # Start health check server
    start_health_server(args.health_port)

    # Mark exporter as up
    EXPORTER_UP.set(1)

    print(f"\nPrometheus metrics available at http://0.0.0.0:{args.port}/metrics")
    print(f"Health check available at http://0.0.0.0:{args.health_port}/health")
    print("Waiting for FIBRE events...\n")
    
    # Poll for events
    try:
        while True:
            bpf.perf_buffer_poll(timeout=1000)
    except KeyboardInterrupt:
        EXPORTER_UP.set(0)
        print("\nShutting down...")

if __name__ == "__main__":
    main()