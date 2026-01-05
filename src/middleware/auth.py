"""Authentication and authorization helpers for API Gateway."""
from fastapi import Header, HTTPException, status
from typing import Optional
from backend.authentication.src.jwt_handler import verify_token, TokenData
import structlog

logger = structlog.get_logger(__name__)


async def require_user(authorization: Optional[str] = Header(None)) -> TokenData:
    """Validate bearer token and return token data."""
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("missing_authorization_header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = authorization.split(" ", 1)[1]
    token_data = verify_token(token)

    if not token_data:
        logger.warning("invalid_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    logger.debug("authenticated_user", user_id=token_data.user_id, role=token_data.role)
    return token_data


async def require_admin(authorization: Optional[str] = Header(None)) -> TokenData:
    """Validate bearer token and require admin role."""
    token_data = await require_user(authorization)

    if token_data.role != "admin":
        logger.warning("admin_required", user_id=token_data.user_id, role=token_data.role)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )

    return token_data

