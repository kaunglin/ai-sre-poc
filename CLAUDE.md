# CLAUDE.md — AI SRE PoC Project Specification

Internal implementation spec — the level of detail an AI assistant (or a new
contributor) needs to make changes safely. It covers architecture, tech stack,
file layout, conventions, and the reasoning behind non-obvious decisions.

**Documentation map:**

| File | Audience | Contents |
|---|---|---|
| `README.md` | Anyone landing on the repo | What it is, architecture diagrams, setup, configuration, limitations |
| `TESTING.md` | Someone reproducing it | Chaos scenarios, verification commands, troubleshooting |
| `CLAUDE.md` | Someone changing it | This file — implementation detail and design rationale |

Keep all three in sync. Where they disagree, the running cluster wins — several
claims in this file were wrong until they were checked against a real run.

---

## Project overview

**Name:** AI SRE PoC — AI-Powered Incident Response on Kubernetes

**Purpose:** A proof-of-concept demonstrating an AI SRE agent that automatically detects incidents
on a Kubernetes cluster, investigates root causes using metrics/logs/cluster state, provides
fix recommendations via an LLM (OpenAI or Anthropic Claude), and executes approved remediations
with human-in-the-loop approval.

**Target outcome:** A portfolio project demonstrating AI-augmented operations for Cloud Architect /
Senior Cloud DevOps roles. The entire stack runs locally on a laptop using k3d (OrbStack as Docker runtime).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        CI/CD Layer                                   │
│   GitHub Actions (build + lint) ──→ ArgoCD (GitOps deploy)          │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     k3d Cluster (1 server + 3 agents)                │
│                     Registry: k3d-ai-sre-registry:5111               │
│                                                                      │
│  ┌─────────────────────────────┐  ┌────────────────────────────────┐ │
│  │   Namespace: apps           │  │   Namespace: monitoring        │ │
│  │                             │  │                                │ │
│  │  ┌─────────┐ ┌───────────┐ │  │  ┌────────────┐ ┌───────────┐ │ │
│  │  │Frontend │ │Order Svc  │ │──│→ │ Prometheus  │ │  Grafana  │ │ │
│  │  │(FastAPI)│ │(FastAPI)  │ │  │  └────────────┘ └───────────┘ │ │
│  │  └─────────┘ └───────────┘ │  │  ┌────────────┐ ┌───────────┐ │ │
│  │  ┌─────────┐ ┌───────────┐ │  │  │    Loki    │ │AlertMgr   │ │ │
│  │  │PostgreSQL│ │  Redis   │ │  │  └────────────┘ └─────┬─────┘ │ │
│  │  └─────────┘ └───────────┘ │  │                       │       │ │
│  └─────────────────────────────┘  └───────────────────────┼───────┘ │
│                                                           │         │
│  ┌────────────────────────────────────────────────────────┼───────┐ │
│  │   Namespace: ai-sre-agent                              │       │ │
│  │                                                        ▼       │ │
│  │   ┌──────────────────────────────────────────────────────────┐ │ │
│  │   │  AI SRE Agent (Python/FastAPI)                          │ │ │
│  │   │  - Receives AlertManager webhooks                       │ │ │
│  │   │  - Queries Prometheus API for metrics                   │ │ │
│  │   │  - Queries Loki API for logs                            │ │ │
│  │   │  - Queries K8s API for pod/deployment/event state       │ │ │
│  │   │  - Sends context to an LLM for root cause analysis      │ │ │
│  │   │  - Serves web dashboard for review + approve/reject     │ │ │
│  │   │  - Executes approved remediations via K8s API           │ │ │
│  │   └──────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   ┌─────────────┐    ┌──────────────┐    ┌───────────────┐
   │  LLM  API   │    │ Web Dashboard│    │ Slack (opt.)  │
   │ (analysis)  │    │ (approve/    │    │ (notifications│
   │             │    │  reject)     │    │  + approval)  │
   └─────────────┘    └──────────────┘    └───────────────┘
