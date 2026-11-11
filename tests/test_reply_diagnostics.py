"""Verify reply-token preflight, redaction and renewed-token selection offline."""

import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.graph_service import (GraphError, GraphService, GraphTokenError,
                                    MissingGraphPermissionError, WrongAudienceError,
                                    decode_graph_token, inspect_graph_token, normalize_graph_token, validate_reply_token)
from services.teams_service import TeamsService
from test_project import config


def jwt(**overrides):
    claims = {"aud": "00000003-0000-0000-c000-000000000000", "exp": int(time.time()) + 3600,
              "tid": "tenant", "oid": "assistant", "scp": "User.Read ChatMessage.Send"} | overrides
    encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.test-signature"


class TokenDiagnosticTests(unittest.TestCase):
    def test_named_diagnostic_exceptions(self):
        with self.assertRaises(WrongAudienceError):
            validate_reply_token(jwt(aud="https://cognitiveservices.azure.com"), tenant_id="tenant", sender_id="assistant")
        with self.assertRaises(MissingGraphPermissionError):
            validate_reply_token(jwt(scp="User.Read"), tenant_id="tenant", sender_id="assistant")

    def test_inspect_includes_application_id_without_logging_bearer(self):
        token = jwt(appid="client-id")
        with self.assertLogs("services.graph_service", level="INFO") as logs:
            report = inspect_graph_token(token)
        self.assertEqual(report["appid"], "client-id")
        self.assertNotIn(token, " ".join(logs.output))

    def test_delegated_scope_is_required_not_application_roles(self):
        with self.assertRaisesRegex(GraphTokenError, "MissingChatMessageSend"):
            validate_reply_token(jwt(scp="User.Read", roles=["ChatMessage.Send", "Chat.Read.All"]),
                                 tenant_id="tenant", sender_id="assistant")

    def test_documented_delegated_send_permissions_are_accepted(self):
        for scope in ("ChatMessage.Send", "Chat.ReadWrite"):
            with self.subTest(scope=scope):
                validate_reply_token(jwt(scp="User.Read " + scope),
                                     tenant_id="tenant", sender_id="assistant")

    def test_read_scopes_and_application_send_roles_are_rejected(self):
        with self.assertRaises(MissingGraphPermissionError):
            validate_reply_token(jwt(scp="Chat.Read Chat.ReadWrite.All Group.Read.All Group.ReadWrite.All",
                                     roles=["Chat.ReadWrite", "Group.ReadWrite.All"]),
                                 tenant_id="tenant", sender_id="assistant")

    def test_invalid_token_claims_have_specific_errors(self):
        cases = [({"aud": "https://cognitiveservices.azure.com"}, "AudienceMismatch"),
                 ({"exp": int(time.time()) - 10}, "TokenExpired"),
                 ({"tid": "other-tenant"}, "TenantMismatch"),
                 ({"oid": "other-user"}, "SenderMismatch"),
                 ({"nbf": int(time.time()) + 3600}, "NotYetValid")]
        for claims, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(GraphTokenError, expected):
                validate_reply_token(jwt(**claims), tenant_id="tenant", sender_id="assistant")

    def test_graph_guid_and_uri_audiences_are_accepted(self):
        for audience in ["00000003-0000-0000-c000-000000000000", "https://graph.microsoft.com", "https://graph.microsoft.com/"]:
            report = validate_reply_token(jwt(aud=audience), tenant_id="tenant", sender_id="assistant")
            self.assertFalse(report["signature_verified"])
            self.assertEqual(report["aud"], audience)

    def test_bearer_prefix_is_rejected(self):
        with self.assertRaisesRegex(GraphTokenError, "ReplyTokenFormat"):
            validate_reply_token("Bearer " + jwt(), tenant_id="tenant", sender_id="assistant")

    def test_authorization_value_is_normalized_before_preflight(self):
        token = jwt()
        self.assertEqual(normalize_graph_token("Bearer " + token), token)
        self.assertEqual(normalize_graph_token(" bearer " + token + " "), token)
        with self.assertRaises(GraphTokenError):
            normalize_graph_token("Bearer Bearer " + token)

    def test_opaque_tokens_are_left_to_graph_validation(self):
        self.assertFalse(validate_reply_token("opaque-token", tenant_id="tenant", sender_id="assistant")["decoded"])
        with self.assertRaises(GraphTokenError):
            decode_graph_token("opaque-token")


class ReplyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These tests isolate token transport; real roster checks have their own suite.
        membership = patch.object(TeamsService, "is_chat_member", new_callable=AsyncMock, return_value=True)
        membership.start()
        self.addCleanup(membership.stop)

    async def test_rejected_application_cache_is_cleared_for_safe_read(self):
        graph = GraphService(config())
        responses = [Mock(ok=False, status_code=401, headers={}, text="", json=Mock(return_value={})),
                     Mock(ok=True, status_code=200, content=b"{}", json=Mock(return_value={"ok": True}))]
        with patch.object(graph, "get_headers", return_value={}), patch.object(graph, "invalidate_application_token") as invalidate, patch(
                "services.graph_service.requests.request", side_effect=responses) as request:
            self.assertEqual(await graph.get("/users/user"), {"ok": True})
        invalidate.assert_called_once()
        self.assertEqual(request.call_count, 2)

    async def test_pasted_bearer_prefix_is_not_duplicated_in_http_header(self):
        token = jwt()
        graph = GraphService(config())
        response = Mock(ok=True, content=b"{}", status_code=201, json=Mock(return_value={"id": "reply"}))
        with patch("services.graph_service.requests.request", return_value=response) as request:
            await TeamsService(graph, config(delegated_token="Bearer " + token)).send_reply("chat", "Hello from PolicyPilot")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual(request.call_args.kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(request.call_args.kwargs["json"],
                         {"body": {"contentType": "text", "content": "Hello from PolicyPilot"}})

    async def test_invalid_token_never_sends_or_falls_back_to_application_auth(self):
        graph = Mock(request=AsyncMock())
        service = TeamsService(graph, config(delegated_token=jwt(aud="wrong-api")))
        with self.assertLogs("services.teams_service", level="ERROR"), self.assertRaises(GraphTokenError):
            await service.send_reply("chat", "answer")
        graph.request.assert_not_awaited()

    async def test_expired_startup_snapshot_uses_new_local_token(self):
        old = jwt(exp=int(time.time()) - 100)
        fresh = jwt()
        cfg = config(delegated_token=old)
        graph = Mock(request=AsyncMock(return_value={"id": "reply"}))
        service = TeamsService(graph, cfg)
        with patch("services.teams_service.settings", cfg), patch("services.teams_service.ENV_PATH") as path, patch(
                "services.teams_service.dotenv_values", return_value={"GRAPH_DELEGATED_ACCESS_TOKEN": fresh,
                                                                       "GRAPH_SENDER_USER_ID": "assistant"}):
            path.exists.return_value = True
            await service.send_reply("chat", "answer")
        self.assertEqual(graph.request.call_args.kwargs["access_token"], fresh)

    async def test_graph_body_is_preserved_but_credentials_are_redacted(self):
        token = jwt()
        cfg = config(graph_diagnostics=True, client_secret="private-client-secret")
        graph = GraphService(cfg)
        response = Mock(ok=False, status_code=401, headers={}, text=json.dumps({"error": {
            "code": "InvalidAuthenticationToken", "message": f"Expired token: {token}; private-client-secret"}}))
        response.json.return_value = {"error": {"code": "InvalidAuthenticationToken", "message": "Expired token"}}
        with patch("services.graph_service.requests.request", return_value=response), self.assertLogs(
                "services.graph_service", level="ERROR") as logs:
            with self.assertRaises(GraphError) as caught:
                await graph.request("POST", "/chats/chat/messages", access_token=token, payload={})
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("Expired token", caught.exception.response_body)
        self.assertIn("Response Body", str(caught.exception))
        diagnostic = " ".join(logs.output) + str(caught.exception)
        self.assertNotIn(token, diagnostic)
        self.assertNotIn("private-client-secret", diagnostic)

    async def test_reply_failure_report_includes_context_without_bearer(self):
        token = jwt()
        graph = Mock(request=AsyncMock(side_effect=GraphError(401, code="InvalidAuthenticationToken", response_body='{"error":"expired"}')))
        service = TeamsService(graph, config(delegated_token=token, graph_diagnostics=True))
        with self.assertLogs("services.teams_service", level="INFO") as logs, self.assertRaises(GraphError):
            await service.send_reply("chat", "answer")
        output = " ".join(logs.output)
        self.assertIn("chat_id=chat", output)
        self.assertIn("sender_id=assistant", output)
        self.assertIn("Status: 401", output)
        self.assertIn('response_body={"error":"expired"}', output)
        self.assertNotIn(token, output)

    async def test_empty_explicit_token_cannot_use_application_headers(self):
        graph = GraphService(config())
        with patch.object(graph, "get_headers") as headers, self.assertRaises(GraphTokenError):
            await graph.request("POST", "/chats/chat/messages", access_token="")
        headers.assert_not_called()
