"""Retries are bounded, honor throttling hints and avoid ambiguous POST replay."""

import json
import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch

from logging_config import JsonFormatter
from services.graph_service import GraphError, GraphService
from services.teams_service import adaptive_card_payload
from test_project import config


class GraphRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_error_has_safe_diagnostics(self):
        graph = GraphService(config())
        response = self.response(400)
        response.json.return_value = {"error": {"code": "ValidationError", "message":
            "Notification URL validation failed: https://private.example/?validationToken=secret"}}
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", return_value=response):
            with self.assertRaises(GraphError) as caught:
                await graph.post("/subscriptions", {})
        self.assertIn("code=ValidationError", str(caught.exception))
        self.assertIn("webhook_validation_failed", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("private.example", str(caught.exception))

    def response(self, status, headers=None):
        return Mock(ok=status == 200, status_code=status, headers=headers or {},
                    content=b"{}", json=Mock(return_value={"ok": True}))

    async def test_get_honors_retry_after_then_succeeds(self):
        graph = GraphService(config())
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", side_effect=[self.response(429, {"Retry-After": "3"}), self.response(200)]) as request, patch(
                "services.graph_service.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await graph.get("/users/user"), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_awaited_once_with(3.0)

    async def test_persistent_get_failure_is_bounded(self):
        graph = GraphService(config())
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", return_value=self.response(503)) as request, patch(
                "services.graph_service.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(GraphError):
                await graph.get("/users/user")
        self.assertEqual(request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.0, 2.0, 4.0])

    async def test_post_is_not_replayed(self):
        graph = GraphService(config())
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", return_value=self.response(503)) as request, patch(
                "services.graph_service.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(GraphError):
                await graph.post("/chats/chat/messages", {"body": {"content": "answer"}})
        request.assert_called_once()
        sleep.assert_not_awaited()

    async def test_long_retry_after_does_not_retry_early(self):
        graph = GraphService(config())
        with patch.object(graph, "get_headers", return_value={}), patch(
                "services.graph_service.requests.request", return_value=self.response(429, {"Retry-After": "120"})) as request:
            with self.assertRaises(GraphError) as caught:
                await graph.get("/users/user")
        self.assertEqual(caught.exception.retry_after, 120)
        request.assert_called_once()


class PayloadAndLoggingTests(unittest.TestCase):
    def test_card_attachment_matches_body_reference(self):
        card = {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "Policy"}]}
        result = adaptive_card_payload(card)
        attachment = result["attachments"][0]
        self.assertIn(attachment["id"], result["body"]["content"])
        self.assertEqual(json.loads(attachment["content"]), card)
        self.assertEqual(attachment["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_invalid_card_is_rejected(self):
        with self.assertRaises(ValueError):
            adaptive_card_payload({"type": "other"})

    def test_structured_event_is_valid_json(self):
        record = logging.LogRecord("services.processor", logging.INFO, "", 0, "Resolved role: %s", ("HR",), None)
        event = json.loads(JsonFormatter().format(record))
        self.assertEqual(event["message"], "Resolved role: HR")
        self.assertEqual(event["level"], "INFO")