```

---

## Tech stack

| Component          | Technology                                | Notes                                       |
|--------------------|-------------------------------------------|----------------------------------------------|
| Container runtime  | OrbStack (Docker)                         | macOS, already installed                     |
| K8s cluster        | k3d (k3s in Docker)                       | 1 server + 3 agent nodes                    |
| Container registry | k3d built-in registry                     | localhost:5111 / k3d-ai-sre-registry:5111   |
| Ingress            | ingress-nginx                             | Helm chart, replaces default traefik         |
| App framework      | Python 3.12 + FastAPI                     | All three services                           |
| Database           | PostgreSQL 16 (alpine)                    | In-cluster, emptyDir volume                  |
| Cache              | Redis 7 (alpine)                          | In-cluster                                   |
| Metrics            | Prometheus (kube-prometheus-stack Helm)    | 15s scrape interval                          |
| Logs               | Loki + Promtail (loki-stack Helm)         | Chart deprecated upstream; image pinned to 2.9.8 — see note below |
| Dashboards         | Grafana (bundled with kube-prometheus)     | admin/admin                                  |
| Alerting           | AlertManager (bundled with kube-prometheus)| Webhook to AI agent                          |
| GitOps             | ArgoCD (argo-cd Helm)                     | Manual sync; needs `GIT_REPO_URL`; excludes `k8s/argocd/` |
| CI/CD              | GitHub Actions                            | Lint + build, deploy is via ArgoCD           |
| AI analysis        | OpenAI API (default) or Anthropic Claude   | `gpt-5.5` / `claude-opus-5`; pick via LLM_PROVIDER |
| AI agent           | Python + kubernetes client + openai/anthropic SDKs | In-cluster with ServiceAccount + RBAC |
| Notifications      | Slack webhook (optional)                  | Falls back to web dashboard only             |

> **On the Loki chart.** `grafana/loki-stack` is archived upstream and pins
> `grafana/loki:2.6.1`, which predates both `store: tsdb` (2.8+) and `schema: v13`
> (2.9+) that `helm-values/loki-values.yaml` configures. It therefore needs an
> explicit `image.tag: 2.9.8`, plus `tsdb_shipper.{active_index_directory,
> cache_location,shared_store}` and `compactor.working_directory` — all four
> default to empty strings and fail with `mkdir : no such file or directory`.
> It also sets `loki.isDefault: false`: the chart provisions its Grafana datasource as
> the default, and so does kube-prometheus-stack for Prometheus. Grafana allows one
> default per org and crash-loops otherwise ("Only one datasource per organization can
> be marked as default").
> Migrating to the current `grafana/loki` chart in SingleBinary mode is the real fix.

> **On ArgoCD sync policy.** Sync is deliberately manual. An automated policy with
> `selfHeal` would revert the chaos scenarios (bad image tag, lowered memory limit)
> before an alert could fire, so the agent would never see an incident. The
> Application also excludes `argocd/*` from its own path so it cannot overwrite its
> `repoURL` with the placeholder committed to the repo.

---

## File structure

```
ai-sre-poc/
├── CLAUDE.md                                    # THIS FILE — implementation spec
├── README.md                                    # Overview, architecture, setup
├── TESTING.md                                   # Chaos scenarios, verification, troubleshooting
├── .gitignore                                   # excludes __pycache__, .ruff_cache, .DS_Store
├── ruff.toml                                    # Pins the lint rule set CI runs
│
├── apps/                                        # Application source code
│   ├── order-service/                           # Main backend API
│   │   ├── main.py                              # FastAPI app with CRUD, metrics, fault injection
│   │   ├── requirements.txt                     # asyncpg, redis, fastapi, prometheus-client
│   │   └── Dockerfile                           # python:3.12-slim based
│   │
│   ├── frontend/                                # Web dashboard for the order app
│   │   ├── main.py                              # FastAPI app, proxies to order-service, serves HTML
│   │   ├── requirements.txt                     # fastapi, httpx, prometheus-client
│   │   └── Dockerfile
│   │
│   └── ai-sre-agent/                            # THE CORE — AI SRE agent
│       ├── agent.py                             # FastAPI app: webhook receiver, investigator, dashboard
│       ├── requirements.txt                     # openai + anthropic, kubernetes, fastapi, httpx
│       └── Dockerfile
│
├── k8s/                                         # Kubernetes manifests (ArgoCD watches this)
│   ├── namespaces.yaml                          # apps, ai-sre-agent namespaces
│   ├── apps/
│   │   └── all.yaml                             # PostgreSQL, Redis, order-service, frontend, ServiceMonitors
│   ├── ai-sre-agent/
│   │   └── all.yaml                             # RBAC, ConfigMap, Deployment, Service (NO Secret — see below)
│   ├── alerting/
│   │   └── prometheus-rules.yaml                # PrometheusRule CRDs for custom alerts
│   ├── ingress/
│   │   ├── apps.yaml                            # orders.localhost
│   │   ├── monitoring.yaml                      # grafana / prometheus / alertmanager.localhost
│   │   └── argocd.yaml                          # argocd.localhost
│   └── argocd/
│       └── application.yaml                     # ArgoCD Application (set repoURL to your remote)
│
├── helm-values/                                 # Helm chart value overrides
│   ├── kube-prometheus-stack-values.yaml         # AlertManager webhook config, scrape settings
│   ├── loki-values.yaml                          # Loki + Promtail config
│   └── argocd-values.yaml                        # ArgoCD minimal config for local
│
├── scripts/                                     # Setup and operational scripts
│   ├── setup-cluster.sh                          # Creates k3d cluster + local registry
│   ├── setup-observability.sh                    # Installs Prometheus, Grafana, Loki, AlertManager
│   ├── setup-argocd.sh                           # Installs ArgoCD (+ Application if $GIT_REPO_URL set)
│   ├── deploy-apps.sh                            # Builds images, pushes to registry, deploys apps
│   ├── deploy-ai-agent.sh                        # Builds + deploys the AI SRE agent
│   └── chaos/                                    # Fault injection scripts
│       ├── inject-crashloop.sh                   # Sets bad image → CrashLoopBackOff
│       ├── inject-oom.sh                         # Lowers memory limits → OOMKill
│       ├── inject-latency.sh                     # Adds 3s latency via fault API
│       ├── break-db.sh                           # Simulates DB disconnect via fault API
│       └── reset.sh                              # Restores everything to normal
│
└── .github/workflows/
    └── build-and-push.yml                        # CI: lint + build all 3 images
```

> **Why there is no Secret in `k8s/ai-sre-agent/all.yaml`.** `deploy-ai-agent.sh`
> creates `ai-sre-agent-secret`, then applies this manifest. A placeholder Secret in
> the file would overwrite the real values on every apply — and again on every ArgoCD
> sync. The Deployment's `secretRef` is marked `optional: true` so the agent still
> starts without it and reports "no API key" rather than crash-looping.
>
> The Secret holds `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DASHBOARD_PASSWORD`,
> `SESSION_SECRET` and `WEBHOOK_TOKEN`. The script **merges**: any value not supplied
> in the environment is kept from the existing Secret, and the password and session key
> are generated only the first time. An earlier version rebuilt the Secret from the
> shell alone, so running it without the API key exported silently replaced a working
> key with `not-set`.

---

## Application details

### Order Service (`apps/order-service/main.py`)

The main backend API that the AI agent monitors.

**Endpoints:**
- `GET /healthz` — liveness probe (always 200)
- `GET /readyz` — readiness probe (checks DB + Redis connectivity)
- `GET /metrics` — Prometheus metrics in OpenMetrics format
- `GET /api/orders` — list orders (cached in Redis)
- `POST /api/orders` — create an order
- `GET /api/orders/{id}` — get single order

**Fault injection endpoints** (used by chaos scripts):
- `POST /fault/latency?ms=3000` — add latency to all requests
- `POST /fault/errors?rate=0.5` — return 500 for X% of requests
- `POST /fault/memory-leak` — allocate 1MB per request (triggers OOM)
- `POST /fault/db-disconnect` — simulate database connection failure
- `POST /fault/reset` — clear all injected faults
- `GET /fault/status` — check current fault state

**Prometheus metrics exposed:**
- `http_requests_total{method, path, status, service}` — counter
- `http_request_duration_seconds{method, path, service}` — histogram

> **The label is `path`, not `endpoint`.** prometheus-operator sets its own `endpoint`
> target label (the ServiceMonitor port name) and renames any colliding metric label to
> `exported_endpoint` — which makes the series baffling to query.

> **`/metrics`, `/healthz` and `/readyz` are excluded from instrumentation.** Probe
> traffic is not user traffic. Counting a failing `/readyz` as a 5xx made
> `HighHTTPErrorRate` fire during a database outage in which no user request ever failed
> — and with no real traffic, probes were 100% of the sample, so the ratio hit 1.0. It
> also fired six minutes *after* recovery, because the 5m rate window still contained the
> failures.
- `orders_created_total{service}` — counter
- `db_connection_errors_total{service}` — counter
- `active_orders{service}` — gauge

**Logging:** Structured JSON to stdout via custom JSONFormatter.
Fields: `timestamp`, `level`, `logger`, `msg`, `module`, `function`, `exception`.

**Connection handling — `ensure_db()` / `ensure_redis()`.** PostgreSQL and Redis are
connected **lazily and retried on demand**, not once at startup. This matters because a
single `kubectl apply` creates all five Deployments simultaneously, so order-service
races PostgreSQL's ~7 second init and loses. Connecting once in the lifespan handler and
caching `None` on failure left the pod permanently `0/1` — it never recovered even after
the database was healthy, and the Service had no endpoints at all.

`/readyz` and all three order endpoints call `ensure_db()`, which reconnects if the pool
is missing and discards a dead pool so the next attempt retries. A readiness probe
*reports* health; something has to actually re-establish the connection, which is why
this lives in the app rather than in a manifest.

### Frontend (`apps/frontend/main.py`)

Simple web UI that proxies API calls to the order-service.
Serves an HTML dashboard at `/` with order list, create form, and health indicator.
All `/api/*` requests are proxied to `ORDER_SERVICE_URL`.

### AI SRE Agent (`apps/ai-sre-agent/agent.py`)

The core of the PoC. This is where all the intelligence lives.

**Provider abstraction.** `LLM_PROVIDER` selects `openai` (default) or `anthropic`.
`analyze_incident()` builds the prompt and parses the response; `_complete_openai()` and
`_complete_anthropic()` are thin transports that differ only in SDK call. Both SDKs ship
in the image, so switching is a ConfigMap edit plus a restart — no rebuild. The OpenAI
path uses Chat Completions with `response_format={"type": "json_object"}`, which
guarantees parseable JSON; the Anthropic path relies on prompt compliance plus the
markdown-fence stripping that both share.

**Async throughout.** Both transports use the async SDK clients, and the synchronous
Kubernetes client calls (`get_k8s_context`, `execute_remediation`) run via
`asyncio.to_thread`. With a single uvicorn worker, a blocking 10-second LLM call would
otherwise freeze the dashboard and the webhook endpoint for the whole investigation.

**Incident lifecycle:**
1. `INVESTIGATING` → Alert received, gathering context
2. `AWAITING_APPROVAL` → AI analysis complete, waiting for engineer
3. `APPROVED` → Engineer approved, about to execute
4. `EXECUTING` → Remediation in progress
5. `RESOLVED` → Approved remediation applied successfully
6. `AUTO_RESOLVED` → Alert cleared on its own; **no remediation was applied**
7. `REJECTED` → Engineer chose to handle manually
8. `FAILED` → Remediation attempt failed

> **`AUTO_RESOLVED` is deliberately separate from `RESOLVED`.** AlertManager sends
> resolution notifications (`send_resolved: true`); the agent uses them to close any
> incident still sitting in `INVESTIGATING` or `AWAITING_APPROVAL` for a condition that
> has since cleared — otherwise the dashboard accumulates approval requests for problems
> that no longer exist. Collapsing the two states would let the agent take credit for
> every self-healing blip, so `ai_sre_incidents_auto_resolved_total` tracks them apart
> from `ai_sre_remediations_total`. That ratio is the honest measure of whether the agent
> is doing anything useful.
>
> Only pending incidents are closed this way — one already `EXECUTING`, `RESOLVED`,
> `REJECTED` or `FAILED` is left alone, so a late notification cannot rewrite history.
> `AUTO_RESOLVED` is also excluded from the dedup filter, or an auto-closed incident would
> block a genuine re-fire of the same alert.

**Webhook endpoint:**
- `POST /webhook/alertmanager` — receives AlertManager alert payloads. Requires
  `Authorization: Bearer <WEBHOOK_TOKEN>` when the token is configured.

> **Why the webhook has its own token.** It cannot use the dashboard session, and the
> Ingress routes every path — so without a token anyone who can reach
> `ai-sre.localhost:18080` could inject incidents. The token lives in
> `monitoring/ai-sre-webhook` (created by `setup-observability.sh`, *before* the Helm
> install, because AlertManager mounts it via `alertmanagerSpec.secrets` and its pod
> cannot start without it). AlertManager reads it through `http_config.authorization.
> credentials_file`; `deploy-ai-agent.sh` copies it into the agent's Secret. If the
> token is missing the agent logs a warning and accepts unauthenticated calls.

Duplicate suppression keys on `(alertname, namespace, pod)`. Keying on `alertname` alone
would discard a second pod's crash-loop as a duplicate of the first. The dedup reads the
in-process working set, so it is per-process — another reason the Deployment is pinned to
`replicas: 1`.

**Incident persistence.** The in-memory dict is the working copy; SQLite at
`$DATA_DIR/incidents.db` (a 1Gi `ReadWriteOnce` PVC on k3d's local-path provisioner) is
the durable copy it is rebuilt from at startup.

- **Saving is driven by attribute assignment, not save() calls.** `Incident.__setattr__`
  marks the incident dirty and a background task flushes dirty incidents once a second
  (and once more on shutdown). A new code path that mutates an incident cannot forget to
  persist it. `add_event()` assigns `updated_at`, so timeline appends are covered.
- **Restart recovery.** An incident caught in `INVESTIGATING` is resumed — its analysis
  was going to run anyway. One caught in `APPROVED`/`EXECUTING` is marked `FAILED`: the
  patch may or may not have landed, and re-running it blind is exactly what needs a human.
- **Retention is 7 days** (`HISTORY_DAYS`), enforced at startup and hourly, in memory and
  on disk. It is also the widest window `GET /api/incidents` accepts — there is nothing
  older to return.
- **`strategy: Recreate`.** A rolling update would start the new pod while the old one
  still holds the RWO volume and is still writing SQLite.

Local-path volumes live on one node's disk: history survives pod restarts and redeploys,
not `k3d cluster delete`.

**Investigation pipeline (runs async on each new alert):**
1. Parse alert labels and annotations
2. Query Prometheus for relevant metrics:
   - Pod restart counts
   - Pod phases
   - Container memory working set
   - CPU usage rates
   - HTTP request rates and latency (if HTTP-related alert)
3. Query Loki for recent logs:
   - Error/exception logs from the namespace
   - Pod-specific logs if pod name is known
4. Query K8s API:
   - Pod status and container states
   - All pods in namespace summary
   - Recent events (last 15, sorted by time)
   - Deployment status (replicas, images)
5. Send all context to the configured LLM with a structured prompt
6. Parse JSON response for: root_cause, analysis, recommended_action, action_type, action_params
7. Update incident status to AWAITING_APPROVAL
8. Optionally notify Slack

**Remediation actions the agent can execute:**
- `restart_pods` — patches a pod-template annotation to trigger a rolling restart
- `rollback_deployment` — restores the pod template from the previous ReplicaSet revision
- `scale_up` — patches deployment replica count
- `adjust_resources` — container-scoped strategic merge patch of requests/limits
- `manual` — no automation, engineer handles it

Any unrecognised `action_type` returns `"Unknown action type"` and does nothing. That
fixed vocabulary is the primary safety bound: the model never emits a command, only a
verb plus parameters.

> **How `rollback_deployment` works.** `apps/v1` has no rollback endpoint — the old
> `extensions/v1beta1` one was removed. `rollback_deployment()` does what
> `kubectl rollout undo` does: read the deployment's current revision, list ReplicaSets
> matching its selector, pick the highest revision below the current one, strip
> `pod-template-hash` (owned by the deployment controller — sending it back would pin
> the deployment to the old ReplicaSet), and patch that pod template onto the deployment.
>
> The result carries a *new* revision number rather than reverting to the old one, which
> is why the agent records `rolledBackFrom` / `rolledBackTo` annotations separately from
> `deployment.kubernetes.io/revision`. Verified: rolling back from a bad image at
> revision 3 produced revision 4 with the restored template, scaled revision 3 to zero,
> and never terminated the healthy pods — zero downtime.

> **`adjust_resources` patch shape.** It builds `{"spec":{"template":{"spec":
> {"containers":[{"name": <name>, "resources": {...}}]}}}}`. A strategic merge patch
> matches containers by name, so the rest of the pod spec is untouched. Passing a whole
> `V1Deployment` object as the patch body works by accident and is fragile.

**Approval shows the arguments, not just the verb.** `to_dict()` includes
`recommended_action_params`, and the dashboard renders them under the recommended action
(`deployment: postgres · namespace: apps · replicas: 1`). Approving `scale_up` without
seeing its target deployment and replica count is not a meaningful review. When the model
supplies no parameters the UI says so explicitly rather than rendering a blank line.

Relatedly, `scale_up` has **no hardcoded replica default**. It reads the deployment's
current count and adds one. The previous `params.get("replicas", 3)` meant an omitted
field would silently scale a database to three instances — each with its own `emptyDir`,
so three independent databases behind one Service.

**Dashboard endpoints** (all require a signed-in session):
- `GET /` — web dashboard (dark theme, auto-refreshes every 5s)
- `GET /api/incidents?since=&until=&q=` — incidents created in the window, newest first.
  ISO 8601 timestamps; defaults to the last 7 days. `400` if the window is wider than 7
  days, starts more than 7 days ago, or is inverted. `q` is a search: every
  whitespace-separated term must appear (AND, case-insensitive, max 8 terms) somewhere in
  the alert name, pod, ID, namespace, summary, description, root cause, analysis,
  recommended action, timeline — or the **gathered evidence** (`logs_context`, the Loki
  lines, and `k8s_context`, pod states and events). Each hit carries
  `match: {field, snippet}`, the line where the first term was found, because the evidence
  fields are not shown on the card
- `GET /api/incidents/{id}` — single incident detail
- `POST /api/incidents/{id}/approve` — approve and execute remediation
- `POST /api/incidents/{id}/reject` — reject, mark for manual handling (only while
  `AWAITING_APPROVAL`)
- `POST /api/incidents/{id}/reinvestigate`, `GET|POST /api/config` — tool-calling mode
- `POST /api/test-alert` — trigger a fake alert for testing the pipeline
- `GET /api/me` — the signed-in user

**Public:** `GET|POST /login`, `/healthz`, `/metrics`, and the webhook (its own token).
`POST /logout` clears the session.

**Sign-in.** One user, `DASHBOARD_USERNAME` (default `admin`), password from
`DASHBOARD_PASSWORD` in the Secret. Sessions are Starlette's signed cookie
(`itsdangerous`), 12 hours, keyed by `SESSION_SECRET` — kept stable by the deploy script
so a redeploy does not sign everyone out. Details that matter:

- **Fails closed.** No `DASHBOARD_PASSWORD` → every login returns `503`. An approve button
  open to anyone on the network is worse than a dashboard nobody can reach.
- **`SameSite=strict`** keeps the cookie off cross-site requests — that is what stops
  another page from POSTing `/approve` in a signed-in engineer's browser.
- Both fields are compared with `hmac.compare_digest`, always, and a failure waits 1s.
- `https_only=False` because the Ingress is plain `http://*.localhost`. Flip it behind TLS.
- Approve/reject record the user in the timeline (`Remediation approved by admin`).

**Dashboard UI.** Approve and reject open an in-page dialog, not the browser's
`confirm()`: it shows the action and every parameter, the model's confidence and risk,
focuses **Cancel** (Enter does not execute), traps Tab, and stays open showing
"Executing…" until the API returns; the result arrives as a toast. The time-range bar
offers 1h / 6h / 24h / 3d / 7d presets (relative — they slide forward on each refresh;
the choice is remembered in `localStorage`) and a custom From/To range with the same
7-day rules as the API, checked client-side first. Any `401` sends the browser to
`/login`.

The search box (focus it with `/`, clear with Esc) queries the server 250 ms after typing
stops and combines with the time range. The client only highlights: `hl()` splits the raw
text on a capture group of the terms and escapes every piece separately, so highlighting
can never splice markup into untrusted incident text.

Each card can be **minimised** (header, ID, one-line root cause and any search snippet)
or **maximised** (a full-page view, closed with Esc or ✕, focus returned to the card).
Open incidents start expanded and closed ones minimised; a manual toggle overrides that
per incident and is kept in `localStorage`, so the 5-second refresh — which re-renders
the list — does not undo it. The maximised view re-renders on each refresh too, so a
status change shows up while you read.

**Access:** an Ingress (`ai-sre.localhost`, via ingress-nginx on host port 18080) rather
than a port-forward. A port-forward is a single TCP tunnel bound to one pod and dies when
that pod is replaced — which is exactly when a remediation is being watched.

**RBAC:** The agent runs with a ServiceAccount that has:
- Read access: pods, pods/log, events, services, configmaps, namespaces, HPA
- Read: replicasets (rollback reads the previous revision), statefulsets
- Read + patch: deployments — the only write the agent makes

> **No `update`, and `patch` only on Deployments.** Every remediation is a
> `patch_namespaced_deployment` call, so granting `update` (full replace) or write access
> to ReplicaSets and StatefulSets only widened the blast radius. Note that RBAC cannot
> restrict *which fields* a patch touches — a patch can change an image as easily as a
> replica count. The fixed action vocabulary is what bounds the patch body; RBAC bounds
> which objects it can land on. The role is still a `ClusterRole`; a namespaced `Role`
> in `WATCH_NAMESPACE` would shrink it further.

---

## Kubernetes namespaces

| Namespace       | Purpose                                         |
|-----------------|--------------------------------------------------|
| `apps`          | Order service, frontend, PostgreSQL, Redis       |
| `monitoring`    | Prometheus, Grafana, Loki, AlertManager          |
| `ai-sre-agent`  | AI SRE agent deployment + RBAC                  |
| `argocd`        | ArgoCD server and controllers                    |
| `ingress-nginx` | Ingress controller                               |

---

## Alert rules (`k8s/alerting/prometheus-rules.yaml`)

| Alert name               | Condition                              | Severity | Category   |
|--------------------------|----------------------------------------|----------|------------|
| PodCrashLoopBackOff      | >3 restarts in 5m                      | critical | pod        |
| PodOOMKilled             | Container terminated with OOMKilled    | critical | memory     |
| PodNotReady              | Pod not ready for 2m+                  | warning  | pod        |
| HighPodRestartRate       | >5 restarts in 1h                      | warning  | pod        |
| HighHTTPErrorRate        | >5% 5xx responses over 5m             | critical | http       |
| HighHTTPLatency          | p95 latency >2s over 5m               | warning  | latency    |
| HighMemoryUsage          | >85% of memory limit for 2m           | warning  | memory     |
| HighCPUUsage             | >85% of CPU limit for 5m              | warning  | cpu        |
| DeploymentReplicaMismatch| Available != desired replicas for 3m  | warning  | deployment |

> **`DeploymentReplicaMismatch` aggregates with `max by (namespace, deployment)`.** Without
> it the series inherits kube-state-metrics' own `pod` label, so the alert names the
> exporter pod as "affected" and the agent then gathers logs and status for that pod
> instead of the deployment that is actually broken.

> **The three restart/termination rules carry a live-pod guard**
> (`and on (namespace, pod) kube_pod_status_phase{phase="Running"} == 1`). Prometheus keeps
> a deleted pod's series, so `increase(...[1h]) > 5` kept firing for a pod that had been
> gone for many minutes — a warning-level alert about nothing, which the agent then had to
> reason its way out of. The 1h window on `HighPodRestartRate` made it linger longest.

> **Coverage gap: no ImagePullBackOff rule.** Every pod-related rule above keys on
> restart counts or readiness. A pod that never starts — bad image tag, missing
> registry credentials — increments no restart counter, so only `PodNotReady` catches
> it, indirectly, after 2 minutes. The built-in `KubeContainerWaiting` covers it
> properly but has `for: 1h`. Worth adding, given the headline chaos scenario produces
> exactly this condition.

> **These rules are applied by `setup-observability.sh`, not by Helm.** They live in a
> `PrometheusRule` CRD labelled `release: kube-prometheus-stack`, matching the
> `ruleSelector` the chart configures. If `kubectl get prometheusrule ai-sre-poc-alerts
> -n monitoring` returns nothing, the script aborted before reaching that step — see
> Conventions → Helm below.

---

## Chaos scenarios

Each scenario is designed to trigger specific alerts and test the AI agent's investigation pipeline.

> **Verification status:** Scenarios 1 and 2 have been run end to end and their documented
> behaviour is observed, not assumed. Scenarios 3–4 are derived from the code and alert rules
> but have **not** been verified in a full run — treat their expected alerts and actions as
> predictions. `TESTING.md` carries the same markers and the verification commands.

### Scenario 1: CrashLoopBackOff
**Script:** `scripts/chaos/inject-crashloop.sh`
**What it does:** Sets order-service image to a non-existent tag
**Expected alerts:** PodNotReady (warning, fires after 2m)

> Despite the scenario name, `PodCrashLoopBackOff` does **not** fire here. A
> non-existent tag produces ImagePullBackOff — the container never starts, so
> `kube_pod_container_status_restarts_total` stays at 0 and the rule's
> `increase(...) > 3` can never be satisfied. `DeploymentReplicaMismatch` does
> not fire either: the default rolling update keeps the old pods running until
> the new one is ready, so available replicas still equal desired. The project
> has no rule covering ImagePullBackOff; the built-in `KubeContainerWaiting`
> catches it, but only after `for: 1h`.

**Expected AI action:** `rollback_deployment` (a restart cannot fix a bad image tag)

### Scenario 2: OOMKill ✅ verified
**Script:** `scripts/chaos/inject-oom.sh`
**What it does:** Reduces memory limit to 32Mi (too low for Python + FastAPI)
**Alerts observed:** `PodOOMKilled`, `PodCrashLoopBackOff`, `HighPodRestartRate`.
`DeploymentReplicaMismatch` does **not** fire — the rolling update keeps the old pods
available, exactly as in scenario 1.
**AI action observed:** `manual` (×3) — **not** `adjust_resources`

No load generation is needed: `uvicorn --workers 2` cannot start inside 32Mi, so it OOMs
immediately. Unlike scenario 1 the container *does* start before dying, which is why the
restart-based rules fire here and cannot there.

> **Open issue: the model declines the action built for this case.** Across all three
> incidents it chose `manual`, reasoning that two healthy replicas were still serving so
> the service was not really down. The prompt's *"only recommend automated actions when you
> are confident… for complex or risky situations, recommend manual"* appears to push it
> toward caution. Prompt tuning, not a code bug — but it means the project's flagship
> `adjust_resources` path is currently unexercised by its own scenario.

> **Second run (2026-11-20), after the prompt began listing each action's parameters.**
> The failure looked different: the container was not `OOMKilled` — Uvicorn's worker
> processes died and the parent exited `Completed` — so `PodOOMKilled` never fired. The
> agent received `PodCrashLoopBackOff`, `PodNotReady` and `HighPodRestartRate` (the last
> auto-resolved). Both live incidents recommended `rollback_deployment` (confidence high,
> risk low) rather than `manual`, and the approved rollback restored the 256Mi limit. A
> valid fix — but the root-cause text described "workers die" without naming the 32Mi
> limit, and `adjust_resources` was still not chosen. One run; treat it as a signal.
>
> **New gap this exposed: one fault, several approvable incidents.** Dedup keys on
> `(alertname, namespace, pod)`, so `PodCrashLoopBackOff` and `PodNotReady` for the same
> pod are separate incidents, each proposing `rollback_deployment`. Approving **both**
> rolls back twice — and the second rollback undoes the first, restoring the broken
> revision. Correlating incidents by target deployment (or refusing a second remediation
> on a deployment patched in the last few minutes) would close it.

### Scenario 3: High latency
**Script:** `scripts/chaos/inject-latency.sh`
**What it does:** Calls fault injection API to add 3000ms to every request
**Expected alerts:** HighHTTPLatency *(unverified)*
**Expected AI action:** `restart_pods` (clears in-memory fault state)

Requires working ServiceMonitor scraping *and* live traffic — a p95 latency
percentile does not exist without requests.

### Scenario 4: Database disconnect
**Script:** `scripts/chaos/break-db.sh`
**What it does:** Calls fault injection API to simulate DB being down
**Expected alerts:** PodNotReady (readiness probe fails), HighHTTPErrorRate *(unverified)*
**Expected AI action:** `restart_pods` or `manual`

`manual` is a legitimate outcome here: from the cluster's perspective PostgreSQL is
perfectly healthy, so the evidence points at the application, not at anything a
deployment patch can fix.

### Reset
**Script:** `scripts/chaos/reset.sh`
**What it does:** Calls `/fault/reset`, re-applies original manifests, rolling restart

---

## Port-forward reference

| Service        | Command                                                                   | URL                     |
|----------------|---------------------------------------------------------------------------|-------------------------|
| **AI SRE Agent** | *(Ingress — no port-forward)*                                          | **http://ai-sre.localhost:18080** |
| Grafana        | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80` | http://localhost:3000   |
| Prometheus     | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090` | http://localhost:9090   |
| AlertManager   | `kubectl port-forward -n monitoring svc/kube-prometheus-stack-alertmanager 9093:9093` | http://localhost:9093   |
| ArgoCD         | `kubectl port-forward -n argocd svc/argocd-server 8443:443`             | https://localhost:8443  |
| Frontend       | `kubectl port-forward -n apps svc/frontend 3001:3000`                    | http://localhost:3001   |
| Order Service  | `kubectl port-forward -n apps svc/order-service 8000:8000`              | http://localhost:8000   |

The k3d load balancer publishes ingress-nginx on host ports **18080/18443**, deliberately
clear of the 8080/8443 used by the port-forwards above. The agent is reached through an
Ingress on that LB rather than a port-forward: a port-forward is one TCP tunnel bound to a
single pod and dies whenever that pod is replaced — i.e. right when a remediation runs.

---

## Environment variables

### AI SRE Agent
| Variable            | Default                                                         | Required |
|---------------------|-----------------------------------------------------------------|----------|
| `LLM_PROVIDER`      | `openai`                                                        | No       |
| `OPENAI_API_KEY`    | (none)                                                          | If provider is `openai` |
| `OPENAI_MODEL`      | `gpt-5.5`                                                       | No       |
| `ANTHROPIC_API_KEY` | (none)                                                          | If provider is `anthropic` |
| `PROMETHEUS_URL`    | `http://kube-prometheus-stack-prometheus.monitoring:9090`        | No       |
| `LOKI_URL`          | `http://loki.monitoring:3100`                                   | No       |
| `SLACK_WEBHOOK_URL` | (none)                                                          | No       |
| `WATCH_NAMESPACE`   | `apps`                                                          | No       |
| `CLAUDE_MODEL`      | `claude-opus-5`                                                 | No       |
| `DASHBOARD_USERNAME`| `admin`                                                         | No       |
| `DASHBOARD_PASSWORD`| (none — sign-in refused)                                        | Yes, to sign in |
| `SESSION_SECRET`    | random per process (restarts sign everyone out)                 | No       |
| `WEBHOOK_TOKEN`     | (none — webhook unauthenticated, warning logged)                | Recommended |
| `DATA_DIR`          | `/data`                                                         | No       |

### Order Service
| Variable       | Default                                     | Required |
|----------------|---------------------------------------------|----------|
| `DB_HOST`      | `postgres.apps.svc.cluster.local`           | No       |
| `DB_PORT`      | `5432`                                      | No       |
| `DB_NAME`      | `orders`                                    | No       |
| `DB_USER`      | `orders`                                    | No       |
| `DB_PASSWORD`  | `orders-secret`                             | No       |
| `REDIS_URL`    | `redis://redis.apps.svc.cluster.local:6379` | No       |

### Frontend
| Variable            | Default                                             | Required |
|---------------------|-----------------------------------------------------|----------|
| `ORDER_SERVICE_URL` | `http://order-service.apps.svc.cluster.local:8000`  | No       |

---

## Conventions

### Code style
- Python 3.12+ with type hints
- FastAPI for all HTTP services
- Structured JSON logging to stdout (no file logging)
- Prometheus metrics on every service at `/metrics`
- Health checks: `/healthz` (liveness), `/readyz` (readiness)

### Container images
- Base: `python:3.12-slim`
- Registry: `localhost:5111` (host) / `k3d-ai-sre-registry:5111` (in-cluster)
- Tag: `latest` for local dev, `{git-sha}` in CI

> **`kubectl apply` reconciles the manifest, not the image.** Behind a floating `:latest`
> tag the manifest is byte-identical after a rebuild, so apply reports `unchanged`, no pod
> is created, and the newly pushed image is never pulled — `imagePullPolicy: Always` does
> not help, because a pull only happens when a pod is created. Both deploy scripts
> therefore run `kubectl rollout restart` after applying. The immutable-tag approach the
> CI workflow uses (`{git-sha}`) avoids the problem entirely.

### Kubernetes
- One combined manifest per component group (`all.yaml`) for simplicity
- Secrets are **never** committed in manifests — created by deploy scripts (see file structure)
- Labels: `app: <service-name>`, `app.kubernetes.io/part-of: ai-sre-poc`
- Resource requests and limits on every container
- Liveness and readiness probes on every container
- ServiceMonitor CRDs for Prometheus scraping (label: `release: kube-prometheus-stack`)

### Helm
- Values files in `helm-values/` directory
- All Helm releases use `--wait` flag for predictable scripting

> **`--wait` plus `set -e` is a trap, and it cost this project its entire alerting layer.**
> `setup-observability.sh` runs `set -euo pipefail`. When Loki crash-looped, `helm --wait`
> timed out and returned non-zero, killing the script *before* `kubectl apply -f k8s/alerting/`.
> The cluster came up looking healthy, the agent processed incidents, and not one of the
> nine custom alert rules existed — every incident came from kube-prometheus-stack's
> built-in `Kube*` rules instead.
>
> The alert rules are now applied **immediately after kube-prometheus-stack and before
> Loki**, since the CRDs are their only real dependency. Ordering steps by dependency
> rather than by narrative keeps a failure in one subsystem from silently disabling
> another. Verify with `kubectl get prometheusrule -n monitoring`.

---

## Setup sequence

Scripts must be run in this order:

```
1. scripts/setup-cluster.sh        → k3d cluster + registry + ingress-nginx
2. scripts/setup-observability.sh  → Prometheus + Grafana + Loki + AlertManager + alert rules
3. scripts/setup-argocd.sh         → ArgoCD (set GIT_REPO_URL to register the Application)
4. scripts/deploy-apps.sh          → Build images + deploy PostgreSQL, Redis, order-service, frontend
5. scripts/deploy-ai-agent.sh      → Build + deploy AI SRE agent (needs OPENAI_API_KEY env var)
```

After setup, the full incident response loop is:
```
Chaos script → Alert fires → AlertManager webhook → AI Agent investigates →
LLM analysis → Dashboard shows recommendation → Engineer approves →
Agent executes remediation → Incident resolved
```

---

## Known gaps and future work

Ordered by how much they limit the PoC. The first four are the ones a reviewer will
notice; `README.md` carries a shorter version of this list.

**1. Single replica.** Incidents now persist (SQLite on a PVC, 7-day retention), so a
restart no longer loses history. But SQLite on a `ReadWriteOnce` volume is single-writer,
and the dedup filter and investigation tasks still work from one process's memory — so
the agent stays at `replicas: 1`. A shared database (PostgreSQL) plus a lock or queue for
"who investigates this alert" is what unblocks scaling out.

**2. No verification step.** After a remediation the status goes to `RESOLVED` based
solely on whether the K8s patch call threw. Nothing re-checks that the alert cleared.
A patch that applies cleanly but doesn't fix the problem still reports success — note
the README's original claim that the agent "confirms the fix resolved the issue" was
never true. The fix: re-query the alert after a grace period, mark `RESOLVED` only when
it clears, and auto-rollback when it doesn't.

**2b. One fault can produce several approvable remediations.** Different alerts for the
same broken deployment become separate incidents, each proposing its own patch. Approving
two `rollback_deployment`s rolls back twice, and the second one restores the broken
revision. See scenario 2. Correlate by target deployment, or refuse a remediation on a
deployment patched in the last few minutes.

**3. No ImagePullBackOff alert rule.** See the coverage-gap note under Alert rules.

**4. Single watched namespace.** `WATCH_NAMESPACE` is one string, though RBAC is
cluster-wide.

**5. The model under-uses automated actions.** In the first OOM run it recommended `manual`
three times where `adjust_resources` was the correct fix (the second run chose
`rollback_deployment` — see scenario 2). Approving `manual` is a no-op by
design, so the incident is left to a human — safe, but it means the automation rarely
engages. Worth A/B testing a less cautious phrasing of the action-selection guidance.

**6. Ephemeral storage.** PostgreSQL and Loki both use `emptyDir`.

**7. Deprecated Loki chart.** Works with an explicit image pin; migrating to
`grafana/loki` SingleBinary is the real fix.

**8. Unpinned `ruff` version in CI.** The workflow runs `pip install ruff`. The rule set
is now pinned by `ruff.toml` (`E4`, `E7`, `E9`, `F`, `I`), so a new release can no longer
change which rules run — but the ruff version itself is still floating, so a rule's
behaviour can still shift between releases. Pin it in the workflow to close this fully.

### Ideas not yet started

- Slack interactive buttons (approve/reject without the dashboard)
- Confidence-gated auto-remediation — skip approval only for `low` risk + `high`
  confidence + reversible actions
- Grafana dashboard auto-provisioning
- Runbook auto-generation from resolved incidents
- CI/CD failure detection (GitHub Actions webhook → agent)
- PagerDuty / Opsgenie integration
- Multi-cluster support
