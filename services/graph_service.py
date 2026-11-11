"""MSAL client credentials and asynchronous requests REST transport."""
import asyncio
import logging
import threading
import re
import base64
import binascii
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
import requests
from msal import ConfidentialClientApplication
from config import Settings, settings

logger = logging.getLogger(__name__)
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_AUDIENCES = {"https://graph.microsoft.com", "https://graph.microsoft.com/",
                   "00000003-0000-0000-c000-000000000000"}


class GraphTokenError(ValueError):
    """A specific local token diagnostic; never includes the token itself."""


class GraphAuthenticationError(RuntimeError):
    """Allowlisted identity-provider diagnostics safe for operational logs."""

    def __init__(self, result: dict) -> None:
        error = result.get("error")
        error = error if error in {"invalid_client", "invalid_scope", "unauthorized_client",
                                  "invalid_grant", "temporarily_unavailable", "server_error"} else "unknown"
        codes = result.get("error_codes", [])
        codes = [str(code) for code in codes[:10]
                 if type(code) is int and 0 <= code <= 99999999] if isinstance(codes, list) else []
        detail = f"; AADSTS={','.join(codes)}" if codes else ""
        super().__init__(f"Graph authentication failed (error={error}{detail}); "
                         "check TENANT_ID, CLIENT_ID, CLIENT_SECRET and application consent")


class WrongAudienceError(GraphTokenError):
    """A readable token targets another API rather than Microsoft Graph."""


class MissingGraphPermissionError(GraphTokenError):
    """The delegated reply token lacks the required sending scope."""


def normalize_graph_token(token: str) -> str:
    """Accept a pasted Authorization value without emitting a double Bearer prefix."""
    if not isinstance(token, str):
        raise GraphTokenError("ReplyTokenFormat: access token must be a string")
    normalized = token.strip()
    if normalized[:7].lower() == "bearer ":
        normalized = normalized[7:].strip()
    if not normalized or normalized.lower() == "bearer" or any(c.isspace() for c in normalized):
        raise GraphTokenError("ReplyTokenFormat: access token is empty or contains whitespace")
    return normalized


