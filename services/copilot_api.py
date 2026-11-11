"""Read-only policy action for the declarative Copilot agent."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.copilot import CopilotAuthentication
from auth.rbac import resolve_user_role
from services.graph_service import GraphError


class PolicyQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)


def copilot_router(config, processor):
    router = APIRouter(prefix="/api/copilot")
    authentication = CopilotAuthentication(config)
    capacity = asyncio.Semaphore(10)

    @router.post("/answer", operation_id="get_policy_answer")
    async def answer(body: PolicyQuestion, request: Request):
        user_id = await authentication.authenticate(request.headers.get("authorization", ""))
        if capacity.locked():
            raise HTTPException(429, "PolicyPilot is busy. Please try again shortly.")
        async with capacity:
            try:
                groups = await processor.users.get_user_groups(user_id)
                context = await processor.rag.answer(body.question, resolve_user_role(groups))
                result = await processor.ai.generate_response(body.question, context)
            except (GraphError, ValueError, RuntimeError, OSError):
                raise HTTPException(503, "The policy answer service is unavailable.") from None
        return {"answer": result, "retrieval_mode": "mock"}

    return router
