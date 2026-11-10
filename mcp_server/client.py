"""HTTP access to the public API. Knows nothing about Django."""
import requests

TIMEOUT = 15


class ApiError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(f"{status} {error}")
        self.status = status
        self.error = error


class Api:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self._token = token

    def _request(self, method, path, *, params=None, json=None, idempotency_key=None):
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = requests.request(method, self.base_url + path, params=params, json=json, headers=headers,
                                 timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            # The exception text can carry the URL; the class name is enough.
            raise ApiError(0, f"unreachable ({type(exc).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if not 200 <= r.status_code < 300:
            error = body.get("error") if isinstance(body, dict) else None
            raise ApiError(r.status_code, str(error or r.reason or "error"))
        return body

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, json, idempotency_key=None):
        return self._request("POST", path, json=json, idempotency_key=idempotency_key)

    def patch(self, path, json):
        return self._request("PATCH", path, json=json)