def decode_graph_token(token: str) -> dict[str, Any]:
    """Decode a JWT payload for diagnostics, WITHOUT verifying its signature.

    This is not an authentication/authorization boundary. Graph validates the
    signature, revocation, policies and effective access. Microsoft can also
    issue opaque/encrypted access tokens, which cannot be decoded here.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Not a JWT")
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
        if not isinstance(claims, dict):
            raise ValueError("Payload is not an object")
        return claims
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise GraphTokenError("Token payload cannot be decoded locally; token may be opaque or malformed") from None


def graph_token_diagnostics(token: str) -> dict[str, Any]:
    """Return only diagnostic claims; never log a complete token or JWT payload."""
    try:
        claims = decode_graph_token(token)
    except GraphTokenError:
        return {"decoded": False, "signature_verified": False}
    permissions = lambda items: [item for item in items if isinstance(item, str)
                                 and re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", item)] if isinstance(items, list) else []
    scopes = claims.get("scp", "")
    audience = claims.get("aud")
    client_id = claims.get("appid", claims.get("azp"))
    return {"decoded": True, "signature_verified": False,
            "aud": audience if isinstance(audience, str) and len(audience) < 250 else "invalid",
            "appid": client_id if isinstance(client_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}", client_id) else None,
            "roles": permissions(claims.get("roles", [])),
            "scp": permissions(scopes.split()) if isinstance(scopes, str) else [],
            "exp": claims.get("exp") if isinstance(claims.get("exp"), (int, float)) else None}


def inspect_graph_token(token: str) -> dict[str, Any]:
    """Log selected unverified JWT claims, never the bearer credential itself."""
    report = graph_token_diagnostics(normalize_graph_token(token))
    logger.info("Graph token claims (signature NOT verified): %s", json.dumps(report))
    return report


def validate_reply_token(token: str, *, tenant_id: str, sender_id: str) -> dict[str, Any]:
    """Fail early for locally observable mistakes; do not reject opaque tokens."""
    if not isinstance(token, str) or not token or token != token.strip() or any(c.isspace() for c in token):
        raise GraphTokenError("ReplyTokenFormat: supply the raw access token without Bearer prefix or whitespace")
    report = graph_token_diagnostics(token)
    if not report["decoded"]:
        return report  # The Graph API is authoritative for opaque/encrypted tokens.
    claims = decode_graph_token(token)
    if claims.get("aud") not in GRAPH_AUDIENCES:
        raise WrongAudienceError("ReplyTokenAudienceMismatch: token is not intended for Microsoft Graph; "
                                 "expected Graph URI or resource ID 00000003-0000-0000-c000-000000000000")
    expires = claims.get("exp")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
        raise GraphTokenError("ReplyTokenExpiryMissing: obtain a fresh delegated token with scripts.login")
    if expires <= time.time() + 60:
        raise GraphTokenError("ReplyTokenExpired: token expired or expires within 60 seconds; run scripts.login")
    not_before = claims.get("nbf")
    if isinstance(not_before, (int, float)) and not_before > time.time() + 60:
        raise GraphTokenError("ReplyTokenNotYetValid: check the system clock and token validity")
    if str(claims.get("tid", "")).lower() != tenant_id.lower():
        raise GraphTokenError("ReplyTokenTenantMismatch: sign in to the configured tenant")
    if str(claims.get("oid", "")).lower() != sender_id.lower():
        raise GraphTokenError("ReplyTokenSenderMismatch: restart after login or configure the matching sender ID")
    scopes = claims.get("scp", "")
    scopes = scopes.split() if isinstance(scopes, str) else []
    # The live Teams endpoint requires these scopes; Group.ReadWrite.All alone
    # returns 403 even though it appears in the documentation's permission table.
    if not {"ChatMessage.Send", "Chat.ReadWrite"}.intersection(scopes):
        raise MissingGraphPermissionError("MissingChatMessageSend: delegated token requires ChatMessage.Send or Chat.ReadWrite in scp; "
                              "application roles cannot authorize ordinary Teams replies. Grant consent and run scripts.login")
    return report


def redact_graph_response(text: str, *secrets: str) -> str:
    """Preserve the full error body except credential material that must not leak."""
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    return text

class GraphError(RuntimeError):
    def __init__(self, status: int, retry_after: float | None = None, *, code: str = "", hint: str = "",
                 response_body: str = "", endpoint: str = "", include_body: bool = False) -> None:
        self.status = status
        self.retry_after = retry_after
        self.code = code
        self.hint = hint
        self.response_body = response_body
        self.endpoint = endpoint
        details = f"; code={code}" if code else ""
        details += f"; hint={hint}" if hint else ""
        if include_body and response_body:
            details += f"; Response Body: {response_body}"
        super().__init__(f"Graph request failed (HTTP {status}{details})")

class GraphService:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        self._client: ConfidentialClientApplication | None = None
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        """Cache application tokens and reacquire five minutes before expiry.

        This cache serves app-authorized Graph operations. It cannot make an
        application token valid for ordinary Teams chat message sending.
        """
        with self._lock:
            now = time.time()
            if self._access_token is not None and now < self._expires_at - 300:
                return self._access_token
            if self._access_token is not None:
                # A new MSAL client avoids reusing an internal near-expiry token.
                self._client = None
            self._access_token, self._expires_at = None, 0.0
            if self._client is None:
                if not all((self.config.tenant_id, self.config.client_id, self.config.client_secret)):
                    raise ValueError("TENANT_ID, CLIENT_ID and CLIENT_SECRET are required")
                self._client = ConfidentialClientApplication(
                    self.config.client_id,
                    authority=f"https://login.microsoftonline.com/{self.config.tenant_id}",
                    client_credential=self.config.client_secret)
            result = self._client.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" not in result:
                raise GraphAuthenticationError(result)
            token = normalize_graph_token(result["access_token"])
            try:
                lifetime = float(result["expires_in"])
                if not math.isfinite(lifetime) or lifetime <= 0:
                    raise ValueError("Invalid expiry")
                expires_at = now + lifetime
            except (KeyError, TypeError, ValueError, OverflowError):
                claims = decode_graph_token(token)
                expiry = claims.get("exp")
                if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                    raise GraphTokenError("Application token has no usable expiry metadata") from None
                expires_at = float(expiry)
            if expires_at <= time.time():
                self._client = None
                raise GraphTokenError("Entra returned an already expired application token")
            self._access_token, self._expires_at = token, expires_at
        logger.info("Graph application token acquired")
        if self.config.graph_diagnostics:
            inspect_graph_token(token)
        return token

    def get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token()}", "Content-Type": "application/json"}

    def invalidate_application_token(self) -> None:
        """Discard the in-memory MSAL client/cache after a rejected app token."""
        with self._lock:
            self._client = None
            self._access_token, self._expires_at = None, 0.0

    async def get(self, path: str) -> dict[str, Any]:
        """Read a Graph resource using application authentication."""
        return await self.request("GET", path)

    async def post(self, path: str, payload: dict, *, access_token: str | None = None) -> dict[str, Any]:
        """Create a resource, optionally using a delegated identity."""
        return await self.request("POST", path, payload=payload, access_token=access_token)

    async def patch(self, path: str, payload: dict) -> dict[str, Any]:
        """Update a Graph resource using application authentication."""
        return await self.request("PATCH", path, payload=payload)

    async def request(self, method: str, path: str, *, payload: dict | None = None,
                      access_token: str | None = None) -> dict[str, Any]:
        if access_token is not None and (not isinstance(access_token, str) or not access_token.strip()):
            raise GraphTokenError("An explicitly supplied delegated token is empty; application-token fallback is forbidden")
        if access_token is not None:
            access_token = normalize_graph_token(access_token)
        url = path if path.startswith("https://") else f"{GRAPH_BASE_URL}/{path.lstrip('/')}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise ValueError("Refusing to send Graph credentials to an untrusted URL")
        def send() -> dict[str, Any]:
            headers = ({"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                       if access_token is not None else self.get_headers())
            try:
                response = requests.request(method, url, headers=headers, json=payload, timeout=(5, 30))
            except requests.RequestException:
                raise GraphError(0) from None
            if self.config.graph_diagnostics:
                logger.info("Graph HTTP response: method=%s endpoint=%s status=%s", method.upper(), url, response.status_code)
            if not response.ok:
                retry_after = None
                raw = response.headers.get("Retry-After")
                if isinstance(raw, str):
                    try:
                        retry_after = max(0.0, float(raw))
                    except ValueError:
                        try:
                            retry_after = max(0.0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                code, hint = "", ""
                try:
                    body = response.json()
                    error = body.get("error", {}) if isinstance(body, dict) else {}
                    if isinstance(error, dict):
                        candidate = error.get("code")
                        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", candidate):
                            code = candidate
                        message = error.get("message", "")
                        message = message.lower() if isinstance(message, str) else ""
                        # Map response text to fixed hints instead of exposing raw
                        # URLs, validation tokens, request bodies or user details.
                        if response.status_code == 401 and ("expired" in message or "lifetime" in message):
                            hint = "token_expired_or_outside_validity_window_renew_matching_token"
                        elif response.status_code == 401 and "audience" in message:
                            hint = "token_audience_mismatch_request_a_microsoft_graph_token"
                        elif response.status_code == 401:
                            hint = "token_rejected_review_response_body_and_renew_matching_token"
                        elif "validation" in message and any(word in message for word in ("notification", "endpoint", "url", "token")):
                            hint = "webhook_validation_failed_check_tunnel_port_and_anonymous_access"
                        elif "missing scope permissions" in message and access_token is not None:
                            hint = "check_delegated_scopes_and_consent_then_run_scripts.login"
                        elif "permission" in message or "access denied" in message:
                            hint = "check_application_permissions_and_admin_consent"
                        elif "expiration" in message or "lifecyclenotificationurl" in message:
                            hint = "check_subscription_expiration_and_lifecycle_settings"
                except ValueError:
                    pass
                raw_body = response.text if isinstance(response.text, str) else ""
                redacted_body = redact_graph_response(raw_body, headers.get("Authorization", "").removeprefix("Bearer "),
                    self.config.client_secret, self.config.azure_openai_key, self.config.client_state)
                if self.config.graph_diagnostics:
                    logger.error("Graph request failed: endpoint=%s status=%s code=%s response_body=%s",
                                 url, response.status_code, code, redacted_body)
                raise GraphError(response.status_code, retry_after, code=code, hint=hint,
                                 response_body=redacted_body, endpoint=url, include_body=self.config.graph_diagnostics)
            logger.debug("Graph %s completed: HTTP %s", method, response.status_code)
            if self.config.graph_diagnostics and method.upper() == "POST" and re.fullmatch(r"/v1.0/chats/[^/]+/messages", parsed.path):
                raw_body = response.text if isinstance(response.text, str) else ""
                logger.info("Teams send response: endpoint=%s status=%s response_body=%s", url,
                    response.status_code, redact_graph_response(raw_body,
                        headers.get("Authorization", "").removeprefix("Bearer "),
                        self.config.client_secret, self.config.azure_openai_key, self.config.client_state))
            return response.json() if response.content else {}
        # GET/PATCH/DELETE can be replayed here; subscription/message POSTs must
        # not be replayed after ambiguous failures that could create duplicates.
        retryable_method = method.upper() in {"GET", "PATCH", "DELETE"}
        for attempt in range(4):
            try:
                return await asyncio.to_thread(send)
            except GraphError as exc:
                if exc.status == 401 and access_token is None and retryable_method and attempt == 0:
                    await asyncio.to_thread(self.invalidate_application_token)
                    logger.warning("Discarded rejected application token; acquiring a fresh token for one safe retry")
                    continue
                if not retryable_method or attempt == 3 or exc.status not in {0, 429, 502, 503, 504}:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else float(2 ** attempt)
                # Do not retry earlier than Graph asks. Leave long cooldowns to
                # the caller rather than blocking a request for many minutes.
                if delay > 60:
                    raise
                logger.warning("Retrying Graph %s after HTTP %s (attempt %s)", method.upper(), exc.status, attempt + 1)
                await asyncio.sleep(delay)
        raise RuntimeError("Graph retry loop exhausted")
