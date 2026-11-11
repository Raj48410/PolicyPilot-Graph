"""Application token lifetime handling, independent of delegated Teams replies."""

import unittest
from unittest.mock import Mock, patch

from services.graph_service import GraphService
from test_project import config


class ApplicationTokenCacheTests(unittest.TestCase):
    def test_application_token_reused_before_refresh_window(self):
        client = Mock()
        client.acquire_token_for_client.return_value = {"access_token": "application-token", "expires_in": 3600}
        graph = GraphService(config())
        with patch("services.graph_service.ConfidentialClientApplication", return_value=client), patch(
                "services.graph_service.time.time", return_value=1000):
            self.assertEqual(graph.get_access_token(), "application-token")
            self.assertEqual(graph.get_access_token(), "application-token")
        client.acquire_token_for_client.assert_called_once_with(scopes=["https://graph.microsoft.com/.default"])
        self.assertEqual(graph._expires_at, 4600)

    def test_application_token_refreshed_five_minutes_before_expiry(self):
        old, new = Mock(), Mock()
        old.acquire_token_for_client.return_value = {"access_token": "old-token", "expires_in": 3600}
        new.acquire_token_for_client.return_value = {"access_token": "new-token", "expires_in": 3600}
        graph = GraphService(config())
        with patch("services.graph_service.ConfidentialClientApplication", side_effect=[old, new]), patch(
                "services.graph_service.time.time", return_value=1000):
            self.assertEqual(graph.get_access_token(), "old-token")
        with patch("services.graph_service.ConfidentialClientApplication", return_value=new), patch(
                "services.graph_service.time.time", return_value=4300):
            self.assertEqual(graph.get_access_token(), "new-token")
        new.acquire_token_for_client.assert_called_once()
        self.assertEqual(graph._expires_at, 7900)

    def test_explicit_invalidation_clears_both_caches(self):
        graph = GraphService(config())
        graph._client, graph._access_token, graph._expires_at = Mock(), "rejected-token", 9999999999
        graph.invalidate_application_token()
        self.assertIsNone(graph._client)
        self.assertIsNone(graph._access_token)
        self.assertEqual(graph._expires_at, 0)

    def test_authentication_failure_does_not_retain_rejected_token(self):
        graph = GraphService(config())
        client = Mock()
        client.acquire_token_for_client.return_value = {"error": "invalid_client"}
        with patch("services.graph_service.ConfidentialClientApplication", return_value=client):
            with self.assertRaises(RuntimeError):
                graph.get_access_token()
        self.assertIsNone(graph._access_token)
        self.assertEqual(graph._expires_at, 0)
