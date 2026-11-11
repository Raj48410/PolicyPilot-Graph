# PolicyPilot Graph Edition

Replies validate the delegated sender against the complete chat roster before
sending. A non-member raises `SenderNotInChatError`; roster lookup failures also
block sending. Roster reads use application `Chat.Read.All` (or
`ChatMember.Read.All`), while message sends use the delegated user's token.
Startup verifies the reply account with Graph `/me`; `/health` reports
`reply_account_ready` for that identity check.

Inspect a chat locally without sending messages:

```powershell
.venv\Scripts\python.exe -m scripts.diagnose_chat "<chat-id>"
```

This prints the reply account, actual member object IDs, display names, UPNs,
membership result and required fix. See `TEAMS_REPLY_MEMBERSHIP_REPORT.txt` for
the verified failing chat. Use a conversation containing the reply account;
self-authored messages from that account are ignored to prevent reply loops.

Teams conversations through Microsoft Graph REST, FastAPI and Microsoft Dev Tunnel.
Python 3.12, MSAL, requests and the async Azure OpenAI SDK. No Azure Bot Service,
Bot Framework, Teams activity handlers or Copilot Studio runtime.

## 1. Entra application registration

Use the existing single-tenant registration, or create one in Entra > App
registrations > New registration. Copy its Directory (tenant) ID and Application
(client) ID. Create a client secret and keep its value in the ignored local `.env`.
Never commit credentials or tokens.

## 2. Graph permissions and authentication

Configure Microsoft Graph permissions on the registration:

| Operation | Permission type | Permission |
| --- | --- | --- |
| Read tenant chat messages / subscribe | Application | Chat.Read.All |
| Read profiles | Application | User.Read.All |
| Read group membership | Application | GroupMember.Read.All |
| Create a chat (optional helper) | Application | Chat.Create |
| Send ordinary Teams chat replies | **Delegated** | ChatMessage.Send |
| Verify delegated sender via /me | Delegated | User.Read |
| Manager lookup (optional helper) | Delegated | User.Read.All |

The administrator grants consent under App registrations > your app > API
permissions > Grant admin consent. Verify the status is Granted for the tenant.
Application permissions must be present in the app token's roles; delegated
consent does not add application permissions.

**The requested application-only ChatMessage.Send flow is not supported.**
Microsoft Graph supports ordinary chat sending on behalf of a signed-in user;
application-only message creation is restricted to migration. The implementation
therefore uses MSAL ConfidentialClientApplication for reads/subscriptions, and a
separate delegated token for replies. Replies appear as that user, who must be a
member of the chat. It does not create a PolicyPilot bot contact.

