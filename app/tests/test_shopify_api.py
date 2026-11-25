import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shopify_api import Shopify, ShopifyError  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, json=None, lines=(), headers=None, text=""):
        self.status_code, self._json, self._lines = status, json, list(lines)
        self.headers, self.text = headers or {}, text

    def json(self):
        return self._json

    def iter_lines(self):
        return iter(self._lines)


class FakeHttp:
    """Plays back queued responses and records the requests made."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.responses.pop(0)


def data(**values):
    return FakeResponse(json={"data": values})


def client(responses, client_id=None):
    http, sleeps = FakeHttp(responses), []
    return Shopify("s.myshopify.com", "2026-10", "secret", client_id, http=http, sleep=sleeps.append), http, sleeps


class GraphqlTest(unittest.TestCase):
    def test_access_token(self):
        shop, http, _ = client([data(shop={"name": "S"})])
        self.assertEqual(shop.graphql("{ shop { name } }"), {"shop": {"name": "S"}})
        method, url, kw = http.calls[0]
        self.assertEqual(url, "https://s.myshopify.com/admin/api/2026-10/graphql.json")
        self.assertEqual(kw["headers"], {"X-Shopify-Access-Token": "secret"})

    def test_client_credentials_and_expired_token(self):
        token = FakeResponse(json={"access_token": "t1", "expires_in": 86399})
        shop, http, _ = client([token, FakeResponse(status=401), token, data(ok=True)], client_id="cid")
        self.assertEqual(shop.graphql("{ ok }"), {"ok": True})
        self.assertEqual(http.calls[0][1], "https://s.myshopify.com/admin/oauth/access_token")
        self.assertEqual(http.calls[0][2]["data"],
                         {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "secret"})
        self.assertEqual(http.calls[3][2]["headers"], {"X-Shopify-Access-Token": "t1"})

    def test_throttled(self):
        throttled = FakeResponse(json={"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]})
        shop, _, sleeps = client([FakeResponse(status=429, headers={"Retry-After": "1.5"}), throttled, data(ok=1)])
        self.assertEqual(shop.graphql("{ ok }"), {"ok": 1})
        self.assertEqual(sleeps, [1.5, 2])

    def test_errors(self):
        denied = {"data": {"x": None}, "errors": [{"message": "Access denied", "path": ["x"]}]}
        shop, _, _ = client([FakeResponse(json=denied), FakeResponse(json=denied)])
        with self.assertRaisesRegex(ShopifyError, "Access denied"):
            shop.graphql("{ x }")
        self.assertEqual(shop.graphql("{ x }", allow_errors=True), ({"x": None}, denied["errors"]))

    def test_type_info_is_cached(self):
        shop, http, _ = client([data(__type={"name": "Order", "fields": []})])
        shop.type_info("Order")
        shop.type_info("Order")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(json.loads(json.dumps(http.calls[0][2]["json"]["variables"])), {"name": "Order"})


def started(op="gid://shopify/BulkOperation/1"):
    return data(bulkOperationRunQuery={"bulkOperation": {"id": op, "status": "CREATED"}, "userErrors": []})


def status(value, url=None, error=None):
    return data(bulkOperation={"id": "gid://shopify/BulkOperation/1", "status": value, "errorCode": error,
                               "url": url})


class BulkTest(unittest.TestCase):
    def test_bulk_query(self):
        busy = data(bulkOperationRunQuery={"bulkOperation": None, "userErrors": [
            {"message": "A bulk query operation for this app and shop is already in progress",
             "code": "OPERATION_IN_PROGRESS"}]})
        results = FakeResponse(lines=[b'{"id": "gid://shopify/Order/1"}', b"",
                                      b'{"id": "gid://shopify/LineItem/2", "__parentId": "gid://shopify/Order/1"}'])
        shop, http, sleeps = client([busy, started(), status("RUNNING"), status("COMPLETED", url="https://storage/x"),
                                     results])
        lines = list(shop.bulk_query("{ orders { edges { node { id } } } }"))
        self.assertEqual([line["id"] for line in lines], ["gid://shopify/Order/1", "gid://shopify/LineItem/2"])
        self.assertEqual(sleeps, [5, 2])
        self.assertEqual(http.calls[-1][:2], ("GET", "https://storage/x"))
        self.assertNotIn("headers", http.calls[-1][2])

    def test_no_results(self):
        shop, _, _ = client([started(), status("COMPLETED")])
        self.assertEqual(list(shop.bulk_query("{ q }")), [])

    def test_failed(self):
        shop, _, _ = client([started(), status("FAILED", error="ACCESS_DENIED")])
        with self.assertRaisesRegex(ShopifyError, "failed: ACCESS_DENIED"):
            list(shop.bulk_query("{ q }"))

    def test_rejected(self):
        shop, _, _ = client([data(bulkOperationRunQuery={"bulkOperation": None, "userErrors": [
            {"message": "Invalid bulk query", "code": "INVALID"}]})])
        with self.assertRaisesRegex(ShopifyError, "Invalid bulk query"):
            list(shop.bulk_query("{ q }"))

    def test_timeout_cancels(self):
        shop, http, _ = client([started(), status("RUNNING"), status("RUNNING"),
                                data(bulkOperationCancel={"userErrors": []})])
        with self.assertRaisesRegex(ShopifyError, "didn't finish"):
            list(shop.bulk_query("{ q }", max_wait=2))
        self.assertIn("bulkOperationCancel", http.calls[-1][2]["json"]["query"])


if __name__ == "__main__":
    unittest.main()
