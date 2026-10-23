"""WEBHOOK_TOKEN: the 17TRACK webhook is a public endpoint — the token makes the
URL itself the credential.

aiohttp routes replace rather than merge: when the token is set, the legacy
public path must NOT ride along beside it, or the "secret" URL is decoration and
anyone who knows a tracked number can still burn refresh quota.
"""
from aiohttp import web

import miniapp


def _paths(app: web.Application) -> set:
    return {r.resource.canonical for r in app.router.routes()}


def test_a_set_token_moves_the_webhook_off_the_public_path(monkeypatch):
    monkeypatch.setattr(miniapp.config, "WEBHOOK_TOKEN", "t" * 20)
    app = miniapp._build_miniapp_app()
    paths = _paths(app)
    assert "/webhook/17track/" + ("t" * 20) in paths, paths
    assert "/webhook/17track" not in paths, "the public path must not ride along"


def test_an_unset_token_registers_no_webhook_at_all(monkeypatch):
    """No token configured: the route is not registered at all. The public path
    used to answer (anti-spoof rule only), which let anyone who knew a tracked
    number burn shared quota and CPU (2026-10-04 audit). Pushes are a latency
    optimization; the poller stays the authority."""
    monkeypatch.setattr(miniapp.config, "WEBHOOK_TOKEN", "")
    app = miniapp._build_miniapp_app()
    paths = _paths(app)
    assert not any(p.startswith("/webhook/17track") for p in paths), paths


def test_the_verify_endpoint_follows_the_same_scope(monkeypatch):
    monkeypatch.setattr(miniapp.config, "WEBHOOK_TOKEN", "t" * 20)
    app = miniapp._build_miniapp_app()
    gets = {r.resource.canonical for r in app.router.routes()
            if "GET" in r.method}
    assert "/webhook/17track/" + ("t" * 20) in gets, gets