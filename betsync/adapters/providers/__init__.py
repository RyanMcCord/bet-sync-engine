"""Sportsbook adapters: the Provider port, implemented once per book.

To add a provider: implement Provider (betsync/ports.py) in a new module here and list it in build().
"""
from .cascade import CascadeProvider
from .http import HttpClient
from .northstar import NorthstarProvider


def build(base_url: str, timeout: float) -> list:
    """The configured providers, talking HTTP to `base_url/<name>`."""
    return [
        NorthstarProvider(HttpClient(f"{base_url}/northstar", timeout).get_json),
        CascadeProvider(HttpClient(f"{base_url}/cascade", timeout).get_json),
    ]


__all__ = ["build", "NorthstarProvider", "CascadeProvider", "HttpClient"]
