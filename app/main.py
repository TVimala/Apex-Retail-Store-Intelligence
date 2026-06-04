"""
main.py — FastAPI entrypoint for the Apex Retail Store Intelligence API.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db.database import init_db, check_db_health
from app.routers import events, stores, health as health_router

# ─────────────────────────────────────────────────────────────────────────────
# Structured logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger("api")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('{"event":"startup","msg":"Initialising database"}')
    await init_db()
    yield
    logger.info('{"event":"shutdown"}')


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Apex Retail Store Intelligence API",
    version="1.0.0",
    description="CCTV-powered store analytics for Apex Retail.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Request logging middleware
# ─────────────────────────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    request.state.trace_id = trace_id
    t0 = time.perf_counter()

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            '{"trace_id":"%s","method":"%s","path":"%s","latency_ms":%d,"status":500,"error":"%s"}',
            trace_id, request.method, request.url.path, latency_ms, str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "trace_id": trace_id},
        )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    store_id = request.path_params.get("store_id", "")
    logger.info(
        '{"trace_id":"%s","method":"%s","path":"%s","store_id":"%s","latency_ms":%d,"status":%d}',
        trace_id, request.method, request.url.path, store_id, latency_ms, response.status_code,
    )
    response.headers["X-Trace-ID"] = trace_id
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.exception("Unhandled exception trace_id=%s", trace_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred",
            "trace_id": trace_id,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routers
# ─────────────────────────────────────────────────────────────────────────────
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(stores.router, prefix="/stores", tags=["stores"])
app.include_router(health_router.router, tags=["health"])


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Apex Retail Store Intelligence API", "version": "1.0.0"}
