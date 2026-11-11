"""Short-lived basic Graph chat-message subscriptions."""
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from config import Settings, settings
from services.graph_service import GraphService
from services.subscription_store import SubscriptionStore
logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(self, graph: GraphService, config: Settings = settings) -> None:
        self.graph, self.config = graph, config
        self.store = SubscriptionStore(config)

    async def load_subscription(self) -> dict | None:
        """Load metadata for the same tenant, application, URL and client state."""
        return await asyncio.to_thread(self.store.load)

    async def _save(self, subscription: dict) -> None:
        try:
            await asyncio.to_thread(self.store.save, subscription)
        except OSError:
            # Keep the successful remote result in memory. Retrying creation
            # after a local disk error would leak duplicate subscriptions.
            logger.error("Subscription is active but local metadata could not be saved")

    def _expiration(self) -> str:
        self.config.validate_graph()
        return (datetime.now(timezone.utc) + timedelta(minutes=self.config.subscription_minutes)).isoformat()

    async def create_subscription(self) -> dict:
        result = await self.graph.request("POST", "/subscriptions", payload={
            "changeType": "created", "notificationUrl": self.config.notification_url,
            "resource": "/chats/getAllMessages", "expirationDateTime": self._expiration(),
            "clientState": self.config.client_state, "includeResourceData": False})
        await self._save(result)
        logger.info("Created Graph subscription")
        return result

    async def renew_subscription(self, subscription_id: str) -> dict:
        result = await self.graph.request("PATCH", f"/subscriptions/{quote(subscription_id, safe='')}",
                                          payload={"expirationDateTime": self._expiration()})
        # Some test/proxy implementations omit the ID on PATCH responses.
        result.setdefault("id", subscription_id)
        await self._save(result)
        logger.info("Renewed Graph subscription")
        return result

    async def delete_subscription(self, subscription_id: str) -> None:
        await self.graph.request("DELETE", f"/subscriptions/{quote(subscription_id, safe='')}")
        await asyncio.to_thread(self.store.clear)
        logger.info("Deleted Graph subscription")
