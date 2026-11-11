"""Polling behavior: timestamps, pagination, retries and transport separation."""

import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app import create_app
from services.graph_service import GraphError
from services.polling_service import PollingService
import test_project
from test_project import config


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_throttle_cooldown_is_honored(self):
        service = self.service([])
        service.poll_once = AsyncMock(side_effect=GraphError(429, retry_after=120))
        with patch("services.polling_service.asyncio.sleep", new_callable=AsyncMock,
                   side_effect=asyncio.CancelledError) as sleep, self.assertLogs("services.polling_service", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await service.run()
        sleep.assert_awaited_once_with(120)

    async def test_chat_throttling_is_propagated_for_cooldown(self):
        service = self.service([GraphError(429, retry_after=120)])
        with self.assertLogs("services.polling_service", level="ERROR"):
            with self.assertRaises(GraphError):
                await service.poll_once()
        self.assertNotIn("chat", service._cursors)

    def service(self, pages):
        graph = Mock(request=AsyncMock(side_effect=pages))
        processor = Mock(process_chat_message=AsyncMock())
        service = PollingService(graph, processor, config(receive_mode="polling", poll_chat_ids=("chat",)))
        service.started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        return service

    def message(self, message_id, minutes_ago=0):
        return {"id": message_id, "createdDateTime": (
            datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}

    async def test_old_messages_are_not_processed(self):
        new, old = self.message("new"), self.message("old", minutes_ago=10)
        service = self.service([{"value": [new, old], "@odata.nextLink": "unused"}])
        await service.poll_once()
        service.processor.process_chat_message.assert_awaited_once_with("chat", new)
        self.assertTrue(service.ready)
        self.assertEqual(service.graph.request.await_count, 1)

    async def test_messages_paginate_then_process_oldest_first(self):
        recent = self.message("recent")
        earlier = self.message("earlier")
        earlier["createdDateTime"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        service = self.service([{"value": [recent], "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"},
                                {"value": [earlier]}])
        await service.poll_once()
        self.assertEqual([call.args[1]["id"] for call in service.processor.process_chat_message.await_args_list],
                         ["earlier", "recent"])

    async def test_failed_processing_retains_cursor_for_retry(self):
        message = self.message("new")
        service = self.service([{"value": [message]}, {"value": [message]}])
        service.processor.process_chat_message.side_effect = [GraphError(503), None]
        with self.assertLogs("services.polling_service", level="ERROR"):
            await service.poll_once()
        self.assertNotIn("chat", service._cursors)
        self.assertFalse(service.ready)
        self.assertEqual(service.last_error, "Graph HTTP 503")
        await service.poll_once()
        self.assertIn("chat", service._cursors)
        self.assertTrue(service.ready)
        self.assertIsNone(service.last_error)

    async def test_discovery_paginates_and_selects_one_on_one_chats(self):
        service = self.service([])
        service.config = config(receive_mode="polling", poll_chat_ids=())
        service.graph.request.side_effect = [
            {"value": [{"id": "one", "chatType": "oneOnOne"}, {"id": "group", "chatType": "group"}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"},
            {"value": [{"id": "two", "chatType": "oneOnOne"}]}]
        self.assertEqual(await service._chat_ids(), ["one", "two"])

    async def test_pagination_loop_does_not_advance_cursor(self):
        service = self.service([
            {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"},
            {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"}])
        with self.assertLogs("services.polling_service", level="ERROR"):
            await service.poll_once()
        self.assertNotIn("chat", service._cursors)

    async def test_polled_messages_share_pipeline_and_duplicate_filter(self):
        processor = test_project.ServiceTests().processor()
        message = processor.teams.get_message.return_value | {"id": "polled"}
        await processor.process_chat_message("chat", message)
        await processor.process_chat_message("chat", message)
        processor.teams.get_message.assert_not_awaited()
        processor.teams.send_reply.assert_awaited_once_with("chat", "answer")

    async def test_polled_self_message_is_ignored(self):
        processor = test_project.ServiceTests().processor(sender="assistant")
        message = processor.teams.get_message.return_value | {"id": "self"}
        await processor.process_chat_message("chat", message)
        processor.teams.send_reply.assert_not_awaited()


class PollingAppTests(unittest.TestCase):
    def setUp(self):
        startup = patch("app.TeamsService.validate_reply_account", new_callable=AsyncMock)
        startup.start()
        self.addCleanup(startup.stop)

    def test_polling_starts_without_public_url_and_without_subscriptions(self):
        async def mock_poll_once(service):
            service.ready = True
        with patch("services.polling_service.PollingService.poll_once", mock_poll_once), patch(
                "app.SubscriptionService.create_subscription", new_callable=AsyncMock) as subscribe:
            app = create_app(config(receive_mode="polling", notification_url="", client_state="", auto_subscribe=True))
            with TestClient(app) as client:
                health = client.get("/health").json()
                self.assertEqual(health["receive_mode"], "polling")
                self.assertTrue(health["polling_ready"])
                self.assertFalse(health["subscription_ready"])
                self.assertEqual(client.post("/graph/webhook", json={"value": []}).status_code, 409)
            subscribe.assert_not_awaited()

    def test_polling_validation_does_not_require_webhook_settings(self):
        config(receive_mode="polling", notification_url="", client_state="").validate_polling()
        with self.assertRaises(ValueError):
            config(receive_mode="polling", poll_interval_seconds=1).validate_polling()
