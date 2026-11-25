"""A small Shopify GraphQL Admin API client.

Authenticates with either an Admin API access token (a custom app created in the
store's admin) or a Dev Dashboard app's client ID and secret, exchanged for a
24-hour token through the client credentials grant. Runs ordinary queries,
introspects types, and runs bulk queries, whose JSONL results it streams.
"""
import json
import time

# Bulk operation user error codes that mean "try again shortly": another bulk
# query is running, or this app already has as many running as Shopify allows.
BUSY_CODES = frozenset({"OPERATION_IN_PROGRESS", "LIMIT_REACHED"})

TYPE_REF = "kind name ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }"
TYPE_QUERY = ("query($name: String!) { __type(name: $name) { kind name interfaces { name } "
              f"fields {{ name type {{ {TYPE_REF} }} args {{ name defaultValue type {{ {TYPE_REF} }} }} }} }} }}")

RUN_BULK = ("mutation($query: String!) { bulkOperationRunQuery(query: $query) { "
            "bulkOperation { id status } userErrors { field message code } } }")
BULK_STATUS = ("query($id: ID!) { bulkOperation(id: $id) { "
               "id status errorCode objectCount url partialDataUrl } }")
CANCEL_BULK = "mutation($id: ID!) { bulkOperationCancel(id: $id) { userErrors { message } } }"


class ShopifyError(RuntimeError):
    """Shopify rejected a request or a bulk operation failed."""


class Shopify:
    def __init__(self, shop_domain, api_version, secret, client_id=None, timeout=120, http=None,
                 sleep=time.sleep):
        self.shop_domain = shop_domain
        self.api_version = api_version
        self.secret = secret
        self.client_id = client_id or None
        self.timeout = timeout
        self.sleep = sleep
        if http is None:
            import requests
            http = requests.Session()
        self.http = http
        self.token = None
        self.types = {}

    @property
    def endpoint(self):
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def authenticate(self):
        """With a client ID, exchange it and the secret for a token; else the secret is the token."""
        if not self.client_id:
            self.token = self.secret
            return
        r = self.http.post(
            f"https://{self.shop_domain}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.secret},
            timeout=self.timeout)
        if r.status_code != 200:
            raise ShopifyError(f"Login failed ({r.status_code}): {r.text[:500]}")
        self.token = r.json()["access_token"]

    def graphql(self, query, variables=None, allow_errors=False, retries=10):
        """Run a query and return its data, waiting out throttling.

        A client credentials token is fetched again once if it's rejected.
        allow_errors returns (data, errors) instead of raising on GraphQL errors,
        for a query whose fields may be denied.
        """
        reauthenticated = False
        for attempt in range(retries + 1):
            if self.token is None:
                self.authenticate()
            r = self.http.post(self.endpoint, json={"query": query, "variables": variables or {}},
                               headers={"X-Shopify-Access-Token": self.token}, timeout=self.timeout)
            if r.status_code == 401 and self.client_id and not reauthenticated:
                self.token, reauthenticated = None, True
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    self.sleep(float(r.headers.get("Retry-After") or 2))
                    continue
            if r.status_code >= 400:
                raise ShopifyError(f"GraphQL request failed ({r.status_code}): {r.text[:1000]}")
            body = r.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors) and attempt < retries:
                self.sleep(2)
                continue
            if allow_errors:
                return body.get("data"), errors
            if errors:
                raise ShopifyError("GraphQL errors: " + "; ".join(e.get("message", str(e)) for e in errors)[:2000])
            return body["data"]
        raise ShopifyError(f"Still throttled after {retries} retries")

    def type_info(self, name):
        """A type's introspection: kind, interfaces, and fields with their types and arguments."""
        if name not in self.types:
            info = self.graphql(TYPE_QUERY, {"name": name})["__type"]
            if info is None:
                raise ShopifyError(f"Shopify has no type named {name!r}")
            self.types[name] = info
        return self.types[name]

    def bulk_query(self, query, max_wait=6 * 3600, start_wait=3600):
        """Run a bulk query and yield each JSONL line as a dict.

        Waits up to start_wait seconds for a slot when the app already has as
        many bulk queries running as Shopify allows, then up to max_wait for the
        operation to finish.
        """
        operation = self._start_bulk(query, start_wait)
        waited, delay = 0, 2
        while True:
            status = self.graphql(BULK_STATUS, {"id": operation})["bulkOperation"]
            if status["status"] == "COMPLETED":
                break
            if status["status"] not in ("CREATED", "RUNNING"):
                raise ShopifyError(f"Bulk query {status['status'].lower()}: {status.get('errorCode')}")
            if waited >= max_wait:
                self.graphql(CANCEL_BULK, {"id": operation})
                raise ShopifyError(f"Bulk query {operation} didn't finish within {max_wait} seconds")
            self.sleep(delay)
            waited += delay
            delay = min(delay * 2, 30)
        if not status.get("url"):
            return  # no results
        r = self.http.get(status["url"], stream=True, timeout=self.timeout)
        if r.status_code >= 400:
            raise ShopifyError(f"Downloading bulk results failed ({r.status_code})")
        for line in r.iter_lines():
            if line:
                yield json.loads(line)

    def _start_bulk(self, query, start_wait):
        waited, delay = 0, 5
        while True:
            result = self.graphql(RUN_BULK, {"query": query})["bulkOperationRunQuery"]
            errors = result.get("userErrors") or []
            if not errors:
                return result["bulkOperation"]["id"]
            if not all(e.get("code") in BUSY_CODES for e in errors) or waited >= start_wait:
                raise ShopifyError("Bulk query rejected: " + "; ".join(e["message"] for e in errors))
            self.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60)
