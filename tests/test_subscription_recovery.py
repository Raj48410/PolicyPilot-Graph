"""Remote subscription recovery and quota conflict regression checks."""
import unittest
from unittest.mock import AsyncMock, Mock

from services.graph_service import GraphError
from services.subscription_service import SubscriptionService
from test_project import config


class SubscriptionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, **changes):
        service = SubscriptionService(Mock(request=AsyncMock()), config())
        service.store = Mock(load=Mock(return_value=None), save=Mock())
        item = dict(id="existing", applicationId=service.config.client_id,
                    resource="chats/getAllMessages", changeType="created",
                    notificationUrl=service.config.notification_url,
                    clientState=service.config.client_state, includeResourceData=False,
                    expirationDateTime="2099-01-01T00:00:00Z")
        item.update(changes)
        return service, item

    async def test_missing_local_record_recovers_and_persists_remote(self):
        service, item = self.service()
        service.graph.request.return_value = {"value": [item]}
        self.assertEqual(await service.load_subscription(), item)
        service.store.save.assert_called_once_with(item)
        service.graph.request.assert_awaited_once_with("GET", "/subscriptions")

    async def test_different_configuration_never_renews_or_deletes(self):
        for changes in ({"notificationUrl": "https://old.invalid/graph/webhook"},
                        {"clientState": "old-secret"}, {"clientState": None},
                        {"includeResourceData": True}, {"changeType": "updated"}):
            with self.subTest(changes=changes):
                service, item = self.service(**changes)
                service.graph.request.return_value = {"value": [item]}
                with self.assertRaisesRegex(RuntimeError, "quota"):
                    await service.load_subscription()
                service.store.save.assert_not_called()
                service.graph.request.assert_awaited_once_with("GET", "/subscriptions")

    async def test_expired_or_other_application_is_not_recovered(self):
        for changes in ({"expirationDateTime": "2000-01-01T00:00:00Z"},
                        {"applicationId": "other-app"}):
            service, item = self.service(**changes)
            service.graph.request.return_value = {"value": [item]}
            self.assertIsNone(await service.load_subscription())

    async def test_quota_race_recovers_and_renews(self):
        service, item = self.service()
        service.graph.request.side_effect = [GraphError(403), {"value": [item]}, item]
        self.assertEqual(await service.create_subscription(), item)
        self.assertEqual([call.args[0] for call in service.graph.request.await_args_list],
                         ["POST", "GET", "PATCH"])

    async def test_unrelated_forbidden_is_preserved(self):
        service, _ = self.service()
        original = GraphError(403)
        service.graph.request.side_effect = [original, {"value": []}]
        with self.assertRaises(GraphError) as caught:
            await service.create_subscription()
        self.assertIs(caught.exception, original)

    async def test_recovery_follows_pagination(self):
        service, item = self.service()
        service.graph.request.side_effect = [
            {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/subscriptions?page=2"},
            {"value": [item]}]
        self.assertEqual(await service.load_subscription(), item)
