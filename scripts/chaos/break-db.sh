#!/usr/bin/env bash
set -euo pipefail

echo "=== CHAOS: Breaking Database Connection ==="
echo "Action: Simulating database disconnect via fault injection endpoint"
echo ""

kubectl port-forward -n apps svc/order-service 18000:8000 &
PF_PID=$!
sleep 2

curl -s -X POST "http://localhost:18000/fault/db-disconnect"
echo ""

kill $PF_PID 2>/dev/null || true

echo ""
echo "Done. The order-service will return 503 for all database operations."
echo "The readiness probe will fail, and pods will be marked as not ready."
echo ""
echo "This should trigger PodNotReady and HighHTTPErrorRate alerts."
echo "Dashboard: http://localhost:8080"
echo ""
echo "To fix: Run ./scripts/chaos/reset.sh"
