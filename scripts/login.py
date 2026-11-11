"""Development delegated login: python -m scripts.login.

Application-only credentials remain separate. No tokens or client secrets are
printed. This short-lived access token is saved in the ignored local .env only;
there is no persisted refresh-token cache.
"""

from pathlib import Path
from uuid import UUID

import requests
from dotenv import set_key
from msal import PublicClientApplication

from config import Settings, settings

SCOPES = ["https://graph.microsoft.com/ChatMessage.Send", "https://graph.microsoft.com/User.Read"]
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def login(config: Settings = settings, env_path: Path = ENV_PATH) -> None:
    """Ask the user to sign in, verify the identity, and save development settings."""
    try:
        UUID(config.tenant_id)
        UUID(config.client_id)
    except ValueError:
        raise ValueError("Set valid TENANT_ID and CLIENT_ID in .env before login") from None
    client = PublicClientApplication(config.client_id,
        authority=f"https://login.microsoftonline.com/{config.tenant_id}")
    flow = client.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError("Cannot start device login. Enable public client flows in the Entra app registration")
    print(flow["message"])
    result = client.acquire_token_by_device_flow(flow)
    token = result.get("access_token")
    if not token:
        raise RuntimeError("Delegated login failed. Check tenant policies and consent for ChatMessage.Send and User.Read")
    # OAuth responses can use the fully qualified scopes requested above.
    # Accept only bare scopes or scopes belonging to Microsoft Graph.
    granted = {scope.lower().removeprefix("https://graph.microsoft.com/")
               for scope in result.get("scope", "").split()}
    if not {"chatmessage.send", "chat.readwrite"}.intersection(granted):
        raise RuntimeError("Login did not grant ChatMessage.Send; .env was not changed")
    claims = result.get("id_token_claims") or {}
    if claims.get("tid") != config.tenant_id:
        raise RuntimeError("Signed-in tenant does not match TENANT_ID; .env was not changed")
    try:
        response = requests.get("https://graph.microsoft.com/v1.0/me?$select=id",
            headers={"Authorization": f"Bearer {token}"}, timeout=(5, 30))
        response.raise_for_status()
        sender_id = response.json()["id"]
        UUID(sender_id)
    except (requests.RequestException, ValueError, KeyError):
        raise RuntimeError("Cannot verify the delegated sender with Graph /me; .env was not changed") from None
    set_key(str(env_path), "GRAPH_DELEGATED_ACCESS_TOKEN", token)
    set_key(str(env_path), "GRAPH_SENDER_USER_ID", sender_id)
    print("Saved delegated token and matching sender ID to .env. Restart the server.")
    print("This development token expires. Run this command again when needed.")


if __name__ == "__main__":
    try:
        login()
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except requests.RequestException:
        print("Login network request failed. Check connectivity and try again.")
        raise SystemExit(1) from None
