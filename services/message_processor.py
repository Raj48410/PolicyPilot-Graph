"""Notification orchestration and bounded, single-process duplicate suppression."""
import asyncio
import hmac
import logging
import time
from collections import OrderedDict
from html.parser import HTMLParser
from ai.openai_service import OpenAIService
from auth.rbac import resolve_user_role
from config import Settings, settings
from rag.rag import RAGService
from services.teams_service import TeamsService, resource_ids
from services.user_service import UserService
logger = logging.getLogger(__name__)

class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
    def handle_data(self, data: str) -> None:
        self.parts.append(data)

class MessageProcessor:
    def __init__(self, teams: TeamsService, users: UserService, rag: RAGService,
                 ai: OpenAIService, config: Settings = settings) -> None:
        self.teams, self.users, self.rag, self.ai, self.config = teams, users, rag, ai, config
        self._lock = asyncio.Lock()
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    async def process_notification(self, notification: dict) -> None:
        state = notification.get("clientState")
        if not isinstance(state, str) or not self.config.client_state or not hmac.compare_digest(
                state.encode(), self.config.client_state.encode()):
            raise ValueError("Invalid notification client state")
        if notification.get("tenantId") != self.config.tenant_id:
            raise ValueError("Unexpected notification tenant")
        ids = resource_ids(notification)
        if notification.get("changeType") != "created" or not ids:
            logger.debug("Ignoring unsupported notification")
            return
        await self._process_message(*ids)

    async def process_chat_message(self, chat_id: str, message: dict) -> None:
        """Process a message retrieved directly by the trusted Graph poller."""
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Graph message ID is required")
        await self._process_message(chat_id, message_id, message)

    async def _process_message(self, chat_id: str, message_id: str, message: dict | None = None) -> None:
        ids = (chat_id, message_id)
        async with self._lock:
            now = time.monotonic()
            while self._seen and next(iter(self._seen.values())) < now - 14400:
                self._seen.popitem(last=False)
            if ids in self._seen:
                logger.debug("Ignoring duplicate notification")
                return
            logger.info("Processing chat message notification")
            message = message if message is not None else await self.teams.get_message(*ids)
            sender = message.get("from") or {}
            user_id = (sender.get("user") or {}).get("id")
            if (sender.get("application") or not user_id or user_id == self.config.sender_user_id
                    or message.get("deletedDateTime") or message.get("messageType", "message") != "message"):
                logger.debug("Ignoring self, application, deleted or system message")
                return
            body = message.get("body") or {}
            question = body.get("content", "")
            if body.get("contentType") == "html":
                parser = _Text()
                parser.feed(question)
                question = " ".join(parser.parts)
            if not question.strip():
                return
            await self.users.get_user(user_id)
            groups = await self.users.get_user_groups(user_id)
            role = resolve_user_role(groups)
            logger.info("Resolved sender role: %s", role)
            context = await self.rag.answer(question, role)
            answer = await self.ai.generate_response(question, context)
            await self.teams.send_reply(ids[0], answer)
            # Evict only when inserting. Evicting before the duplicate check can
            # process the oldest cached message again whenever the cache is full.
            while len(self._seen) >= 10000:
                self._seen.popitem(last=False)
            self._seen[ids] = time.monotonic()
            logger.info("Teams response sent")
