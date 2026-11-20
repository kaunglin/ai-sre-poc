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

kubectl create namespace ai-sre-agent --dry-run=client -o yaml | kubectl apply -f -

# Build ai-sre-agent-secret, KEEPING what is already there. Earlier versions
# rebuilt it from the current shell only, so running this script without the
# API key exported overwrote a working key with "not-set".
existing() {
  kubectl -n ai-sre-agent get secret ai-sre-agent-secret \
    -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d 2>/dev/null || true
}
OPENAI_VALUE="${OPENAI_API_KEY:-$(existing OPENAI_API_KEY)}"
ANTHROPIC_VALUE="${ANTHROPIC_API_KEY:-$(existing ANTHROPIC_API_KEY)}"

# Dashboard sign-in: generated once, then reused. Set DASHBOARD_PASSWORD in the
# environment to choose your own.
DASHBOARD_PASSWORD_VALUE="${DASHBOARD_PASSWORD:-$(existing DASHBOARD_PASSWORD)}"
NEW_PASSWORD=false
if [ -z "$DASHBOARD_PASSWORD_VALUE" ]; then
  DASHBOARD_PASSWORD_VALUE="$(openssl rand -base64 24 | tr -d '/+=\n' | cut -c1-20)"
  NEW_PASSWORD=true
fi
# Signs the session cookie. Kept stable so a redeploy does not sign everyone out.
SESSION_SECRET_VALUE="$(existing SESSION_SECRET)"
[ -n "$SESSION_SECRET_VALUE" ] || SESSION_SECRET_VALUE="$(openssl rand -hex 32)"

# AlertManager authenticates to the webhook with this token. setup-observability.sh
# creates it in the monitoring namespace (AlertManager mounts it from there); the
# agent gets a copy.
WEBHOOK_TOKEN_VALUE="$(kubectl -n monitoring get secret ai-sre-webhook \
  -o 'jsonpath={.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)"
if [ -z "$WEBHOOK_TOKEN_VALUE" ]; then
  echo "WARNING: monitoring/ai-sre-webhook not found — the webhook will accept"
  echo "unauthenticated calls. Re-run scripts/setup-observability.sh to create it."
fi

# Values go through a process-substituted env file (printf is a shell builtin),
# so they never appear in another process's command line.
kubectl create secret generic ai-sre-agent-secret \
  --namespace ai-sre-agent \
  --from-env-file=<(
    printf 'OPENAI_API_KEY=%s\n' "${OPENAI_VALUE:-not-set}"
    printf 'ANTHROPIC_API_KEY=%s\n' "${ANTHROPIC_VALUE:-not-set}"
    printf 'DASHBOARD_PASSWORD=%s\n' "$DASHBOARD_PASSWORD_VALUE"
    printf 'SESSION_SECRET=%s\n' "$SESSION_SECRET_VALUE"
    [ -z "$WEBHOOK_TOKEN_VALUE" ] || printf 'WEBHOOK_TOKEN=%s\n' "$WEBHOOK_TOKEN_VALUE"
  ) \
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
echo "Dashboard: http://ai-sre.localhost:18080  (user: admin)"
if [ "$NEW_PASSWORD" = true ]; then
  echo "  A dashboard password was generated. Read it with:"
else
  echo "  Password unchanged. Read it with:"
fi
echo "  kubectl -n ai-sre-agent get secret ai-sre-agent-secret -o jsonpath='{.data.DASHBOARD_PASSWORD}' | base64 -d; echo"
echo ""
echo "To test the full pipeline:"
echo "  1. Sign in at http://ai-sre.localhost:18080"
echo "  2. Click 'Trigger test alert' button"
echo "  3. Watch the agent investigate and analyze"
echo "  4. Review the recommendation and approve/reject"
echo ""
echo "Or run a chaos scenario:"
echo "  ./scripts/chaos/inject-crashloop.sh"
