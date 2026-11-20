# Testing Guide

How to break the application on purpose and verify the agent actually fixes it.

The theme running through this guide: **a remediation that reports success is not the same
as a remediation that worked.** Every scenario below includes a check against the cluster,
not against the agent's own status field.

---

## Contents

- [Before you start](#before-you-start)
- [Scenario 1 — bad image tag](#scenario-1--bad-image-tag) ✅ verified
- [Scenario 2 — OOMKill](#scenario-2--oomkill) ✅ verified
- [Scenario 3 — high latency](#scenario-3--high-latency)
- [Scenario 4 — database outage](#scenario-4--database-outage)
- [Resilience test — dependency outage](#resilience-test--dependency-outage) ✅ verified
- [Triggering DeploymentReplicaMismatch](#triggering-deploymentreplicamismatch) ✅ verified
- [Troubleshooting](#troubleshooting)
- [Useful queries](#useful-queries)

> Scenarios 1 and 2, the resilience test, and the DeploymentReplicaMismatch recipe have
> been run end to end — their outputs below are real. **Scenarios 3 and 4 have not been
> verified**; treat their expected results as predictions and please correct this file if
> yours differ. Two of scenario 1's originally documented alerts turned out to be
> impossible, so predictions here are not reliable until run.

---

## Before you start

Confirm a healthy baseline. Injecting a fault into an already-broken cluster teaches you
nothing.

```bash
kubectl get pods -A | grep -v Running | grep -v Completed   # should print only the header
kubectl get pods -n apps                                    # 5 pods, all 1/1
```

Open the dashboard and leave it running:

```bash
open http://ai-sre.localhost:18080
```

This goes through the Ingress, so it survives pod restarts and remediations. A
`kubectl port-forward` is bound to a single pod and drops the moment that pod is
replaced — which is precisely when a chaos test gets interesting.

**Check a rollback target exists.** Several scenarios recommend `rollback_deployment`,
which needs a previous ReplicaSet revision to return to. On a freshly created deployment
there isn't one, and the agent will correctly report "nothing to roll back to":

```bash
kubectl get rs -n apps -l app=order-service -o json | python3 -c "
import sys,json
for rs in json.load(sys.stdin)['items']:
    print(' rev', rs['metadata']['annotations'].get('deployment.kubernetes.io/revision'),
          rs['spec']['template']['spec']['containers'][0]['image'].split('/')[-1])"
```

If only one revision exists, run `kubectl rollout restart deployment/order-service -n apps`
first to create history.

---

## Scenario 1 — bad image tag

```bash
./scripts/chaos/inject-crashloop.sh
```

Points `order-service` at `:does-not-exist`.

### What actually happens

> **Despite the script's name, `PodCrashLoopBackOff` does not fire.** A non-existent tag
> produces **ImagePullBackOff** — the container never starts, so
> `kube_pod_container_status_restarts_total` stays at 0 and that rule's
> `increase(...) > 3` can never be satisfied.
>
> `DeploymentReplicaMismatch` does not fire either. The default rolling update creates one
> new pod and keeps the old ones running until it is ready. It never is, so available
> replicas still equal desired.
>
> The alert you get is **`PodNotReady`** (warning, `for: 2m`).

This matters for a subtle reason: the service stays *up* the whole time. Two healthy pods
keep serving. The deployment is stuck, not down — which is exactly the kind of thing that
sits unnoticed until the next scaling event.

### Watch it

```bash
kubectl get pods -n apps -w      # new pod goes ImagePullBackOff, old ones stay Running
```

After ~2 minutes, check which alerts fired:

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/alerts' \
  | python3 -c "
import sys,json
for a in json.load(sys.stdin)['data']['alerts']:
    if a['labels'].get('namespace')=='apps':
        print(' ', a['labels']['alertname'], a['state'], a['labels'].get('severity'))"
```

### What the agent produced

```
PodNotReady — order-service-68b77d4db5-h295f          WARNING   AWAITING APPROVAL

ROOT CAUSE
  The order-service deployment was updated to a non-existent image tag, causing
  the new pod to remain in ImagePullBackOff.

AI ANALYSIS
  ...container is Waiting with ImagePullBackOff because Kubernetes cannot pull
  k3d-ai-sre-registry:5111/order-service:does-not-exist; events confirm repeated
  ErrImagePull and NotFound errors. There are no container restarts, no application
  error logs, and CPU/memory metrics for the existing running pods look normal. The
  deployment currently reports 2 available replicas and two older pods are still
  Running, so the service appears to remain available, but the rollout is stuck and
  any further scaling/rescheduling could reduce availability.

RECOMMENDED ACTION (ROLLBACK_DEPLOYMENT)
  Rollback the order-service deployment in namespace apps to the previous working
  revision...

TIMELINE
  14:54:50  Alert received: PodNotReady
  14:54:50  Gathering metrics from Prometheus
  14:54:51  Gathering logs from Loki
  14:54:51  Gathering Kubernetes resource status
  14:54:51  Sending context to openai for analysis
  14:54:58  AI analysis complete — confidence: high, risk: low
```

Eight seconds, and it picked the only action of the five that can actually fix a bad image
tag. `restart_pods` would have recreated pods with the same broken image.

### Approve, then verify

Click **Approve & execute**, then check the cluster — not the dashboard:

```bash
# 1. Image restored?
kubectl get deployment order-service -n apps \
  -o jsonpath='{.spec.template.spec.containers[0].image}'; echo

# 2. Did the agent leave its fingerprints?
kubectl get deployment order-service -n apps \
  -o jsonpath='{.metadata.annotations}' | tr ',' '\n' | grep rolledBack

# 3. Which ReplicaSet is serving?
kubectl get rs -n apps -l app=order-service -o json | python3 -c "
import sys,json
for rs in sorted(json.load(sys.stdin)['items'],
                 key=lambda r: int(r['metadata']['annotations'].get('deployment.kubernetes.io/revision','0'))):
    print(f\"  rev {rs['metadata']['annotations'].get('deployment.kubernetes.io/revision')}:\",
          f\"replicas={rs['spec']['replicas']} ready={rs['status'].get('readyReplicas',0)}\",
          rs['spec']['template']['spec']['containers'][0]['image'].split('/')[-1])"
```

**Verified output:**

```
image:       k3d-ai-sre-registry:5111/order-service:latest
annotations: ai-sre-agent/rolledBackFrom = 3
             ai-sre-agent/rolledBackTo   = 2

  rev 1: replicas=0 ready=0  order-service:latest
  rev 3: replicas=0 ready=0  order-service:does-not-exist    ← killed
  rev 4: replicas=2 ready=2  order-service:latest            ← live
```

The pods keep their original age and `restarts=0` — the rollback is zero-downtime, because
the healthy pods were never terminated.

> **Why revision 4 and not 2?** `kubectl rollout undo` doesn't travel back in time. Patching
> an old pod template onto the deployment makes Kubernetes reuse that ReplicaSet and assign
> it a *new* revision number. The agent does the same thing, which is why the annotations
> record `rolledBackFrom: 3, rolledBackTo: 2` while the live revision reads 4.

Reset:
```bash
./scripts/chaos/reset.sh
```

---

## Scenario 2 — OOMKill

```bash
./scripts/chaos/inject-oom.sh
```

Drops the memory limit to 32Mi, well under what Python + FastAPI needs.

**No load generation needed.** The container runs `uvicorn --workers 2` — a parent plus
two Python workers do not fit in 32Mi, so it OOMs at startup. The script's suggestion to
generate traffic assumes the app starts and then grows; it never gets that far.

### Verified results

| Alert | Fired? | Notes |
|---|---|---|
| `PodOOMKilled` | ✅ | `exitCode: 137`, `reason: OOMKilled` |
| `PodCrashLoopBackOff` | ✅ | Restarts **do** increment here, unlike scenario 1 |
| `HighPodRestartRate` | ✅ | |
| `DeploymentReplicaMismatch` | ❌ | Rolling update keeps the old pods available — same reason as scenario 1 |

The contrast with scenario 1 is the point: a bad image never starts, so restart-based rules
cannot see it; an OOM starts and dies, so they can.

```bash
kubectl get pods -n apps -w        # OOMKilled -> CrashLoopBackOff
kubectl get pod <pod> -n apps -o jsonpath='{.status.containerStatuses[0].lastState}'
```

### The agent recommended `manual`, not `adjust_resources`

This is the notable result. Across three separate incidents (`PodOOMKilled`,
`PodCrashLoopBackOff`, `PodNotReady`) the model chose `manual` every time, despite
`adjust_resources` existing precisely for this case.

Its reasoning was consistent: it observed two healthy replicas still serving and described
the crash-looping pod as "the old order-service pod", concluding the service was not
actually down. Defensible, but unhelpful — raising the memory limit is the fix.

The prompt says *"Only recommend automated actions when you are confident. For complex or
risky situations, recommend manual."* That guidance appears to be pushing it toward
caution. **This is an open prompt-tuning issue, not a code bug** — the scenario the project
was designed around does not trigger its intended action.

> ⚠️ Approving a `manual` recommendation does nothing by design — there is no action to
> execute. Earlier the dashboard still marked such incidents `RESOLVED`, reporting success
> over an unchanged cluster. The Approve button is now hidden for `manual` and the status
> goes to `FAILED` if a remediation reports no success.

Fix it by hand and let the alert clear:
```bash
./scripts/chaos/reset.sh
```

---

## Scenario 3 — high latency

```bash
./scripts/chaos/inject-latency.sh
```

Calls the order-service fault API to add 3000 ms to every request.

**Expected alert:** `HighHTTPLatency` (warning) — p95 over 2s for 2 minutes.

This scenario only works if the ServiceMonitors are scraping. Confirm first:

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/query?query=http_request_duration_seconds_count' \
  | head -c 200; echo
```

Empty result means no metrics, and the alert can never fire.

You need traffic for a latency percentile to exist:
```bash
kubectl port-forward -n apps svc/frontend 3001:3000 &
while true; do curl -s http://localhost:3001/api/orders >/dev/null; done
```

**Expected action:** `restart_pods` — the fault lives in the process's memory, so a restart
clears it. This is the one scenario where restarting is genuinely the right answer.

---

## Scenario 4 — database outage

```bash
./scripts/chaos/break-db.sh
```

Flips a flag making order-service behave as though PostgreSQL is gone.

**Expected alerts:** `PodNotReady` (readiness probe fails), and `HighHTTPErrorRate` if
traffic is flowing.

**Expected action:** `restart_pods` or `manual`. Note the agent may reasonably recommend
`manual` here — from the cluster's point of view PostgreSQL is perfectly healthy, so the
evidence points at the application, not at anything a deployment patch can fix. That is
the correct call, and a good illustration of the model declining to act.

---

## Resilience test — dependency outage

Not a chaos script; a check that order-service survives its database being unavailable.

> **Deleting the PostgreSQL pod is not a sufficient test.** It passes on broken code.
> asyncpg's connection pool re-establishes dropped connections by itself, so a quick
> pod bounce never exercises the app's own retry path — readiness never even flips.
> The bug being tested happens at **startup**, when there is no pool yet.

The test must remove the database *and keep it away while order-service boots*:

```bash
# 1. Remove the database entirely
kubectl scale deployment postgres -n apps --replicas=0
kubectl get pods -n apps -l app=postgres        # wait until none remain

# 2. Force order-service to start with no database available
kubectl rollout restart deployment/order-service -n apps
kubectl get pods -n apps -l app=order-service   # expect 0/1
```

`0/1` is correct here — this is the exact state the original bug produced *permanently*.
Confirm the cause:

```bash
kubectl logs -n apps -l app=order-service | grep -i "failed to connect"
kubectl get endpoints order-service -n apps     # empty
```

Now the decisive step:

```bash
# 3. Bring the database back
kubectl scale deployment postgres -n apps --replicas=1
kubectl get pods -n apps -l app=order-service -w   # Ctrl-C at 1/1
```

**Pass** — verified output from a real run, same pod throughout, `restarts=0`:

```
07:26:05  ERROR  Failed to connect to database: [Errno 111] Connection refused   <- boot
          ...92 attempts, one every 10 seconds, for 13 minutes...
07:39:38  ERROR  Failed to connect to database: [Errno 111] Connection refused
07:39:48  INFO   Database connection established and schema created              <- healed
```

Check it yourself with:
```bash
kubectl logs -n apps -l app=order-service | grep -E "Failed to connect|connection established"
```

**Fail:** one error line at boot and then silence, pod stuck `0/1` forever.

The `restarts=0` detail is what makes this conclusive: the container was never killed
and replaced, it recovered **in place**. Kubernetes would never have restarted it for
you either — the liveness probe is on `/healthz`, which always returns 200. Without an
app-level retry it would sit there broken indefinitely.

This matters more than it looks. The same race happens on every cold start: one
`kubectl apply` creates all five Deployments simultaneously, so order-service races
PostgreSQL's ~7 second init — and loses.

---

## Triggering DeploymentReplicaMismatch

This alert is **much harder to trigger than it looks**, and understanding why explains
scenarios 1 and 2. The rule compares desired against available replicas:

```promql
max by (namespace, deployment) (kube_deployment_spec_replicas{namespace="apps"})
!=
max by (namespace, deployment) (kube_deployment_status_replicas_available{namespace="apps"})
```

**Breaking the pod template never trips it.** A bad image or a tiny memory limit starts a
rolling update that creates the new pod and *keeps the old ones running* until it is ready.
`available` stays equal to `desired`, so the rule stays `inactive`. That is why it did not
fire in either scenario 1 or scenario 2.

It fires only when:
- **all existing pods become unavailable** (e.g. scaling PostgreSQL to zero takes every
  order-service pod unready), or
- **`spec.replicas` rises** while new pods cannot become available

### Reliable trigger

A throwaway deployment that can never pull its image — isolated, deterministic, and it
does not disturb `order-service`:

```bash
kubectl create deployment chaos-replica-test -n apps \
  --image=nope.invalid/does-not-exist:v1 --replicas=2
```

`desired=2, available=0` immediately. Allow **~3.5 minutes**: ~15s scrape + `for: 3m` +
~10s AlertManager grouping. Checking earlier shows `pending` and looks like failure.

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/alerts' \
| python3 -c "
import sys,json
for a in json.load(sys.stdin)['data']['alerts']:
    if a['labels']['alertname']=='DeploymentReplicaMismatch':
        print(' ', a['state'], '| labels:', a['labels'])"
```

### Verified results

```
labels: {alertname: DeploymentReplicaMismatch, category: deployment,
         deployment: chaos-replica-test, namespace: apps, severity: warning}
has pod label: False
agent incident pod field: ''
```

**No `pod` label** — the alert is deployment-scoped, as it should be. The dashboard titles
it `DeploymentReplicaMismatch — cluster`.

> Before the rule was aggregated with `max by (namespace, deployment)`, the series
> inherited kube-state-metrics' own `pod` label and the incident was titled
> `DeploymentReplicaMismatch — kube-prometheus-stack-kube-state-metrics-7757df4df-qh5xh`.
> The agent then gathered logs and pod status for that exporter instead of the broken
> deployment — and the model had to work out for itself that its input was misleading:
> *"The referenced kube-state-metrics pod is not found and appears to be a stale/misleading
> affected-pod label rather than the cause."*

The agent correctly recommended `manual` and even identified the situation: *"if this is an
intentional chaos test, suppress or scope the alert for this deployment."* A deployment
created broken has no good revision to roll back to.

### Cleanup

```bash
kubectl delete deployment chaos-replica-test -n apps
```

The alert clears, AlertManager sends the resolved notification, and the incident should
move to `AUTO_RESOLVED` by itself.

---

## Troubleshooting

Real failures encountered while building this, with the diagnosis that found each one.

### Loki is in CrashLoopBackOff

```bash
kubectl logs loki-0 -n monitoring --tail=5
```

| Error | Cause | Fix |
|---|---|---|
| `invalid schema config: invalid schema version` | The chart pins `loki:2.6.1`, which predates `tsdb` (2.8+) and `schema: v13` (2.9+) | `image.tag: 2.9.8` in `helm-values/loki-values.yaml` |
| `mkdir : no such file or directory` | A path setting is the empty string — note the blank path in the message | Set `tsdb_shipper.active_index_directory`, `cache_location`, `shared_store`, and `compactor.working_directory` |

The fastest way to debug Loki config is to read what it's actually running and compare a
broken block against one the chart configured itself:

```bash
kubectl get secret loki -n monitoring -o jsonpath='{.data.loki\.yaml}' | base64 -d
```

That's how the missing `shared_store` was found — `boltdb_shipper` had it, `tsdb_shipper`
didn't. The asymmetry was the bug.

### An alert rule change appears to do nothing

`kubectl apply -f k8s/alerting/` says `configured`, but Prometheus still runs the old rule.
This is normal — there are **three hops** after the apply:

```
PrometheusRule object -> operator renders a ConfigMap -> kubelet syncs the volume
                      -> config-reloader signals Prometheus
```

The kubelet sync alone can take ~60s, so allow **1–2 minutes**. Trace where it is:

```bash
# hop 2 — the object
kubectl get prometheusrule ai-sre-poc-alerts -n monitoring -o yaml | grep -c "max by"
# hop 3 — the rendered ConfigMap
kubectl get configmap -n monitoring -o name | grep rulefiles
# hop 4 — what Prometheus loaded
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/rules' | grep -c "max by"
```

If hops 2 and 3 are updated and hop 4 is not, just wait. Checking too early and concluding
it failed is the most common mistake here.

### The dashboard shows an old UI after a redeploy

The dashboard's HTML, CSS and JavaScript are served inline from `/`. "Auto-refreshes every
5 seconds" re-fetches only the **incident data** via `fetch()` — never the page itself. So
after `./scripts/deploy-ai-agent.sh`, a tab opened beforehand keeps running the old
JavaScript against the new backend.

Symptom: buttons or fields that a fix was supposed to change still look the same.

**Hard-refresh the page (`Cmd+Shift+R`).** To confirm the server really has the new code:

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- \
  http://ai-sre-agent.ai-sre-agent:5000/ | grep -c "manual-note"
```

### No custom alerts ever fire

Check whether the rules exist at all:

```bash
kubectl get prometheusrule ai-sre-poc-alerts -n monitoring
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/rules' \
  | grep -c '"name":"app\.'
```

If the PrometheusRule is missing, `setup-observability.sh` aborted before applying it. The
script runs `set -euo pipefail`, so **any** earlier failure — historically the Loki install
— kills it before the alerting step, leaving a cluster that looks healthy but has no custom
alerting whatsoever.

```bash
kubectl apply -f k8s/alerting/      # fixes it immediately
helm history loki -n monitoring     # shows whether an earlier install failed
```

After applying, allow ~60s: the operator renders the rule into a ConfigMap, then the
kubelet syncs the volume, then the config-reloader triggers Prometheus.

### Re-running a deploy script changes nothing

Symptom: build succeeds, push succeeds, `kubectl apply` says `unchanged`, and the old code
keeps running.

```bash
kubectl get pods -n apps -l app=order-service   # AGE older than your deploy?
kubectl get deployment order-service -n apps -o jsonpath='{.metadata.generation}'
```

`kubectl apply` reconciles the *manifest*, not the image contents. Behind a floating
`:latest` tag the manifest never changes, so no pod is created and the new image is never
pulled. `imagePullPolicy: Always` does not help — a pull only happens when a pod is created.

```bash
kubectl rollout restart deployment/order-service -n apps
```

The deploy scripts now do this automatically. An immutable tag (a git SHA, as the CI
workflow uses) avoids the problem entirely.

### order-service stuck at 0/1

```bash
kubectl logs -n apps -l app=order-service | grep -i "failed to connect"
kubectl get endpoints order-service -n apps     # empty = no ready pod
```

Compare the app's connection timestamp against PostgreSQL's "ready to accept connections"
line. If the app tried first, it lost the startup race. Restart it — and if it does not
self-heal, the reconnect logic is missing.

### The agent's key shows PLACEHOLDER

```bash
kubectl get secret ai-sre-agent-secret -n ai-sre-agent \
  -o jsonpath='{.data.OPENAI_API_KEY}' | base64 -d; echo
```

Means a manifest containing a placeholder Secret was applied *after* the deploy script
created the real one. The Secret is intentionally absent from `k8s/ai-sre-agent/all.yaml`
for this reason — if you add one back, the next `kubectl apply` or ArgoCD sync will
overwrite your key.

### Port already in use

The k3d load balancer publishes ingress on **18080/18443** specifically to keep 8080 and
8443 free for port-forwards. If 8080 is busy, something else is using it:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

### Cannot browse to http://ai-sre.localhost

Almost always a missing port. The k3d load balancer publishes ingress-nginx on host port
**18080**, not 80:

```bash
docker port k3d-ai-sre-poc-serverlb        # 80/tcp -> 0.0.0.0:18080
```

So the URL is `http://ai-sre.localhost:18080`. Without the port the browser tries port 80
on 127.0.0.1 and gets connection refused.

**Do not add `ai-sre.localhost` to `/etc/hosts`.** macOS hard-maps `*.localhost` to
127.0.0.1 in its resolver, so the entry is ignored — and if it ever were honoured, it
would point at an address with nothing listening on 18080. Check what is actually
happening with:

```bash
python3 -c "import socket; print(socket.gethostbyname('ai-sre.localhost'))"   # 127.0.0.1
```

To confirm the Ingress itself is fine, bypass DNS entirely:

```bash
python3 -c "
import urllib.request
r=urllib.request.Request('http://localhost:18080/healthz', headers={'Host':'ai-sre.localhost'})
print(urllib.request.urlopen(r, timeout=5).read().decode())"
```

If that returns `{"status":"ok"}` the cluster side is healthy and the problem is purely
in how the URL is being typed or resolved.

### HighHTTPErrorRate fires but no user request failed

Check which endpoint produced the 5xx:

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://kube-prometheus-stack-prometheus.monitoring:9090/api/v1/query?query=sum%20by%20(path,status)%20(http_requests_total%7Bnamespace%3D%22apps%22%7D)'
```

If every 5xx has `path="/readyz"`, the alert is measuring **health probes**, not users.
During a dependency outage the kubelet hits `/readyz` every 10s and each 503 increments
`http_requests_total`. With no real traffic, probes are 100% of the sample, so the 5xx
ratio hits 1.0 and a critical alert fires while nothing user-facing has broken.

It also fires *late* — the 5-minute rate window keeps those failures visible for minutes
after recovery, so the alert can arrive after the problem is already fixed.

The middleware excludes `/metrics`, `/healthz` and `/readyz` from instrumentation for
exactly this reason. If you re-add them, expect this alert back.

> The metric label for the request path is **`path`**, not `endpoint`.
> prometheus-operator sets its own `endpoint` target label (the ServiceMonitor port name)
> and renames any colliding metric label to `exported_endpoint`.

### Noise incidents from Watchdog / InfoInhibitor

These are monitoring meta-alerts, not application incidents — `Watchdog` fires permanently
by design as a dead-man's switch. If they reach the agent, each one burns an LLM call.

AlertManager's route should default to the `null` receiver and send only
`severity =~ "critical|warning"` to the agent, with `Watchdog|InfoInhibitor` explicitly
dropped. Check with:

```bash
kubectl get secret alertmanager-kube-prometheus-stack-alertmanager -n monitoring \
  -o jsonpath='{.data.alertmanager\.yaml\.gz}' | base64 -d | gunzip
```

Incidents persist for 7 days, so a restart no longer clears them. To wipe the history,
delete the database file and restart:
```bash
kubectl exec -n ai-sre-agent deploy/ai-sre-agent -- rm -f /data/incidents.db /data/incidents.db-wal /data/incidents.db-shm
kubectl rollout restart deployment/ai-sre-agent -n ai-sre-agent
```

### Webhook calls are rejected with 401

The agent logs `Rejected webhook call with a missing or wrong bearer token`. AlertManager
and the agent hold different tokens — usually because `monitoring/ai-sre-webhook` was
recreated after the agent was deployed. Re-run `./scripts/deploy-ai-agent.sh`; it copies
the current token into the agent's Secret and restarts it.

### Sign-in returns "Sign-in is not configured"

`DASHBOARD_PASSWORD` is missing from `ai-sre-agent-secret` — the Secret predates the
login feature. Re-run `./scripts/deploy-ai-agent.sh`; it generates one and keeps every
other value.

---

## Useful queries

The agent's own API:

The API needs a signed-in session. Sign in once with curl and reuse the cookie:

```bash
PW=$(kubectl -n ai-sre-agent get secret ai-sre-agent-secret -o jsonpath='{.data.DASHBOARD_PASSWORD}' | base64 -d)
curl -s -c /tmp/ai-sre.jar -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" http://ai-sre.localhost:18080/login; unset PW

# incidents from the last 24 hours (window: at most 7 days)
curl -s -b /tmp/ai-sre.jar "http://ai-sre.localhost:18080/api/incidents?since=$(date -u -v-24H +%Y-%m-%dT%H:%M:%SZ)" \
  | python3 -m json.tool

# trigger a synthetic incident without breaking anything
curl -s -b /tmp/ai-sre.jar -X POST http://ai-sre.localhost:18080/api/test-alert
```

(`date -v-24H` is macOS; on Linux use `date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ`.)

Loki:

```bash
kubectl exec -n monitoring loki-0 -- wget -qO- http://localhost:3100/ready
kubectl exec -n monitoring loki-0 -- wget -qO- \
  'http://localhost:3100/loki/api/v1/label/namespace/values'
```

Prometheus — active alerts, rules, and targets:

```bash
P='http://kube-prometheus-stack-prometheus.monitoring:9090'
kubectl exec -n monitoring loki-0 -- wget -qO- "$P/api/v1/alerts"
kubectl exec -n monitoring loki-0 -- wget -qO- "$P/api/v1/rules"
kubectl exec -n monitoring loki-0 -- wget -qO- "$P/api/v1/targets?state=active"
```

> Querying from inside `loki-0` avoids port-forwards entirely — that pod has `wget` and
> sits in the same namespace as the monitoring services. Handy when a port-forward won't
> stay up.

---

## Tearing down

```bash
k3d cluster delete ai-sre-poc
k3d registry delete k3d-ai-sre-registry
```
