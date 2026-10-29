"""A small JSON-over-HTTP client that turns transport outcomes into the provider error types."""
import requests

from .base import PermanentError, RateLimited, TransientError


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 3.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def get_json(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params={k: v for k, v in params.items() if v is not None}, timeout=self.timeout)
        except requests.Timeout as e:
            raise TransientError(f"timed out after {self.timeout}s") from e
        except requests.ConnectionError as e:
            raise TransientError(f"could not connect: {e.__class__.__name__}") from e
        if r.status_code == 429:
            raise RateLimited("rate limited (429)", _retry_after(r.headers.get("Retry-After")))
        if r.status_code >= 500:
            raise TransientError(f"server error ({r.status_code})")
        if r.status_code >= 400:
            raise PermanentError(f"request rejected ({r.status_code}): {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise PermanentError("response wasn't JSON") from e


def _retry_after(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0
