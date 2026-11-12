"""A small Salesforce REST and Bulk API 2.0 client.

Logs in with the OAuth 2.0 JWT bearer flow: a JWT naming the External Client
App (its consumer key) and the integration user, signed with the private key
whose certificate is uploaded to the app.
"""
import base64
import json
import time


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
        """Call /services/data/v<version><path>, logging in again once if the token expired."""
        for attempt in (1, 2):
            if self.token is None:
                self.authenticate()
            r = self.http.request(
                method, f"{self.instance_url}/services/data/v{self.api_version}{path}",
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
