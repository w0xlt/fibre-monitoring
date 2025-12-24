#!/usr/bin/env python3
"""
FIBRE USDT Metrics Exporter for Prometheus
Captures block relay performance from bitcoind USDT tracepoints
"""

import sys
import time
import argparse
from bcc import BPF, USDT
from prometheus_client import start_http_server, Counter, Histogram, Gauge

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

def handle_event(cpu, data, size, node_name):
    event = bpf["events"].event(data)
    
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

# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='FIBRE USDT Metrics Exporter')
    parser.add_argument('--bitcoind', '-b', required=True, help='Path to bitcoind binary')
    parser.add_argument('--pid', '-p', type=int, help='PID of running bitcoind (optional)')
    parser.add_argument('--port', type=int, default=9435, help='Prometheus metrics port (default: 9435)')
    parser.add_argument('--node-name', '-n', default='localhost', help='Node name label')
    args = parser.parse_args()
    
    print(f"FIBRE USDT Metrics Exporter")
    print(f"  bitcoind: {args.bitcoind}")
    print(f"  port: {args.port}")
    print(f"  node: {args.node_name}")
    
    # Set up USDT probes
    usdt = USDT(path=args.bitcoind, pid=args.pid)
    
    try:
        usdt.enable_probe(probe="block_reconstructed", fn_name="trace_block_reconstructed")
        print("  ✓ Attached: udp:block_reconstructed")
    except Exception as e:
        print(f"  ✗ Failed to attach block_reconstructed: {e}")
    
    try:
        usdt.enable_probe(probe="block_send_start", fn_name="trace_block_send_start")
        print("  ✓ Attached: udp:block_send_start")
    except Exception as e:
        print(f"  ✗ Failed to attach block_send_start: {e}")
    
    try:
        usdt.enable_probe(probe="block_race_winner", fn_name="trace_race_winner")
        print("  ✓ Attached: udp:block_race_winner")
    except Exception as e:
        print(f"  ✗ Failed to attach block_race_winner: {e}")
    
    try:
        usdt.enable_probe(probe="block_race_time", fn_name="trace_race_time")
        print("  ✓ Attached: udp:block_race_time")
    except Exception as e:
        print(f"  ✗ Failed to attach block_race_time: {e}")
    
    # Load BPF program
    global bpf
    bpf = BPF(text=BPF_PROGRAM, usdt_contexts=[usdt])
    
    # Set up event callback
    def _handle_event(cpu, data, size):
        handle_event(cpu, data, size, args.node_name)
    
    bpf["events"].open_perf_buffer(_handle_event)
    
    # Start Prometheus HTTP server
    start_http_server(args.port)
    print(f"\nPrometheus metrics available at http://0.0.0.0:{args.port}/metrics")
    print("Waiting for FIBRE events...\n")
    
    # Poll for events
    try:
        while True:
            bpf.perf_buffer_poll(timeout=1000)
    except KeyboardInterrupt:
        print("\nShutting down...")

if __name__ == "__main__":
    main()