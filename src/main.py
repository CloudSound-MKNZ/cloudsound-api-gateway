"""API Gateway main application.

Central entry point for all CloudSound API requests.
Handles routing, authentication, rate limiting, and request forwarding.
"""
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
import structlog

from cloudsound_shared.health import router as health_router
from cloudsound_shared.metrics import get_metrics
from cloudsound_shared.middleware.error_handler import (
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler,
)
from cloudsound_shared.middleware.correlation import CorrelationIDMiddleware
from cloudsound_shared.logging import configure_logging, get_logger
from cloudsound_shared.config.settings import app_settings

from .metrics import init_metrics, record_request
from .middleware.auth import AuthMiddleware
from .middleware.rate_limit import RateLimitMiddleware, RateLimitConfig
from .middleware.proxy import ProxyMiddleware, ServiceRegistry
from .routes.gateway import router as gateway_router

# Configure logging
configure_logging(log_level=app_settings.log_level, log_format=app_settings.log_format)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("api_gateway_starting", version=app_settings.app_version)
    
    # Initialize metrics
    init_metrics(app_settings.app_version)
    
    logger.info(
        "api_gateway_started",
        version=app_settings.app_version,
        environment=app_settings.environment,
    )
    
    yield
    
    # Shutdown
    logger.info("api_gateway_shutdown")


# Create FastAPI app
app = FastAPI(
    title="CloudSound API Gateway",
    version=app_settings.app_version,
    description="Central API Gateway for CloudSound platform",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS middleware (must be first)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Correlation ID middleware
app.add_middleware(CorrelationIDMiddleware)

# Authentication middleware
app.add_middleware(AuthMiddleware)

# Rate limiting middleware
rate_limit_config = RateLimitConfig(
    requests_per_minute=100,  # 100 requests per minute
    burst_size=20,
    exempt_routes=("/health", "/metrics", "/docs", "/openapi.json"),
)
app.add_middleware(RateLimitMiddleware, config=rate_limit_config)

# Proxy middleware (forwards to backend services)
service_registry = ServiceRegistry()
app.add_middleware(ProxyMiddleware, registry=service_registry, timeout=30.0)

# Exception handlers
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# Include routers
app.include_router(health_router)
app.include_router(gateway_router)


# Request timing middleware
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    """Record request timing metrics."""
    start_time = time.time()
    
    response = await call_next(request)
    
    duration = time.time() - start_time
    
    # Record metrics
    record_request(
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration=duration,
    )
    
    # Add timing header
    response.headers["X-Response-Time"] = f"{duration:.3f}s"
    
    return response


# Prometheus metrics endpoint
@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint."""
    return Response(content=get_metrics(), media_type="text/plain")


# Root endpoint
@app.get("/")
async def root():
    """API Gateway root endpoint."""
    return {
        "service": "CloudSound API Gateway",
        "version": app_settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }


# API version info
@app.get("/api")
async def api_info():
    """API information."""
    return {
        "version": "v1",
        "base_url": "/api/v1",
        "endpoints": {
            "radio": "/api/v1/radio",
            "concerts": "/api/v1/concerts",
            "search": "/api/v1/search",
            "auth": "/api/v1/auth",
            "discover": "/api/v1/discover",
            "events": "/api/v1/events",
            "admin": "/api/v1/admin",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

