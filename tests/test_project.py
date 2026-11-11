"""Offline behavior checks for Graph transport, processing and webhook admission."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from ai.openai_service import OpenAIService
from app import create_app
from auth.rbac import resolve_user_role
from config import Settings
from rag.rag import RAGService
from services.graph_service import GraphError, GraphService
from services.message_processor import MessageProcessor
from services.subscription_service import SubscriptionService
from services.teams_service import TeamsService, resource_ids
from services.user_service import UserService


def config(**overrides) -> Settings:
    values = dict(tenant_id="tenant", client_id="client", client_secret="secret",
                  client_state="state", notification_url="https://policypilot.contoso.com/graph/webhook",
                  auto_subscribe=False, delegated_token="delegated", sender_user_id="assistant",
                  subscription_file="", azure_openai_endpoint="", azure_openai_key="",
                  azure_openai_deployment="", receive_mode="webhook", graph_diagnostics=False)
    return Settings(**(values | overrides))


def notification(**overrides) -> dict:
    return dict(clientState="state", tenantId="tenant", subscriptionId="sub",
                changeType="created", resource="chats/chat/messages/message") | overrides


class RoleAndResourceTests(unittest.TestCase):
    def test_notification_url_must_point_to_app_webhook(self):
        for url in ("https://graph.microsoft.com/v1.0/subscriptions",
                    "https://YOUR-ACTUAL-TUNNEL.devtunnels.ms/graph/webhook",
                    "https://localhost/graph/webhook", "https://example.com/wrong-path",
                    "https://example.com/graph/webhook?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config(notification_url=url).validate_graph()
        config().validate_graph()

    def test_role_precedence_and_default(self):
        self.assertEqual(resolve_user_role([]), "Employee")
        self.assertEqual(resolve_user_role(["Finance Team"]), "Finance")
        self.assertEqual(resolve_user_role(["HR Team", "Finance Team"]), "HR")
        self.assertEqual(resolve_user_role([{"displayName": "GlobalAdmin"}, "HR Team"]), "Admin")

    def test_resource_forms(self):
        self.assertEqual(resource_ids(notification()), ("chat", "message"))
        self.assertEqual(resource_ids(notification(resource="chats('a%3Ab')/messages('123')")), ("a:b", "123"))
        self.assertIsNone(resource_ids(notification(resource="users/other")))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_rejects_untrusted_pagination_url(self):
        with self.assertRaises(ValueError):
            await GraphService(config()).request("GET", "https://attacker.example/v1.0/users")

    async def test_graph_error_does_not_include_body(self):
        graph = GraphService(config())
        response = Mock(ok=False, status_code=403)
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", return_value=response):
            with self.assertRaises(GraphError) as caught:
                await graph.request("GET", "/users/user")
        self.assertEqual(str(caught.exception), "Graph request failed (HTTP 403)")

    async def test_send_uses_delegated_token(self):
        graph = Mock(request=AsyncMock(side_effect=[{"value": [{"userId": "assistant"}]}, {"id": "reply"}]))
        await TeamsService(graph, config()).send_reply("chat/id", "answer")
        graph.request.assert_awaited_with("POST", "/chats/chat%2Fid/messages",
            access_token="delegated", payload={"body": {"contentType": "text", "content": "answer"}})

    async def test_send_refuses_application_only_auth(self):
        graph = Mock(request=AsyncMock())
        with self.assertRaises(ValueError):
            await TeamsService(graph, config(delegated_token="")).send_reply("chat", "answer")
        graph.request.assert_not_awaited()

    async def test_paginated_groups(self):
        graph = Mock(request=AsyncMock(side_effect=[
            {"value": [{"id": "1", "displayName": "HR Team"}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"},
            {"value": [{"id": "2", "displayName": "Finance Team"}]}]))
        groups = await UserService(graph).get_user_groups("user")
        self.assertEqual(len(groups), 2)
        self.assertEqual(graph.request.await_count, 2)

    async def test_subscription_payload(self):
        graph = Mock(request=AsyncMock(return_value={"id": "sub"}))
        subscriptions = SubscriptionService(graph, config())
        await subscriptions.create_subscription()
        payload = graph.request.call_args.kwargs["payload"]
        self.assertEqual(payload["resource"], "/chats/getAllMessages")
        self.assertEqual(payload["changeType"], "created")
        self.assertFalse(payload["includeResourceData"])
        await subscriptions.renew_subscription("sub")
        await subscriptions.delete_subscription("sub")
        self.assertEqual(graph.request.call_args.args, ("DELETE", "/subscriptions/sub"))

    def processor(self, sender="human"):
        teams = Mock(get_message=AsyncMock(return_value={"from": {"user": {"id": sender}},
                     "body": {"contentType": "html", "content": "<p>Leave policy?</p>"}}),
                     send_reply=AsyncMock(return_value={"id": "reply"}))
        users = Mock(get_user=AsyncMock(return_value={"id": sender}),
                     get_user_groups=AsyncMock(return_value=["HR Team"]))
        ai = Mock(generate_response=AsyncMock(return_value="answer"))
        rag = Mock(answer=AsyncMock(return_value="context"))
        return MessageProcessor(teams, users, rag, ai, config())

    async def test_pipeline_and_duplicate_suppression(self):
        processor = self.processor()
        await processor.process_notification(notification())
        await processor.process_notification(notification())
        processor.rag.answer.assert_awaited_once_with("Leave policy?", "HR")
        processor.ai.generate_response.assert_awaited_once_with("Leave policy?", "context")
        processor.teams.send_reply.assert_awaited_once_with("chat", "answer")

    async def test_ignore_self(self):
        processor = self.processor(sender="assistant")
        await processor.process_notification(notification())
        processor.users.get_user.assert_not_awaited()
        processor.teams.send_reply.assert_not_awaited()

    async def test_full_cache_still_suppresses_oldest_duplicate(self):
        processor = self.processor()
        with patch("services.message_processor.time.monotonic", return_value=100):
            processor._seen[("chat", "message")] = 100
            for index in range(9999):
                processor._seen[("chat", str(index))] = 100
            await processor.process_notification(notification())
        processor.teams.get_message.assert_not_awaited()
        self.assertEqual(len(processor._seen), 10000)

    async def test_bad_state_is_rejected_before_fetch(self):
        processor = self.processor()
        with self.assertRaises(ValueError):
            await processor.process_notification(notification(clientState="wrong"))
        processor.teams.get_message.assert_not_awaited()

    async def test_directory_failure_does_not_send(self):
        processor = self.processor()
        processor.users.get_user_groups.side_effect = GraphError(403)
        with self.assertRaises(GraphError):
            await processor.process_notification(notification())
        processor.teams.send_reply.assert_not_awaited()

    async def test_placeholders_work_without_azure(self):
        context = await RAGService().answer("question", "HR")
        answer = await OpenAIService(config()).generate_response("question", context)
        self.assertIn("not connected", answer)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        startup = patch("app.TeamsService.validate_reply_account", new_callable=AsyncMock)
        startup.start()
        self.addCleanup(startup.stop)
        self.app = create_app(config())

    def test_health_and_validation(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health").json()["status"], "healthy")
            for method in (client.get, client.post):
                response = method("/graph/webhook", params={"validationToken": "a+b & c"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "a+b & c")
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_invalid_batches(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/graph/webhook", json={}).status_code, 400)
            for changes in ({"clientState": "bad"}, {"tenantId": "other"}):
                response = client.post("/graph/webhook", json={"value": [notification(**changes)]})
                self.assertEqual(response.status_code, 403)

    def test_admits_valid_notification(self):
        self.app.state.processor = Mock(process_notification=AsyncMock())
        with TestClient(self.app) as client:
            response = client.post("/graph/webhook", json={"value": [notification()]})
            self.assertEqual(response.json(), {"status": "received"})
            client.portal.call(self.app.state.queue.join)
            self.app.state.processor.process_notification.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
