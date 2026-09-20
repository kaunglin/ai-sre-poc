#!/usr/bin/env bash
set -euo pipefail

echo "=== CHAOS: Injecting High Latency ==="
echo "Action: Adding 3000ms latency to every order-service request"
echo ""

# Port-forward to the order-service (temporarily)
kubectl port-forward -n apps svc/order-service 18000:8000 &
PF_PID=$!
sleep 2

curl -s -X POST "http://localhost:18000/fault/latency?ms=3000"
echo ""

kill $PF_PID 2>/dev/null || true

echo ""
echo "Done. Every request to order-service now has 3s of added latency."
echo "This should trigger HighHTTPLatency alerts."
echo ""
echo "Dashboard: http://localhost:8080"
echo ""
echo "To remove: Run ./scripts/chaos/reset.sh"
