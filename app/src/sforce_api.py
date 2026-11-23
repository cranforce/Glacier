"""A small Salesforce REST and Bulk API 2.0 client.

Logs in with the OAuth 2.0 JWT bearer flow: a JWT naming the External Client
App (its consumer key) and the integration user, signed with the private key
whose certificate is uploaded to the app. Queries run through either API and
come back the same way, as CSV pages shaped like the Bulk API's.
"""
import base64
import csv
import io
import json
import re
import time
from decimal import Decimal

# The REST API writes datetimes as 2026-10-01T12:00:00.000+0000; the Bulk API's
# CSV, as 2026-10-01T12:00:00.000Z.
REST_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})\+0000$")


class SalesforceError(RuntimeError):
    """Salesforce rejected a request or a Bulk API job failed."""


class Salesforce:
    def __init__(self, my_domain_url, client_id, username, private_key_pem, api_version="64.0",
                 timeout=120, http=None, clock=time.time):
        if not my_domain_url.startswith("https://"):
            raise SalesforceError(f"My Domain URL must start with https://: {my_domain_url!r}")
        self.my_domain_url = my_domain_url.rstrip("/")
        self.client_id = client_id
        self.username = username
        self.private_key_pem = private_key_pem
        self.clock = clock
        self.api_version = str(api_version)
        self.timeout = timeout
        if http is None:
            import requests
            http = requests.Session()
        self.http = http
        self.token = None
        self.instance_url = None

    @property
    def audience(self):
        """Salesforce wants the generic login host as the JWT audience, not My Domain."""
        sandbox = ".sandbox.my.salesforce.com" in self.my_domain_url
        return "https://test.salesforce.com" if sandbox else "https://login.salesforce.com"

    def assertion(self):
        """A JWT for the bearer flow, signed RS256 and good for three minutes."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        def b64(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        try:
            key = serialization.load_pem_private_key(self.private_key_pem.strip().encode(), password=None)
        except (ValueError, TypeError) as e:
            raise SalesforceError(f"The secret isn't an unencrypted PEM private key ({e})") from e
        claims = {"iss": self.client_id, "sub": self.username, "aud": self.audience,
                  "exp": int(self.clock()) + 180}
        signing_input = (b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"."
                         + b64(json.dumps(claims).encode()))
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + b64(signature)).decode()

    def authenticate(self):
        r = self.http.post(
            f"{self.my_domain_url}/services/oauth2/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": self.assertion()},
            timeout=self.timeout)
        if r.status_code != 200:
            raise SalesforceError(f"Login failed ({r.status_code}): {r.text[:500]}")
        body = r.json()
        self.token = body["access_token"]
        self.instance_url = body.get("instance_url") or self.my_domain_url

    def request(self, method, path, headers=None, **kwargs):
        """Call /services/data/v<version><path>, logging in again once if the token expired.
        A path that starts with /services/ (a REST query's nextRecordsUrl) is used as is."""
        if not path.startswith("/services/"):
            path = f"/services/data/v{self.api_version}{path}"
        for attempt in (1, 2):
            if self.token is None:
                self.authenticate()
            r = self.http.request(
                method, f"{self.instance_url}{path}",
                headers={"Authorization": f"Bearer {self.token}", **(headers or {})},
                timeout=self.timeout, **kwargs)
            if r.status_code == 401 and attempt == 1:
                self.token = None
                continue
            if r.status_code >= 400:
                raise SalesforceError(f"{method} {path} failed ({r.status_code}): {r.text[:1000]}")
            return r

    def list_objects(self):
        return self.request("GET", "/sobjects").json()["sobjects"]

    def describe(self, sobject):
        return self.request("GET", f"/sobjects/{sobject}/describe").json()

    def bulk_query(self, soql, all_rows=False, page_size=50000, max_wait=3600, sleep=time.sleep):
        """Run a Bulk API 2.0 query and yield (csv_bytes, record_count) per results page.

        Every page starts with its own header row. all_rows uses queryAll, which
        includes deleted records still in the recycle bin.
        """
        job = self.request("POST", "/jobs/query", json={
            "operation": "queryAll" if all_rows else "query",
            "query": soql,
            "contentType": "CSV",
            "columnDelimiter": "COMMA",
            "lineEnding": "LF",
        }).json()
        job_id = job["id"]

        waited, delay = 0, 2
        while True:
            status = self.request("GET", f"/jobs/query/{job_id}").json()
            state = status["state"]
            if state == "JobComplete":
                break
            if state in ("Failed", "Aborted"):
                raise SalesforceError(f"Bulk query {state.lower()}: {status.get('errorMessage')}")
            if waited >= max_wait:
                self.request("PATCH", f"/jobs/query/{job_id}", json={"state": "Aborted"})
                raise SalesforceError(f"Bulk query {job_id} didn't finish within {max_wait} seconds")
            sleep(delay)
            waited += delay
            delay = min(delay * 2, 30)

        locator = None
        while True:
            params = {"maxRecords": page_size}
            if locator:
                params["locator"] = locator
            r = self.request("GET", f"/jobs/query/{job_id}/results", params=params,
                             headers={"Accept": "text/csv"})
            yield r.content, int(r.headers.get("Sforce-NumberOfRecords", "0"))
            locator = r.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

    def rest_query(self, soql, all_rows=False, page_size=50000):
        """Run a REST API query and yield (csv_bytes, record_count) pages like bulk_query's.

        Salesforce returns up to 2,000 records per call; they're gathered into
        pages of up to page_size. Each page starts with a header row naming the
        SOQL's fields, and an empty result yields one page with only that row.
        all_rows uses queryAll, which includes deleted records still in the
        recycle bin. Uses the org's API request limit, not its Bulk API limits.
        """
        fields = select_fields(soql)
        path = "/queryAll" if all_rows else "/query"
        params = {"q": soql}
        rows, yielded = [], False
        while path:
            body = self.request("GET", path, params=params).json(parse_float=Decimal)
            rows += (csv_row(record, fields) for record in body["records"])
            while len(rows) >= page_size:
                yield csv_page(fields, rows[:page_size]), page_size
                rows, yielded = rows[page_size:], True
            path, params = (None if body.get("done", True) else body["nextRecordsUrl"]), None
        if rows or not yielded:
            yield csv_page(fields, rows), len(rows)


def select_fields(soql):
    """The fields a simple SELECT ... FROM query names, e.g. ['Id', 'Account.Name']."""
    match = re.match(r"^\s*SELECT\s+(.+?)\s+FROM\s", soql, re.IGNORECASE | re.DOTALL)
    if not match or "(" in match.group(1):
        raise SalesforceError(f"Can't run this query through the REST API: {soql[:200]}")
    return [f.strip() for f in match.group(1).split(",")]


def csv_value(value):
    """A REST API JSON value written the way the Bulk API's CSV writes it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return REST_DATETIME.sub(r"\1Z", value)
    return str(value)


def csv_row(record, fields):
    """One record's values for the fields, following relationship paths like Account.Name."""
    row = []
    for field in fields:
        value = record
        for part in field.split("."):
            if not isinstance(value, dict):
                value = None
                break
            # Match case-insensitively: SOQL doesn't care how the query spells a field
            value = next((v for k, v in value.items() if k.lower() == part.lower()), None)
        row.append(csv_value(value))
    return row


def csv_page(fields, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")
