"""Sportsbook adapters. To add a provider: implement Provider (base.py) and list it in build()."""
from .base import PermanentError, Page, Provider, ProviderError, RateLimited, RecordError, TransientError
from .cascade import CascadeProvider
from .http import HttpClient
from .northstar import NorthstarProvider


def build(base_url: str, timeout: float) -> list[Provider]:
    """The configured providers, talking HTTP to `base_url/<name>`."""
    return [
        NorthstarProvider(HttpClient(f"{base_url}/northstar", timeout).get_json),
        CascadeProvider(HttpClient(f"{base_url}/cascade", timeout).get_json),
    ]


__all__ = ["build", "Provider", "Page", "ProviderError", "TransientError", "RateLimited", "PermanentError",
           "RecordError", "NorthstarProvider", "CascadeProvider", "HttpClient"]
