"""
Order Service — Sample FastAPI application for AI SRE PoC.

Features:
- CRUD operations for orders
- Prometheus metrics (request count, latency, error rate)
- Structured JSON logging
- Health check endpoints
- Fault injection endpoints (for chaos testing)
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("order-service")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

# NOTE: the label is `path`, not `endpoint`. prometheus-operator sets its own
# `endpoint` target label (the ServiceMonitor port name) and renames any
# colliding metric label to `exported_endpoint`, which makes these series
# confusing to query.
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status", "service"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path", "service"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)
ORDERS_CREATED = Counter(
    "orders_created_total", "Total orders created", ["service"]
)
DB_CONNECTION_ERRORS = Counter(
    "db_connection_errors_total", "Database connection errors", ["service"]
)
ACTIVE_ORDERS = Gauge(
    "active_orders", "Currently active orders", ["service"]
)

SERVICE_NAME = "order-service"

# ---------------------------------------------------------------------------
# Fault injection state (toggled via API for chaos testing)
# ---------------------------------------------------------------------------

faults = {
    "latency_ms": 0,        # Injected latency in milliseconds
    "error_rate": 0.0,      # Probability of returning 500
    "memory_leak": False,   # Simulate memory leak
    "db_disconnect": False, # Pretend DB is down
}
memory_leak_store: list[bytes] = []

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_HOST = os.getenv("DB_HOST", "postgres.apps.svc.cluster.local")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "orders")
DB_USER = os.getenv("DB_USER", "orders")
DB_PASSWORD = os.getenv("DB_PASSWORD", "orders-secret")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis.apps.svc.cluster.local:6379")

# ---------------------------------------------------------------------------
# Database and Redis connections
# ---------------------------------------------------------------------------

db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None

# Dependencies may not be up when this process starts, and they can go away
# later. Connecting once at boot leaves the pod permanently broken if it loses
# that race, so connections are established lazily and retried on demand.
_db_lock = asyncio.Lock()
_redis_lock = asyncio.Lock()


async def ensure_db() -> bool:
    """Return True if a usable pool exists, (re)connecting if necessary."""
    if db_pool is not None:
        return True
    async with _db_lock:
        if db_pool is not None:      # another request won the race
            return True
        await init_db()
        return db_pool is not None


async def ensure_redis() -> bool:
    """Return True if a usable Redis client exists. Never fatal."""
    if redis_client is not None:
        return True
    async with _redis_lock:
        if redis_client is not None:
            return True
        await init_redis()
        return redis_client is not None


async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(255) NOT NULL,
                    product VARCHAR(255) NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    total_price DECIMAL(10,2) NOT NULL DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
        logger.info("Database connection established and schema created")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        db_pool = None


async def init_redis():
    global redis_client
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Failed to connect to Redis (non-fatal): {e}")
        redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting order-service...")
    await init_db()
    await init_redis()
    yield
    logger.info("Shutting down order-service...")
    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Order Service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware — metrics and fault injection
# ---------------------------------------------------------------------------

@app.middleware("http")
async def metrics_and_faults(request: Request, call_next):
    # Skip infrastructure endpoints. Probes are not user traffic: counting a
    # failing /readyz as a 5xx makes HighHTTPErrorRate fire during any
    # dependency outage even when no user request ever failed — and with no
    # real traffic, probes are 100% of the sample, so the ratio hits 1.0.
    if request.url.path in ("/metrics", "/healthz", "/readyz"):
        return await call_next(request)

    start_time = time.time()

    # Fault injection: latency
    if faults["latency_ms"] > 0:
        await asyncio.sleep(faults["latency_ms"] / 1000.0)

    # Fault injection: random errors
    if faults["error_rate"] > 0 and random.random() < faults["error_rate"]:
        logger.error(f"Injected fault: returning 500 for {request.url.path}")
        duration = time.time() - start_time
        REQUEST_COUNT.labels(request.method, request.url.path, "500", SERVICE_NAME).inc()
        REQUEST_LATENCY.labels(request.method, request.url.path, SERVICE_NAME).observe(duration)
        return Response(
            content=json.dumps({"error": "Internal server error (injected fault)"}),
            status_code=500,
            media_type="application/json",
        )

    # Fault injection: memory leak
    if faults["memory_leak"]:
        memory_leak_store.append(b"X" * 1024 * 1024)  # 1MB per request
        logger.warning(f"Memory leak: allocated {len(memory_leak_store)}MB total")

    try:
        response = await call_next(request)
        duration = time.time() - start_time
        REQUEST_COUNT.labels(
            request.method, request.url.path, str(response.status_code), SERVICE_NAME
        ).inc()
        REQUEST_LATENCY.labels(
            request.method, request.url.path, SERVICE_NAME
        ).observe(duration)
        return response
    except Exception:
        duration = time.time() - start_time
        REQUEST_COUNT.labels(request.method, request.url.path, "500", SERVICE_NAME).inc()
        REQUEST_LATENCY.labels(request.method, request.url.path, SERVICE_NAME).observe(duration)
        logger.exception(f"Unhandled error processing {request.url.path}")
        raise


# ---------------------------------------------------------------------------
# Health check endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/readyz")
async def ready():
    checks = {"db": False, "redis": False}

    global db_pool, redis_client

    if not faults["db_disconnect"] and await ensure_db():
        try:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["db"] = True
        except Exception:
            # Pool is unusable — discard it so the next probe reconnects.
            db_pool = None

    if await ensure_redis():
        try:
            await redis_client.ping()
            checks["redis"] = True
        except Exception:
            redis_client = None

    all_ok = checks["db"]  # DB is required, Redis is nice-to-have
    status_code = 200 if all_ok else 503
    return Response(
        content=json.dumps({"status": "ok" if all_ok else "degraded", "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Order API
# ---------------------------------------------------------------------------

class OrderCreate(BaseModel):
    customer_name: str
    product: str
    quantity: int = 1
    total_price: float = 0.0


class OrderUpdate(BaseModel):
    status: str | None = None
    quantity: int | None = None


@app.get("/api/orders")
async def list_orders():
    if faults["db_disconnect"]:
        DB_CONNECTION_ERRORS.labels(SERVICE_NAME).inc()
        logger.error("Database connection refused (fault injected)")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not await ensure_db():
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Try cache first
    if redis_client:
        try:
            cached = await redis_client.get("orders:list")
            if cached:
                logger.info("Cache hit for orders list")
                return json.loads(cached)
        except Exception:
            pass

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT 100"
            )
            orders = [dict(row) for row in rows]
            # Convert datetime to string for JSON
            for order in orders:
                for key in ("created_at", "updated_at"):
                    if order.get(key):
                        order[key] = order[key].isoformat()
                order["total_price"] = float(order["total_price"])

            # Cache the result
            if redis_client:
                try:
                    await redis_client.setex("orders:list", 30, json.dumps(orders))
                except Exception:
                    pass

            return orders
    except Exception as e:
        DB_CONNECTION_ERRORS.labels(SERVICE_NAME).inc()
        logger.error(f"Failed to fetch orders: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/orders", status_code=201)
async def create_order(order: OrderCreate):
    if faults["db_disconnect"]:
        DB_CONNECTION_ERRORS.labels(SERVICE_NAME).inc()
        logger.error("Database connection refused (fault injected)")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not await ensure_db():
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO orders (customer_name, product, quantity, total_price)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                order.customer_name, order.product, order.quantity, order.total_price,
            )
            ORDERS_CREATED.labels(SERVICE_NAME).inc()
            ACTIVE_ORDERS.labels(SERVICE_NAME).inc()
            logger.info(
                f"Order created: id={row['id']} customer={order.customer_name} product={order.product}"
            )

            # Invalidate cache
            if redis_client:
                try:
                    await redis_client.delete("orders:list")
                except Exception:
                    pass

            result = dict(row)
            for key in ("created_at", "updated_at"):
                if result.get(key):
                    result[key] = result[key].isoformat()
            result["total_price"] = float(result["total_price"])
            return result
    except Exception as e:
        DB_CONNECTION_ERRORS.labels(SERVICE_NAME).inc()
        logger.error(f"Failed to create order: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    if faults["db_disconnect"]:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if not await ensure_db():
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        result = dict(row)
        for key in ("created_at", "updated_at"):
            if result.get(key):
                result[key] = result[key].isoformat()
        result["total_price"] = float(result["total_price"])
        return result


# ---------------------------------------------------------------------------
# Fault injection API (for chaos testing)
# ---------------------------------------------------------------------------

@app.post("/fault/latency")
async def inject_latency(ms: int = 2000):
    faults["latency_ms"] = ms
    logger.warning(f"FAULT INJECTED: {ms}ms latency on every request")
    return {"status": "injected", "latency_ms": ms}


@app.post("/fault/errors")
async def inject_errors(rate: float = 0.5):
    faults["error_rate"] = min(rate, 1.0)
    logger.warning(f"FAULT INJECTED: {rate*100}% error rate")
    return {"status": "injected", "error_rate": rate}


@app.post("/fault/memory-leak")
async def inject_memory_leak():
    faults["memory_leak"] = True
    logger.warning("FAULT INJECTED: memory leak enabled")
    return {"status": "injected", "memory_leak": True}


@app.post("/fault/db-disconnect")
async def inject_db_disconnect():
    faults["db_disconnect"] = True
    logger.warning("FAULT INJECTED: database disconnect simulated")
    return {"status": "injected", "db_disconnect": True}


@app.post("/fault/reset")
async def reset_faults():
    global memory_leak_store
    faults["latency_ms"] = 0
    faults["error_rate"] = 0.0
    faults["memory_leak"] = False
    faults["db_disconnect"] = False
    memory_leak_store = []
    logger.info("All faults cleared")
    return {"status": "cleared", "faults": faults}


@app.get("/fault/status")
async def fault_status():
    return {
        "faults": faults,
        "memory_leak_mb": len(memory_leak_store),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
