#!/usr/bin/env bash
set -euo pipefail

echo "=== AI SRE PoC — Build & Deploy Applications ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REGISTRY="localhost:5111"

# Create namespaces
kubectl apply -f "${PROJECT_DIR}/k8s/namespaces.yaml"

# Build and push order-service
echo "Building order-service..."
docker build -t "${REGISTRY}/order-service:latest" "${PROJECT_DIR}/apps/order-service/"
docker push "${REGISTRY}/order-service:latest"

# Build and push frontend
echo "Building frontend..."
docker build -t "${REGISTRY}/frontend:latest" "${PROJECT_DIR}/apps/frontend/"
docker push "${REGISTRY}/frontend:latest"

# Deploy database and cache first
echo "Deploying PostgreSQL, Redis, order-service, and frontend..."
kubectl apply -f "${PROJECT_DIR}/k8s/apps/all.yaml"

# Wait for database to be ready
echo "Waiting for PostgreSQL to be ready..."
kubectl wait --for=condition=Ready pods -l app=postgres -n apps --timeout=120s

echo "Waiting for Redis to be ready..."
kubectl wait --for=condition=Ready pods -l app=redis -n apps --timeout=60s

# `kubectl apply` is a no-op when only the image contents changed behind a
# floating :latest tag — the manifest is byte-identical, so no new pod is
# created and the freshly pushed image is never pulled. Force a rollout so
# re-running this script actually redeploys the code you just built.
echo "Rolling out freshly built images..."
kubectl rollout restart deployment/order-service deployment/frontend -n apps

# Wait for apps
echo "Waiting for order-service pods..."
kubectl wait --for=condition=Ready pods -l app=order-service -n apps --timeout=120s 2>/dev/null || \
  echo "  (order-service pods may take a moment to connect to DB)"

echo "Waiting for frontend pods..."
kubectl wait --for=condition=Ready pods -l app=frontend -n apps --timeout=60s 2>/dev/null || true

echo ""
echo "=== Application deployment complete ==="
echo ""
kubectl get pods -n apps
echo ""
echo "Port-forward the frontend:"
echo "  kubectl port-forward -n apps svc/frontend 3001:3000"
echo "  Then open: http://localhost:3001"
echo ""
echo "Next: ./scripts/deploy-ai-agent.sh"
