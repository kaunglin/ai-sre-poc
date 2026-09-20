"""
AI SRE Agent — The core of the AI-powered incident response PoC.

Workflow:
1. Receives AlertManager webhooks
2. Gathers context from Prometheus, Loki, and K8s API
3. Sends context to Claude API for root cause analysis
4. Posts recommendations to the web dashboard
5. Executes approved remediations via K8s API
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum

import anthropic
import httpx
import openai
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("ai-sre-agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _clean_env(name: str) -> str:
    """Read an env var, treating deploy-time placeholders as unset."""
    value = os.getenv(name, "").strip()
    return "" if value in ("not-set", "PLACEHOLDER") else value


# Which API the agent analyses incidents with. Both providers answer the same
# prompt and the same JSON contract, so the rest of the agent doesn't care.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
OPENAI_API_KEY = _clean_env("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
ANTHROPIC_API_KEY = _clean_env("ANTHROPIC_API_KEY")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://loki.monitoring:3100")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")  # Optional
NAMESPACE = os.getenv("WATCH_NAMESPACE", "apps")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")

# How the agent gathers evidence:
#   dossier — Python runs a fixed set of queries and pastes the results into one
#             prompt. Predictable cost and latency; blind to anything the fixed
#             queries do not capture.
#   tools   — the model drives its own investigation through read-only tools.
#             Better on unfamiliar incidents, variable cost, needs a loop cap.
# This is the boot default; the dashboard can override it at runtime.
INVESTIGATION_MODE = os.getenv("INVESTIGATION_MODE", "dossier").strip().lower()
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "8000"))

# Runtime overrides from the dashboard. In-memory, like the incident store.
runtime_config = {"investigation_mode": INVESTIGATION_MODE}

# Resolved once so the "no key" path can name the variable the operator must set.
LLM_API_KEY_VAR = "OPENAI_API_KEY" if LLM_PROVIDER == "openai" else "ANTHROPIC_API_KEY"
LLM_API_KEY = OPENAI_API_KEY if LLM_PROVIDER == "openai" else ANTHROPIC_API_KEY

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

ALERTS_RECEIVED = Counter("ai_sre_alerts_received_total", "Alerts received", ["severity"])
ANALYSES_COMPLETED = Counter("ai_sre_analyses_completed_total", "AI analyses completed")
TOOL_CALLS = Counter("ai_sre_tool_calls_total", "Investigation tool calls", ["tool"])
INCIDENTS_AUTO_RESOLVED = Counter(
    "ai_sre_incidents_auto_resolved_total",
    "Incidents closed because the alert cleared without remediation",
)
REMEDIATIONS_EXECUTED = Counter("ai_sre_remediations_total", "Remediations executed", ["action", "status"])

# ---------------------------------------------------------------------------
# Incident store (in-memory for PoC)
# ---------------------------------------------------------------------------

class IncidentStatus(str, Enum):
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    AUTO_RESOLVED = "auto_resolved"   # alert cleared on its own; we did nothing
    REJECTED = "rejected"
    FAILED = "failed"


class Incident:
    def __init__(self, alert_data: dict):
        self.id = str(uuid.uuid4())[:8]
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.status = IncidentStatus.INVESTIGATING
        self.alert_data = alert_data
        self.alert_name = alert_data.get("labels", {}).get("alertname", "Unknown")
        self.severity = alert_data.get("labels", {}).get("severity", "unknown")
        self.pod = alert_data.get("labels", {}).get("pod", "")
        self.namespace = alert_data.get("labels", {}).get("namespace", NAMESPACE)
        self.summary = alert_data.get("annotations", {}).get("summary", "")
        self.description = alert_data.get("annotations", {}).get("description", "")
        # Investigation results
        self.metrics_context = ""
        self.logs_context = ""
        self.k8s_context = ""
        # AI analysis
        self.ai_analysis = ""
        self.root_cause = ""
        self.recommended_action = ""
        self.recommended_action_type = ""  # restart_pod, rollback, scale, manual
        self.recommended_action_params = {}
        # Every analysis run against this incident, oldest first. The fields
        # above mirror the most recent one — that is what Approve acts on — so
        # re-investigating never leaves an ambiguous recommendation on screen.
        self.analyses: list[dict] = []
        # Remediation
        self.remediation_result = ""
        self.timeline: list[dict] = []

    def add_event(self, event: str):
        self.timeline.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
        })
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status.value,
            "alert_name": self.alert_name,
            "severity": self.severity,
            "pod": self.pod,
            "namespace": self.namespace,
            "summary": self.summary,
            "description": self.description,
            "ai_analysis": self.ai_analysis,
            "root_cause": self.root_cause,
            "recommended_action": self.recommended_action,
            "recommended_action_type": self.recommended_action_type,
            # Surfaced so the approval gate can show WHAT will be patched, not
            # just the verb. Approving "scale_up" without seeing the target
            # deployment and replica count is not a meaningful review.
            "recommended_action_params": self.recommended_action_params,
            "remediation_result": self.remediation_result,
            "analyses": self.analyses,
            "timeline": self.timeline,
        }


incidents: dict[str, Incident] = {}

# ---------------------------------------------------------------------------
# Kubernetes client
# ---------------------------------------------------------------------------

def get_k8s_clients():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()


core_v1, apps_v1 = get_k8s_clients()

# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

async def query_prometheus(query: str) -> str:
    """Execute a PromQL query and return formatted results."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
            data = resp.json()
            if data.get("status") == "success":
                results = data.get("data", {}).get("result", [])
                if not results:
                    return f"Query: {query}\nResult: No data"
                lines = [f"Query: {query}"]
                for r in results[:10]:  # Limit results
                    metric = r.get("metric", {})
                    value = r.get("value", [None, "N/A"])
                    labels = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
                    lines.append(f"  {labels}: {value[1]}")
                return "\n".join(lines)
            return f"Query: {query}\nError: {data.get('error', 'unknown')}"
    except Exception as e:
        return f"Query: {query}\nError: {e}"


async def query_loki(query: str, limit: int = 50) -> str:
    """Query Loki for recent logs."""
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=15)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": query,
                    # Nanoseconds is the format Loki documents. Bare seconds
                    # also work (Loki infers the unit), but being explicit
                    # avoids depending on that inference.
                    "start": str(int(start.timestamp() * 1_000_000_000)),
                    "end": str(int(end.timestamp() * 1_000_000_000)),
                    "limit": limit,
                },
            )
            data = resp.json()
            if data.get("status") == "success":
                streams = data.get("data", {}).get("result", [])
                lines = []
                for stream in streams:
                    for ts, line in stream.get("values", [])[-20:]:
                        lines.append(line)
                if not lines:
                    return f"Loki query: {query}\nNo logs found in last 15 minutes"
                return f"Loki query: {query}\nRecent logs ({len(lines)} entries):\n" + "\n".join(lines[-30:])
            return f"Loki query: {query}\nError: {data.get('error', 'unknown')}"
    except Exception as e:
        return f"Loki query: {query}\nError: {e}"


