import base64
import json
import os
import sys
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sforce_api import Salesforce, SalesforceError  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, json=None, content=b"", headers=None, text=""):
        self.status_code, self._json, self.content = status, json, content
        self.headers, self.text = headers or {}, text

    def json(self):
        return self._json


class FakeHttp:
    """Plays back queued responses and records the requests made."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.responses.pop(0)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


TOKEN = FakeResponse(json={"access_token": "t1", "instance_url": "https://x.my.salesforce.com"})


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()


def b64decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def client(responses, domain="https://x.my.salesforce.com", pem=PEM):
    http = FakeHttp(responses)
    sf = Salesforce(domain, "id", "svc@x.org", pem, "64.0", http=http, clock=lambda: 1000)
    return sf, http


class SalesforceTest(unittest.TestCase):
    def test_jwt_bearer_login(self):
        sf, http = client([TOKEN, FakeResponse(json={"sobjects": []})])
        sf.list_objects()
        method, url, kw = http.calls[0]
        self.assertEqual(url, "https://x.my.salesforce.com/services/oauth2/token")
        self.assertEqual(kw["data"]["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        header, claims, signature = kw["data"]["assertion"].split(".")
        self.assertEqual(json.loads(b64decode(header))["alg"], "RS256")
        self.assertEqual(json.loads(b64decode(claims)),
                         {"iss": "id", "sub": "svc@x.org", "aud": "https://login.salesforce.com",
                          "exp": 1180})
        KEY.public_key().verify(b64decode(signature), f"{header}.{claims}".encode(),
                                padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(http.calls[1][2]["headers"]["Authorization"], "Bearer t1")

    def test_sandbox_audience(self):
        sf, _ = client([], domain="https://x--uat.sandbox.my.salesforce.com")
        self.assertEqual(sf.audience, "https://test.salesforce.com")

    def test_bad_private_key(self):
        sf, _ = client([], pem="not a key")
        with self.assertRaisesRegex(SalesforceError, "PEM private key"):
            sf.authenticate()

    def test_login_failure(self):
        sf, _ = client([FakeResponse(status=400, text="invalid_client")])
        with self.assertRaisesRegex(SalesforceError, "Login failed"):
            sf.list_objects()

    def test_relogin_once_on_expired_token(self):
        sf, http = client([TOKEN, FakeResponse(status=401), TOKEN, FakeResponse(json={"sobjects": [1]})])
        self.assertEqual(sf.list_objects(), [1])
        self.assertEqual(sum(1 for c in http.calls if c[1].endswith("/oauth2/token")), 2)

    def test_requires_https(self):
        with self.assertRaises(SalesforceError):
            Salesforce("http://x.my.salesforce.com", "id", "u", PEM, http=FakeHttp([]))

    def test_bulk_query_polls_and_pages(self):
        sf, http = client([
            TOKEN,
            FakeResponse(json={"id": "J1"}),
            FakeResponse(json={"state": "InProgress"}),
            FakeResponse(json={"state": "JobComplete"}),
            FakeResponse(content=b'"Id"\na\nb\n', headers={"Sforce-NumberOfRecords": "2", "Sforce-Locator": "L2"}),
            FakeResponse(content=b'"Id"\nc\n', headers={"Sforce-NumberOfRecords": "1", "Sforce-Locator": "null"}),
        ])
        sleeps = []
        pages = list(sf.bulk_query("SELECT Id FROM X", all_rows=True, sleep=sleeps.append))
        self.assertEqual([n for _, n in pages], [2, 1])
        self.assertEqual(sleeps, [2])
        self.assertEqual(http.calls[1][2]["json"]["operation"], "queryAll")
        self.assertEqual(http.calls[5][2]["params"], {"maxRecords": 50000, "locator": "L2"})

    def test_bulk_query_failure(self):
        sf, _ = client([TOKEN, FakeResponse(json={"id": "J1"}),
                        FakeResponse(json={"state": "Failed", "errorMessage": "bad field"})])
        with self.assertRaisesRegex(SalesforceError, "bad field"):
            list(sf.bulk_query("SELECT Nope FROM X", sleep=lambda s: None))


if __name__ == "__main__":
    unittest.main()
