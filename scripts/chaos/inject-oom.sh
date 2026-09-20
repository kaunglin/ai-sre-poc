#!/usr/bin/env bash
set -euo pipefail

echo "=== CHAOS: Injecting OOMKill ==="
echo "Action: Reducing order-service memory limit to 32Mi (will OOM under load)"
echo ""

kubectl patch deployment order-service -n apps --type='json' -p='[
  {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "32Mi"},
  {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "16Mi"}
]'

echo "Done. The order-service will likely OOMKill under any load."
echo ""
echo "Generate load to trigger it:"
echo "  for i in \$(seq 1 50); do curl -s http://localhost:3001/api/orders > /dev/null & done"
echo ""
echo "Watch: kubectl get pods -n apps -w"
echo "Dashboard: http://localhost:8080"
