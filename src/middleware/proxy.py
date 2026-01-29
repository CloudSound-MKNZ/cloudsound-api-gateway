"""Proxy middleware for forwarding requests to backend services.

Routes requests to appropriate microservices based on URL patterns.
"""

import httpx
from typing import Optional, Dict, Any
from urllib.parse import urljoin
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse
import structlog

from cloudsound_shared.config.settings import app_settings

logger = structlog.get_logger(__name__)


class ServiceRegistry:
    """Registry of backend services and their URLs."""

    def __init__(self):
        self.services: Dict[str, str] = {
            "radio": app_settings.radio_streaming_url,
            "concerts": app_settings.concert_management_url,
            "auth": app_settings.authentication_url,
            "analytics": app_settings.analytics_url,
            "discovery": app_settings.music_discovery_url,
            "events": app_settings.event_manager_url,
            "admin": app_settings.admin_management_url,
        }

        # Route prefix to service mapping
        self.routes: Dict[str, str] = {
            "/api/v1/radio": "radio",
            "/api/v1/stream": "radio",
            "/api/v1/search": "radio",
            "/api/v1/concerts": "concerts",
            "/api/v1/auth": "auth",
            "/api/v1/analytics": "analytics",
            "/api/v1/discover": "discovery",
            "/api/v1/events": "events",
            "/api/v1/admin": "admin",
        }

    def get_service_url(self, path: str) -> Optional[str]:
        """Get service URL for a given path.

        Args:
            path: Request path

        Returns:
            Service base URL or None
        """
        for route_prefix, service_name in self.routes.items():
            if path.startswith(route_prefix):
                return self.services.get(service_name)
        return None

    def get_backend_path(self, path: str) -> str:
        """Get the path to forward to backend (strip gateway prefix if needed).

        Args:
            path: Original request path

        Returns:
            Path to forward to backend
        """
        # Keep the full path for now - services handle their own routing
        return path


class ProxyMiddleware(BaseHTTPMiddleware):
    """Middleware for proxying requests to backend services.

    Handles:
    - Service discovery and routing
    - Request forwarding with headers
    - Response streaming
    - Error handling
    """

    def __init__(
        self,
        app,
        registry: Optional[ServiceRegistry] = None,
        timeout: float = 30.0,
    ):
        super().__init__(app)
        self.registry = registry or ServiceRegistry()
        self.timeout = timeout

        # Persistent HTTP client
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Check if this path should be proxied
        service_url = self.registry.get_service_url(path)

        if not service_url:
            # Not a proxied route, handle locally
            logger.debug("proxy_skip", path=path, reason="not_proxied")
            return await call_next(request)

        # Forward to backend service
        logger.debug("proxy_dispatch", path=path, service_url=service_url)
        return await self._forward_request(request, service_url)

    async def _forward_request(
        self,
        request: Request,
        service_url: str,
    ) -> Response:
        """Forward request to backend service.

        Args:
            request: Original request
            service_url: Backend service base URL

        Returns:
            Response from backend
        """
        client = await self._get_client()

        # Build target URL
        backend_path = self.registry.get_backend_path(request.url.path)
        target_url = urljoin(service_url, backend_path)

        # Add query string
        if request.url.query:
            target_url = f"{target_url}?{request.url.query}"

        # Forward headers (filter sensitive ones)
        headers = dict(request.headers)
        headers.pop("host", None)  # Remove original host

        # Add forwarding headers
        headers["X-Forwarded-For"] = self._get_client_ip(request)
        headers["X-Forwarded-Host"] = request.headers.get("host", "")
        headers["X-Forwarded-Proto"] = request.url.scheme

        # Add correlation ID if present
        if hasattr(request.state, "correlation_id"):
            headers["X-Correlation-ID"] = request.state.correlation_id

        logger.info(
            "proxy_request",
            method=request.method,
            path=request.url.path,
            target=target_url,
        )

        try:
            # Get request body
            body = await request.body()

            # Forward request
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )

            # Build response
            response_headers = dict(response.headers)
            # Remove hop-by-hop headers
            for header in ["transfer-encoding", "connection", "keep-alive"]:
                response_headers.pop(header, None)

            # Log response details for debugging
            response_size = len(response.content) if response.content else 0
            logger.info(
                "proxy_response",
                path=request.url.path,
                status=response.status_code,
                response_size=response_size,
                content_type=response.headers.get("content-type"),
            )

            # For events/poll endpoint, log response preview to debug empty responses
            if "/events/poll" in request.url.path and response_size > 0:
                try:
                    import json

                    preview = response.content[:500].decode("utf-8", errors="ignore")
                    parsed = json.loads(response.content) if response.content else {}
                    logger.info(
                        "proxy_response_preview",
                        path=request.url.path,
                        preview=preview,
                        events_fetched=parsed.get("events_fetched")
                        if isinstance(parsed, dict)
                        else None,
                    )
                except Exception:
                    pass

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type"),
            )

        except httpx.TimeoutException:
            logger.error("proxy_timeout", target=target_url)
            return Response(
                content='{"detail": "Service timeout"}',
                status_code=504,
                media_type="application/json",
            )
        except httpx.ConnectError:
            logger.error("proxy_connect_error", target=target_url)
            return Response(
                content='{"detail": "Service unavailable"}',
                status_code=503,
                media_type="application/json",
            )
        except Exception as e:
            import traceback

            logger.error(
                "proxy_error",
                target=target_url,
                error=str(e),
                error_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            return Response(
                content='{"detail": "Internal gateway error"}',
                status_code=502,
                media_type="application/json",
            )

    def _get_client_ip(self, request: Request) -> str:
        """Get client IP from request."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        if request.client:
            return request.client.host

        return "unknown"
