"""API Gateway routing configuration.

Defines route handlers and aggregation endpoints.
Some routes are handled directly, others are proxied to backend services.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List
import httpx
import asyncio
import structlog

from cloudsound_shared.config.settings import app_settings
from ..middleware.auth import require_user, require_admin, TokenData

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Gateway"])


# Service URLs
SERVICES = {
    "radio": app_settings.radio_streaming_url,
    "concerts": app_settings.concert_management_url,
    "auth": app_settings.authentication_url,
    "analytics": app_settings.analytics_url,
    "discovery": app_settings.music_discovery_url,
    "events": app_settings.event_manager_url,
}


@router.get("/gateway/services")
async def list_services() -> Dict[str, Any]:
    """List all registered backend services."""
    return {
        "services": list(SERVICES.keys()),
        "count": len(SERVICES),
    }


@router.get("/gateway/health")
async def check_services_health() -> Dict[str, Any]:
    """Check health of all backend services."""
    async def check_service(name: str, url: str) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{url}/health")
                return {
                    "name": name,
                    "status": "healthy" if response.status_code == 200 else "unhealthy",
                    "code": response.status_code,
                }
        except Exception as e:
            return {
                "name": name,
                "status": "unavailable",
                "error": str(e),
            }
    
    # Check all services concurrently
    tasks = [check_service(name, url) for name, url in SERVICES.items()]
    results = await asyncio.gather(*tasks)
    
    healthy_count = sum(1 for r in results if r["status"] == "healthy")
    
    return {
        "services": results,
        "total": len(results),
        "healthy": healthy_count,
        "status": "healthy" if healthy_count == len(results) else "degraded",
    }


@router.get("/gateway/user")
async def get_current_user(user: TokenData = Depends(require_user)) -> Dict[str, Any]:
    """Get information about the current authenticated user."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
        "authenticated": True,
    }


# Aggregation endpoints - combine data from multiple services

@router.get("/home")
async def get_home_data() -> Dict[str, Any]:
    """Get aggregated data for the home page.
    
    Combines:
    - Featured radio stations
    - Upcoming concerts
    - Recent activity
    """
    async def fetch_stations():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{SERVICES['radio']}/api/v1/radio/stations",
                    params={"limit": 6},
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning("fetch_stations_failed", error=str(e))
        return []
    
    async def fetch_concerts():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{SERVICES['concerts']}/api/v1/concerts",
                    params={"limit": 6, "upcoming": True},
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning("fetch_concerts_failed", error=str(e))
        return []
    
    # Fetch concurrently
    stations, concerts = await asyncio.gather(
        fetch_stations(),
        fetch_concerts(),
    )
    
    return {
        "featured_stations": stations[:6] if isinstance(stations, list) else [],
        "upcoming_concerts": concerts[:6] if isinstance(concerts, list) else [],
    }


@router.get("/dashboard")
async def get_dashboard_data(
    user: TokenData = Depends(require_user),
) -> Dict[str, Any]:
    """Get aggregated dashboard data for authenticated users.
    
    Combines:
    - User's listening history
    - Recommended stations
    - Saved concerts
    """
    async def fetch_history():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{SERVICES['analytics']}/api/v1/analytics/history",
                    params={"user_id": user.user_id, "limit": 10},
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning("fetch_history_failed", error=str(e))
        return []
    
    async def fetch_recommendations():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{SERVICES['radio']}/api/v1/radio/stations",
                    params={"limit": 4},
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning("fetch_recommendations_failed", error=str(e))
        return []
    
    history, recommendations = await asyncio.gather(
        fetch_history(),
        fetch_recommendations(),
    )
    
    return {
        "user_id": user.user_id,
        "listening_history": history if isinstance(history, list) else [],
        "recommended_stations": recommendations if isinstance(recommendations, list) else [],
    }


@router.get("/admin/overview")
async def get_admin_overview(
    user: TokenData = Depends(require_admin),
) -> Dict[str, Any]:
    """Get admin dashboard overview.
    
    Combines statistics from all services.
    """
    async def fetch_stats(service: str, endpoint: str) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                url = f"{SERVICES.get(service, '')}{endpoint}"
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning(f"fetch_{service}_stats_failed", error=str(e))
        return {}
    
    # Fetch stats from services
    results = await asyncio.gather(
        fetch_stats("radio", "/api/v1/radio/stats"),
        fetch_stats("concerts", "/api/v1/concerts/stats"),
        fetch_stats("analytics", "/api/v1/analytics/stats"),
        fetch_stats("discovery", "/api/v1/discover/storage/stats"),
        return_exceptions=True,
    )
    
    return {
        "admin_id": user.user_id,
        "radio_stats": results[0] if not isinstance(results[0], Exception) else {},
        "concert_stats": results[1] if not isinstance(results[1], Exception) else {},
        "analytics_stats": results[2] if not isinstance(results[2], Exception) else {},
        "storage_stats": results[3] if not isinstance(results[3], Exception) else {},
    }