References: [send message](https://learn.microsoft.com/en-us/graph/api/chatmessage-post),
[list chats](https://learn.microsoft.com/en-us/graph/api/chat-list),
[manager lookup](https://learn.microsoft.com/en-us/graph/api/user-list-manager).
SharePoint and mail permissions are not needed until those integrations are built.
The project does not send emails or create tickets.

## 3. Install Python dependencies and configure secrets

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Fill TENANT_ID, CLIENT_ID, CLIENT_SECRET and a long random GRAPH_CLIENT_STATE.
Azure generation requires AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY and the exact
AZURE_OPENAI_DEPLOYMENT name. AZURE_OPENAI_API_VERSION defaults to 2024-10-21;
choose a version supported by your deployment. No model calls are made by tests.

For development delegated login, add ChatMessage.Send and User.Read, obtain
consent, and enable Authentication > Allow public client flows on the registration
if your tenant permits device login. Run:

```powershell
.venv\Scripts\python.exe -m scripts.login
```

Sign in as the Teams reply user. The helper verifies /me and saves a short-lived
GRAPH_DELEGATED_ACCESS_TOKEN and matching GRAPH_SENDER_USER_ID without printing
secrets. Rerun when the token expires. Production needs an approved delegated
flow and a secure renewing token provider. Manager lookup needs separate delegated
User.Read.All consent; the development login requests only sending/profile scopes.

## 4. Install and host Microsoft Dev Tunnel

```powershell
winget install Microsoft.devtunnel
# Open a new terminal after installation:
devtunnel user login
devtunnel host -p 8000 --allow-anonymous
```

Keep the tunnel terminal running. The base command is `devtunnel host -p 8000`;
Graph delivery additionally needs anonymous tunnel access because Graph cannot
complete the tunnel's interactive sign-in. Client-state and tenant validation
remain enforced by FastAPI for notification batches. Use an organization-approved
tunnel policy. This exposes the local app while the tunnel is running.

[Microsoft setup](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/get-started)
and [tunnel access controls](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/security).

## 5. Set the actual forwarding URL

Copy the exact HTTPS forwarding URL printed for port 8000, append `/graph/webhook`,
and set `.env`:

```dotenv
GRAPH_RECEIVE_MODE=webhook
GRAPH_AUTO_SUBSCRIBE=true
GRAPH_NOTIFICATION_URL=https://YOUR-ACTUAL-TUNNEL.devtunnels.ms/graph/webhook
GRAPH_SUBSCRIPTION_MINUTES=45
GRAPH_SUBSCRIPTION_FILE=data/subscription.json
```

The hostname is illustrative. Do not use graph.microsoft.com, localhost, or the
example hostname. Update the URL if the tunnel changes. Existing credentials in
`.env` are preserved; `.env.example` contains no secrets. Polling remains a
commented fallback in both environment files and is inactive in webhook mode.

## 6. Run FastAPI

In a second terminal at the project root:

```powershell
.venv\Scripts\python.exe -m scripts.check_config
.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

Configuration checks validate format/presence only, not live consent, token
validity, tunnel accessibility or Azure connectivity. Restart after changing `.env`.
For production use one process without --reload for this local-storage scaffold.

## 7. Validate the webhook

Open the actual public URL followed by `/graph/webhook?validationToken=abc123`.
It must return exactly `abc123` as plain text with HTTP 200. Also test POST:

```powershell
Invoke-WebRequest -Method POST -Uri "$($env:POLICYPILOT_TEST_URL)/graph/webhook?validationToken=abc123"
```

Set POLICYPILOT_TEST_URL to your real tunnel base URL in that terminal first.
GET and POST validation are implemented; Microsoft Graph uses POST for validation.
The tunnel must forward directly to FastAPI without an interactive access page.

## 8. Automatic subscriptions

Startup schedules subscription creation after the server can receive validation.
The resource is `/chats/getAllMessages`, with `created` events and no encrypted
resource data. Tenant-wide subscriptions can deliver messages from all tenant chats.
Graph validates the public URL while creation is in flight. There is no need to
POST a second subscription manually.

The subscription is renewed five minutes before expiration. ID and expiration are
saved atomically to data/subscription.json with a configuration fingerprint, never
raw secrets/client state. Matching metadata is reused on restart; expired/missing
records are recreated. Expiration is configurable from 15 to 60 minutes, avoiding
the lifecycle endpoint requirement for longer Teams subscriptions.

Check http://localhost:8000/health for status healthy, receive_mode webhook and
subscription_ready true. The health route shows configuration modes separately
from process health; ai_mode azure_openai does not prove a live AI call succeeded.
Graph 403 generally needs a review of permission type, admin consent and the tenant.

## 9. Test Teams chat

After subscription readiness, send a new message from another account in a chat
containing the delegated reply user. The processor retrieves the message, ignores
self/application/system messages, resolves direct group memberships and a role,
retrieves context, calls Azure OpenAI and posts plain text back to the same chat.

Role precedence: Global Admin (or legacy GlobalAdmin) > HR Team > Finance Team >
IT Team > Employee. These are demo group names, not detection of directory roles.
Production authorization should bind immutable group IDs and filter documents before
retrieval. RAG currently returns **mock context**. SharePoint loading, FAISS/vector
search, embeddings, ticketing and Outlook are extension points, not live features.
The model is instructed not to invent policy details from mock data.

`adaptive_card_payload` prepares Graph attachment payloads separately from plain
text replies. Card submission callbacks and interactive card conversations are not
implemented. The default processor continues to send plain text.

## Reliability and verification

For reply authentication diagnostics, run
`.venv\Scripts\python.exe -m scripts.diagnose_reply_token`. It inspects audience,
roles, delegated scopes and expiration without printing the token or contacting
Microsoft. Decoding is not signature validation; Graph remains authoritative.
See [the reply diagnostic report](GRAPH_REPLY_DIAGNOSTICS.md) for the corrected
double-Bearer-header issue and the outstanding delegated consent requirement.

`GRAPH_DIAGNOSTICS=true` temporarily logs the reply endpoint, chat/sender IDs,
selected token claims and full Graph error bodies with credential redaction.
Set it false after troubleshooting. Detailed diagnostic mode intentionally
includes identifiers and error metadata, so restrict access to the local logs.

Graph GET/PATCH/DELETE requests have bounded retries for transport failures,
429 and transient gateway/server errors. Retry-After is honored; delays longer
than 60 seconds propagate to the caller without an early retry. POSTs are never
blindly replayed because a network failure can hide a successful message or
subscription creation. A caller retry may still require remote reconciliation.
Application tokens are cached with their expiration timestamp and renewed five
minutes early. A rejected application token clears both application and MSAL
in-memory caches before one safe retry; this does not enable application-only
Teams chat replies, which are not supported by the normal message-send API.

Application events are structured JSON in logs/policypilot.log, rotating at 5 MB
with three backups. Console logs remain available. Tokens, full Graph error bodies,
questions and model context are not logged by application services in normal mode.
Temporary Graph diagnostics include error bodies after credential redaction.

The notification queue and four-hour/10,000-message duplicate cache are in memory.
Accepted notifications can be lost on crashes, failed processing is logged for
replay but not automatically replayed, and multiple workers do not share state.
Production needs durable queueing, dead-letter handling, shared idempotency and a
single subscription manager. Changed configuration or ambiguous creation failures
can leave overlapping subscriptions requiring tenant-side reconciliation.

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests mock Microsoft and Azure calls. They do not publish a tunnel, send chat
messages, change consent or consume model credits.
