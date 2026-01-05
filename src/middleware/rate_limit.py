"""Rate limiting middleware for API Gateway.

Implements token bucket algorithm for rate limiting requests.
"""
import time
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_size: int = 10  # Allow burst of requests
    
    # Different limits for different routes
    route_limits: Dict[str, int] = field(default_factory=dict)
    
    # Exempt routes from rate limiting
    exempt_routes: Tuple[str, ...] = (
        "/health",
        "/metrics",
    )


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""
    tokens: float
    last_update: float
    capacity: int
    refill_rate: float  # tokens per second
    
    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens. Returns True if successful."""
        now = time.time()
        
        # Refill tokens based on time elapsed
        elapsed = now - self.last_update
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_rate
        )
        self.last_update = now
        
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def time_until_available(self, tokens: int = 1) -> float:
        """Calculate time until tokens are available."""
        if self.tokens >= tokens:
            return 0
        
        needed = tokens - self.tokens
        return needed / self.refill_rate


class RateLimiter:
    """Rate limiter using token bucket algorithm.
    
    Usage:
        limiter = RateLimiter(requests_per_minute=60)
        
        if not limiter.is_allowed(client_ip):
            raise HTTPException(429, "Rate limit exceeded")
    """
    
    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_size: int = 10,
    ):
        """Initialize rate limiter.
        
        Args:
            requests_per_minute: Sustained request rate
            burst_size: Maximum burst of requests
        """
        self.requests_per_minute = requests_per_minute
        self.burst_size = burst_size
        self.refill_rate = requests_per_minute / 60.0  # per second
        
        # Client buckets: client_id -> TokenBucket
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
        
        # Cleanup old buckets periodically
        self._cleanup_interval = 300  # 5 minutes
        self._last_cleanup = time.time()
    
    async def is_allowed(self, client_id: str) -> Tuple[bool, Dict[str, any]]:
        """Check if request is allowed for client.
        
        Args:
            client_id: Unique client identifier (IP, user ID, etc.)
            
        Returns:
            Tuple of (is_allowed, rate_limit_info)
        """
        async with self._lock:
            # Cleanup old buckets periodically
            await self._maybe_cleanup()
            
            # Get or create bucket
            if client_id not in self._buckets:
                self._buckets[client_id] = TokenBucket(
                    tokens=float(self.burst_size),
                    last_update=time.time(),
                    capacity=self.burst_size,
                    refill_rate=self.refill_rate,
                )
            
            bucket = self._buckets[client_id]
            allowed = bucket.consume(1)
            
            info = {
                "limit": self.requests_per_minute,
                "remaining": int(bucket.tokens),
                "reset": int(bucket.time_until_available()),
            }
            
            if not allowed:
                logger.warning(
                    "rate_limit_exceeded",
                    client_id=client_id,
                    remaining=info["remaining"],
                )
            
            return allowed, info
    
    async def _maybe_cleanup(self) -> None:
        """Clean up old buckets to prevent memory growth."""
        now = time.time()
        
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        # Remove buckets that haven't been used recently
        cutoff = now - self._cleanup_interval
        old_buckets = [
            k for k, v in self._buckets.items()
            if v.last_update < cutoff
        ]
        
        for key in old_buckets:
            del self._buckets[key]
        
        self._last_cleanup = now
        
        if old_buckets:
            logger.debug("rate_limit_buckets_cleaned", count=len(old_buckets))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware for rate limiting requests.
    
    Adds rate limit headers to responses and returns 429 when exceeded.
    """
    
    def __init__(
        self,
        app,
        config: Optional[RateLimitConfig] = None,
    ):
        super().__init__(app)
        self.config = config or RateLimitConfig()
        self.limiter = RateLimiter(
            requests_per_minute=self.config.requests_per_minute,
            burst_size=self.config.burst_size,
        )
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip exempt routes
        path = request.url.path
        if self._is_exempt(path):
            return await call_next(request)
        
        # Get client identifier
        client_id = self._get_client_id(request)
        
        # Check rate limit
        allowed, info = await self.limiter.is_allowed(client_id)
        
        if not allowed:
            return Response(
                content='{"detail": "Rate limit exceeded. Try again later."}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                media_type="application/json",
                headers={
                    "X-RateLimit-Limit": str(info["limit"]),
                    "X-RateLimit-Remaining": str(info["remaining"]),
                    "X-RateLimit-Reset": str(info["reset"]),
                    "Retry-After": str(info["reset"]),
                },
            )
        
        # Process request
        response = await call_next(request)
        
        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
        
        return response
    
    def _is_exempt(self, path: str) -> bool:
        """Check if path is exempt from rate limiting."""
        for exempt in self.config.exempt_routes:
            if path.startswith(exempt):
                return True
        return False
    
    def _get_client_id(self, request: Request) -> str:
        """Get unique client identifier.
        
        Uses authenticated user ID if available, otherwise IP.
        """
        # Try to get user ID from auth
        if hasattr(request.state, "user") and request.state.user:
            return f"user:{request.state.user.user_id}"
        
        # Fall back to IP address
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        
        client = request.client
        if client:
            return f"ip:{client.host}"
        
        return "unknown"

