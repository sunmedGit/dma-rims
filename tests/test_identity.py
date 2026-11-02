"""Tests for caller identity and the Editor/Viewer split."""

from __future__ import annotations

from app.identity import (
    AccessLevel,
    ChainResolver,
    DatabricksGroupResolver,
    StaticGroupResolver,
    user_from_headers,
)

EDITORS = frozenset({"ra.lead@example.com"})
GROUP = "ra-editors"


def resolver():
    return StaticGroupResolver(EDITORS, group_name=GROUP)


def headers(**kwargs):
    mapping = {
        "X-Forwarded-Email": kwargs.get("email", ""),
        "X-Forwarded-Preferred-Username": kwargs.get("username", ""),
        "X-Forwarded-User": kwargs.get("user_id", ""),
        "X-Forwarded-Access-Token": kwargs.get("token", ""),
    }
    return {k: v for k, v in mapping.items() if v}


# --------------------------------------------------------------------------

def test_a_group_member_is_an_editor():
    user = user_from_headers(
        headers(email="ra.lead@example.com"), resolver(), editor_group=GROUP
    )
    assert user.level is AccessLevel.EDITOR
    assert user.can_edit


def test_everyone_else_is_a_viewer():
    user = user_from_headers(
        headers(email="someone@example.com"), resolver(), editor_group=GROUP
    )
    assert user.level is AccessLevel.VIEWER
    assert not user.can_edit


def test_group_matching_ignores_case():
    user = user_from_headers(
        headers(email="RA.Lead@Example.com"), resolver(), editor_group=GROUP.upper()
    )
    assert user.can_edit


def test_no_headers_means_viewer_not_error():
    """An unauthenticated call must read as nobody, not crash or escalate."""
    user = user_from_headers({}, resolver(), editor_group=GROUP)
    assert user.level is AccessLevel.VIEWER
    assert user.actor == "unknown"


def test_actor_prefers_email_then_username():
    assert (
        user_from_headers(headers(email="a@b.com", username="ab"), resolver(), GROUP).actor
        == "a@b.com"
    )
    assert user_from_headers(headers(username="ab"), resolver(), GROUP).actor == "ab"


def test_dev_mode_supplies_an_identity_when_headers_are_absent():
    user = user_from_headers(
        {}, resolver(), editor_group=GROUP, dev_mode=True,
        dev_user_email="ra.lead@example.com",
    )
    assert user.email == "ra.lead@example.com"
    assert user.can_edit


def test_dev_mode_does_not_override_real_headers():
    user = user_from_headers(
        headers(email="real@example.com"), resolver(), editor_group=GROUP,
        dev_mode=True, dev_user_email="ra.lead@example.com",
    )
    assert user.email == "real@example.com"
    assert not user.can_edit


def test_an_unconfigured_editor_group_still_works_with_an_email_list():
    user = user_from_headers(
        headers(email="ra.lead@example.com"),
        StaticGroupResolver(EDITORS),
        editor_group="",
    )
    assert user.can_edit


def test_nothing_configured_means_nobody_edits():
    user = user_from_headers(
        headers(email="ra.lead@example.com"),
        StaticGroupResolver(frozenset()),
        editor_group="",
    )
    assert not user.can_edit


# --------------------------------------------------------------------------
# failing closed
# --------------------------------------------------------------------------

class _ExplodingClient:
    class users:  # noqa: N801 - mirrors the SDK's shape
        @staticmethod
        def get(_):
            raise RuntimeError("workspace unavailable")

        @staticmethod
        def list(**_):
            raise RuntimeError("workspace unavailable")


def test_group_lookup_failure_denies_edit_rather_than_granting_it():
    resolver = DatabricksGroupResolver(workspace_client=_ExplodingClient())
    user = user_from_headers(
        headers(email="ra.lead@example.com", user_id="123"),
        resolver,
        editor_group=GROUP,
    )
    assert user.level is AccessLevel.VIEWER


def test_chain_falls_back_to_the_static_list():
    broken = DatabricksGroupResolver(workspace_client=_ExplodingClient())
    chain = ChainResolver(broken, StaticGroupResolver(EDITORS, group_name=GROUP))
    user = user_from_headers(
        headers(email="ra.lead@example.com", user_id="123"), chain, editor_group=GROUP
    )
    assert user.can_edit
