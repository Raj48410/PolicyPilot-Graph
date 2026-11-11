"""Offline checks for generation, Graph operations and subscription persistence."""

import json
import unittest
from unittest.mock import AsyncMock, Mock

from openai import OpenAIError

from ai.openai_service import OpenAIService, UNAVAILABLE
from auth.rbac import resolve_user_role
from services.graph_service import GraphError, GraphService
from services.subscription_service import SubscriptionService
from services.subscription_store import SubscriptionStore
from services.teams_service import TeamsService
from services.user_service import UserService
from test_project import config


class EnterpriseServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_convenience_methods_preserve_authentication(self):
        graph = GraphService(config())
        graph.request = AsyncMock(return_value={"id": "result"})
        await graph.get("/users/user")
        graph.request.assert_awaited_with("GET", "/users/user")
        await graph.post("/chats", {"chatType": "oneOnOne"})
        graph.request.assert_awaited_with("POST", "/chats", payload={"chatType": "oneOnOne"}, access_token=None)
        await graph.patch("/subscriptions/sub", {"expirationDateTime": "date"})
        graph.request.assert_awaited_with("PATCH", "/subscriptions/sub", payload={"expirationDateTime": "date"})

    async def test_create_chat_uses_two_user_members(self):
        graph = Mock(request=AsyncMock(return_value={"id": "chat"}))
        result = await TeamsService(graph, config()).create_chat("target")
        self.assertEqual(result["id"], "chat")
        payload = graph.request.call_args.kwargs["payload"]
        self.assertEqual(payload["chatType"], "oneOnOne")
        self.assertEqual([m["user@odata.bind"] for m in payload["members"]],
                         ["https://graph.microsoft.com/v1.0/users/assistant",
                          "https://graph.microsoft.com/v1.0/users/target"])

    async def test_create_chat_rejects_self_chat(self):
        graph = Mock(request=AsyncMock())
        with self.assertRaises(ValueError):
            await TeamsService(graph, config()).create_chat("assistant")
        graph.request.assert_not_awaited()

    async def test_manager_404_means_unassigned(self):
        graph = Mock(request=AsyncMock(side_effect=GraphError(404)))
        self.assertIsNone(await UserService(graph, config()).get_manager("user"))
        self.assertEqual(graph.request.call_args.kwargs["access_token"], "delegated")

    async def test_manager_forbidden_propagates(self):
        graph = Mock(request=AsyncMock(side_effect=GraphError(403)))
        with self.assertRaises(GraphError):
            await UserService(graph, config()).get_manager("user")

    async def test_manager_requires_delegated_auth(self):
        graph = Mock(request=AsyncMock())
        with self.assertRaises(ValueError):
            await UserService(graph, config(delegated_token="")).get_manager("user")
        graph.request.assert_not_awaited()

    async def test_manager_normalizes_profile(self):
        graph = Mock(request=AsyncMock(return_value={"id": "manager", "displayName": "Manager", "extra": "omit"}))
        manager = await UserService(graph, config()).get_manager("user")
        self.assertEqual(manager["id"], "manager")
        self.assertNotIn("extra", manager)

    async def test_generation_uses_deployment_and_context(self):
        client = Mock()
        client.chat.completions.create = AsyncMock(return_value=Mock(
            choices=[Mock(message=Mock(content=" Authorized answer "))]))
        service = OpenAIService(config(azure_openai_endpoint="https://test.openai.azure.com",
            azure_openai_key="test", azure_openai_deployment="policy-model"), client=client)
        self.assertEqual(await service.generate_response("question", "context"), "Authorized answer")
        arguments = client.chat.completions.create.call_args.kwargs
        self.assertEqual(arguments["model"], "policy-model")
        self.assertIn("context", arguments["messages"][1]["content"])
        self.assertIn("question", arguments["messages"][1]["content"])

    async def test_generation_failure_does_not_leak_details(self):
        client = Mock()
        client.chat.completions.create = AsyncMock(side_effect=OpenAIError("sensitive-details"))
        service = OpenAIService(config(azure_openai_endpoint="https://test.openai.azure.com",
            azure_openai_key="test", azure_openai_deployment="policy-model"), client=client)
        with self.assertLogs("ai.openai_service", level="ERROR") as logs:
            answer = await service.generate_response("question", "context")
        self.assertEqual(answer, UNAVAILABLE)
        self.assertNotIn("sensitive-details", " ".join(logs.output))

    async def test_generation_missing_configuration_makes_no_request(self):
        client = Mock()
        client.chat.completions.create = AsyncMock()
        answer = await OpenAIService(config(), client).generate_response("question", "context")
        self.assertIn("not connected", answer)
        client.chat.completions.create.assert_not_awaited()

    async def test_empty_generation_uses_fallback_and_closes_client(self):
        client = Mock(close=AsyncMock())
        client.chat.completions.create = AsyncMock(return_value=Mock(choices=[]))
        service = OpenAIService(config(azure_openai_endpoint="https://test.openai.azure.com",
            azure_openai_key="test", azure_openai_deployment="policy-model"), client=client)
        self.assertEqual(await service.generate_response("question", "context"), UNAVAILABLE)
        await service.close()
        client.close.assert_awaited_once()

    async def test_subscription_disk_failure_keeps_successful_creation(self):
        result = {"id": "sub", "expirationDateTime": "2099-01-01T00:00:00+00:00"}
        graph = Mock(request=AsyncMock(return_value=result))
        service = SubscriptionService(graph, config())
        service.store = Mock(save=Mock(side_effect=OSError("disk error")))
        with self.assertLogs("services.subscription_service", level="ERROR"):
            self.assertEqual(await service.create_subscription(), result)
        graph.request.assert_awaited_once()

    async def test_subscription_restores_saved_metadata(self):
        service = SubscriptionService(Mock(), config())
        service.store = Mock(load=Mock(return_value={"id": "saved"}))
        self.assertEqual(await service.load_subscription(), {"id": "saved"})


class SubscriptionMetadataTests(unittest.TestCase):
    def test_metadata_roundtrip_binds_config_and_excludes_client_state(self):
        store = SubscriptionStore(config(subscription_file="data/test.json", client_state="private-state"))
        store.path = Mock(name="metadata_path")
        store.path.name = "subscription.json"
        subscription = {"id": "sub", "expirationDateTime": "2099-01-01T00:00:00+00:00",
                        "clientState": "must-not-be-saved"}
        store.save(subscription)
        encoded = store.path.with_name.return_value.write_text.call_args.args[0]
        self.assertNotIn("private-state", encoded)
        self.assertNotIn("must-not-be-saved", encoded)
        store.path.read_text.return_value = encoded
        self.assertEqual(store.load()["id"], "sub")
        changed = SubscriptionStore(config(subscription_file="data/test.json", client_state="changed"))
        changed.path = store.path
        self.assertIsNone(changed.load())
        self.assertEqual(set(json.loads(encoded)["subscription"]), {"id", "expirationDateTime"})

    def test_corrupt_metadata_is_ignored(self):
        store = SubscriptionStore(config(subscription_file="data/test.json"))
        store.path = Mock(read_text=Mock(return_value="invalid-json"))
        with self.assertLogs("services.subscription_store", level="WARNING"):
            self.assertIsNone(store.load())

    def test_new_roles_and_precedence(self):
        self.assertEqual(resolve_user_role(["IT Team"]), "IT")
        self.assertEqual(resolve_user_role(["IT Team", "Finance Team"]), "Finance")
        self.assertEqual(resolve_user_role(["IT Team", "Global Admin"]), "Admin")
