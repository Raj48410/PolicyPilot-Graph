"""Roster admission checks must finish successfully before any message POST."""
import unittest
from unittest.mock import AsyncMock, Mock

from services.graph_service import GraphError, GraphTokenError
from services.teams_service import SenderNotInChatError, TeamsService
from services.user_service import UserService
from test_project import config
from test_reply_diagnostics import jwt


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_on_second_page_sends_after_all_reads(self):
        graph = Mock(request=AsyncMock(side_effect=[
            {"value": [{"id": "roster-id", "userId": "other"}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
            {"value": [{"id": "opaque", "userId": "ASSISTANT", "tenantId": "TENANT"}]},
            {"id": "reply"}]))
        self.assertEqual(await TeamsService(graph, config()).send_reply("chat/id", "hello"), {"id": "reply"})
        self.assertEqual([c.args[0] for c in graph.request.await_args_list], ["GET", "GET", "POST"])
        self.assertEqual(graph.request.await_args_list[0].args[1], "/chats/chat%2Fid/members")
        self.assertNotIn("access_token", graph.request.await_args_list[0].kwargs)
        self.assertEqual(graph.request.call_args.kwargs["access_token"], "delegated")

    async def test_nonmember_does_not_send_even_if_roster_id_matches(self):
        graph = Mock(request=AsyncMock(return_value={"value": [{"id": "assistant", "userId": "other"}]}))
        with self.assertRaises(SenderNotInChatError):
            await TeamsService(graph, config()).send_reply("chat", "hello")
        self.assertEqual(graph.request.await_count, 1)
        self.assertEqual(graph.request.call_args.args[0], "GET")

    async def test_other_tenant_same_user_id_is_not_member(self):
        graph = Mock(request=AsyncMock(return_value={"value": [{"userId": "assistant", "tenantId": "other"}]}))
        self.assertFalse(await TeamsService(graph, config()).is_chat_member("chat", "assistant"))

    async def test_roster_read_failure_never_sends(self):
        graph = Mock(request=AsyncMock(side_effect=GraphError(403)))
        with self.assertRaises(GraphError):
            await TeamsService(graph, config()).send_reply("chat", "hello")
        self.assertEqual(graph.request.await_count, 1)

    async def test_pagination_cycle_never_sends(self):
        graph = Mock(request=AsyncMock(return_value={"value": [], "@odata.nextLink": "/chats/chat/members"}))
        with self.assertRaises(RuntimeError):
            await TeamsService(graph, config()).send_reply("chat", "hello")
        self.assertEqual(graph.request.await_count, 1)

    async def test_diagnostic_returns_upn_from_directory_not_email(self):
        graph = Mock(request=AsyncMock(side_effect=[
            {"value": [{"id": "opaque", "userId": "assistant", "displayName": "Reply", "email": "alias"}]},
            {"id": "assistant", "userPrincipalName": "reply@example.org"}]))
        members = await TeamsService(graph, config()).get_chat_members("chat")
        self.assertEqual(members[0]["id"], "opaque")
        self.assertEqual(members[0]["userPrincipalName"], "reply@example.org")

    async def test_startup_claim_mismatch_prevents_identity_request(self):
        graph = Mock(request=AsyncMock())
        with self.assertRaises(GraphTokenError):
            await UserService(graph, config()).get_reply_account(jwt(oid="other"))
        graph.request.assert_not_awaited()

    async def test_startup_verifies_identity_with_me(self):
        graph = Mock(request=AsyncMock(return_value={"id": "assistant", "displayName": "Reply"}))
        account = await TeamsService(graph, config(delegated_token=jwt())).validate_reply_account()
        self.assertEqual(account["displayName"], "Reply")
        self.assertTrue(graph.request.call_args.args[1].startswith("/me?"))

    async def test_opaque_token_me_mismatch_is_rejected(self):
        graph = Mock(request=AsyncMock(return_value={"id": "other"}))
        with self.assertRaises(GraphTokenError):
            await TeamsService(graph, config()).validate_reply_account()