def get_k8s_context(namespace: str, pod_name: str = "") -> str:
    """Gather Kubernetes context: pod status, events, recent deployments."""
    context_parts = []

    try:
        # Pod status
        if pod_name:
            try:
                pod = core_v1.read_namespaced_pod(pod_name, namespace)
                container_statuses = []
                for cs in (pod.status.container_statuses or []):
                    status_info = {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                    }
                    if cs.state.waiting:
                        status_info["state"] = f"Waiting: {cs.state.waiting.reason}"
                        if cs.state.waiting.message:
                            status_info["message"] = cs.state.waiting.message
                    elif cs.state.running:
                        status_info["state"] = "Running"
                    elif cs.state.terminated:
                        status_info["state"] = f"Terminated: {cs.state.terminated.reason}"
                    if cs.last_state and cs.last_state.terminated:
                        status_info["last_terminated_reason"] = cs.last_state.terminated.reason
                        status_info["last_terminated_exit_code"] = cs.last_state.terminated.exit_code
                    container_statuses.append(status_info)

                context_parts.append(
                    f"Pod: {pod_name}\n"
                    f"  Phase: {pod.status.phase}\n"
                    f"  Node: {pod.spec.node_name}\n"
                    f"  Containers: {json.dumps(container_statuses, indent=2)}"
                )
            except Exception as e:
                context_parts.append(f"Pod {pod_name}: Error fetching - {e}")

        # All pods in namespace
        pods = core_v1.list_namespaced_pod(namespace)
        pod_summary = []
        for p in pods.items:
            restarts = sum(cs.restart_count for cs in (p.status.container_statuses or []))
            pod_summary.append(
                f"  {p.metadata.name}: phase={p.status.phase}, restarts={restarts}"
            )
        context_parts.append(f"All pods in {namespace}:\n" + "\n".join(pod_summary))

        # Recent events
        events = core_v1.list_namespaced_event(
            namespace, limit=20,
        )
        if events.items:
            event_lines = []
            for ev in sorted(events.items, key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:15]:
                ts = ev.last_timestamp or ev.event_time or "unknown"
                event_lines.append(
                    f"  [{ev.type}] {ts} {ev.reason}: {ev.message}"
                )
            context_parts.append(f"Recent events in {namespace}:\n" + "\n".join(event_lines))

        # Deployments
        deployments = apps_v1.list_namespaced_deployment(namespace)
        dep_lines = []
        for d in deployments.items:
            dep_lines.append(
                f"  {d.metadata.name}: replicas={d.spec.replicas}, "
                f"available={d.status.available_replicas or 0}, "
                f"image={d.spec.template.spec.containers[0].image}"
            )
        context_parts.append(f"Deployments in {namespace}:\n" + "\n".join(dep_lines))

    except Exception as e:
        context_parts.append(f"Error gathering K8s context: {e}")

    return "\n\n".join(context_parts)


def get_pod_logs(pod: str, namespace: str = "", container: str = "",
                 previous: bool = False, tail_lines: int = 200) -> str:
    """Read logs straight from a pod via the Kubernetes API.

    Loki only ever sees the error-filtered view. On an unfamiliar incident the
    useful evidence is often what Loki filtered out: the lines *before* the
    error, the startup sequence, or the logs of a container that already died
    (previous=True). The ServiceAccount already holds pods/log.
    """
    namespace = namespace or NAMESPACE
    try:
        kwargs = {"name": pod, "namespace": namespace, "tail_lines": tail_lines}
        if container:
            kwargs["container"] = container
        if previous:
            kwargs["previous"] = True
        body = core_v1.read_namespaced_pod_log(**kwargs)
        label = f"{pod}{' (previous container)' if previous else ''}"
        return f"Logs for {label}:\n{body}" if body else f"Logs for {label}: (empty)"
    except Exception as e:
        return f"Could not read logs for {pod} in {namespace}: {e}"


async def gather_investigation_context(incident: Incident) -> None:
    """Gather all context needed for AI analysis."""
    incident.add_event("Gathering metrics from Prometheus")

    # Prometheus queries based on alert type
    prom_queries = [
        f'kube_pod_container_status_restarts_total{{namespace="{incident.namespace}"}}',
        f'kube_pod_status_phase{{namespace="{incident.namespace}"}}',
        f'container_memory_working_set_bytes{{namespace="{incident.namespace}", container!=""}}',
        f'rate(container_cpu_usage_seconds_total{{namespace="{incident.namespace}", container!=""}}[5m])',
    ]

    if "http" in incident.alert_name.lower() or "latency" in incident.alert_name.lower():
        prom_queries.extend([
            f'rate(http_requests_total{{namespace="{incident.namespace}"}}[5m])',
            f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{namespace="{incident.namespace}"}}[5m])) by (le, service))',
        ])

    metrics_results = await asyncio.gather(*[query_prometheus(q) for q in prom_queries])
    incident.metrics_context = "\n\n".join(metrics_results)

    # Loki logs
    incident.add_event("Gathering logs from Loki")
    # The `|= "a" or "b"` line-filter form takes bare operands, so repeating
    # `|=` after `or` is a parse error. A case-insensitive regex covers it.
    log_queries = [
        f'{{namespace="{incident.namespace}"}} |~ "(?i)(error|exception|traceback)"',
    ]
    if incident.pod:
        log_queries.append(f'{{namespace="{incident.namespace}", pod="{incident.pod}"}}')

    log_results = await asyncio.gather(*[query_loki(q) for q in log_queries])
    incident.logs_context = "\n\n".join(log_results)

    # Kubernetes context
    incident.add_event("Gathering Kubernetes resource status")
    # The kubernetes client is synchronous; keep it off the event loop.
    incident.k8s_context = await asyncio.to_thread(
        get_k8s_context, incident.namespace, incident.pod
    )


# ---------------------------------------------------------------------------
# AI analysis
#
# Two providers are supported so the PoC can run on whichever API credits are
# on hand. Both get the same prompt and must return the same JSON contract;
# only the transport differs. Select with LLM_PROVIDER=openai|anthropic.
# ---------------------------------------------------------------------------

