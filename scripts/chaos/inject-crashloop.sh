#!/usr/bin/env bash
set -euo pipefail

echo "=== CHAOS: Injecting CrashLoopBackOff ==="
echo "Action: Updating order-service to a non-existent image tag"
echo ""

kubectl set image deployment/order-service \
  order-service=k3d-ai-sre-registry:5111/order-service:does-not-exist \
  -n apps

echo "Done. The order-service pods will enter CrashLoopBackOff."
echo "Watch with: kubectl get pods -n apps -w"
echo ""
echo "The AI SRE Agent should detect this via AlertManager and propose a fix."
echo "Check the dashboard: http://localhost:8080"
echo ""
echo "To fix manually: kubectl set image deployment/order-service order-service=k3d-ai-sre-registry:5111/order-service:latest -n apps"
