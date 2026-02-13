#!/usr/bin/env bash
#
# Generate a self-signed CA and per-node TLS certificates for fibre_exporter.
#
# Usage:
#   ./generate-certs.sh <node-ip> [node-ip ...]
#
# Example:
#   ./generate-certs.sh 192.168.1.10 192.168.1.11 10.0.0.5
#
# Output structure:
#   certs/
#     ca.crt              <- give this to Prometheus (tls_config.ca_file)
#     ca.key              <- keep safe, only needed to sign new node certs
#     <ip>/
#       node.crt          <- copy to that node
#       node.key          <- copy to that node
#
set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
CA_DAYS=3650          # 10 years
NODE_DAYS=3650        # 10 years
KEY_BITS=2048
OUT_DIR="certs"
CA_SUBJECT="/CN=FIBRE Exporter CA"
# ────────────────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    echo "Usage: $0 <node-ip> [node-ip ...]"
    echo ""
    echo "Example:"
    echo "  $0 192.168.1.10 192.168.1.11 10.0.0.5"
    exit 1
fi

command -v openssl >/dev/null 2>&1 || { echo "Error: openssl is required but not installed"; exit 1; }

mkdir -p "$OUT_DIR"

# ── generate CA (only if it doesn't already exist) ──────────────────────────
if [ -f "$OUT_DIR/ca.crt" ] && [ -f "$OUT_DIR/ca.key" ]; then
    echo "Using existing CA: $OUT_DIR/ca.crt"
else
    echo "Generating CA certificate..."
    openssl genrsa -out "$OUT_DIR/ca.key" "$KEY_BITS" 2>/dev/null
    openssl req -new -x509 \
        -key "$OUT_DIR/ca.key" \
        -out "$OUT_DIR/ca.crt" \
        -days "$CA_DAYS" \
        -subj "$CA_SUBJECT" \
        -nodes 2>/dev/null
    chmod 600 "$OUT_DIR/ca.key"
    echo "  Created: $OUT_DIR/ca.crt (give this to Prometheus)"
    echo "  Created: $OUT_DIR/ca.key (keep safe)"
fi

# ── generate per-node certs ─────────────────────────────────────────────────
for NODE_IP in "$@"; do
    NODE_DIR="$OUT_DIR/$NODE_IP"
    mkdir -p "$NODE_DIR"

    if [ -f "$NODE_DIR/node.crt" ] && [ -f "$NODE_DIR/node.key" ]; then
        echo "Skipping $NODE_IP (cert already exists)"
        continue
    fi

    echo "Generating cert for $NODE_IP..."

    # Create a temporary extension file for IP SAN
    EXT_FILE=$(mktemp)
    cat > "$EXT_FILE" <<EXTEOF
[v3_ext]
subjectAltName = IP:$NODE_IP,IP:127.0.0.1
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EXTEOF

    # Generate key + CSR
    openssl genrsa -out "$NODE_DIR/node.key" "$KEY_BITS" 2>/dev/null
    openssl req -new \
        -key "$NODE_DIR/node.key" \
        -out "$NODE_DIR/node.csr" \
        -subj "/CN=$NODE_IP" \
        -nodes 2>/dev/null

    # Sign with CA
    openssl x509 -req \
        -in "$NODE_DIR/node.csr" \
        -CA "$OUT_DIR/ca.crt" \
        -CAkey "$OUT_DIR/ca.key" \
        -CAcreateserial \
        -out "$NODE_DIR/node.crt" \
        -days "$NODE_DAYS" \
        -extfile "$EXT_FILE" \
        -extensions v3_ext 2>/dev/null

    # Cleanup
    rm -f "$NODE_DIR/node.csr" "$EXT_FILE"
    chmod 600 "$NODE_DIR/node.key"

    echo "  Created: $NODE_DIR/node.crt"
    echo "  Created: $NODE_DIR/node.key"
done

echo ""
echo "Done. Next steps:"
echo ""
echo "  1. Copy certs to each node:"
echo "     scp $OUT_DIR/<ip>/node.{crt,key} <user>@<ip>:/etc/fibre-exporter/"
echo ""
echo "  2. Start the exporter with TLS:"
echo "     fibre_exporter.py --bitcoind /path/to/bitcoind \\"
echo "       --tls-cert /etc/fibre-exporter/node.crt \\"
echo "       --tls-key  /etc/fibre-exporter/node.key"
echo ""
echo "  3. Mount CA cert into Prometheus (docker-compose.yml):"
echo "     volumes:"
echo "       - ../certs/ca.crt:/etc/prometheus/fibre-ca.crt:ro"
echo ""
echo "  4. Configure Prometheus scrape (prometheus.yml):"
echo "     scrape_configs:"
echo "       - job_name: fibre"
echo "         scheme: https"
echo "         tls_config:"
echo "           ca_file: /etc/prometheus/fibre-ca.crt"
echo "         static_configs:"
echo "           - targets: [$(printf "'%s:9435', " "$@" | sed 's/, $//')]"
