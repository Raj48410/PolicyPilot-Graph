"""Short-lived basic Graph chat-message subscriptions."""
import logging
import asyncio
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from config import Settings, settings
from services.graph_service import GraphError, GraphService
from services.subscription_store import SubscriptionStore
logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(self, graph: GraphService, config: Settings = settings) -> None:
        self.graph, self.config = graph, config
        self.store = SubscriptionStore(config)

    async def load_subscription(self) -> dict | None:
        """Load metadata for the same tenant, application, URL and client state."""
        saved = await asyncio.to_thread(self.store.load)
        return saved if saved else await self.find_subscription()

    async def find_subscription(self) -> dict | None:
        """Recover a lost local record without taking over another webhook."""
        path = "/subscriptions"
        visited = set()
        while path:
            if path in visited:
                raise RuntimeError("Subscription pagination repeated a page")
            visited.add(path)
            page = await self.graph.request("GET", path)
            for item in page.get("value", []):
                if item.get("resource", "").strip("/").lower() != "chats/getallmessages":
                    continue
                if item.get("applicationId") != self.config.client_id:
                    continue
                expires = datetime.fromisoformat(item["expirationDateTime"].replace("Z", "+00:00"))
                if expires <= datetime.now(timezone.utc):
                    continue
                state = item.get("clientState")
                if (item.get("notificationUrl") != self.config.notification_url
                        or not isinstance(state, str)
                        or not hmac.compare_digest(state.encode(), self.config.client_state.encode())
                        or item.get("changeType") != "created"
                        or item.get("includeResourceData", False)):
                    raise RuntimeError(
                        "An existing chats/getAllMessages subscription occupies this app's quota, "
                        "but its webhook configuration differs or cannot be verified. "
                        "Stop the old instance and wait until its subscription expires, or explicitly "
                        "delete the old subscription using GET /subscriptions then DELETE /subscriptions/{id}. "
                        "No existing subscription was changed.")
                await self._save(item)
                logger.info("Recovered existing Graph subscription from remote metadata")
                return item
            path = page.get("@odata.nextLink")
        return None

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
        try:
            result = await self.graph.request("POST", "/subscriptions", payload={
                "changeType": "created", "notificationUrl": self.config.notification_url,
                "resource": "/chats/getAllMessages", "expirationDateTime": self._expiration(),
                "clientState": self.config.client_state, "includeResourceData": False})
        except GraphError as exc:
            # A competing process may have created it after startup discovery.
            if exc.status not in {403, 409}:
                raise
            existing = await self.find_subscription()
            if existing is None:
                raise
            return await self.renew_subscription(existing["id"])
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
