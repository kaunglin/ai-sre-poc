#!/usr/bin/env bash
set -euo pipefail

echo "=== AI SRE PoC — Deploy AI SRE Agent ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REGISTRY="localhost:5111"

# Which provider the agent uses. Must match LLM_PROVIDER in the ConfigMap
# (k8s/ai-sre-agent/all.yaml) — that manifest is the runtime source of truth;
# this only decides which key we check for here.
LLM_PROVIDER="${LLM_PROVIDER:-openai}"
if [ "$LLM_PROVIDER" = "openai" ]; then
  KEY_VAR="OPENAI_API_KEY"
else
  KEY_VAR="ANTHROPIC_API_KEY"
fi

if [ -z "${!KEY_VAR:-}" ]; then
  echo "WARNING: $KEY_VAR not set (provider: $LLM_PROVIDER)."
  echo "The agent will deploy but AI analysis will be skipped."
  echo "Set it with: export $KEY_VAR='your-key-here'"
  echo ""
fi

# Build and push AI SRE Agent
echo "Building ai-sre-agent..."
docker build -t "${REGISTRY}/ai-sre-agent:latest" "${PROJECT_DIR}/apps/ai-sre-agent/"
docker push "${REGISTRY}/ai-sre-agent:latest"

# Update the secret with the actual API key
kubectl create namespace ai-sre-agent --dry-run=client -o yaml | kubectl apply -f -

# Both keys go in; the agent reads whichever one LLM_PROVIDER selects and
# treats "not-set" as absent.
kubectl create secret generic ai-sre-agent-secret \
  --namespace ai-sre-agent \
  --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY:-not-set}" \
  --from-literal=ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-not-set}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Deploy
echo "Deploying AI SRE Agent..."
kubectl apply -f "${PROJECT_DIR}/k8s/ai-sre-agent/all.yaml"

# envFrom secrets are not hot-reloaded, so restart to pick up a rotated key
kubectl rollout restart deployment/ai-sre-agent -n ai-sre-agent

# Wait for agent to be ready
echo "Waiting for AI SRE Agent to be ready..."
kubectl wait --for=condition=Ready pods -l app=ai-sre-agent -n ai-sre-agent --timeout=120s 2>/dev/null || \
  echo "  (agent pod may take a moment to start)"

echo ""
echo "=== AI SRE Agent deployed ==="
echo ""
kubectl get pods -n ai-sre-agent
echo ""
echo "Port-forward the dashboard:"
echo "  kubectl port-forward -n ai-sre-agent svc/ai-sre-agent 8080:5000"
echo "  Then open: http://localhost:8080"
echo ""
echo "To test the full pipeline:"
echo "  1. Open http://localhost:8080 (AI SRE dashboard)"
echo "  2. Click 'Trigger test alert' button"
echo "  3. Watch the agent investigate and analyze"
echo "  4. Review the recommendation and approve/reject"
echo ""
echo "Or run a chaos scenario:"
echo "  ./scripts/chaos/inject-crashloop.sh"