def _build_analysis_prompt(incident: Incident) -> str:
    return f"""You are an AI SRE agent investigating a production incident on a Kubernetes cluster.
Analyze the following alert, metrics, logs, and Kubernetes state to determine:
1. Root cause of the issue
2. Severity assessment
3. Recommended remediation action

ALERT DETAILS:
- Alert: {incident.alert_name}
- Severity: {incident.severity}
- Summary: {incident.summary}
- Description: {incident.description}
- Affected pod: {incident.pod or 'N/A'}
- Namespace: {incident.namespace}

PROMETHEUS METRICS:
{incident.metrics_context}

RECENT LOGS:
{incident.logs_context}

KUBERNETES STATE:
{incident.k8s_context}

Respond in this exact JSON format:
{{
  "root_cause": "One clear sentence describing the root cause",
  "severity": "critical|high|medium|low",
  "analysis": "Detailed analysis paragraph explaining your reasoning, what evidence points to this conclusion, and any contributing factors",
  "recommended_action": "Specific action to take, described clearly",
  "action_type": "restart_pods|rollback_deployment|scale_up|adjust_resources|manual",
  "action_params": {{
    "deployment": "deployment-name",
    "namespace": "namespace",
    "replicas": 3
  }},
  "confidence": "high|medium|low",
  "risk_level": "low|medium|high"
}}

Only recommend automated actions (restart_pods, rollback_deployment, scale_up, adjust_resources) when you are confident.
For complex or risky situations, recommend "manual" with clear instructions.
"""


# ---------------------------------------------------------------------------
# Tool-driven investigation
#
# The same functions the dossier path calls on a fixed schedule, exposed so the
# model can choose what to look at. Strictly read-only: remediation stays behind
# the approval gate and its five fixed verbs. Widening the *investigation*
# surface is safe; widening the *action* surface is not.
# ---------------------------------------------------------------------------

TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "query_prometheus",
        "description": "Run a PromQL query. Use for restart counts, pod phase, memory, "
                       "CPU, request rates and latency percentiles.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "PromQL expression"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "query_loki",
        "description": "Run a LogQL query over the last 15 minutes of logs. Good for "
                       "finding errors across a namespace.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": 'LogQL, e.g. {namespace="apps"} |~ "(?i)error"'},
            "limit": {"type": "integer", "description": "Max lines, default 50"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_k8s_context",
        "description": "Pod status and container states, all pods in the namespace with "
                       "restart counts, the last 15 events, and deployment replica counts "
                       "and images.",
        "parameters": {"type": "object", "properties": {
            "namespace": {"type": "string"},
            "pod_name": {"type": "string", "description": "Optional, to focus one pod"},
        }, "required": ["namespace"]}}},
    {"type": "function", "function": {
        "name": "get_pod_logs",
        "description": "Read raw logs from one pod. Use when the Loki error filter is not "
                       "enough: full context around a failure, the startup sequence, or "
                       "the logs of a container that already crashed (previous=true).",
        "parameters": {"type": "object", "properties": {
            "pod": {"type": "string"},
            "namespace": {"type": "string", "description": "Defaults to the watched namespace"},
            "container": {"type": "string", "description": "Optional"},
            "previous": {"type": "boolean",
                         "description": "Logs from the last terminated container"},
            "tail_lines": {"type": "integer", "description": "Default 200"},
        }, "required": ["pod"]}}},
]

TOOL_IMPLS = {
    "query_prometheus": query_prometheus,
    "query_loki": query_loki,
    "get_k8s_context": get_k8s_context,
    "get_pod_logs": get_pod_logs,
}


def _build_tool_prompt(incident: Incident) -> str:
    """Opening prompt for tool mode: the alert only, plus the JSON contract.

    Deliberately withholds the dossier — the point of this mode is that the
    model decides what evidence it needs.
    """
    return f"""You are an AI SRE agent investigating a production incident on a Kubernetes cluster.

ALERT DETAILS:
- Alert: {incident.alert_name}
- Severity: {incident.severity}
- Summary: {incident.summary}
- Description: {incident.description}
- Affected pod: {incident.pod or 'N/A'}
- Namespace: {incident.namespace}

Investigate using the available tools. Gather the evidence you actually need —
metrics, logs, and Kubernetes state — before concluding. If the error filter in
Loki is not revealing the cause, read the pod's raw logs; for a container that
has already crashed, read its previous logs.

When you have enough evidence, reply with ONLY this JSON object and no other
text, no markdown fences:
{{
  "root_cause": "One clear sentence describing the root cause",
  "severity": "critical|high|medium|low",
  "analysis": "Detailed reasoning: what evidence points to this conclusion, which
               tool results were decisive, and any contributing factors",
  "recommended_action": "Specific action to take, described clearly",
  "action_type": "restart_pods|rollback_deployment|scale_up|adjust_resources|manual",
  "action_params": {{"deployment": "deployment-name", "namespace": "namespace"}},
  "confidence": "high|medium|low",
  "risk_level": "low|medium|high"
}}

Only recommend an automated action when the evidence identifies a cause that
action would fix. Use "manual" for genuine ambiguity — not merely for low urgency."""


def _summarise_args(args: dict) -> str:
    """Short one-line rendering of tool arguments for the incident timeline."""
    parts = []
    for k, v in args.items():
        text = str(v)
        parts.append(f"{k}={text[:60] + '…' if len(text) > 60 else text}")
    return ", ".join(parts)


async def _run_tool(name: str, args: dict) -> str:
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"Unknown tool: {name}"
    TOOL_CALLS.labels(name).inc()
    try:
        if asyncio.iscoroutinefunction(impl):
            result = await impl(**args)
        else:
            result = await asyncio.to_thread(impl, **args)
    except TypeError as e:
        return f"Invalid arguments for {name}: {e}"
    except Exception as e:
        return f"{name} failed: {e}"
    return str(result)[:MAX_TOOL_RESULT_CHARS]


async def _investigate_with_tools(incident: Incident) -> tuple[str, int]:
    """Let the model drive. Returns (raw response text, tool call count)."""
    client = openai.AsyncOpenAI(api_key=LLM_API_KEY)
    messages = [{"role": "user", "content": _build_tool_prompt(incident)}]
    calls = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, tools=TOOL_SCHEMA,
        )
        message = response.choices[0].message
        messages.append(message)

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            return (message.content or "").strip(), calls

        for call in tool_calls:
            # v3 distinguishes function tools from custom tools; only ours apply.
            if getattr(call, "type", "function") != "function":
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": "Unsupported tool type"})
                continue
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls += 1
            incident.add_event(f"Tool: {name}({_summarise_args(args)})")
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": await _run_tool(name, args)})

    # Budget spent. Ask for a conclusion from whatever was gathered rather than
    # returning nothing — a partial analysis still beats an empty incident.
    incident.add_event(f"Tool budget exhausted after {MAX_TOOL_ITERATIONS} rounds")
    messages.append({"role": "user", "content":
                     "Tool budget exhausted. Respond now with the JSON object, "
                     "using the evidence you already have."})
    final = await client.chat.completions.create(
        model=OPENAI_MODEL, messages=messages,
        response_format={"type": "json_object"},
    )
    return (final.choices[0].message.content or "").strip(), calls


