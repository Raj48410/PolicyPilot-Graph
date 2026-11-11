"""Delegated login tests: no browser, credentials or Microsoft calls required."""
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from config import Settings
from scripts.login import login


class LoginTests(unittest.TestCase):
    def test_saves_verified_sender_without_printing_token(self, scope="ChatMessage.Send User.Read"):
        tenant = "11111111-1111-1111-1111-111111111111"
        sender = "22222222-2222-2222-2222-222222222222"
        client = Mock()
        client.initiate_device_flow.return_value = {"user_code": "test", "message": "Sign in"}
        client.acquire_token_by_device_flow.return_value = {
            "access_token": "secret-token", "scope": scope,
            "id_token_claims": {"tid": tenant}}
        response = Mock()
        response.json.return_value = {"id": sender}
        output = io.StringIO()
        target = Path("test.env")
        with patch("scripts.login.PublicClientApplication", return_value=client), patch(
                "scripts.login.requests.get", return_value=response), patch(
                "scripts.login.set_key") as save, redirect_stdout(output):
            login(Settings(tenant_id=tenant, client_id=sender), target)
        self.assertEqual(save.call_count, 2)
        save.assert_any_call(str(target), "GRAPH_DELEGATED_ACCESS_TOKEN", "secret-token")
        save.assert_any_call(str(target), "GRAPH_SENDER_USER_ID", sender)
        self.assertNotIn("secret-token", output.getvalue())

    def test_fully_qualified_graph_scopes_are_accepted(self):
        self.test_saves_verified_sender_without_printing_token(
            "https://graph.microsoft.com/ChatMessage.Send https://graph.microsoft.com/User.Read")

    def test_alternative_delegated_send_scope_is_accepted(self):
        self.test_saves_verified_sender_without_printing_token("Chat.ReadWrite User.Read")

    def test_missing_send_scope_preserves_existing_env(self):
        tenant = "11111111-1111-1111-1111-111111111111"
        client = Mock()
        client.initiate_device_flow.return_value = {"user_code": "test", "message": "Sign in"}
        client.acquire_token_by_device_flow.return_value = {
            "access_token": "wrong-token", "scope": "User.Read"}
        with patch("scripts.login.PublicClientApplication", return_value=client), patch(
                "scripts.login.set_key") as save, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "ChatMessage.Send"):
                login(Settings(tenant_id=tenant, client_id=tenant), Path("test.env"))
        save.assert_not_called()
