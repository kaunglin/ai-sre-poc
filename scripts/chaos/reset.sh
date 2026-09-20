#!/usr/bin/env bash
set -euo pipefail

echo "=== CHAOS: Resetting all faults ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Reset fault injection flags via API
echo "Resetting fault injection endpoints..."
kubectl port-forward -n apps svc/order-service 18000:8000 &
PF_PID=$!
sleep 2
curl -s -X POST "http://localhost:18000/fault/reset" || true
kill $PF_PID 2>/dev/null || true

# Restore original deployment (correct image + resources)
echo "Restoring order-service deployment..."
kubectl apply -f "${PROJECT_DIR}/k8s/apps/all.yaml"

# Force a rolling restart to pick up clean state
echo "Rolling restart of order-service..."
kubectl rollout restart deployment/order-service -n apps

echo "Waiting for pods to be ready..."
kubectl wait --for=condition=Ready pods -l app=order-service -n apps --timeout=120s 2>/dev/null || true

echo ""
echo "=== All faults cleared ==="
kubectl get pods -n apps
