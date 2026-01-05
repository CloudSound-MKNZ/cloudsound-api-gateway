"""Authentication middleware for API Gateway.

Validates JWT tokens and enforces role-based access control.
"""
from fastapi import Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
import jwt
import structlog

from cloudsound_shared.config.settings import app_settings

logger = structlog.get_logger(__name__)

# Routes that don't require authentication
PUBLIC_ROUTES = [
    "/health",
    "/health/ready",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/refresh",
    # Public read endpoints
    "/api/v1/radio/stations",
    "/api/v1/concerts",
    "/api/v1/search",
]

# Routes that require admin role
ADMIN_ROUTES = [
    "/api/v1/admin",
    "/api/v1/concerts",  # POST, PUT, DELETE require admin
]


@dataclass
class TokenData:
    """Data extracted from JWT token."""
    user_id: str
    email: Optional[str] = None
    role: str = "user"
    exp: Optional[datetime] = None


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware for JWT authentication.
    
    Validates tokens on protected routes and adds user info to request state.
    """
    
    async def dispatch(self, request: Request, call_next):
        # Skip auth for public routes
        path = request.url.path
        
        if self._is_public_route(path):
            return await call_next(request)
        
        # Get token from header
        auth_header = request.headers.get("Authorization")
        
        if not auth_header:
            # Allow request but mark as unauthenticated
            request.state.user = None
            request.state.is_authenticated = False
            return await call_next(request)
        
        try:
            token_data = self._verify_token(auth_header)
            request.state.user = token_data
            request.state.is_authenticated = True
            
            logger.debug(
                "request_authenticated",
                user_id=token_data.user_id,
                role=token_data.role,
                path=path,
            )
            
        except HTTPException:
            request.state.user = None
            request.state.is_authenticated = False
        
        return await call_next(request)
    
    def _is_public_route(self, path: str) -> bool:
        """Check if route is public."""
        for route in PUBLIC_ROUTES:
            if path.startswith(route):
                return True
        return False
    
    def _verify_token(self, auth_header: str) -> TokenData:
        """Verify JWT token from Authorization header."""
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format",
            )
        
        token = auth_header.split(" ", 1)[1]
        
        try:
            payload = jwt.decode(
                token,
                app_settings.secret_key,
                algorithms=[app_settings.jwt_algorithm],
            )
            
            return TokenData(
                user_id=payload.get("sub", ""),
                email=payload.get("email"),
                role=payload.get("role", "user"),
                exp=datetime.fromtimestamp(payload.get("exp", 0)),
            )
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
            )
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {str(e)}",
            )


# Dependency functions for route-level auth
security = HTTPBearer(auto_error=False)


async def require_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> TokenData:
    """Dependency that requires a valid user token.
    
    Usage:
        @app.get("/protected")
        async def protected_route(user: TokenData = Depends(require_user)):
            return {"user_id": user.user_id}
    """
    # Check if already authenticated by middleware
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user
    
    # Try to get from Authorization header
    auth_header = request.headers.get("Authorization")
    
    if not auth_header or not auth_header.lower().startswith("bearer "):
        logger.warning("missing_authorization_header", path=request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = auth_header.split(" ", 1)[1]
    
    try:
        payload = jwt.decode(
            token,
            app_settings.secret_key,
            algorithms=[app_settings.jwt_algorithm],
        )
        
        token_data = TokenData(
            user_id=payload.get("sub", ""),
            email=payload.get("email"),
            role=payload.get("role", "user"),
        )
        
        logger.debug("user_authenticated", user_id=token_data.user_id)
        return token_data
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_admin(request: Request) -> TokenData:
    """Dependency that requires admin role.
    
    Usage:
        @app.post("/admin/action")
        async def admin_action(user: TokenData = Depends(require_admin)):
            return {"admin_id": user.user_id}
    """
    token_data = await require_user(request)
    
    if token_data.role != "admin":
        logger.warning(
            "admin_required",
            user_id=token_data.user_id,
            role=token_data.role,
            path=request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    
    return token_data


def get_current_user(request: Request) -> Optional[TokenData]:
    """Get current user from request state (non-failing).
    
    Returns None if not authenticated.
    """
    if hasattr(request.state, "user"):
        return request.state.user
    return None