async def _complete_openai(prompt: str) -> str:
    """Ask OpenAI for the analysis and return the raw response text."""
    client = openai.AsyncOpenAI(api_key=LLM_API_KEY)
    # No token cap: json_object mode holds the reply to the small object the
    # prompt asks for, and the cap parameter name varies by model family.
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


async def _complete_anthropic(prompt: str) -> str:
    """Ask Claude for the analysis and return the raw response text."""
    client = anthropic.AsyncAnthropic(api_key=LLM_API_KEY)
    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def analyze_incident(incident: Incident, mode: str | None = None) -> None:
    """Analyse an incident and record the result as the active recommendation.

    `mode` selects how evidence is gathered — "dossier" (fixed queries, one
    call) or "tools" (model-driven, several calls). Defaults to the runtime
    setting. Every run is appended to incident.analyses so the same incident can
    be investigated both ways and compared.
    """
    mode = (mode or runtime_config["investigation_mode"]).lower()
    started = datetime.now(timezone.utc)
    tool_calls = 0
    incident.add_event(f"Analysing via {LLM_PROVIDER} ({mode} mode)")

    if not LLM_API_KEY:
        incident.ai_analysis = f"{LLM_API_KEY_VAR} not set — skipping AI analysis"
        incident.root_cause = "Unable to determine (no API key)"
        incident.recommended_action = f"Please set {LLM_API_KEY_VAR}"
        incident.recommended_action_type = "manual"
        incident.status = IncidentStatus.AWAITING_APPROVAL
        return

    response_text = ""

    try:
        if mode == "tools":
            if LLM_PROVIDER != "openai":
                raise ValueError("tools mode currently requires LLM_PROVIDER=openai")
            response_text, tool_calls = await _investigate_with_tools(incident)
        else:
            # Dossier mode gathers evidence up front, on a fixed schedule.
            if not incident.metrics_context:
                await gather_investigation_context(incident)
            prompt = _build_analysis_prompt(incident)
            if LLM_PROVIDER == "openai":
                response_text = await _complete_openai(prompt)
            elif LLM_PROVIDER == "anthropic":
                response_text = await _complete_anthropic(prompt)
            else:
                raise ValueError(
                    f"Unknown LLM_PROVIDER {LLM_PROVIDER!r} (expected 'openai' or 'anthropic')"
                )

        # Clean up potential markdown formatting
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
        if response_text.endswith("```"):
            response_text = response_text.rsplit("```", 1)[0]

        analysis = json.loads(response_text)

        incident.ai_analysis = analysis.get("analysis", "")
        incident.root_cause = analysis.get("root_cause", "")
        incident.recommended_action = analysis.get("recommended_action", "")
        incident.recommended_action_type = analysis.get("action_type", "manual")
        incident.recommended_action_params = analysis.get("action_params", {})
        incident.status = IncidentStatus.AWAITING_APPROVAL

        confidence = analysis.get("confidence", "unknown")
        risk = analysis.get("risk_level", "unknown")
        duration = (datetime.now(timezone.utc) - started).total_seconds()
        incident.analyses.append({
            "mode": mode,
            "provider": LLM_PROVIDER,
            "model": OPENAI_MODEL if LLM_PROVIDER == "openai" else CLAUDE_MODEL,
            "at": started.isoformat(),
            "duration_s": round(duration, 1),
            "tool_calls": tool_calls,
            "root_cause": incident.root_cause,
            "analysis": incident.ai_analysis,
            "recommended_action": incident.recommended_action,
            "action_type": incident.recommended_action_type,
            "action_params": incident.recommended_action_params,
            "confidence": confidence,
            "risk_level": risk,
        })
        incident.add_event(
            f"Analysis complete ({mode}, {duration:.0f}s, {tool_calls} tool calls) "
            f"— confidence: {confidence}, risk: {risk}"
        )

        ANALYSES_COMPLETED.inc()

    except json.JSONDecodeError:
        incident.ai_analysis = response_text or "Failed to parse response"
        incident.root_cause = "AI returned non-JSON response"
        incident.recommended_action = "Review AI output manually"
        incident.recommended_action_type = "manual"
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident.add_event("AI analysis returned unparseable response")

    except Exception as e:
        logger.error(f"{LLM_PROVIDER} API error: {e}")
        incident.ai_analysis = f"Error calling {LLM_PROVIDER} API: {e}"
        incident.root_cause = "Unable to determine"
        incident.recommended_action = "Investigate manually"
        incident.recommended_action_type = "manual"
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident.add_event(f"AI analysis failed: {e}")


# ---------------------------------------------------------------------------
# Remediation execution
# ---------------------------------------------------------------------------

def rollback_deployment(deployment: str, namespace: str) -> str:
    """Restore the pod template from the deployment's previous revision.

    apps/v1 has no rollback endpoint, so this does what `kubectl rollout undo`
    does: find the ReplicaSet holding the previous revision and patch its pod
    template back onto the deployment.
    """
    dep = apps_v1.read_namespaced_deployment(deployment, namespace)
    annotations = dep.metadata.annotations or {}
    current_revision = int(annotations.get("deployment.kubernetes.io/revision", "1"))

    match_labels = (dep.spec.selector.match_labels or {})
    selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    replica_sets = apps_v1.list_namespaced_replica_set(
        namespace, label_selector=selector
    ).items

    candidates = []
    for rs in replica_sets:
        revision = int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
        if 0 < revision < current_revision:
            candidates.append((revision, rs))

    if not candidates:
        return (
            f"No previous revision found for deployment/{deployment} "
            f"(currently at revision {current_revision}) — nothing to roll back to"
        )

    revision, previous = max(candidates, key=lambda pair: pair[0])
    template = k8s_client.ApiClient().sanitize_for_serialization(previous.spec.template)
    # pod-template-hash is owned by the deployment controller; sending it back
    # would pin the deployment to the old ReplicaSet.
    template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)

    body = {
        "metadata": {
            "annotations": {
                "ai-sre-agent/rolledBackAt": datetime.now(timezone.utc).isoformat(),
                "ai-sre-agent/rolledBackFrom": str(current_revision),
                "ai-sre-agent/rolledBackTo": str(revision),
            }
        },
        "spec": {"template": template},
    }
    apps_v1.patch_namespaced_deployment(deployment, namespace, body)
    return (
        f"Rolled deployment/{deployment} back from revision "
        f"{current_revision} to {revision}"
    )


