"""Tab identity checks, role enforcement and safe chat responses."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.graph_service import GraphError
from services.tab_chat import chat_router
from test_project import config


class TabChatTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.graph = Mock(request=AsyncMock(return_value={"id": "signed-in-user", "displayName": "Alice"}))
        self.processor = Mock(
            users=Mock(get_user_groups=AsyncMock(return_value=[{"displayName": "HR Team"}])),
            rag=Mock(answer=AsyncMock(return_value="authorized context")),
            ai=Mock(generate_response=AsyncMock(return_value="answer")))
        app = FastAPI()
        app.include_router(chat_router(self.config, self.graph, self.processor))
        self.client = TestClient(app)
        self.msal = Mock()
        self.msal.initiate_device_flow.return_value = {"user_code": "CODE", "device_code": "private", "interval": 0}
        self.msal.acquire_token_by_device_flow.return_value = {
            "access_token": "private-token", "id_token_claims": {"tid": self.config.tenant_id}, "expires_in": 3600}
        patcher = patch("services.tab_chat.PublicClientApplication", return_value=self.msal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self):
        response = self.client.post("/api/login")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private", response.text)
        headers = {"Authorization": "Bearer " + response.json()["session"]}
        return headers, self.client.post("/api/login/status", headers=headers)

    def test_chat_requires_verified_login(self):
        self.assertEqual(self.client.post("/api/chat", json={"message": "hello"}).status_code, 401)
        token = self.client.post("/api/login").json()["session"]
        response = self.client.post("/api/chat", headers={"Authorization": "Bearer " + token}, json={"message": "hi"})
        self.assertEqual(response.status_code, 401)
        self.processor.ai.generate_response.assert_not_awaited()

    def test_authenticated_chat_uses_verified_identity_and_server_role(self):
        headers, response = self.sign_in()
        self.assertEqual(response.json()["status"], "ready")
        response = self.client.post("/api/chat", headers=headers, json={"message": " Leave policy? "})
        self.assertEqual(response.json(), {"answer": "answer"})
        self.processor.users.get_user_groups.assert_awaited_once_with("signed-in-user")
        self.processor.rag.answer.assert_awaited_once_with("Leave policy?", "HR")
        self.graph.request.assert_awaited_once_with("GET", "/me?$select=id,displayName", access_token="private-token")

    def test_rejects_wrong_tenant(self):
        self.msal.acquire_token_by_device_flow.return_value["id_token_claims"]["tid"] = "other"
        _, response = self.sign_in()
        self.assertEqual(response.status_code, 401)
        self.graph.request.assert_not_awaited()

    def test_pending_sign_in_does_not_grant_access(self):
        self.msal.acquire_token_by_device_flow.return_value = {"error": "authorization_pending"}
        headers, response = self.sign_in()
        self.assertEqual(response.json(), {"status": "pending"})
        self.assertEqual(self.client.post("/api/chat", headers=headers, json={"message": "hi"}).status_code, 401)

    def test_rejects_blank_oversized_and_client_supplied_roles(self):
        headers, _ = self.sign_in()
        for body in ({"message": "   "}, {"message": "x" * 4001}, {"message": "hi", "role": "Admin"}):
            self.assertEqual(self.client.post("/api/chat", headers=headers, json=body).status_code, 422)
        self.processor.ai.generate_response.assert_not_awaited()

    def test_group_failure_blocks_retrieval_and_hides_details(self):
        headers, _ = self.sign_in()
        self.processor.users.get_user_groups.side_effect = GraphError(403)
        response = self.client.post("/api/chat", headers=headers, json={"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.processor.rag.answer.assert_not_awaited()

    def test_logout_invalidates_session(self):
        headers, _ = self.sign_in()
        self.client.post("/api/logout", headers=headers)
        self.assertEqual(self.client.post("/api/chat", headers=headers, json={"message": "hi"}).status_code, 401)
