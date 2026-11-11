"""Verify Entra v2 delegated access tokens issued for the PolicyPilot API."""
import asyncio
from uuid import UUID

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient


class CopilotAuthentication:
    def __init__(self, config):
        self.config = config
        self.keys = None

    async def authenticate(self, authorization: str) -> str:
        if not self.config.copilot_api_audience or not self.config.tenant_id:
            raise HTTPException(503, "Copilot API authentication is not configured.")
        if not authorization.startswith("Bearer ") or len(authorization) > 20000:
            raise HTTPException(401, "A delegated API access token is required.", headers={"WWW-Authenticate": "Bearer"})
        try:
            tenant = str(UUID(self.config.tenant_id))
            if self.keys is None:
                self.keys = PyJWKClient(
                    f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10)
            token = authorization[7:]
            key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"],
                audience=self.config.copilot_api_audience,
                issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
                options={"require": ["exp", "iat", "nbf", "aud", "iss", "tid", "oid", "scp"]})
            if claims["tid"] != tenant or claims.get("ver") != "2.0":
                raise jwt.InvalidTokenError()
            if self.config.copilot_api_scope not in claims["scp"].split():
                raise HTTPException(403, "The required delegated API scope is missing.")
            return str(UUID(claims["oid"]))
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
            raise HTTPException(401, "Invalid or expired API access token.",
                                headers={"WWW-Authenticate": "Bearer"}) from None
