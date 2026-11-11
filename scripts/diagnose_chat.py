"""Read-only local roster diagnostic: python -m scripts.diagnose_chat CHAT_ID."""
import argparse
import asyncio
import json

from config import settings
from services.graph_service import GraphError, GraphService, GraphTokenError
from services.teams_service import TeamsService


async def diagnose(chat_id: str) -> dict:
    if not chat_id.strip() or "<" in chat_id or ">" in chat_id:
        raise ValueError("Replace <chat-id> with the actual chat ID from the message logs")
    service = TeamsService(GraphService(settings), settings)
    account = await service.validate_reply_account()
    members = await service.get_chat_members(chat_id)
    member = any(str(m.get("userId") or "").lower() == account["id"].lower()
                 and (not m.get("tenantId") or m["tenantId"].lower() == settings.tenant_id.lower())
                 for m in members)
    report = {"chat_id": chat_id, "reply_account": account, "sender_is_member": member,
              "members": members,
              "required_fix": ("None; membership permits a reply attempt" if member else
                  "Use a chat containing the reply account, add it to a group chat, or sign in as a participant"),
              "message_sent": False}
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id")
    try:
        asyncio.run(diagnose(parser.parse_args().chat_id))
    except (GraphError, GraphTokenError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Chat diagnostic failed: {exc}\n")
