"""Supabase JWT verification — the middleware side of auth.

The browser talks to Supabase directly (via `@supabase/supabase-js`) to sign
in, receives an access token, and sends it to us as `Authorization: Bearer
<jwt>`. This module decodes the JWT locally and returns an `AuthUser`. No
network call back to Supabase per request once the JWKS is cached.

**Signing algorithms:** modern Supabase projects sign tokens with asymmetric
keys (ES256 / P-256 or RS256) and publish the public keys at
`/auth/v1/.well-known/jwks.json`. Older / un-migrated projects use a shared
HS256 secret. We inspect the token's `alg` header and route to the right
path so both work during migration.

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


# Supported asymmetric algorithms. HS256 is handled separately via the
# shared-secret path.
_ASYMMETRIC_ALGS = ("ES256", "RS256")

# Cached JWKS client. PyJWKClient has its own in-memory cache; we only need
# one instance per process.
_JWKS_CLIENT = None


def _get_jwks_client():
    """Lazy-construct a PyJWKClient pointed at the project's JWKS endpoint.
    Supabase serves keys at `<project>/auth/v1/.well-known/jwks.json`."""
    global _JWKS_CLIENT
    if _JWKS_CLIENT is not None:
        return _JWKS_CLIENT
    if not config.SUPABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_URL not configured — cannot fetch JWKS to verify tokens.",
        )
    try:
        from jwt import PyJWKClient  # type: ignore
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="PyJWT is not installed. Run `pip install PyJWT` on the server.",
        ) from e
    jwks_url = f"{config.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
    _JWKS_CLIENT = PyJWKClient(jwks_url)
    return _JWKS_CLIENT


def _decode_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return its claims. Picks HS256-via-secret or
    ES256/RS256-via-JWKS based on the token's `alg` header."""
    try:
        import jwt  # type: ignore
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="PyJWT is not installed. Run `pip install PyJWT` on the server.",
        ) from e

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token header: {e}")
    alg = header.get("alg") or ""

    try:
        # Small `leeway` (seconds) absorbs minor client/server clock drift.
        # Without it, a JWT issued on a slightly-ahead machine trips
        # "token is not yet valid (iat)" on decode.
        if alg == "HS256":
            if not config.SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=503,
                    detail="SUPABASE_JWT_SECRET not configured — cannot verify HS256 tokens.",
                )
            claims = jwt.decode(
                token,
                config.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                leeway=10,
            )
        elif alg in _ASYMMETRIC_ALGS:
            jwks_client = _get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience="authenticated",
                leeway=10,
            )
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Unsupported token algorithm: {alg or '(missing)'}",
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
