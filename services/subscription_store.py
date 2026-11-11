"""Single-process local subscription metadata, excluding credentials/client state."""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from config import Settings

logger = logging.getLogger(__name__)


class SubscriptionStore:
    def __init__(self, config: Settings) -> None:
        self.path = Path(config.subscription_file) if config.subscription_file else None
        binding = [config.tenant_id, config.client_id, config.notification_url, config.client_state]
        self.fingerprint = hashlib.sha256(json.dumps(binding).encode()).hexdigest()

    def load(self) -> dict | None:
        """Ignore stale configuration bindings and malformed or missing metadata."""
        if self.path is None:
            return None
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("fingerprint") != self.fingerprint:
                return None
            subscription = saved["subscription"]
            if not isinstance(subscription["id"], str) or not subscription["id"]:
                return None
            expires = datetime.fromisoformat(subscription["expirationDateTime"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                return None
            return subscription
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Saved subscription metadata could not be loaded")
            return None

    def save(self, subscription: dict) -> None:
        if self.path is None:
            return
        metadata = {"id": subscription["id"], "expirationDateTime": subscription["expirationDateTime"]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps({"fingerprint": self.fingerprint, "subscription": metadata}), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)
