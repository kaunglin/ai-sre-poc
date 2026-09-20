"""
Frontend Service — Simple web UI for the Order Service.
Proxies API calls and serves a basic HTML dashboard.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


# Structured logging
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
logger = logging.getLogger("frontend")

# Metrics
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests",
                        ["method", "path", "status", "service"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Request latency",
                            ["method", "path", "service"])

SERVICE_NAME = "frontend"
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service.apps.svc.cluster.local:8000")

app = FastAPI(title="Frontend", version="1.0.0")


@app.middleware("http")
async def instrument(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    REQUEST_COUNT.labels(request.method, request.url.path, str(response.status_code), SERVICE_NAME).inc()
    REQUEST_LATENCY.labels(request.method, request.url.path, SERVICE_NAME).observe(duration)
    return response


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#f5f5f5;padding:2rem}
  h1{margin-bottom:1.5rem;color:#1a1a1a}
  .card{background:#fff;border-radius:8px;padding:1.5rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.1)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:.75rem;border-bottom:1px solid #eee}
  th{font-weight:600;color:#666;font-size:.875rem;text-transform:uppercase}
  .status{display:inline-block;padding:.25rem .75rem;border-radius:4px;font-size:.75rem;font-weight:600}
  .status-pending{background:#fff3cd;color:#856404}
  .status-completed{background:#d4edda;color:#155724}
  .btn{padding:.5rem 1rem;border:none;border-radius:4px;cursor:pointer;font-size:.875rem;font-weight:500}
  .btn-primary{background:#2563eb;color:#fff}
  .btn-primary:hover{background:#1d4ed8}
  form{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem}
  input{padding:.5rem;border:1px solid #ddd;border-radius:4px;font-size:.875rem}
  #error{color:#dc3545;margin:.5rem 0;display:none}
  .health{display:flex;gap:.5rem;align-items:center;font-size:.875rem;color:#666}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
  .dot-green{background:#22c55e}.dot-red{background:#ef4444}
</style>
</head>
<body>
<h1>Order Dashboard</h1>
<div class="card">
  <h3 style="margin-bottom:1rem">Create order</h3>
  <form onsubmit="createOrder(event)">
    <input id="name" placeholder="Customer name" required>
    <input id="product" placeholder="Product" required>
    <input id="qty" type="number" value="1" min="1" style="width:80px" required>
    <input id="price" type="number" step="0.01" placeholder="Price" required>
    <button class="btn btn-primary" type="submit">Create</button>
  </form>
  <div id="error"></div>
</div>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
    <h3>Orders</h3>
    <div class="health">
      <span class="dot" id="health-dot"></span>
      <span id="health-text">Checking...</span>
    </div>
  </div>
  <table>
    <thead><tr><th>ID</th><th>Customer</th><th>Product</th><th>Qty</th><th>Price</th><th>Status</th><th>Created</th></tr></thead>
    <tbody id="orders"></tbody>
  </table>
</div>
<script>
const API = '/api';
// Order fields are user-supplied; escape before building HTML.
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function loadOrders(){
  try{
    const r=await fetch(API+'/orders');
    if(!r.ok)throw new Error(r.statusText);
    const data=await r.json();
    document.getElementById('orders').innerHTML=data.map(o=>`
      <tr><td>${esc(o.id)}</td><td>${esc(o.customer_name)}</td><td>${esc(o.product)}</td>
      <td>${esc(o.quantity)}</td><td>$${Number(o.total_price).toFixed(2)}</td>
      <td><span class="status status-${esc(o.status)}">${esc(o.status)}</span></td>
      <td>${new Date(o.created_at).toLocaleString()}</td></tr>
    `).join('');
  }catch(e){showError('Failed to load orders: '+e.message)}
}
async function createOrder(e){
  e.preventDefault();hideError();
  try{
    const r=await fetch(API+'/orders',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({customer_name:document.getElementById('name').value,
        product:document.getElementById('product').value,
        quantity:parseInt(document.getElementById('qty').value),
        total_price:parseFloat(document.getElementById('price').value)})});
    if(!r.ok)throw new Error(await r.text());
    e.target.reset();loadOrders();
  }catch(e){showError('Failed to create order: '+e.message)}
}
async function checkHealth(){
  try{
    const r=await fetch(API+'/health');const d=await r.json();
    const ok=d.checks?.db;
    document.getElementById('health-dot').className='dot '+(ok?'dot-green':'dot-red');
    document.getElementById('health-text').textContent=ok?'Healthy':'Degraded';
  }catch{
    document.getElementById('health-dot').className='dot dot-red';
    document.getElementById('health-text').textContent='Unreachable';
  }
}
function showError(m){const e=document.getElementById('error');e.textContent=m;e.style.display='block'}
function hideError(){document.getElementById('error').style.display='none'}
loadOrders();checkHealth();setInterval(loadOrders,10000);setInterval(checkHealth,15000);
</script>
</body></html>"""


# Proxy API calls to order-service
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(request: Request, path: str):
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{ORDER_SERVICE_URL}/{path}"
        if path == "health":
            url = f"{ORDER_SERVICE_URL}/readyz"

        body = await request.body()
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers={"content-type": request.headers.get("content-type", "application/json")},
            )
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type"))
        except httpx.ConnectError as e:
            logger.error(f"Cannot reach order-service: {e}")
            return Response(
                content=json.dumps({"error": "Order service unreachable"}),
                status_code=502, media_type="application/json",
            )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
