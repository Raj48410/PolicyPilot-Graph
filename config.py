"""Environment configuration; credentials are never logged."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

@dataclass(frozen=True)
class Settings:
    tenant_id: str = field(default_factory=lambda: os.getenv("TENANT_ID", ""))
    client_id: str = field(default_factory=lambda: os.getenv("CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("CLIENT_SECRET", ""), repr=False)
    notification_url: str = field(default_factory=lambda: os.getenv("GRAPH_NOTIFICATION_URL", ""))
    client_state: str = field(default_factory=lambda: os.getenv("GRAPH_CLIENT_STATE", ""), repr=False)
    subscription_minutes: int = field(default_factory=lambda: int(os.getenv("GRAPH_SUBSCRIPTION_MINUTES", "45")))
    auto_subscribe: bool = field(default_factory=lambda: os.getenv("GRAPH_AUTO_SUBSCRIBE", "true").lower() == "true")
    subscription_file: str = field(default_factory=lambda: os.getenv("GRAPH_SUBSCRIPTION_FILE", "data/subscription.json"))
    receive_mode: str = field(default_factory=lambda: os.getenv("GRAPH_RECEIVE_MODE", "webhook").lower())
    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("GRAPH_POLL_INTERVAL_SECONDS", "10")))
    poll_chat_ids: tuple[str, ...] = field(default_factory=lambda: tuple(
        item.strip() for item in os.getenv("GRAPH_POLL_CHAT_IDS", "").split(",") if item.strip()))
    delegated_token: str = field(default_factory=lambda: os.getenv("GRAPH_DELEGATED_ACCESS_TOKEN", ""), repr=False)
    graph_diagnostics: bool = field(default_factory=lambda: os.getenv("GRAPH_DIAGNOSTICS", "false").lower() == "true")
    sender_user_id: str = field(default_factory=lambda: os.getenv("GRAPH_SENDER_USER_ID", ""))
    azure_openai_endpoint: str = field(default_factory=lambda: os.getenv("AZURE_OPENAI_ENDPOINT", ""))
    azure_openai_key: str = field(default_factory=lambda: os.getenv("AZURE_OPENAI_KEY", ""), repr=False)
    azure_openai_deployment: str = field(default_factory=lambda: os.getenv("AZURE_OPENAI_DEPLOYMENT", ""))
    azure_openai_api_version: str = field(default_factory=lambda: os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))

    @property
    def ai_configured(self) -> bool:
        return all((self.azure_openai_endpoint, self.azure_openai_key, self.azure_openai_deployment))

    def validate_graph(self) -> None:
        required = {"TENANT_ID": self.tenant_id, "CLIENT_ID": self.client_id,
                    "CLIENT_SECRET": self.client_secret, "GRAPH_CLIENT_STATE": self.client_state,
                    "GRAPH_NOTIFICATION_URL": self.notification_url}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing configuration: {', '.join(missing)}")
        webhook = urlsplit(self.notification_url)
        hostname = (webhook.hostname or "").lower()
        if any(marker in hostname for marker in ("your-", "your_", "example", "<", ">")):
            raise ValueError("Replace the placeholder GRAPH_NOTIFICATION_URL with your actual public app URL")
        if webhook.scheme != "https" or not webhook.hostname or webhook.username or webhook.password:
            raise ValueError("GRAPH_NOTIFICATION_URL must be public HTTPS")
        if webhook.hostname in {"graph.microsoft.com", "localhost", "127.0.0.1", "0.0.0.0"}:
            raise ValueError("GRAPH_NOTIFICATION_URL must point to your public app, not Graph or localhost")
        if webhook.path != "/graph/webhook" or webhook.query or webhook.fragment:
            raise ValueError("GRAPH_NOTIFICATION_URL must end with /graph/webhook without a query or fragment")
        if not 15 <= self.subscription_minutes <= 60:
            raise ValueError("GRAPH_SUBSCRIPTION_MINUTES must be between 15 and 60")

    def validate_polling(self) -> None:
        """Local polling needs Graph credentials and a reply user, not a public URL."""
        required = {"TENANT_ID": self.tenant_id, "CLIENT_ID": self.client_id,
                    "CLIENT_SECRET": self.client_secret, "GRAPH_SENDER_USER_ID": self.sender_user_id,
                    "GRAPH_DELEGATED_ACCESS_TOKEN": self.delegated_token}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing polling configuration: {', '.join(missing)}")
        if not 5 <= self.poll_interval_seconds <= 3600:
            raise ValueError("GRAPH_POLL_INTERVAL_SECONDS must be between 5 and 3600")

settings = Settings()
TENANT_ID = settings.tenant_id
CLIENT_ID = settings.client_id
CLIENT_SECRET = settings.client_secret
GRAPH_NOTIFICATION_URL = settings.notification_url
GRAPH_CLIENT_STATE = settings.client_state
AZURE_OPENAI_ENDPOINT = settings.azure_openai_endpoint
AZURE_OPENAI_KEY = settings.azure_openai_key
AZURE_OPENAI_DEPLOYMENT = settings.azure_openai_deployment
