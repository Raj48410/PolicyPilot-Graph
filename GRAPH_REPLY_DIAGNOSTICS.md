# Teams reply authentication diagnostic report

## Confirmed findings

The local `.env` contained `GRAPH_DELEGATED_ACCESS_TOKEN=Bearer <token>`.
`GraphService.request()` also prepended `Bearer ` when constructing the header.
The resulting `Authorization: Bearer Bearer <token>` is malformed and explains
the observed `401 InvalidAuthenticationToken`. The redundant prefix has been
removed from `.env`, and supplied tokens are now normalized before use so that
exactly one Bearer prefix is emitted. Actual tokens and secrets are not included
in this report.

A second independent blocker remains: the decoded current reply token has no
`ChatMessage.Send` delegated scope. Its Graph audience is
`00000003-0000-0000-c000-000000000000`, its sender matches configuration, and it
was not expired at the time of inspection. The `roles` array is empty, which is
normal for this delegated token; scopes live in `scp`. Decoding did not verify
the signature, revocation, tenant policy or Graph's effective authorization.

The application authentication path already correctly uses
`ConfidentialClientApplication.acquire_token_for_client()` with
`https://graph.microsoft.com/.default`. It is used for reads and subscriptions.
The reply path uses `GRAPH_DELEGATED_ACCESS_TOKEN` (or an injected provider), not
an Azure OpenAI token and not the client-credentials token. No fallback to an
application token is allowed when an explicitly supplied reply token is empty.

## Implemented corrections and diagnostics

- `normalize_graph_token()` strips one pasted Bearer prefix, rejects remaining
  whitespace, and produces a raw token for exactly one Authorization prefix.
- `decode_graph_token()` and `graph_token_diagnostics()` inspect only selected
  claims. Microsoft Graph's GUID and URI audience representations are accepted.
- Reply preflight detects expiry/near-expiry, future validity, wrong API audience,
  wrong tenant, mismatched sender and missing delegated `ChatMessage.Send`.
  Opaque/encrypted tokens are left to Graph for authoritative validation.
- `inspect_graph_token()` logs audience, appid (or azp), roles, scopes and expiry.
  Wrong audiences raise `WrongAudienceError`; missing sending scope raises
  `MissingGraphPermissionError`, both subclasses of `GraphTokenError`.
- If an application token is rejected with 401 on a safe read/update/delete,
  the in-memory MSAL cache is discarded and the operation gets one fresh-token
  attempt. Reply POSTs never substitute application credentials or blindly retry.
- Default local reply credentials are reread from `.env` so a renewed token for
  the same sender is not trapped in the startup configuration snapshot. A changed
  sender explicitly requires restart to keep self-message filtering correct.
- Diagnostic reports include endpoint, chat ID, configured sender, audience,
  roles, scopes, expiration, HTTP status and Graph error body. The error body is
  retained in `GraphError.response_body`; known credentials and bearer/JWT strings
  are redacted. Full response logging is gated by `GRAPH_DIAGNOSTICS=true`, which
  is temporarily enabled in the local `.env`. Set it false after troubleshooting.
  The diagnostic mode also logs status and body for successful reply responses.
- `Content-Type: application/json` and the plain-text Graph message body are
  preserved. Message POSTs are not blindly retried.

## Required sign-in step

### Application-only architecture constraint

The requested replacement with only TENANT_ID / CLIENT_ID / CLIENT_SECRET cannot
deliver ordinary Teams chat replies through this endpoint. Microsoft documents
ChatMessage.Send as delegated and the application permission as migration-only.
Consequently, delegated reply validation and the existing login helper were
retained; deleting them would not resolve this platform restriction. No
application-only reply implementation or successful live message is claimed.

The application-authentication path now explicitly caches `_access_token` and
`_expires_at`, refreshes five minutes before expiration and clears both this cache
and the in-memory MSAL client on invalidation. Safe operations receive at most one
fresh-token retry after a 401. This improves reads/subscriptions but does not
change the chat-send API's authorization model.

On the registration matching CLIENT_ID, grant/consent delegated Microsoft Graph
`ChatMessage.Send` and `User.Read`. Then run:

```powershell
.venv\Scripts\python.exe -m scripts.login
.venv\Scripts\python.exe -m scripts.diagnose_reply_token
```

The login command must finish with its saved-token message. A token missing the
sending scope is not saved by that helper. Restart FastAPI after login, keep the
tunnel running and send a new test message from another account. Failed prior
messages are not automatically replayed.

Ordinary `POST /chats/{chat-id}/messages` sending cannot be authorized by adding
`ChatMessage.Send` to application roles. Application-only message sending is
restricted to migration. The reply token does not need chat-reading or directory
application roles: those belong to the separate application token used for reads.

References: [Graph message sending](https://learn.microsoft.com/en-us/graph/api/chatmessage-post),
[Microsoft token claims](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference).

## Verification

74 offline tests cover this project, including a regression that supplies `Bearer <token>`
and verifies the HTTP header is exactly `Bearer <token>`, expired startup-token
replacement, scope/audience/tenant/sender checks, named exceptions, app-cache
invalidation, no application fallback,
Graph response-body diagnostics and secret redaction. No live Teams message was
sent during this investigation. Complete end-to-end success remains dependent
on obtaining the correctly scoped delegated token and testing it against Graph.
