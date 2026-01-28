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

__version__ = "1.1.0"

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
            metrics_auth_username=os.environ.get("FIBRE_METRICS_AUTH_USERNAME", base_config.metrics_auth_username),
            metrics_auth_password=os.environ.get("FIBRE_METRICS_AUTH_PASSWORD", base_config.metrics_auth_password),
        )

    def has_metrics_auth(self) -> bool:
        """Check if metrics authentication is configured."""
        return bool(self.metrics_auth_username and self.metrics_auth_password)


# ============================================================================
# Logging Setup
# ============================================================================


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure structured logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Configure root logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    logger = logging.getLogger("fibre_exporter")
    logger.setLevel(log_level)
    logger.addHandler(handler)

    return logger


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

    race_wins: Counter = field(default_factory=lambda: Counter(
        "fibre_block_race_wins_total",
        "Block race wins by mechanism",
        ["node", "mechanism"],
    ))

    race_latency: Histogram = field(default_factory=lambda: Histogram(
        "fibre_block_race_latency_seconds",
        "Block race latency by mechanism",
        ["node", "mechanism"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
    ))

    race_margin: Histogram = field(default_factory=lambda: Histogram(
        "fibre_race_margin_seconds",
        "Race margin (how much faster winner was)",
        ["node", "winner"],
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0],
    ))

    races_with_both: Counter = field(default_factory=lambda: Counter(
        "fibre_races_with_both_total",
        "Races where both mechanisms participated",
        ["node"],
    ))

    last_block_height: Gauge = field(default_factory=lambda: Gauge(
        "fibre_last_block_height",
        "Height of most recently processed block",
        ["node"],
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
    3: "race_winner",
    4: "race_time",
}


class FibreExporter:
    """Main FIBRE metrics exporter class."""

    def __init__(self, config: ExporterConfig) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level)
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
            elif event.type == 3:  # RACE_WINNER
                self._handle_race_winner(event)
            elif event.type == 4:  # RACE_TIME
                self._handle_race_time(event)

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

    def _handle_race_winner(self, event: Any) -> None:
        """Handle race winner event."""
        node = self.config.node_name
        winner_str = event.winner.decode("utf-8", errors="ignore").rstrip("\x00")

        if winner_str.startswith("F"):
            mechanism = "fibre_udp"
        elif winner_str.startswith("B"):
            mechanism = "bip152_cmpct"
        else:
            mechanism = "other"

        self.metrics.race_wins.labels(node=node, mechanism=mechanism).inc()
        self.metrics.last_block_height.labels(node=node).set(event.height)

        if self.config.verbose:
            self.logger.info(f"Race winner: height={event.height} winner={mechanism}")

    def _handle_race_time(self, event: Any) -> None:
        """Handle race time event."""
        node = self.config.node_name
        self.metrics.last_block_height.labels(node=node).set(event.height)

        udp_latency_sec = None
        cmpct_latency_sec = None

        if event.udp_ns >= 0:
            udp_latency_sec = event.udp_ns / 1_000_000_000.0
            self.metrics.race_latency.labels(node=node, mechanism="fibre_udp").observe(udp_latency_sec)

        if event.cmpct_ns >= 0:
            cmpct_latency_sec = event.cmpct_ns / 1_000_000_000.0
            self.metrics.race_latency.labels(node=node, mechanism="bip152_cmpct").observe(cmpct_latency_sec)

        if event.udp_ns >= 0 and event.cmpct_ns >= 0:
            self.metrics.races_with_both.labels(node=node).inc()
            if event.udp_ns <= event.cmpct_ns:
                margin = (event.cmpct_ns - event.udp_ns) / 1_000_000_000.0
                self.metrics.race_margin.labels(node=node, winner="fibre_udp").observe(margin)
            else:
                margin = (event.udp_ns - event.cmpct_ns) / 1_000_000_000.0
                self.metrics.race_margin.labels(node=node, winner="bip152_cmpct").observe(margin)

        if self.config.verbose:
            self.logger.info(
                f"Race time: height={event.height} "
                f"udp={udp_latency_sec:.3f}s cmpct={cmpct_latency_sec:.3f}s"
                if udp_latency_sec and cmpct_latency_sec
                else f"Race time: height={event.height}"
            )

    def _attach_probes(self) -> int:
        """Attach USDT probes to bitcoind. Returns number of probes attached."""
        usdt = USDT(path=self.config.bitcoind_path, pid=self.config.pid)

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
                self.logger.info(f"Attached probe: udp:{probe_name}")
                attached_count += 1
            except Exception as e:
                self.logger.warning(f"Failed to attach probe {probe_name}: {e}")

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
