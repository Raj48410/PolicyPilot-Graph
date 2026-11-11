"""Directory profiles and paginated group membership."""
import logging
from urllib.parse import quote
from config import Settings, settings
from services.graph_service import GraphError, GraphService, GraphTokenError, validate_reply_token
logger = logging.getLogger(__name__)

class UserService:
    def __init__(self, graph: GraphService, config: Settings = settings) -> None:
        self.graph = graph
        self.config = config

    async def get_reply_account(self, token: str) -> dict:
        """Verify delegated identity with Graph; decoded JWT claims alone aren't authentication."""
        validate_reply_token(token, tenant_id=self.config.tenant_id,
                             sender_id=self.config.sender_user_id)
        user = await self.graph.request("GET", "/me?$select=id,displayName,userPrincipalName",
                                        access_token=token)
        if str(user.get("id", "")).lower() != self.config.sender_user_id.lower():
            raise GraphTokenError("ReplyTokenSenderMismatch: Graph /me does not match GRAPH_SENDER_USER_ID")
        logger.info("Reply account: %s | Reply object id: %s", user.get("displayName"), user["id"])
        return {key: user.get(key) for key in ("id", "displayName", "userPrincipalName")}

    async def get_user(self, user_id: str) -> dict:
        user = await self.graph.request("GET", f"/users/{quote(user_id, safe='')}?$select=id,displayName,mail,userPrincipalName")
        return {key: user.get(key) for key in ("id", "displayName", "mail", "userPrincipalName")}

    async def get_manager(self, user_id: str) -> dict | None:
        """Return a normalized manager, or None when no manager is assigned."""
        # This endpoint documents delegated User.Read.All, not application access.
        if not self.config.delegated_token:
            raise ValueError("Manager lookup requires a delegated token with User.Read.All")
        try:
            manager = await self.graph.request("GET", f"/users/{quote(user_id, safe='')}/manager?$select=id,displayName,mail,userPrincipalName",
                                               access_token=self.config.delegated_token)
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise
        return {key: manager.get(key) for key in ("id", "displayName", "mail", "userPrincipalName")}

    async def get_user_groups(self, user_id: str) -> list[dict]:
        path = f"/users/{quote(user_id, safe='')}/memberOf/microsoft.graph.group?$select=id,displayName"
        groups: list[dict] = []
        visited: set[str] = set()
        while path:
            if path in visited:
                raise RuntimeError("Graph membership pagination repeated a page")
            visited.add(path)
            page = await self.graph.request("GET", path)
            groups.extend({"id": group.get("id"), "displayName": group.get("displayName")}
                          for group in page.get("value", []))
            path = page.get("@odata.nextLink", "")
        logger.debug("Resolved %d directory groups", len(groups))
        return groups
