"""JWT verification against the configured Keycloak realm.

The OIDC public keys (JWKS) are fetched lazily and cached for
``Settings.keycloak_jwks_cache_seconds``. The token's ``azp`` claim is
checked against the configured allow-list of clients.

No module-level environment reads happen here; all configuration comes from
:func:`proposal_service.config.get_settings`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2AuthorizationCodeBearer
from jose import JWTError, jwt

from proposal_service.config import Settings, get_settings


logger = logging.getLogger(__name__)


_JWKS_CACHE: dict[str, Any] = {"keys": None, "expires_at": 0.0}


def build_oauth_scheme(settings: Settings) -> OAuth2AuthorizationCodeBearer:
    """Build the OAuth2 scheme used as a FastAPI dependency."""

    base = (settings.keycloak_public_url or "").rstrip("/")
    realm = settings.keycloak_realm

    #   Send browser to Keycloak's login page for this realm when authorize gets called.
    return OAuth2AuthorizationCodeBearer(
        authorizationUrl=f"{base}/realms/{realm}/protocol/openid-connect/auth",
        #   where code is exchanged for token
        tokenUrl=f"{base}/realms/{realm}/protocol/openid-connect/token",
    )


# Module-level scheme is fine: it is read once via Depends(get_settings) and
# its URLs only feed OpenAPI metadata, not the actual verification flow.
def get_oauth_scheme() -> OAuth2AuthorizationCodeBearer:
    """Return the OAuth scheme bound to current settings."""
    
    return build_oauth_scheme(get_settings())


_oauth2_scheme = get_oauth_scheme()


def fetch_jwks(settings: Settings) -> dict[str, Any]:
    """Fetch and cache the JWKS for the configured realm.

    The cache is invalidated by ``Settings.keycloak_jwks_cache_seconds``.

    Raises:
        HTTPException: ``503`` when Keycloak is unreachable.
    """
    now = time.monotonic()
    if _JWKS_CACHE["keys"] is not None and now < _JWKS_CACHE["expires_at"]:
        return _JWKS_CACHE["keys"]

    base = (settings.keycloak_url or "").rstrip("/")
    url = f"{base}/realms/{settings.keycloak_realm}/protocol/openid-connect/certs"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("JWKS fetch failed: %s", exc)
        raise HTTPException(status_code=503, detail="Auth backend unavailable") from exc

    keys = response.json()
    _JWKS_CACHE["keys"] = keys
    _JWKS_CACHE["expires_at"] = now + settings.keycloak_jwks_cache_seconds
    return keys


def verify_token(
    token: str = Depends(get_oauth_scheme()),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify an incoming JWT and return its payload.

    Args:
        token: Bearer token supplied by ``Authorization`` header.
        settings: Application settings.

    Returns:
        The decoded JWT claims.

    Raises:
        HTTPException: ``401`` on invalid signature, issuer, or client.
    """
    try:
        jwks = fetch_jwks(settings)
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            raise HTTPException(status_code=401, detail="Signing key not found")

        public_base = (settings.keycloak_public_url or "").rstrip("/")
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=f"{public_base}/realms/{settings.keycloak_realm}",
            options={"verify_aud": False},
        )
        if payload.get("azp") not in settings.keycloak_allowed_clients:
            logger.warning("Rejected token from unknown client azp=%r", payload.get("azp"))
            raise HTTPException(status_code=401, detail="Invalid client")
        return payload

    except JWTError as exc:
        # Avoid leaking the underlying jose error details to clients.
        logger.warning("Invalid JWT: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token") from exc

        