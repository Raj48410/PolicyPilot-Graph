"""Application reads and delegated Teams replies."""
import inspect
import re
import json
import asyncio
import logging
from pathlib import Path
from uuid import uuid4
from collections.abc import Awaitable, Callable
from urllib.parse import quote, unquote
from config import Settings, settings
from dotenv import dotenv_values
from services.graph_service import (GRAPH_BASE_URL, GraphError, GraphService, GraphTokenError,
                                    graph_token_diagnostics, normalize_graph_token, validate_reply_token)
from services.user_service import UserService

logger = logging.getLogger(__name__)
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

TokenProvider = Callable[[], str | Awaitable[str]]


class SenderNotInChatError(RuntimeError):
    """The configured delegated reply account cannot reply to this conversation."""

    def __init__(self, chat_id: str, user_id: str) -> None:
        self.chat_id, self.user_id = chat_id, user_id
        super().__init__("Configured reply account is not a member of target chat. "
                         "Use a chat containing the reply account or sign in as a participant.")


def adaptive_card_payload(card: dict) -> dict:
    """Build a Graph attachment payload; interactive submit handling is not wired.

    Kept separate from the plain-text path until card behavior is evaluated in
    Teams. OpenUrl actions can link to an existing authorized application page.
    """
    if card.get("type") != "AdaptiveCard" or not isinstance(card.get("body"), list):
        raise ValueError("Expected an AdaptiveCard object with a body array")
    attachment_id = uuid4().hex
    return {"body": {"contentType": "html", "content": f'<attachment id="{attachment_id}"></attachment>'},
            "attachments": [{"id": attachment_id, "contentType": "application/vnd.microsoft.card.adaptive",
                             "contentUrl": None, "content": json.dumps(card)}]}

def resource_ids(notification: dict) -> tuple[str, str] | None:
    resource = notification.get("resource")
    if not isinstance(resource, str):
        return None
    match = re.fullmatch(r"/?chats/([^/]+)/messages/([^/?]+)", resource)
    if not match:
        match = re.fullmatch(r"/?chats\('([^']+)'\)/messages\('([^']+)'\)", resource)
    return (unquote(match[1]), unquote(match[2])) if match else None