def execute_remediation(incident: Incident) -> tuple[bool, str]:
    """Execute the approved remediation action.

    Returns (succeeded, message). The caller must not infer success by
    string-matching the message — an earlier version checked for the substring
    "failed", so a no-op like "Manual action required" was reported as RESOLVED
    while the cluster stayed broken.
    """
    action_type = incident.recommended_action_type
    params = incident.recommended_action_params
    namespace = params.get("namespace", incident.namespace)
    deployment = params.get("deployment", "")

    try:
        if action_type == "restart_pods":
            # Rolling restart by patching the deployment annotation
            if not deployment:
                # Try to infer deployment from pod name
                deployment = "-".join(incident.pod.split("-")[:-2]) if incident.pod else ""
            if not deployment:
                return False, "Cannot determine deployment name for restart"

            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "ai-sre-agent/restartedAt": datetime.now(timezone.utc).isoformat()
                            }
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(deployment, namespace, body)
            REMEDIATIONS_EXECUTED.labels("restart_pods", "success").inc()
            return True, f"Rolling restart initiated for deployment/{deployment} in {namespace}"

        elif action_type == "rollback_deployment":
            if not deployment:
                return False, "Cannot determine deployment name for rollback"
            result = rollback_deployment(deployment, namespace)
            # rollback_deployment reports its own no-op case in the message
            ok = not result.startswith("No previous revision")
            REMEDIATIONS_EXECUTED.labels(
                "rollback_deployment", "success" if ok else "failure"
            ).inc()
            return ok, result

        elif action_type == "scale_up":
            if not deployment:
                return False, "Cannot determine deployment name for scaling"
            # No hardcoded default: a silent fallback that scales a database
            # to 3 replicas is a worse failure than doing nothing. If the model
            # did not specify a count, scale up by exactly one from current.
            replicas = params.get("replicas")
            if replicas is None:
                current = apps_v1.read_namespaced_deployment(
                    deployment, namespace
                ).spec.replicas or 0
                replicas = current + 1
            body = {"spec": {"replicas": replicas}}
            apps_v1.patch_namespaced_deployment(deployment, namespace, body)
            REMEDIATIONS_EXECUTED.labels("scale_up", "success").inc()
            return True, f"Scaled deployment/{deployment} to {replicas} replicas"

        elif action_type == "adjust_resources":
            if not deployment:
                return False, "Cannot determine deployment name"
            dep = apps_v1.read_namespaced_deployment(deployment, namespace)
            container_name = dep.spec.template.spec.containers[0].name
            memory_limit = params.get("memory_limit", "512Mi")
            cpu_limit = params.get("cpu_limit", "500m")

            # Patch just this container's resources — a strategic merge patch
            # matches containers by name, so the rest of the spec is untouched.
            body = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": container_name,
                                "resources": {
                                    "limits": {"memory": memory_limit, "cpu": cpu_limit},
                                    "requests": {
                                        "memory": params.get("memory_request", "256Mi"),
                                        "cpu": params.get("cpu_request", "250m"),
                                    },
                                },
                            }]
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(deployment, namespace, body)
            REMEDIATIONS_EXECUTED.labels("adjust_resources", "success").inc()
            return True, f"Updated resources for deployment/{deployment}: memory={memory_limit}, cpu={cpu_limit}"

        elif action_type == "manual":
            # Not a success: nothing was done. Approve is blocked for this
            # action type anyway; this is the belt-and-braces path.
            return False, "Manual action required — no automated remediation applied"

        else:
            return False, f"Unknown action type: {action_type}"

    except Exception as e:
        REMEDIATIONS_EXECUTED.labels(action_type, "failure").inc()
        logger.error(f"Remediation failed: {e}")
        return False, f"Remediation failed: {e}"


# ---------------------------------------------------------------------------
# Optional Slack notification
# ---------------------------------------------------------------------------

