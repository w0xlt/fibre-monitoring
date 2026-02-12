#!/usr/bin/env python3
"""
FIBRE USDT Metrics Exporter for Prometheus
Captures block relay performance from bitcoind USDT tracepoints
"""

from __future__ import annotations

import argparse
import base64
import hmac
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from bcc import BPF, USDT
from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server, REGISTRY

# ============================================================================
# Version
# ============================================================================

__version__ = "1.2.0"

# ============================================================================
# Configuration
# ============================================================================


@dataclass
class ExporterConfig:
    """Configuration for the FIBRE exporter."""

    bitcoind_path: str
    pid: Optional[int] = None
    node_name: str = "localhost"
    metrics_port: int = 9435
    health_port: int = 9436
    verbose: bool = False
    log_level: str = "INFO"
    log_file: Optional[str] = None
    # Basic auth for metrics endpoint
    metrics_auth_username: Optional[str] = None
    metrics_auth_password: Optional[str] = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ExporterConfig:
        """Create config from parsed arguments."""
        return cls(
            bitcoind_path=args.bitcoind,
            pid=args.pid,
            node_name=args.node_name,
            metrics_port=args.port,
            health_port=args.health_port,
            verbose=args.verbose,
            log_level=args.log_level,
            log_file=getattr(args, 'log_file', None),
            metrics_auth_username=getattr(args, 'metrics_auth_username', None),
            metrics_auth_password=getattr(args, 'metrics_auth_password', None),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> ExporterConfig:
        """Load config from YAML file."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls(
            bitcoind_path=data.get("bitcoind_path", data.get("bitcoind")),
            pid=data.get("pid"),
            node_name=data.get("node_name", "localhost"),
            metrics_port=data.get("metrics_port", data.get("port", 9435)),
            health_port=data.get("health_port", 9436),
            verbose=data.get("verbose", False),
            log_level=data.get("log_level", "INFO"),
            log_file=data.get("log_file"),
            metrics_auth_username=data.get("metrics_auth_username"),
            metrics_auth_password=data.get("metrics_auth_password"),
        )

    @classmethod
    def from_env(cls, base_config: Optional[ExporterConfig] = None) -> ExporterConfig:
        """Override config from environment variables."""
        if base_config is None:
            base_config = cls(bitcoind_path="")

        return cls(
            bitcoind_path=os.environ.get("FIBRE_BITCOIND_PATH", base_config.bitcoind_path),
            pid=int(os.environ["FIBRE_PID"]) if "FIBRE_PID" in os.environ else base_config.pid,
            node_name=os.environ.get("FIBRE_NODE_NAME", base_config.node_name),
            metrics_port=int(os.environ.get("FIBRE_METRICS_PORT", base_config.metrics_port)),
            health_port=int(os.environ.get("FIBRE_HEALTH_PORT", base_config.health_port)),
            verbose=os.environ.get("FIBRE_VERBOSE", str(base_config.verbose)).lower() == "true",
            log_level=os.environ.get("FIBRE_LOG_LEVEL", base_config.log_level),
            log_file=os.environ.get("FIBRE_LOG_FILE", base_config.log_file),
            metrics_auth_username=os.environ.get("FIBRE_METRICS_AUTH_USERNAME", base_config.metrics_auth_username),
            metrics_auth_password=os.environ.get("FIBRE_METRICS_AUTH_PASSWORD", base_config.metrics_auth_password),
        )

    def has_metrics_auth(self) -> bool:
        """Check if metrics authentication is configured."""
        return bool(self.metrics_auth_username and self.metrics_auth_password)


# ============================================================================
# Logging Setup
# ============================================================================


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure structured logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger("fibre_exporter")
    logger.setLevel(log_level)

    # Always log to stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    # Optionally also log to file (line-buffered for real-time tailing)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# ============================================================================
# PID Detection
# ============================================================================


def find_bitcoind_pid(bitcoind_path: str) -> Optional[int]:
    """Find the PID of a running bitcoind process.

    Tries multiple methods to find the PID:
    1. pgrep with the full path
    2. pgrep with just the binary name
    3. pidof with the binary name

    Returns the PID if found, None otherwise.
    """
    binary_name = os.path.basename(bitcoind_path)

    # Try pgrep with full path first (most specific)
    try:
        result = subprocess.run(
            ["pgrep", "-f", bitcoind_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            if len(pids) == 1:
                return int(pids[0])
            # Multiple PIDs found, try to be more specific
    except Exception:
        pass

    # Try pgrep with binary name
    try:
        result = subprocess.run(
            ["pgrep", "-x", binary_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            if len(pids) == 1:
                return int(pids[0])
    except Exception:
        pass

    # Try pidof as fallback
    try:
        result = subprocess.run(
            ["pidof", binary_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split()
            if len(pids) == 1:
                return int(pids[0])
    except Exception:
        pass

    return None


# ============================================================================
# Prometheus Metrics
# ============================================================================


@dataclass
class FibreMetrics:
    """Container for all Prometheus metrics."""

    # FIBRE metrics
    blocks_reconstructed: Counter = field(default_factory=lambda: Counter(
        "fibre_blocks_reconstructed_total",
        "Total blocks reconstructed via FIBRE/UDP",
        ["node"],
    ))

    block_reconstruction_time: Histogram = field(default_factory=lambda: Histogram(
        "fibre_block_reconstruction_duration_seconds",
        "Block reconstruction time",
        ["node"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    ))

    chunks_used: Histogram = field(default_factory=lambda: Histogram(
        "fibre_block_chunks_used",
        "Number of chunks used per block",
        ["node"],
        buckets=[1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000],
    ))

    chunks_received: Counter = field(default_factory=lambda: Counter(
        "fibre_chunks_received_total",
        "Total chunks received",
        ["node"],
    ))

    chunks_used_total: Counter = field(default_factory=lambda: Counter(
        "fibre_chunks_used_total",
        "Total chunks used",
        ["node"],
    ))

    blocks_sent: Counter = field(default_factory=lambda: Counter(
        "fibre_blocks_sent_total",
        "Total blocks sent via FIBRE/UDP",
        ["node"],
    ))

    block_deliveries: Counter = field(default_factory=lambda: Counter(
        "fibre_block_deliveries_total",
        "Block deliveries by mechanism and peer",
        ["node", "mechanism", "peer"],
    ))

    last_block_height: Gauge = field(default_factory=lambda: Gauge(
        "fibre_last_block_height",
        "Height of most recently processed block",
        ["node"],
    ))

    # Block connection metrics (fires for ALL blocks, regardless of delivery path)
    blocks_connected: Counter = field(default_factory=lambda: Counter(
        "bitcoin_blocks_connected_total",
        "Total blocks connected to the chain (all delivery paths)",
        ["node"],
    ))

    block_connection_time: Histogram = field(default_factory=lambda: Histogram(
        "bitcoin_block_connection_duration_seconds",
        "Time to connect a block to the chain",
        ["node"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    ))

    block_tx_count: Histogram = field(default_factory=lambda: Histogram(
        "bitcoin_block_tx_count",
        "Number of transactions per connected block",
        ["node"],
        buckets=[1, 10, 50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000],
    ))

    # Exporter self-monitoring metrics
    exporter_up: Gauge = field(default_factory=lambda: Gauge(
        "fibre_exporter_up",
        "Whether the FIBRE exporter is running (1 = up, 0 = down)",
    ))

    exporter_start_time: Gauge = field(default_factory=lambda: Gauge(
        "fibre_exporter_start_time_seconds",
        "Unix timestamp when the exporter started",
    ))

    exporter_events_total: Counter = field(default_factory=lambda: Counter(
        "fibre_exporter_events_processed_total",
        "Total number of events processed by the exporter",
        ["event_type"],
    ))

    exporter_errors_total: Counter = field(default_factory=lambda: Counter(
        "fibre_exporter_errors_total",
        "Total number of errors encountered by the exporter",
        ["error_type"],
    ))

    exporter_probes_attached: Gauge = field(default_factory=lambda: Gauge(
        "fibre_exporter_probes_attached",
        "Number of USDT probes successfully attached",
    ))

    exporter_info: Info = field(default_factory=lambda: Info(
        "fibre_exporter",
        "Information about the FIBRE exporter",
    ))


# ============================================================================
# BPF Program
# ============================================================================

BPF_PROGRAM = """
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(events);

enum event_type {
    EVENT_BLOCK_RECONSTRUCTED = 1,
    EVENT_BLOCK_SEND_START = 2,
    EVENT_BLOCK_DELIVERY = 3,
    EVENT_BLOCK_CONNECTED = 10,
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
    char peer[48];
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

int trace_block_delivery(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_DELIVERY;

    // Args: block_hash, height, winner, winner_peer
    bpf_usdt_readarg(2, ctx, &event.height);

    u64 winner_ptr;
    bpf_usdt_readarg(3, ctx, &winner_ptr);
    bpf_probe_read_user_str(&event.winner, sizeof(event.winner), (void *)winner_ptr);

    u64 peer_ptr;
    bpf_usdt_readarg(4, ctx, &peer_ptr);
    bpf_probe_read_user_str(&event.peer, sizeof(event.peer), (void *)peer_ptr);

    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_block_connected(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_CONNECTED;

    // Args: block_hash, height, tx_count, inputs, sigops, connection_time_ns
    bpf_usdt_readarg(2, ctx, &event.height);
    bpf_usdt_readarg(3, ctx, &event.udp_ns);        // reuse s64 for tx_count (8@)
    bpf_usdt_readarg(6, ctx, &event.duration_us);    // reuse s64 for connection_time_ns (-8@)

    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

# ============================================================================
# Health Check Server
# ============================================================================


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks."""

    def do_GET(self) -> None:
        if self.path in ("/health", "/ready"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default logging
        pass


def start_health_server(port: int) -> HTTPServer:
    """Start health check server in a background thread."""
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ============================================================================
# Metrics Server with Basic Auth
# ============================================================================


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics with optional basic auth."""

    # These are set by the factory function
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None

    def _check_auth(self) -> bool:
        """Check basic authentication. Returns True if auth passes or is not configured."""
        if not self.auth_username or not self.auth_password:
            return True

        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return False

        try:
            if not auth_header.startswith("Basic "):
                return False

            encoded_credentials = auth_header[6:]
            decoded = base64.b64decode(encoded_credentials).decode("utf-8")
            username, password = decoded.split(":", 1)

            # Use constant-time comparison to prevent timing attacks
            username_match = hmac.compare_digest(username, self.auth_username)
            password_match = hmac.compare_digest(password, self.auth_password)

            return username_match and password_match
        except Exception:
            return False

    def _send_auth_required(self) -> None:
        """Send 401 Unauthorized response."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="FIBRE Metrics"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def do_GET(self) -> None:
        if self.path == "/metrics":
            if not self._check_auth():
                self._send_auth_required()
                return

            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(generate_latest(REGISTRY))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default logging
        pass


def create_metrics_handler(
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> type[MetricsHandler]:
    """Create a MetricsHandler class with auth credentials."""

    class ConfiguredMetricsHandler(MetricsHandler):
        auth_username = username
        auth_password = password

    return ConfiguredMetricsHandler


def start_metrics_server(
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> HTTPServer:
    """Start metrics server with optional basic auth in a background thread."""
    handler_class = create_metrics_handler(username, password)
    server = HTTPServer(("0.0.0.0", port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ============================================================================
# FIBRE Exporter
# ============================================================================

EVENT_TYPE_NAMES: dict[int, str] = {
    1: "block_reconstructed",
    2: "block_send_start",
    3: "block_delivery",
    10: "block_connected",
}


class FibreExporter:
    """Main FIBRE metrics exporter class."""

    def __init__(self, config: ExporterConfig) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level, config.log_file)
        self.metrics = FibreMetrics()
        self.bpf: Optional[BPF] = None
        self._running = False

    def _handle_event(self, cpu: int, data: Any, size: int) -> None:
        """Process a single BPF event."""
        try:
            event = self.bpf["events"].event(data)
            event_type_name = EVENT_TYPE_NAMES.get(event.type, "unknown")
            self.metrics.exporter_events_total.labels(event_type=event_type_name).inc()

            if event.type == 1:  # BLOCK_RECONSTRUCTED
                self._handle_block_reconstructed(event)
            elif event.type == 2:  # BLOCK_SEND_START
                self._handle_block_send_start(event)
            elif event.type == 3:  # BLOCK_DELIVERY
                self._handle_block_delivery(event)
            elif event.type == 10:  # BLOCK_CONNECTED
                self._handle_block_connected(event)

        except Exception as e:
            self.metrics.exporter_errors_total.labels(error_type="event_processing").inc()
            self.logger.error(f"Error processing event: {e}")

    def _handle_block_reconstructed(self, event: Any) -> None:
        """Handle block reconstructed event."""
        node = self.config.node_name
        duration_sec = event.duration_us / 1_000_000.0

        self.metrics.blocks_reconstructed.labels(node=node).inc()
        self.metrics.block_reconstruction_time.labels(node=node).observe(duration_sec)
        self.metrics.chunks_used.labels(node=node).observe(event.chunks_used)
        self.metrics.chunks_used_total.labels(node=node).inc(event.chunks_used)
        self.metrics.chunks_received.labels(node=node).inc(event.chunks_recvd)

        if self.config.verbose:
            self.logger.info(
                f"Block reconstructed: duration={duration_sec:.3f}s "
                f"chunks_used={event.chunks_used} chunks_recvd={event.chunks_recvd}"
            )

    def _handle_block_send_start(self, event: Any) -> None:
        """Handle block send start event."""
        self.metrics.blocks_sent.labels(node=self.config.node_name).inc()

        if self.config.verbose:
            self.logger.info("Block send started")

    def _handle_block_delivery(self, event: Any) -> None:
        """Handle block delivery event — records which peer delivered via which mechanism."""
        node = self.config.node_name
        winner_str = event.winner.decode("utf-8", errors="ignore").rstrip("\x00")
        peer_str = event.peer.decode("utf-8", errors="ignore").rstrip("\x00")

        if winner_str.startswith("F"):
            mechanism = "fibre_udp"
        elif winner_str.startswith("B"):
            mechanism = "bip152_cmpct"
        else:
            mechanism = "other"

        self.metrics.block_deliveries.labels(node=node, mechanism=mechanism, peer=peer_str).inc()
        self.metrics.last_block_height.labels(node=node).set(event.height)

        if self.config.verbose:
            self.logger.info(
                f"Block delivery: height={event.height} mechanism={mechanism} peer={peer_str}"
            )

    def _handle_block_connected(self, event: Any) -> None:
        """Handle block connected event (fires for every block)."""
        node = self.config.node_name
        connection_time_ns = event.duration_us  # reused s64 field holds connection_time_ns
        tx_count = event.udp_ns  # reused s64 field holds tx_count

        self.metrics.blocks_connected.labels(node=node).inc()
        self.metrics.last_block_height.labels(node=node).set(event.height)

        if connection_time_ns > 0:
            connection_time_sec = connection_time_ns / 1_000_000_000.0
            self.metrics.block_connection_time.labels(node=node).observe(connection_time_sec)

        if tx_count > 0:
            self.metrics.block_tx_count.labels(node=node).observe(tx_count)

        if self.config.verbose:
            conn_ms = connection_time_ns / 1_000_000.0 if connection_time_ns > 0 else 0
            self.logger.info(
                f"Block connected: height={event.height} "
                f"tx_count={tx_count} connection_time={conn_ms:.1f}ms"
            )

    def _attach_probes(self) -> int:
        """Attach USDT probes to bitcoind. Returns number of probes attached."""
        # Store as instance attr to prevent GC — USDT.__del__ calls bcc_usdt_close()
        # which detaches the uprobes if the object is garbage collected.
        self._usdt = USDT(path=self.config.bitcoind_path, pid=self.config.pid)
        usdt = self._usdt

        # FIBRE-specific probes (udp provider) — only fire via compact block / FIBRE paths
        fibre_probes = [
            ("block_reconstructed", "trace_block_reconstructed", "udp"),
            ("block_send_start", "trace_block_send_start", "udp"),
            ("block_race_winner", "trace_block_delivery", "udp"),
        ]
        # General block probe (validation provider) — fires for ALL blocks unconditionally
        validation_probes = [
            ("block_connected", "trace_block_connected", "validation"),
        ]

        attached_count = 0
        for probe_name, fn_name, provider in fibre_probes + validation_probes:
            try:
                usdt.enable_probe(probe=probe_name, fn_name=fn_name)
                self.logger.info(f"Attached probe: {provider}:{probe_name}")
                attached_count += 1
            except Exception as e:
                self.logger.warning(f"Failed to attach probe {provider}:{probe_name}: {e}")

        if attached_count == 0:
            return 0

        # Load BPF program
        self.bpf = BPF(text=BPF_PROGRAM, usdt_contexts=[usdt])
        self.bpf["events"].open_perf_buffer(self._handle_event)

        return attached_count

    def run(self) -> None:
        """Run the exporter."""
        self.logger.info(f"FIBRE USDT Metrics Exporter v{__version__}")
        self.logger.info(f"Configuration: bitcoind={self.config.bitcoind_path} "
                        f"node={self.config.node_name} port={self.config.metrics_port}")

        # Auto-detect PID if not provided
        if self.config.pid is None:
            self.logger.info("PID not specified, attempting auto-detection...")
            detected_pid = find_bitcoind_pid(self.config.bitcoind_path)
            if detected_pid:
                self.config.pid = detected_pid
                self.logger.info(f"Auto-detected bitcoind PID: {detected_pid}")
            else:
                self.logger.error("Could not auto-detect bitcoind PID")
                self.logger.error("Please specify --pid manually or ensure bitcoind is running")
                sys.exit(1)

        # Verify binary path matches running process
        try:
            proc_exe = os.readlink(f"/proc/{self.config.pid}/exe")
            if os.path.realpath(self.config.bitcoind_path) != os.path.realpath(proc_exe):
                self.logger.warning(
                    f"Binary path mismatch! --bitcoind={self.config.bitcoind_path} "
                    f"but /proc/{self.config.pid}/exe -> {proc_exe}"
                )
                self.logger.warning("USDT probes may not fire if attached to the wrong binary")
            else:
                self.logger.info(f"Binary path verified: matches /proc/{self.config.pid}/exe")
        except OSError as e:
            self.logger.warning(f"Could not verify binary path: {e}")

        # Set exporter info
        self.metrics.exporter_info.info({
            "version": __version__,
            "node_name": self.config.node_name,
            "bitcoind_path": self.config.bitcoind_path,
        })
        self.metrics.exporter_start_time.set(time.time())

        # Attach probes
        attached_count = self._attach_probes()

        if attached_count == 0:
            self.metrics.exporter_up.set(0)
            self.logger.error("No USDT probes could be attached")
            self.logger.error("Possible causes:")
            self.logger.error("  - bitcoind was not compiled with USDT tracepoint support")
            self.logger.error("  - The specified PID is not a bitcoind process")
            self.logger.error("  - Insufficient permissions (try running with sudo)")
            sys.exit(1)

        self.metrics.exporter_probes_attached.set(attached_count)
        self.logger.info(f"Attached {attached_count}/4 probes successfully")

        # Start servers
        if self.config.has_metrics_auth():
            start_metrics_server(
                self.config.metrics_port,
                self.config.metrics_auth_username,
                self.config.metrics_auth_password,
            )
            self.logger.info("Metrics endpoint basic auth: enabled")
        else:
            start_http_server(self.config.metrics_port)
            self.logger.info("Metrics endpoint basic auth: disabled")

        start_health_server(self.config.health_port)
        self.metrics.exporter_up.set(1)

        self.logger.info(f"Prometheus metrics: http://0.0.0.0:{self.config.metrics_port}/metrics")
        self.logger.info(f"Health check: http://0.0.0.0:{self.config.health_port}/health")
        self.logger.info("Waiting for FIBRE events...")

        # Poll for events
        self._running = True
        try:
            while self._running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            pass
        finally:
            self.metrics.exporter_up.set(0)
            self.logger.info("Shutting down...")


# ============================================================================
# CLI
# ============================================================================


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="FIBRE USDT Metrics Exporter for Prometheus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  FIBRE_BITCOIND_PATH          Path to bitcoind binary
  FIBRE_PID                    PID of running bitcoind
  FIBRE_NODE_NAME              Node name label
  FIBRE_METRICS_PORT           Prometheus metrics port
  FIBRE_HEALTH_PORT            Health check port
  FIBRE_VERBOSE                Enable verbose logging (true/false)
  FIBRE_LOG_LEVEL              Log level (DEBUG, INFO, WARNING, ERROR)
  FIBRE_METRICS_AUTH_USERNAME  Basic auth username for /metrics endpoint
  FIBRE_METRICS_AUTH_PASSWORD  Basic auth password for /metrics endpoint

Examples:
  # Run with command line arguments
  %(prog)s --bitcoind /usr/local/bin/bitcoind --node-name mynode

  # Run with config file
  %(prog)s --config /etc/fibre-exporter/config.yaml

  # Run with environment variables
  FIBRE_BITCOIND_PATH=/usr/local/bin/bitcoind %(prog)s

  # Run with basic auth enabled
  %(prog)s --bitcoind /usr/local/bin/bitcoind --metrics-auth-username prometheus --metrics-auth-password secret
""",
    )

    parser.add_argument(
        "--bitcoind", "-b",
        help="Path to bitcoind binary",
    )
    parser.add_argument(
        "--pid", "-p",
        type=int,
        help="PID of running bitcoind (optional, auto-detected)",
    )
    parser.add_argument(
        "--node-name", "-n",
        default="localhost",
        help="Node name label (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9435,
        help="Prometheus metrics port (default: 9435)",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=9436,
        help="Health check port (default: 9436)",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging of individual events",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        help="Path to log file (logs to both stdout and file)",
    )
    parser.add_argument(
        "--metrics-auth-username",
        help="Basic auth username for /metrics endpoint",
    )
    parser.add_argument(
        "--metrics-auth-password",
        help="Basic auth password for /metrics endpoint",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Build configuration from multiple sources (config file -> env vars -> CLI args)
    if args.config and args.config.exists():
        config = ExporterConfig.from_yaml(args.config)
        config = ExporterConfig.from_env(config)
    else:
        # Start with CLI defaults, then override with env vars
        config = ExporterConfig.from_args(args)
        config = ExporterConfig.from_env(config)

    # CLI args override everything
    if args.bitcoind:
        config.bitcoind_path = args.bitcoind
    if args.pid:
        config.pid = args.pid
    if args.node_name != "localhost":
        config.node_name = args.node_name
    if args.port != 9435:
        config.metrics_port = args.port
    if args.health_port != 9436:
        config.health_port = args.health_port
    if args.verbose:
        config.verbose = args.verbose
    if args.log_level != "INFO":
        config.log_level = args.log_level
    if getattr(args, 'log_file', None):
        config.log_file = args.log_file
    if args.metrics_auth_username:
        config.metrics_auth_username = args.metrics_auth_username
    if args.metrics_auth_password:
        config.metrics_auth_password = args.metrics_auth_password

    # Validate required config
    if not config.bitcoind_path:
        print("Error: bitcoind path is required", file=sys.stderr)
        print("Provide via --bitcoind, config file, or FIBRE_BITCOIND_PATH env var", file=sys.stderr)
        sys.exit(1)

    # Run exporter
    exporter = FibreExporter(config)
    exporter.run()


if __name__ == "__main__":
    main()
