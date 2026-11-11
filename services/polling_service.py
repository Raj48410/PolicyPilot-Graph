"""Local Graph polling without inbound networking or Graph subscriptions."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from config import Settings, settings
from services.graph_service import GraphError, GraphService
from services.message_processor import MessageProcessor

logger = logging.getLogger(__name__)


class PollingService:
    """Poll new messages, keeping successful per-chat cursors in memory.

    A restart intentionally starts from the new startup time, avoiding historical
    replies. Cursors advance only after successful fetching and processing.
    """

    def __init__(self, graph: GraphService, processor: MessageProcessor, config: Settings = settings) -> None:
        self.graph, self.processor, self.config = graph, processor, config
        self.started_at = datetime.now(timezone.utc)
        self._cursors: dict[str, datetime] = {}
        self.ready = False
        self.last_success: str | None = None
        self.last_error: str | None = None

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        """Only expose sanitized status/type, never response bodies or tokens."""
        return f"Graph HTTP {exc.status}" if isinstance(exc, GraphError) else type(exc).__name__

    async def _chat_ids(self) -> list[str]:
        if self.config.poll_chat_ids:
            return list(dict.fromkeys(self.config.poll_chat_ids))
        path = f"/users/{quote(self.config.sender_user_id, safe='')}/chats?$top=50"
        result: list[str] = []
        visited: set[str] = set()
        while path:
            if path in visited:
                raise RuntimeError("Chat pagination repeated a page")
            visited.add(path)
            page = await self.graph.request("GET", path)
            result.extend(chat["id"] for chat in page.get("value", [])
                          if chat.get("chatType") == "oneOnOne" and chat.get("id"))
            path = page.get("@odata.nextLink", "")
        return list(dict.fromkeys(result))

    async def _messages_since(self, chat_id: str, since: datetime) -> list[dict]:
        path = f"/chats/{quote(chat_id, safe='')}/messages?$top=50&$orderby=createdDateTime%20desc"
        messages: dict[str, dict] = {}
        visited: set[str] = set()
        while path:
            if path in visited or len(visited) >= 100:
                raise RuntimeError("Message pagination repeated or exceeded 100 pages; cursor not advanced")
            visited.add(path)
            page = await self.graph.request("GET", path)
            reached_old = False
            for message in page.get("value", []):
                try:
                    created = datetime.fromisoformat(message["createdDateTime"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        raise ValueError("Missing timestamp timezone")
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise RuntimeError("Graph message timestamp is invalid; cursor not advanced") from None
                if created < since:
                    reached_old = True
                elif message.get("id"):
                    messages[message["id"]] = message
            if reached_old:
                break
            path = page.get("@odata.nextLink", "")
        return sorted(messages.values(), key=lambda item: datetime.fromisoformat(
            item["createdDateTime"].replace("Z", "+00:00")))

    async def poll_once(self) -> None:
        """Fetch all pages before processing each chat; isolate failures by chat."""
        cycle_started = datetime.now(timezone.utc)
        chats = await self._chat_ids()
        failed = False
        for chat_id in chats:
            try:
                since = max(self.started_at, self._cursors.get(chat_id, self.started_at) - timedelta(seconds=30))
                messages = await self._messages_since(chat_id, since)
                for message in messages:
                    await self.processor.process_chat_message(chat_id, message)
                self._cursors[chat_id] = cycle_started
            except Exception as exc:
                failed = True
                self.last_error = self._error_summary(exc)
                logger.error("Chat polling failed (%s); cursor retained for retry", self.last_error)
                if isinstance(exc, GraphError) and exc.status == 429:
                    raise
        self.ready = not failed
        if self.ready:
            self.last_error = None
            self.last_success = datetime.now(timezone.utc).isoformat()
        logger.info("Polling cycle complete: %d chats, ready=%s", len(chats), self.ready)

    async def run(self) -> None:
        self.config.validate_polling()
        logger.info("Local polling started; only messages created after startup will be processed")
        while True:
            delay = self.config.poll_interval_seconds
            try:
                await self.poll_once()
            except Exception as exc:
                self.ready = False
                self.last_error = self._error_summary(exc)
                logger.error("Polling failed (%s); retrying after the configured interval", self.last_error)
                if isinstance(exc, GraphError):
                    delay = max(delay, exc.retry_after or 0)
            await asyncio.sleep(delay)
