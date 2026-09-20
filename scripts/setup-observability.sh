#!/usr/bin/env bash
set -euo pipefail

echo "=== AI SRE PoC — Observability Setup ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Create monitoring namespace
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

# Add Helm repos
echo "Adding Helm repos..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update

# Install kube-prometheus-stack (Prometheus + Grafana + AlertManager)
echo "Installing kube-prometheus-stack..."
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values "${PROJECT_DIR}/helm-values/kube-prometheus-stack-values.yaml" \
  --wait --timeout 300s

# Apply custom alert rules.
# These depend only on the kube-prometheus-stack CRDs, so they are applied
# BEFORE Loki. Previously they came last, and because `set -e` is on, a failed
# Loki install aborted the script and silently left the cluster with no custom
# alerting at all — the agent would then only ever see built-in Kube* alerts.
echo "Applying custom alert rules..."
kubectl apply -f "${PROJECT_DIR}/k8s/alerting/"

# Ingress for Grafana / Prometheus / AlertManager. Applied here because this
# script owns the monitoring namespace.
echo "Applying monitoring ingress..."
kubectl apply -f "${PROJECT_DIR}/k8s/ingress/monitoring.yaml"

# Install Loki for log aggregation
echo "Installing Loki..."
helm upgrade --install loki grafana/loki-stack \
  --namespace monitoring \
  --values "${PROJECT_DIR}/helm-values/loki-values.yaml" \
  --wait --timeout 180s

# Wait for all pods
echo "Waiting for monitoring pods to be ready..."
kubectl wait --for=condition=Ready pods --all -n monitoring --timeout=300s 2>/dev/null || true

echo ""
echo "=== Observability setup complete ==="
echo ""
echo "Port-forward commands (run in separate terminals):"
echo "  Grafana:       kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80"
echo "  Prometheus:    kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090"
echo "  AlertManager:  kubectl port-forward -n monitoring svc/kube-prometheus-stack-alertmanager 9093:9093"
echo ""
echo "Grafana credentials: admin / admin"
echo ""
echo "Next: ./scripts/setup-argocd.sh"
