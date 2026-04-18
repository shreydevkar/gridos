"""Supabase JWT verification — the middleware side of auth.

The browser talks to Supabase directly (via `@supabase/supabase-js`) to sign
in, receives an access token, and sends it to us as `Authorization: Bearer
<jwt>`. This module decodes the JWT locally with the project's JWT secret
and returns an `AuthUser`. No network call back to Supabase per request —
local HS256 verify is sub-millisecond.

FastAPI usage:

    from cloud.auth import require_user, AuthUser

    @app.get("/whoami")
    def whoami(user: AuthUser = Depends(require_user)):
        return {"id": user.id, "email": user.email}

The dependency raises HTTP 401 on missing, expired, or tampered tokens. In
OSS mode (`SAAS_MODE=false`) the dependency is a no-op — it returns a
sentinel `AuthUser(id="oss", email=None)` so endpoints that `Depends`-inject
it work identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from cloud import config


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: Optional[str]


# Sentinel returned from OSS mode. Not a real user; callers should check
# `config.SAAS_MODE` before doing anything that relies on `id` being a uuid.
_OSS_SENTINEL = AuthUser(id="oss", email=None)


def _decode_jwt(token: str) -> dict:
    """Verify HS256 signature against SUPABASE_JWT_SECRET and return claims.
    Lazy-imports PyJWT so OSS deploys don't need the dep."""
    try:
        import jwt  # type: ignore
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="PyJWT is not installed. Run `pip install PyJWT` on the server.",
        ) from e

    if not config.SUPABASE_JWT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_JWT_SECRET not configured — auth cannot verify tokens.",
        )

    try:
        claims = jwt.decode(
            token,
            config.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            # Supabase tokens use `aud: "authenticated"` for signed-in users.
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Token audience mismatch.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    return claims


def _parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1].strip()


def require_user(authorization: Optional[str] = Header(None)) -> AuthUser:
    """Dependency: 401s in SaaS mode if no valid JWT; pass-through in OSS mode."""
    if not config.SAAS_MODE:
        return _OSS_SENTINEL

    token = _parse_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token> header.")

    claims = _decode_jwt(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token is missing the 'sub' claim.")
    email = claims.get("email")
    return AuthUser(id=user_id, email=email)


def optional_user(authorization: Optional[str] = Header(None)) -> Optional[AuthUser]:
    """Dependency: returns AuthUser if a valid JWT is present, else None.
    Useful for endpoints that behave differently for authenticated vs
    anonymous requests but don't require auth."""
    if not config.SAAS_MODE:
        return _OSS_SENTINEL
    token = _parse_bearer(authorization)
    if not token:
        return None
    try:
        claims = _decode_jwt(token)
    except HTTPException:
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    return AuthUser(id=user_id, email=claims.get("email"))
