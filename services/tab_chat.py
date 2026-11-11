"""In-memory device sign-in sessions for the single-process Teams tab."""
import asyncio
import secrets
import time

import requests
from fastapi import APIRouter, HTTPException, Request
from msal import PublicClientApplication
from pydantic import BaseModel, ConfigDict, Field

from auth.rbac import resolve_user_role
from services.graph_service import GraphError


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=4000)


def chat_router(config, graph, processor):
    router = APIRouter(prefix="/api")
    sessions = {}

    def session(request):
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        item = sessions.get(key)
        if not item or item["expires"] <= time.time():
            sessions.pop(key, None)
            raise HTTPException(401, "Please sign in again.")
        return item

    @router.post("/login")
    async def login():
        for key in list(sessions):
            if sessions[key]["expires"] <= time.time():
                del sessions[key]
        if len(sessions) >= 20:
            raise HTTPException(429, "Sign-in capacity reached. Please try again later.")
        if not config.tenant_id or not config.client_id:
            raise HTTPException(503, "Microsoft sign-in is not configured.")
        # Reserve capacity before network I/O.
        key = secrets.token_urlsafe(32)
        item = dict(expires=time.time() + 900, lock=asyncio.Lock(), user=None, next_poll=0)
        sessions[key] = item
        try:
            client = await asyncio.to_thread(
                PublicClientApplication, config.client_id,
                authority=f"https://login.microsoftonline.com/{config.tenant_id}", timeout=15)
            flow = await asyncio.to_thread(client.initiate_device_flow, scopes=["User.Read"])
            if "user_code" not in flow:
                raise ValueError("Device sign-in unavailable")
            item.update(client=client, flow=flow,
                        expires=time.time() + min(flow.get("expires_in", 900), 900))
        except (ValueError, RuntimeError, requests.RequestException):
            sessions.pop(key, None)
            raise HTTPException(503, "Cannot start sign-in. Check connectivity and enable public client flows in Entra.") from None
        return {"session": key, "code": flow["user_code"],
                "url": "https://microsoft.com/devicelogin", "interval": flow.get("interval", 5)}

    @router.post("/login/status")
    async def login_status(request: Request):
        item = session(request)
        if item["user"]:
            return {"status": "ready", "name": item["user"].get("displayName", "Employee")}
        if item["lock"].locked() or time.time() < item["next_poll"]:
            return {"status": "pending"}
        async with item["lock"]:
            item["next_poll"] = time.time() + item["flow"].get("interval", 5)
            try:
                result = await asyncio.to_thread(
                    item["client"].acquire_token_by_device_flow, item["flow"],
                    exit_condition=lambda flow: True)
                if result.get("error") in {"authorization_pending", "slow_down"}:
                    return {"status": "pending"}
                if (not result.get("access_token")
                        or (result.get("id_token_claims") or {}).get("tid") != config.tenant_id):
                    item["expires"] = 0
                    raise HTTPException(401, "Sign-in failed or expired. Please sign in again.")
                user = await graph.request("GET", "/me?$select=id,displayName",
                                           access_token=result["access_token"])
                if not user.get("id"):
                    raise ValueError("Missing user identity")
                item.update(user=user, expires=time.time() + min(int(result.get("expires_in", 3600)), 3600) - 60)
                # No Graph access or refresh tokens are retained after verification.
                item.pop("client", None)
                item.pop("flow", None)
            except (GraphError, requests.RequestException, ValueError, RuntimeError):
                item["expires"] = 0
                raise HTTPException(503, "Unable to verify sign-in. Please sign in again.") from None
        return {"status": "ready", "name": user.get("displayName", "Employee")}

    @router.post("/logout")
    async def logout(request: Request):
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        sessions.pop(key, None)
        return {"status": "signed_out"}

    @router.post("/chat")
    async def chat(message: ChatMessage, request: Request):
        item = session(request)
        if not item["user"]:
            raise HTTPException(401, "Complete Microsoft sign-in first.")
        if item["lock"].locked():
            raise HTTPException(429, "Please wait for your previous answer.")
        async with item["lock"]:
            try:
                groups = await processor.users.get_user_groups(item["user"]["id"])
                role = resolve_user_role(groups)
                context = await processor.rag.answer(message.message, role)
                answer = await processor.ai.generate_response(message.message, context)
            except (GraphError, ValueError, RuntimeError, OSError):
                raise HTTPException(503, "Unable to answer right now. Please try again later.") from None
        return {"answer": answer}

    return router
