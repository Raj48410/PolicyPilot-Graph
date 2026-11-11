# Connect Copilot to PolicyPilot

The app package now includes an API action. It is **not ready for upload** until
the OAuth vault registration and real developer URLs are configured.

## 1. Expose the backend API in Entra

Use a tenant-owned Entra API app registration. Under **Expose an API**, set its
Application ID URI and add a delegated scope named `access_as_user`. Set
`api.requestedAccessTokenVersion` to `2` in the Entra registration manifest.

In the backend's local `.env`, add:

```dotenv
COPILOT_API_AUDIENCE=<API application's client UUID>
COPILOT_API_SCOPE=access_as_user
```

The UUID is the API application's ID, which can differ from the existing Graph
client ID. For Entra v2 tokens, the audience is the API application's client ID.
`TENANT_ID` remains your tenant ID. Existing Graph application credentials and
permissions remain necessary for directory group lookup.

## 2. Register OAuth for the action

Follow Microsoft's [OAuth registration instructions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-oauth)
in Teams Developer Portal or Agents Toolkit. Use an OAuth client with delegated
permission to your API's scope, and consent according to your tenant policy.
For standard OAuth authorization-code registration, configure:

- Authorization URL: `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/authorize`
- Token URL: `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token`
- Scope: `<your API Application ID URI>/access_as_user`
- Web redirect URI on the OAuth client: `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`
- Associated Teams app ID: the `id` in `PolicyPilot-App/manifest.json`.

Keep any client secret in the OAuth registration vault, never in the app package.
Replace `OAUTH_REGISTRATION_ID_REQUIRED` in `PolicyPilot-App/api-plugin.json`
with the registration ID returned by Microsoft. This is not an Entra client ID.

## 3. Verify hosting and package

Set `servers[0].url` in `PolicyPilot-App/openapi.json` to your current public HTTPS
base URL. Keep the tab URL and `validDomains` in `manifest.json` aligned with that
host. Replace all `YOUR_POLICYPILOT_URL` developer URLs with actual support,
privacy, and terms pages. Install requirements and restart FastAPI after editing
environment settings.

Rebuild both ZIPs after configuring these files:

```powershell
Compress-Archive -Path .\PolicyPilot-App\manifest.json,.\PolicyPilot-App\declarativeAgent.json,.\PolicyPilot-App\api-plugin.json,.\PolicyPilot-App\openapi.json,.\PolicyPilot-App\color.png,.\PolicyPilot-App\outline.png -DestinationPath .\PolicyPilot.zip -Force
Copy-Item -LiteralPath .\PolicyPilot.zip -Destination .\PolicyPilot-App.zip -Force
```

Upload the complete ZIP as an agent/app update (version `1.0.3`), not as a skill.
Sign in when Copilot invokes the action. A successful policy question should call
`POST /api/copilot/answer` in the backend access log. Missing or invalid tokens
return 401; missing API scope returns 403; unset API audience returns 503.

Copilot presents the returned answer; this is still a declarative agent, not a
replacement for Copilot's conversation engine. Retrieval remains mock context,
and role mapping remains the existing demonstration based on group display names.
No ticket creation, Teams message sending, or SharePoint retrieval is added by
this action.
