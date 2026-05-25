from __future__ import annotations

import pytest

from mailattachments2dropbox.sentinel import (
    RoutingDecision,
    RoutingError,
    extension_allowed,
    parse_sentinel,
    route,
    sender_allowed,
)


@pytest.mark.parametrize(
    ("body", "expected_branch", "expected_subfolder"),
    [
        (":::in:::paypal", "in", "paypal"),
        ("Hi\n:::out\nrest", "out", None),
        ("text :::IN:::Kasse text", "in", "kasse"),
        ("no sentinel here", None, None),
        ("", None, None),
    ],
)
def test_parse_sentinel(body, expected_branch, expected_subfolder):
    parsed = parse_sentinel(body, ":::", ":::")
    if expected_branch is None:
        assert parsed is None
    else:
        assert parsed is not None
        branch, sub, _raw = parsed
        assert branch == expected_branch
        assert sub == expected_subfolder


def test_route_defaults_when_no_sentinel(settings, mapping):
    decision = route("Hello world", mapping, settings)
    assert isinstance(decision, RoutingDecision)
    assert decision.target_subpath == "Inbox/Auto-Assignment"
    assert decision.matched_token is None


def test_route_known_sentinel(settings, mapping):
    decision = route(":::in:::paypal", mapping, settings)
    assert isinstance(decision, RoutingDecision)
    assert decision.target_subpath == "Inbox/PayPal"
    assert decision.matched_token == ":::in:::paypal"


def test_route_branch_only_uses_default_subfolder(settings, mapping):
    decision = route(":::in", mapping, settings)
    assert isinstance(decision, RoutingDecision)
    assert decision.target_subpath == "Inbox/Auto-Assignment"


def test_route_out_branch_without_subfolder(settings, mapping):
    decision = route(":::out", mapping, settings)
    assert isinstance(decision, RoutingDecision)
    # out has default_subfolder="", so attachments land directly in the branch root.
    assert decision.target_subpath == "Outbox"


def test_route_unknown_branch_returns_error(settings, mapping):
    decision = route(":::nope:::xyz", mapping, settings)
    assert isinstance(decision, RoutingError)
    assert "nope" in decision.reason


def test_route_unknown_subfolder_returns_error(settings, mapping):
    decision = route(":::in:::quatsch", mapping, settings)
    assert isinstance(decision, RoutingError)
    assert "quatsch" in decision.reason


def test_sender_allowed_empty_list_accepts_everyone():
    ok, rule = sender_allowed("anyone@example.com", [])
    assert ok is True
    assert rule is None


def test_sender_allowed_glob_match():
    ok, rule = sender_allowed("alice@example.com", ["*@example.com", "bob@other.com"])
    assert ok is True
    assert rule == "*@example.com"


def test_sender_rejected_when_not_in_list():
    ok, _ = sender_allowed("attacker@evil.com", ["*@example.com"])
    assert ok is False


def test_sender_empty_rejected_when_list_present():
    ok, _ = sender_allowed("", ["*@example.com"])
    assert ok is False


def test_extension_allowed():
    assert extension_allowed("a.PDF", ["pdf"]) is True
    assert extension_allowed("a.exe", ["pdf"]) is False
    assert extension_allowed("noext", ["pdf"]) is False
    assert extension_allowed("a.exe", []) is True