async def notify_slack(incident: Incident):
    """Send a notification to Slack (optional)."""
    if not SLACK_WEBHOOK_URL:
        return

    severity_emoji = {"critical": "🔴", "warning": "🟡"}.get(incident.severity, "⚪")

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{severity_emoji} AI SRE Alert: {incident.alert_name}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": (
                    f"*Incident ID:* `{incident.id}`\n"
                    f"*Status:* {incident.status.value}\n"
                    f"*Root cause:* {incident.root_cause}\n"
                    f"*Recommendation:* {incident.recommended_action}\n"
                    f"*Dashboard:* Review and approve at the AI SRE dashboard"
                )}
            },
        ],
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(SLACK_WEBHOOK_URL, json=payload)
    except Exception as e:
        logger.warning(f"Failed to send Slack notification: {e}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="AI SRE Agent", version="1.0.0")


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "ai-sre-agent"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# AlertManager webhook receiver
@app.post("/webhook/alertmanager")
async def alertmanager_webhook(request: Request):
    """Receive alerts from AlertManager and start investigation."""
    payload = await request.json()

    for alert in payload.get("alerts", []):
        status = alert.get("status", "firing")
        labels = alert.get("labels", {})
        alert_name = labels.get("alertname", "Unknown")
        severity = labels.get("severity", "unknown")
        # Identity used for both dedup and resolution matching. Keying on
        # alertname alone would treat a second pod's failure as a duplicate.
        identity = (alert_name, labels.get("namespace", NAMESPACE), labels.get("pod", ""))

        if status != "firing":
            # AlertManager sends these because send_resolved is enabled. Close
            # any incident still waiting on a human for a condition that has
            # since cleared, so the dashboard stops asking for approval to fix
            # problems that no longer exist.
            pending = (IncidentStatus.INVESTIGATING, IncidentStatus.AWAITING_APPROVAL)
            closed = [
                i for i in incidents.values()
                if (i.alert_name, i.namespace, i.pod) == identity and i.status in pending
            ]
            for stale in closed:
                stale.status = IncidentStatus.AUTO_RESOLVED
                stale.remediation_result = (
                    "Alert cleared on its own — no remediation was applied"
                )
                stale.add_event("Alert resolved upstream; closed without action")
                INCIDENTS_AUTO_RESOLVED.inc()
            logger.info(
                f"Alert resolved: {alert_name} — closed {len(closed)} pending incident(s)"
            )
            continue

        ALERTS_RECEIVED.labels(severity).inc()
        logger.info(f"Alert received: {alert_name} (severity={severity})")

        # Check for duplicate active incidents
        active = [i for i in incidents.values()
                   if (i.alert_name, i.namespace, i.pod) == identity
                   and i.status not in (IncidentStatus.RESOLVED, IncidentStatus.AUTO_RESOLVED,
                                        IncidentStatus.REJECTED, IncidentStatus.FAILED)]
        if active:
            logger.info(
                f"Skipping duplicate alert {alert_name} for {identity[2] or identity[1]} "
                f"— incident {active[0].id} already active"
            )
            continue

        # Create incident and start async investigation
        incident = Incident(alert)
        incidents[incident.id] = incident
        incident.add_event(f"Alert received: {alert_name}")

        # Run investigation in background
        asyncio.create_task(investigate_incident(incident))

    return {"status": "ok", "incidents": len(incidents)}


async def investigate_incident(incident: Incident):
    """Full investigation pipeline: gather context → AI analysis → notify."""
    try:
        await analyze_incident(incident)
        await notify_slack(incident)
        logger.info(
            f"Incident {incident.id} investigation complete. "
            f"Root cause: {incident.root_cause}. "
            f"Recommended action: {incident.recommended_action}"
        )
    except Exception as e:
        logger.error(f"Investigation failed for incident {incident.id}: {e}")
        incident.ai_analysis = f"Investigation error: {e}"
        incident.status = IncidentStatus.FAILED
        incident.add_event(f"Investigation failed: {e}")


# Approve / reject remediation
@app.post("/api/incidents/{incident_id}/approve")
async def approve_incident(incident_id: str):
    incident = incidents.get(incident_id)
    if not incident:
        return Response(status_code=404, content=json.dumps({"error": "Incident not found"}))
    if incident.status != IncidentStatus.AWAITING_APPROVAL:
        return Response(status_code=400,
                        content=json.dumps({"error": f"Incident is {incident.status.value}, not awaiting approval"}))

    if incident.recommended_action_type == "manual":
        # There is nothing to execute. Approving would previously mark the
        # incident RESOLVED without touching the cluster.
        return Response(
            status_code=400,
            media_type="application/json",
            content=json.dumps({
                "error": "No automated remediation for this incident "
                         "(action_type is 'manual'). Fix it by hand, then use "
                         "Reject to close the incident."
            }),
        )

    incident.status = IncidentStatus.APPROVED
    incident.add_event("Engineer approved remediation")
    logger.info(f"Incident {incident_id}: remediation approved, executing {incident.recommended_action_type}")

    # Execute remediation (sync K8s client — keep it off the event loop)
    incident.status = IncidentStatus.EXECUTING
    ok, result = await asyncio.to_thread(execute_remediation, incident)
    incident.remediation_result = result
    incident.status = IncidentStatus.RESOLVED if ok else IncidentStatus.FAILED
    incident.add_event(f"Remediation result: {result}")

    logger.info(f"Incident {incident_id}: {incident.status.value} — {result}")
    return incident.to_dict()


@app.post("/api/incidents/{incident_id}/reject")
async def reject_incident(incident_id: str):
    incident = incidents.get(incident_id)
    if not incident:
        return Response(status_code=404, content=json.dumps({"error": "Incident not found"}))

    incident.status = IncidentStatus.REJECTED
    incident.add_event("Engineer rejected automated remediation — handling manually")
    logger.info(f"Incident {incident_id}: remediation rejected by engineer")
    return incident.to_dict()


# Investigation mode — read and set at runtime
@app.get("/api/config")
async def get_config():
    return {
        "investigation_mode": runtime_config["investigation_mode"],
        "modes": ["dossier", "tools"],
        "provider": LLM_PROVIDER,
        "max_tool_iterations": MAX_TOOL_ITERATIONS,
    }


@app.post("/api/config")
async def set_config(request: Request):
    body = await request.json()
    mode = str(body.get("investigation_mode", "")).lower()
    if mode not in ("dossier", "tools"):
        return Response(status_code=400, media_type="application/json",
                        content=json.dumps({"error": "mode must be 'dossier' or 'tools'"}))
    if mode == "tools" and LLM_PROVIDER != "openai":
        return Response(status_code=400, media_type="application/json",
                        content=json.dumps({
                            "error": "tools mode currently requires LLM_PROVIDER=openai"}))
    runtime_config["investigation_mode"] = mode
    logger.info(f"Investigation mode set to {mode}")
    return {"investigation_mode": mode}


# Re-run the analysis on an existing incident, optionally in a different mode.
# The point is comparison: same alert, same cluster state, two approaches.
@app.post("/api/incidents/{incident_id}/reinvestigate")
async def reinvestigate_incident(incident_id: str, request: Request):
    incident = incidents.get(incident_id)
    if not incident:
        return Response(status_code=404, media_type="application/json",
                        content=json.dumps({"error": "Incident not found"}))
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = str(body.get("mode") or runtime_config["investigation_mode"]).lower()
    if mode not in ("dossier", "tools"):
        return Response(status_code=400, media_type="application/json",
                        content=json.dumps({"error": "mode must be 'dossier' or 'tools'"}))

    # Re-analysing is an observation, not a state change: an incident awaiting
    # approval stays awaiting approval, a resolved one stays resolved.
    previous_status = incident.status
    incident.add_event(f"Re-investigating in {mode} mode")
    await analyze_incident(incident, mode=mode)
    if previous_status not in (IncidentStatus.INVESTIGATING,
                               IncidentStatus.AWAITING_APPROVAL):
        incident.status = previous_status
    return incident.to_dict()


# List incidents
@app.get("/api/incidents")
async def list_incidents():
    sorted_incidents = sorted(incidents.values(), key=lambda i: i.created_at, reverse=True)
    return [i.to_dict() for i in sorted_incidents]


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str):
    incident = incidents.get(incident_id)
    if not incident:
        return Response(status_code=404, content=json.dumps({"error": "Incident not found"}))
    return incident.to_dict()


