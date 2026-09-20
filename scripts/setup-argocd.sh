#!/usr/bin/env bash
set -euo pipefail

echo "=== AI SRE PoC — ArgoCD Setup ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Create namespace
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

# Add Helm repo
helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update

# Install ArgoCD
echo "Installing ArgoCD..."
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --values "${PROJECT_DIR}/helm-values/argocd-values.yaml" \
  --wait --timeout 300s

# Wait for ArgoCD server
echo "Waiting for ArgoCD server..."
kubectl wait --for=condition=Ready pods -l app.kubernetes.io/name=argocd-server -n argocd --timeout=120s

# Ingress for the ArgoCD UI
echo "Applying ArgoCD ingress..."
kubectl apply -f "${PROJECT_DIR}/k8s/ingress/argocd.yaml"

# Register the GitOps Application. ArgoCD watches nothing without one.
if [ -n "${GIT_REPO_URL:-}" ]; then
  echo "Registering ArgoCD Application for ${GIT_REPO_URL}..."
  sed "s|https://github.com/YOUR_USERNAME/ai-sre-poc.git|${GIT_REPO_URL}|" \
    "${PROJECT_DIR}/k8s/argocd/application.yaml" | kubectl apply -f -
else
  echo ""
  echo "NOTE: GIT_REPO_URL not set — skipping Application registration."
  echo "      ArgoCD is installed but is not watching this repo yet."
  echo "      Push the repo to a remote, then re-run with:"
  echo "        export GIT_REPO_URL=https://github.com/<you>/ai-sre-poc.git"
  echo ""
fi

# Get initial admin password
ARGOCD_PASS=$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" 2>/dev/null | base64 -d || echo "not-yet-available")

echo ""
echo "=== ArgoCD setup complete ==="
echo ""
echo "Port-forward: kubectl port-forward -n argocd svc/argocd-server 8443:443"
echo "URL:          https://localhost:8443"
echo "Username:     admin"
echo "Password:     ${ARGOCD_PASS}"
echo ""
echo "Next: ./scripts/deploy-apps.sh"
