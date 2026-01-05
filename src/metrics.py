"""Prometheus metrics for API Gateway."""
from prometheus_client import Counter, Histogram, Gauge, Info
import structlog

logger = structlog.get_logger(__name__)

# Service info
SERVICE_INFO = Info(
    "api_gateway_service",
    "API Gateway service information",
)

# Request metrics
REQUESTS_TOTAL = Counter(
    "api_gateway_requests_total",
    "Total requests processed",
    ["method", "path", "status"],
)

REQUEST_DURATION = Histogram(
    "api_gateway_request_duration_seconds",
    "Request duration in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

# Proxy metrics
PROXY_REQUESTS = Counter(
    "api_gateway_proxy_requests_total",
    "Total proxied requests",
    ["service", "status"],
)

PROXY_DURATION = Histogram(
    "api_gateway_proxy_duration_seconds",
    "Proxy request duration",
    ["service"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

# Rate limiting metrics
RATE_LIMIT_HITS = Counter(
    "api_gateway_rate_limit_hits_total",
    "Total rate limit hits",
    ["client_type"],
)

# Auth metrics
AUTH_ATTEMPTS = Counter(
    "api_gateway_auth_attempts_total",
    "Total authentication attempts",
    ["status"],
)

# Active connections
ACTIVE_CONNECTIONS = Gauge(
    "api_gateway_active_connections",
    "Current active connections",
)


def init_metrics(version: str = "1.0.0") -> None:
    """Initialize service metrics."""
    SERVICE_INFO.info({
        "version": version,
        "service": "api-gateway",
    })
    logger.info("metrics_initialized", version=version)


def record_request(method: str, path: str, status: int, duration: float) -> None:
    """Record a request."""
    # Normalize path to avoid cardinality explosion
    normalized_path = _normalize_path(path)
    
    REQUESTS_TOTAL.labels(
        method=method,
        path=normalized_path,
        status=str(status),
    ).inc()
    
    REQUEST_DURATION.labels(
        method=method,
        path=normalized_path,
    ).observe(duration)


def record_proxy_request(service: str, status: int, duration: float) -> None:
    """Record a proxied request."""
    PROXY_REQUESTS.labels(service=service, status=str(status)).inc()
    PROXY_DURATION.labels(service=service).observe(duration)


def record_rate_limit_hit(client_type: str = "ip") -> None:
    """Record a rate limit hit."""
    RATE_LIMIT_HITS.labels(client_type=client_type).inc()


def record_auth_attempt(success: bool) -> None:
    """Record an authentication attempt."""
    AUTH_ATTEMPTS.labels(status="success" if success else "failure").inc()


def _normalize_path(path: str) -> str:
    """Normalize path to reduce metric cardinality.
    
    Replace UUIDs and IDs with placeholders.
    """
    import re
    
    # Replace UUIDs
    path = re.sub(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        '{uuid}',
        path,
        flags=re.IGNORECASE,
    )
    
    # Replace numeric IDs
    path = re.sub(r'/\d+(?=/|$)', '/{id}', path)
    
    return path