# Manual trigger for testing
@app.post("/api/test-alert")
async def test_alert():
    """Simulate an alert for testing the full pipeline."""
    fake_alert = {
        "labels": {
            "alertname": "PodCrashLoopBackOff",
            "severity": "critical",
            "namespace": NAMESPACE,
            "pod": "order-service-test-pod",
            "container": "order-service",
            "category": "pod",
        },
        "annotations": {
            "summary": "Pod order-service-test-pod is crash-looping",
            "description": "Pod order-service-test-pod in namespace apps has restarted 5 times in the last 5 minutes.",
        },
        "status": "firing",
    }
    incident = Incident(fake_alert)
    incidents[incident.id] = incident
    incident.add_event("Test alert triggered manually")
    asyncio.create_task(investigate_incident(incident))
    return {"status": "ok", "incident_id": incident.id}


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI SRE Agent — Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e1e1e6;padding:1.5rem}
h1{font-size:1.5rem;margin-bottom:.25rem;color:#fff}
.subtitle{color:#8b8b96;margin-bottom:1.5rem;font-size:.875rem}
.stats{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}
.stat{background:#1a1b26;border-radius:8px;padding:1rem 1.25rem;flex:1;min-width:120px}
.stat-value{font-size:1.75rem;font-weight:700;color:#fff}
.stat-label{font-size:.75rem;color:#8b8b96;text-transform:uppercase;letter-spacing:.05em;margin-top:.25rem}
.incident{background:#1a1b26;border-radius:8px;padding:1.25rem;margin-bottom:.75rem;border-left:3px solid #333}
.incident.critical{border-left-color:#ef4444}
.incident.warning{border-left-color:#eab308}
.incident.resolved{border-left-color:#22c55e;opacity:.7}
.incident.auto_resolved{border-left-color:#14b8a6;opacity:.65}
.incident.rejected{border-left-color:#6b7280;opacity:.6}
.incident-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem;flex-wrap:wrap;gap:.5rem}
.incident-title{font-weight:600;font-size:1rem;color:#fff}
.badge{padding:.2rem .6rem;border-radius:4px;font-size:.7rem;font-weight:600;text-transform:uppercase}
.badge-critical{background:#7f1d1d;color:#fca5a5}
.badge-warning{background:#713f12;color:#fde68a}
.badge-investigating{background:#1e3a5f;color:#93c5fd}
.badge-awaiting{background:#5b21b6;color:#c4b5fd}
.badge-resolved{background:#14532d;color:#86efac}
.badge-auto{background:#134e4a;color:#99f6e4}
.badge-rejected{background:#374151;color:#9ca3af}
.badge-executing{background:#7c2d12;color:#fed7aa}
.badge-failed{background:#7f1d1d;color:#fca5a5}
.section{margin-top:.75rem}
.section-label{font-size:.7rem;color:#8b8b96;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.25rem}
.analysis{background:#12131d;border-radius:6px;padding:.75rem;font-size:.85rem;line-height:1.5;color:#c8c8d0;white-space:pre-wrap}
.params{margin-top:.4rem;font-size:.78rem;color:#93c5fd;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.params .k{color:#8b8b96}
.manual-note{font-size:.8rem;color:#fbbf24;align-self:center}
.modebar{display:flex;align-items:center;gap:.6rem;background:#1a1b26;border-radius:8px;padding:.5rem .9rem;margin-bottom:1rem;font-size:.8rem;color:#8b8b96}
.modebar select{background:#12131d;color:#e1e1e6;border:1px solid #333;border-radius:5px;padding:.3rem .5rem;font-size:.8rem}
.modebar .hint{color:#6b7280}
.btn-reinv{background:#1e3a5f;color:#93c5fd}.btn-reinv:hover{background:#25497a}
.runmeta{font-size:.72rem;color:#6b7280;margin-top:.35rem}
.prev{margin-top:.75rem;border-top:1px solid #262735;padding-top:.5rem}
.prev summary{cursor:pointer;font-size:.75rem;color:#8b8b96}
.prev-item{background:#101119;border-radius:6px;padding:.6rem;margin-top:.5rem;font-size:.8rem;color:#c8c8d0}
.prev-item .h{color:#93c5fd;font-size:.72rem;margin-bottom:.25rem}
.actions{display:flex;gap:.5rem;margin-top:.75rem}
.btn{padding:.5rem 1.25rem;border:none;border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;transition:all .15s}
.btn-approve{background:#16a34a;color:#fff}.btn-approve:hover{background:#15803d}
.btn-reject{background:#374151;color:#d1d5db}.btn-reject:hover{background:#4b5563}
.btn-test{background:#7c3aed;color:#fff;margin-bottom:1rem}.btn-test:hover{background:#6d28d9}
.btn-sm{padding:.35rem .75rem;font-size:.75rem}
.timeline{margin-top:.5rem}
.timeline-entry{font-size:.75rem;color:#8b8b96;padding:.15rem 0;border-left:1px solid #333;padding-left:.75rem;margin-left:.25rem}
.timeline-entry .time{color:#6b7280}
.empty{text-align:center;color:#6b7280;padding:3rem;font-size:.9rem}
.refresh-note{color:#6b7280;font-size:.75rem;margin-top:.5rem}
</style>
</head>
<body>
<h1>AI SRE Agent</h1>
<p class="subtitle">AI-powered incident response with human-in-the-loop approval</p>

<div style="display:flex;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap">
  <button class="btn btn-test" onclick="testAlert()">Trigger test alert</button>
  <button class="btn btn-sm btn-reject" onclick="loadIncidents()">Refresh</button>
</div>

<div class="stats">
  <div class="stat"><div class="stat-value" id="total">0</div><div class="stat-label">Total</div></div>
  <div class="stat"><div class="stat-value" id="active" style="color:#ef4444">0</div><div class="stat-label">Active</div></div>
  <div class="stat"><div class="stat-value" id="awaiting" style="color:#a78bfa">0</div><div class="stat-label">Awaiting</div></div>
  <div class="stat"><div class="stat-value" id="resolved_count" style="color:#22c55e">0</div><div class="stat-label">Closed</div></div>
</div>

<div class="modebar">
  <span>Investigation mode</span>
  <select id="mode" onchange="setMode(this.value)">
    <option value="dossier">Dossier — fixed queries, one call</option>
    <option value="tools">Tool-driven — model chooses what to look at</option>
  </select>
  <span class="hint" id="modehint"></span>
</div>

<div id="incidents"><div class="empty">No incidents yet. Trigger a test alert or wait for real alerts.</div></div>
<p class="refresh-note">Auto-refreshes every 5 seconds</p>

<script>
const API='/api';
// Incident text comes from AlertManager annotations and from the model — treat
// all of it as untrusted when building HTML.
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function loadIncidents(){
  try{
    const r=await fetch(API+'/incidents');
    const data=await r.json();
    document.getElementById('total').textContent=data.length;
    document.getElementById('active').textContent=data.filter(i=>['investigating','executing'].includes(i.status)).length;
    document.getElementById('awaiting').textContent=data.filter(i=>i.status==='awaiting_approval').length;
    document.getElementById('resolved_count').textContent=data.filter(i=>['resolved','auto_resolved'].includes(i.status)).length;
    if(!data.length){document.getElementById('incidents').innerHTML='<div class="empty">No incidents yet.</div>';return}
    document.getElementById('incidents').innerHTML=data.map(renderIncident).join('');
  }catch(e){console.error(e)}
}

async function loadConfig(){
  try{
    const c=await (await fetch(API+'/config')).json();
    document.getElementById('mode').value=c.investigation_mode;
    document.getElementById('modehint').textContent =
      c.investigation_mode==='tools'
        ? `up to ${c.max_tool_iterations} tool calls per incident`
        : 'predictable cost and latency';
  }catch(e){console.error(e)}
}
async function setMode(mode){
  const r=await fetch(API+'/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({investigation_mode:mode})});
  if(!r.ok){const d=await r.json().catch(()=>({}));alert(d.error||'Could not change mode');}
  loadConfig();
}
async function reinvestigate(id,mode){
  const btn=event&&event.target; if(btn){btn.disabled=true;btn.textContent='Investigating…';}
  const r=await fetch(API+'/incidents/'+id+'/reinvestigate',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
  if(!r.ok){const d=await r.json().catch(()=>({}));alert(d.error||'Re-investigation failed');}
  loadIncidents();
}

// Earlier runs against the same incident, so two modes can be compared.
function renderPrevious(i){
  const prior=(i.analyses||[]).slice(0,-1);
  if(!prior.length) return '';
  return `<details class="prev"><summary>Previous analyses (${prior.length})</summary>` +
    prior.map(a=>`<div class="prev-item">
      <div class="h">${esc(a.mode)} · ${esc(a.model||'')} · ${esc(a.duration_s)}s · ${esc(a.tool_calls)} tool calls · confidence ${esc(a.confidence)}</div>
      <div><b>${esc(a.root_cause)}</b></div>
      <div>${esc(a.analysis)}</div>
      <div class="params"><span class="k">action:</span> ${esc(a.action_type)}</div>
    </div>`).join('') + `</details>`;
}

// The approval gate must show the arguments, not just the verb.
function renderParams(p){
  if(!p||typeof p!=='object'||!Object.keys(p).length)
    return '<div class="params"><span class="k">no parameters supplied</span></div>';
  return '<div class="params">'+Object.entries(p)
    .map(([k,v])=>`<span class="k">${esc(k)}:</span> ${esc(typeof v==='object'?JSON.stringify(v):v)}`)
    .join(' &nbsp;·&nbsp; ')+'</div>';
}

function renderIncident(i){
  const statusBadge={investigating:'badge-investigating',awaiting_approval:'badge-awaiting',
    approved:'badge-investigating',executing:'badge-executing',resolved:'badge-resolved',auto_resolved:'badge-auto',
    rejected:'badge-rejected',failed:'badge-failed'};
  const cls=i.severity==='critical'?'critical':i.severity==='warning'?'warning':'';
  const resolved=['resolved','auto_resolved','rejected','failed'].includes(i.status);
  return `<div class="incident ${resolved?i.status:cls}">
    <div class="incident-header">
      <span class="incident-title">${esc(i.alert_name)} — ${esc(i.pod||'cluster')}</span>
      <div style="display:flex;gap:.5rem">
        <span class="badge badge-${esc(i.severity)}">${esc(i.severity)}</span>
        <span class="badge ${statusBadge[i.status]||''}">${esc(i.status.replace('_',' '))}</span>
      </div>
    </div>
    <div style="font-size:.8rem;color:#8b8b96">ID: ${esc(i.id)} · ${new Date(i.created_at).toLocaleString()}</div>
    ${i.summary?`<div style="margin-top:.5rem;font-size:.85rem">${esc(i.summary)}</div>`:''}
    ${i.root_cause?`<div class="section"><div class="section-label">Root cause</div>
      <div class="analysis">${esc(i.root_cause)}</div></div>`:''}
    ${i.ai_analysis?`<div class="section"><div class="section-label">AI analysis</div>
      <div class="analysis">${esc(i.ai_analysis)}</div></div>`:''}
    ${i.recommended_action?`<div class="section"><div class="section-label">Recommended action (${esc(i.recommended_action_type)})</div>
      <div class="analysis">${esc(i.recommended_action)}</div>
      ${i.recommended_action_type&&i.recommended_action_type!=='manual'?renderParams(i.recommended_action_params):''}
      ${(i.analyses||[]).length?`<div class="runmeta">${esc(i.analyses.at(-1).mode)} mode · ${esc(i.analyses.at(-1).duration_s)}s · ${esc(i.analyses.at(-1).tool_calls)} tool calls · confidence ${esc(i.analyses.at(-1).confidence)}</div>`:''}</div>`:''}
    ${i.remediation_result?`<div class="section"><div class="section-label">Remediation result</div>
      <div class="analysis">${esc(i.remediation_result)}</div></div>`:''}
    ${i.status==='awaiting_approval'?(i.recommended_action_type==='manual'
      ? `<div class="actions">
      <button class="btn btn-reject" onclick="reject('${esc(i.id)}')">Close (handled manually)</button>
      <button class="btn btn-reinv btn-sm" onclick="reinvestigate('${esc(i.id)}','tools')">&#8635; Re-investigate with tools</button>
      <span class="manual-note">No automated remediation — the agent proposes no action it can execute.</span>
    </div>`
      : `<div class="actions">
      <button class="btn btn-approve" onclick="approve('${esc(i.id)}')">Approve &amp; execute</button>
      <button class="btn btn-reject" onclick="reject('${esc(i.id)}')">Reject (handle manually)</button>
      <button class="btn btn-reinv btn-sm" onclick="reinvestigate('${esc(i.id)}','tools')">&#8635; Re-investigate with tools</button>
    </div>`):''}
    ${renderPrevious(i)}
    ${['resolved','auto_resolved','rejected','failed'].includes(i.status)?`<div class="actions">
      <button class="btn btn-reinv btn-sm" onclick="reinvestigate('${esc(i.id)}','tools')">&#8635; Re-investigate with tools</button>
      <button class="btn btn-reinv btn-sm" onclick="reinvestigate('${esc(i.id)}','dossier')">&#8635; Re-investigate (dossier)</button>
    </div>`:''}
    ${i.timeline?.length?`<div class="section"><div class="section-label">Timeline</div>
      <div class="timeline">${i.timeline.map(t=>
        `<div class="timeline-entry"><span class="time">${new Date(t.time).toLocaleTimeString()}</span> ${esc(t.event)}</div>`
      ).join('')}</div></div>`:''}
  </div>`;
}

async function approve(id){
  if(!confirm('Execute the recommended remediation?'))return;
  const r=await fetch(API+'/incidents/'+id+'/approve',{method:'POST'});
  if(!r.ok){
    const d=await r.json().catch(()=>({}));
    alert(d.error||('Approve failed: HTTP '+r.status));
  }
  loadIncidents();
}
async function reject(id){
  await fetch(API+'/incidents/'+id+'/reject',{method:'POST'});loadIncidents();
}
async function testAlert(){
  await fetch(API+'/test-alert',{method:'POST'});
  setTimeout(loadIncidents,1000);setTimeout(loadIncidents,5000);setTimeout(loadIncidents,15000);
}
loadConfig();loadIncidents();setInterval(loadIncidents,5000);
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
