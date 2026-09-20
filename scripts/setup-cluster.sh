#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="ai-sre-poc"
REGISTRY_NAME="ai-sre-registry"
REGISTRY_PORT=5111

echo "=== AI SRE PoC — Cluster Setup ==="

# Check prerequisites
for cmd in k3d kubectl helm; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: $cmd is required. Install it first."
    exit 1
  fi
done

# Check Docker (OrbStack provides this)
if ! docker info &>/dev/null; then
  echo "ERROR: Docker is not running. Start OrbStack first."
  exit 1
fi

# Clean up existing cluster if present
if k3d cluster list | grep -q "$CLUSTER_NAME"; then
  echo "Deleting existing cluster..."
  k3d cluster delete "$CLUSTER_NAME"
fi

# Clean up existing registry if present
if k3d registry list | grep -q "$REGISTRY_NAME"; then
  echo "Deleting existing registry..."
  k3d registry delete "$REGISTRY_NAME"
fi

# Create local container registry
echo "Creating local container registry..."
k3d registry create "$REGISTRY_NAME" --port "$REGISTRY_PORT"

# Create k3d cluster with 3 worker nodes
echo "Creating k3d cluster: $CLUSTER_NAME (1 server + 3 agents)..."
k3d cluster create "$CLUSTER_NAME" \
  --servers 1 \
  --agents 3 \
  --registry-use "k3d-${REGISTRY_NAME}:${REGISTRY_PORT}" \
  --port "18080:80@loadbalancer" \
  --port "18443:443@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --wait

# Verify cluster is ready
echo "Waiting for cluster nodes to be ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s

echo ""
echo "Cluster info:"
kubectl get nodes -o wide
echo ""
echo "Registry: k3d-${REGISTRY_NAME}:${REGISTRY_PORT}"
echo ""

# Install ingress-nginx (more production-like than traefik for this PoC)
echo "Installing ingress-nginx..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo update
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.type=LoadBalancer \
  --set controller.watchIngressWithoutClass=true \
  --wait --timeout 120s

echo ""
echo "=== Cluster setup complete ==="
echo "  Cluster:  $CLUSTER_NAME"
echo "  Registry: localhost:${REGISTRY_PORT} (inside cluster: k3d-${REGISTRY_NAME}:${REGISTRY_PORT})"
echo "  Ingress:  http://localhost:18080 / https://localhost:18443"
echo "            (8080/8443 are left free for the documented port-forwards)"
echo "  Nodes:    $(kubectl get nodes --no-headers | wc -l | tr -d ' ')"
echo ""
echo "Next: ./scripts/setup-observability.sh"