class TeamsService:
    def __init__(self, graph: GraphService, config: Settings = settings,
                 token_provider: TokenProvider | None = None) -> None:
        self.graph, self.config, self.token_provider = graph, config, token_provider

    async def _reply_token(self) -> str:
        token = self.token_provider() if self.token_provider else await asyncio.to_thread(self._environment_token)
        if inspect.isawaitable(token):
            token = await token
        if not token:
            raise GraphTokenError("Replies require delegated ChatMessage.Send authentication")
        return normalize_graph_token(token)

    async def validate_reply_account(self) -> dict:
        return await UserService(self.graph, self.config).get_reply_account(await self._reply_token())

    async def get_chat_members(self, chat_id: str) -> list[dict]:
        """Read all roster pages using application Chat.Read.All (or ChatMember.Read.All).

        `id` is an opaque membership ID; only `userId` identifies a directory user.
        Directory lookup supplies UPN because roster email need not equal UPN.
        """
        members = await self._chat_roster(chat_id)
        users = UserService(self.graph, self.config)
        for member in members:
            member["userPrincipalName"] = None
            if member["userId"]:
                try:
                    profile = await users.get_user(member["userId"])
                    member["userPrincipalName"] = profile.get("userPrincipalName")
                except GraphError:
                    logger.warning("Cannot resolve member UPN: user_id=%s", member["userId"])
        return members

    async def _chat_roster(self, chat_id: str) -> list[dict]:
        path = f"/chats/{quote(chat_id, safe='')}/members"
        members, visited = [], set()
        while path:
            if path in visited:
                raise RuntimeError("Graph chat membership pagination repeated a page")
            visited.add(path)
            page = await self.graph.request("GET", path)
            if not isinstance(page.get("value"), list):
                raise RuntimeError("Graph returned an invalid chat roster")
            for member in page["value"]:
                members.append({key: member.get(key) for key in
                                ("id", "userId", "tenantId", "displayName", "email")})
            path = page.get("@odata.nextLink", "")
        return members

    async def is_chat_member(self, chat_id: str, user_id: str) -> bool:
        members = await self._chat_roster(chat_id)
        member_ids = [m["userId"] for m in members if isinstance(m.get("userId"), str)]
        logger.info("Chat membership: reply_user_id=%s chat_id=%s member_ids=%s",
                    user_id, chat_id, json.dumps(member_ids))
        return any(isinstance(m.get("userId"), str) and m["userId"].lower() == user_id.lower()
                   and (not m.get("tenantId") or m["tenantId"].lower() == self.config.tenant_id.lower())
                   for m in members)

    def _environment_token(self) -> str:
        """Pick up renewed local credentials without reusing a startup snapshot.

        Only the default application settings read the file; injected settings
        and custom token providers remain deterministic and independently usable.
        """
        if self.config is settings and ENV_PATH.exists():
            values = dotenv_values(ENV_PATH)
            if "GRAPH_DELEGATED_ACCESS_TOKEN" in values:
                file_sender = values.get("GRAPH_SENDER_USER_ID")
                if file_sender and file_sender.lower() != self.config.sender_user_id.lower():
                    raise GraphTokenError("ReplyTokenSenderChanged: restart FastAPI after signing in as a different user")
                return values.get("GRAPH_DELEGATED_ACCESS_TOKEN") or ""
        return self.config.delegated_token

    async def get_message(self, chat_id: str, message_id: str) -> dict:
        return await self.graph.request("GET", f"/chats/{quote(chat_id, safe='')}/messages/{quote(message_id, safe='')}")

    async def create_chat(self, user_id: str) -> dict:
        """Create/reuse a 1:1 chat between the configured reply user and a user.

        Application Chat.Create permission creates the chat; replies still use
        the delegated sender's token. An application is not a chat member.
        """
        sender = self.config.sender_user_id
        if not sender or not user_id or sender == user_id:
            raise ValueError("Chat creation requires distinct sender and target user IDs")
        members = [{"@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{quote(member, safe='')}"}
                   for member in (sender, user_id)]
        return await self.graph.request("POST", "/chats", payload={"chatType": "oneOnOne", "members": members})

    async def send_reply(self, chat_id: str, content: str) -> dict:
        if not self.config.sender_user_id:
            raise ValueError("GRAPH_SENDER_USER_ID is required to prevent reply loops")
        token = await self._reply_token()
        path = f"/chats/{quote(chat_id, safe='')}/messages"
        endpoint = f"{GRAPH_BASE_URL}{path}"
        report = graph_token_diagnostics(token)
        if self.config.graph_diagnostics:
            logger.info("Sending Teams reply: chat_id=%s sender_id=%s endpoint=%s token=%s",
                        chat_id, self.config.sender_user_id, endpoint, json.dumps(report))
        try:
            validate_reply_token(token, tenant_id=self.config.tenant_id, sender_id=self.config.sender_user_id)
        except GraphTokenError as exc:
            logger.error("Teams reply token preflight failed: %s", exc)
            raise
        if not await self.is_chat_member(chat_id, self.config.sender_user_id):
            logger.warning("Configured reply account is not a member of target chat. reply_user_id=%s chat_id=%s",
                           self.config.sender_user_id, chat_id)
            raise SenderNotInChatError(chat_id, self.config.sender_user_id)
        try:
            result = await self.graph.request("POST", path, access_token=token,
                payload={"body": {"contentType": "text", "content": content}})
        except GraphError as exc:
            logger.error("Graph Reply Failed | Status: %s | Code: %s | Message: %s | Endpoint: %s",
                         exc.status, exc.code or "unknown", exc.hint or "See Graph response body", endpoint)
            if self.config.graph_diagnostics:
                logger.error("Teams reply diagnostic: chat_id=%s sender_id=%s endpoint=%s token=%s response_body=%s",
                             chat_id, self.config.sender_user_id, endpoint, json.dumps(report), exc.response_body)
            # Never substitute the application token or blindly retry a chat POST.
            raise
        logger.info("Teams reply Graph request succeeded")
        return result
