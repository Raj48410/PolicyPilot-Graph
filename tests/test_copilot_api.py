"""Copilot action token validation and authorization regression tests."""
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from auth.copilot import CopilotAuthentication
from services.copilot_api import copilot_router
from test_project import config

TENANT = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"


class TokenTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def auth(self):
        auth = CopilotAuthentication(config(tenant_id=TENANT, copilot_api_audience="policy-api"))
        auth.keys = Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key=self.key.public_key())))
        return auth

    def token(self, **changes):
        claims = dict(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", aud="policy-api",
                      tid=TENANT, oid=USER, scp="access_as_user", ver="2.0",
                      exp=int(time.time()) + 600, iat=int(time.time()) - 10, nbf=int(time.time()) - 10)
        claims.update(changes)
        return "Bearer " + jwt.encode(claims, self.key, algorithm="RS256")

    async def test_valid_signed_delegated_token(self):
        self.assertEqual(await self.auth().authenticate(self.token()), USER)

    async def test_rejects_wrong_audience_tenant_issuer_expiry_and_application_token(self):
        for changes in ({"aud": "https://graph.microsoft.com"}, {"tid": "other"},
                        {"iss": "https://attacker.invalid"}, {"exp": 1}, {"scp": ""}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                await self.auth().authenticate(self.token(**changes))

    async def test_invalid_signature_is_rejected(self):
        auth = self.auth()
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        auth.keys.get_signing_key_from_jwt.return_value.key = other_key.public_key()
        with self.assertRaises(HTTPException) as caught:
            await auth.authenticate(self.token())
        self.assertEqual(caught.exception.status_code, 401)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.processor = Mock(users=Mock(get_user_groups=AsyncMock(return_value=["HR Team"])),
                              rag=Mock(answer=AsyncMock(return_value="context")),
                              ai=Mock(generate_response=AsyncMock(return_value="backend answer")))
        app = FastAPI()
        app.include_router(copilot_router(config(copilot_api_audience="policy-api"), self.processor))
        self.client = TestClient(app)

    def test_verified_user_drives_retrieval(self):
        with patch("auth.copilot.CopilotAuthentication.authenticate", new=AsyncMock(return_value=USER)):
            response = self.client.post("/api/copilot/answer", json={"question": "Leave?"})
        self.assertEqual(response.json(), {"answer": "backend answer", "retrieval_mode": "mock"})
        self.processor.users.get_user_groups.assert_awaited_once_with(USER)
        self.processor.rag.answer.assert_awaited_once_with("Leave?", "HR")

    def test_missing_token_never_reaches_ai(self):
        self.assertEqual(self.client.post("/api/copilot/answer", json={"question": "Leave?"}).status_code, 401)
        self.processor.ai.generate_response.assert_not_awaited()

    def test_input_cannot_override_identity_or_role(self):
        for data in ({"question": " "}, {"question": "x" * 4001}, {"question": "Leave?", "role": "Admin"}):
            self.assertEqual(self.client.post("/api/copilot/answer", json=data).status_code, 422)
