"""Tests for the Signal control-surface tools (``tools/signal_tool.py``).

These are fully hermetic: no real network or Signal account. The low-level
``tools.signal_tool._call`` (which would otherwise reach signal-cli over HTTP
via ``signal_rpc.call_rpc``) is monkeypatched to a recording fake, so every
test exercises only the dispatch / routing / validation / serialization logic.

Conventions mirror tests/gateway/test_signal_format.py: pytest classes,
``@pytest.mark.asyncio`` only where async is involved (none here), and
``monkeypatch`` for env + attribute patching.
"""

import json

import pytest

from gateway.platforms import signal_rpc
from tools import signal_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def signal_env(monkeypatch):
    """Configure Signal so the core tool is 'available' (admin still off)."""
    monkeypatch.setenv("SIGNAL_HTTP_URL", "http://localhost:8080")
    monkeypatch.setenv("SIGNAL_ACCOUNT", "+15550009999")
    monkeypatch.delenv("SIGNAL_ADMIN_TOOLS", raising=False)
    monkeypatch.delenv("SIGNAL_WRITE_TOOLS", raising=False)
    monkeypatch.delenv("SIGNAL_READ_RECEIPTS", raising=False)
    monkeypatch.delenv("SIGNAL_REPLY_QUOTE", raising=False)
    monkeypatch.delenv("SIGNAL_REACTIONS", raising=False)
    # Most admin tests exercise param-mapping, not the confirmation gate, so
    # default confirmation OFF here; TestAdminConfirmation turns it back on.
    monkeypatch.setenv("SIGNAL_ADMIN_REQUIRE_CONFIRM", "false")
    return monkeypatch


@pytest.fixture
def no_allowlist(monkeypatch):
    """Force 'all actions allowed' so config loading never gates dispatch.

    ``_load_allowed_actions_config`` reads real user config; pin it to None so
    these tests are independent of the host's hermes config.
    """
    monkeypatch.setattr(signal_tool, "_load_allowed_actions_config", lambda: None)


class _RecordingCall:
    """A fake for ``tools.signal_tool._call`` that records (method, params).

    Returns a canned result (default ``{"ok": True}``) or raises a preset
    exception so error-path serialization can be exercised.
    """

    def __init__(self, result=None, raises=None):
        self.result = {"ok": True} if result is None else result
        self.raises = raises
        self.calls = []  # list of (method, params)

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def last(self):
        return self.calls[-1]

    @property
    def method(self):
        return self.calls[-1][0]

    @property
    def params(self):
        return self.calls[-1][1]


@pytest.fixture
def rec(monkeypatch):
    """Install a recording fake _call and return it for inspection."""
    fake = _RecordingCall()
    monkeypatch.setattr("tools.signal_tool._call", fake)
    return fake


# ===========================================================================
# Schema shape
# ===========================================================================
class TestSchemas:
    """_CORE_SCHEMA / _ADMIN_SCHEMA structure + action enums."""

    EXPECTED_CORE = sorted(signal_tool._CORE_ACTIONS)
    EXPECTED_WRITE = sorted(signal_tool._WRITE_ACTIONS)
    EXPECTED_ADMIN = sorted(signal_tool._ADMIN_ACTIONS)

    def test_core_schema_json_serializable(self):
        # Round-trips cleanly — schemas are shipped to the model as JSON.
        dumped = json.dumps(signal_tool._CORE_SCHEMA)
        assert json.loads(dumped) == signal_tool._CORE_SCHEMA

    def test_write_schema_json_serializable(self):
        dumped = json.dumps(signal_tool._WRITE_SCHEMA)
        assert json.loads(dumped) == signal_tool._WRITE_SCHEMA

    def test_admin_schema_json_serializable(self):
        dumped = json.dumps(signal_tool._ADMIN_SCHEMA)
        assert json.loads(dumped) == signal_tool._ADMIN_SCHEMA

    def test_core_schema_basic_fields(self):
        schema = signal_tool._CORE_SCHEMA
        assert schema["name"] == "signal"
        assert schema["parameters"]["required"] == ["action"]

    def test_write_schema_basic_fields(self):
        schema = signal_tool._WRITE_SCHEMA
        assert schema["name"] == "signal_write"
        assert schema["parameters"]["required"] == ["action"]

    def test_admin_schema_basic_fields(self):
        schema = signal_tool._ADMIN_SCHEMA
        assert schema["name"] == "signal_admin"
        assert schema["parameters"]["required"] == ["action"]

    def test_core_action_enum_matches_actions(self):
        enum = signal_tool._CORE_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert sorted(enum) == self.EXPECTED_CORE
        # Spot-check read-only names are present; write actions live in signal_write.
        for name in ("version", "list_contacts", "list_groups", "get_contact"):
            assert name in enum
        for name in ("send_reaction", "create_poll", "send_receipt"):
            assert name not in enum

    def test_write_action_enum_matches_actions(self):
        enum = signal_tool._WRITE_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert sorted(enum) == self.EXPECTED_WRITE
        for name in ("send_reaction", "create_poll", "vote_poll", "send_receipt"):
            assert name in enum

    def test_admin_action_enum_matches_actions(self):
        enum = signal_tool._ADMIN_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert sorted(enum) == self.EXPECTED_ADMIN
        for name in ("block_contact", "unblock_contact", "trust_identity",
                     "update_group", "remote_delete", "pin_message"):
            assert name in enum

    def test_core_write_and_admin_enums_disjoint(self):
        core = set(signal_tool._CORE_SCHEMA["parameters"]["properties"]["action"]["enum"])
        write = set(signal_tool._WRITE_SCHEMA["parameters"]["properties"]["action"]["enum"])
        admin = set(signal_tool._ADMIN_SCHEMA["parameters"]["properties"]["action"]["enum"])
        assert core.isdisjoint(write)
        assert core.isdisjoint(admin)
        assert write.isdisjoint(admin)

    def test_no_destructive_action_exposed(self):
        """Tier-4 method names must not surface as actions in either tool."""
        all_actions = (
            set(signal_tool._CORE_SCHEMA["parameters"]["properties"]["action"]["enum"])
            | set(signal_tool._WRITE_SCHEMA["parameters"]["properties"]["action"]["enum"])
            | set(signal_tool._ADMIN_SCHEMA["parameters"]["properties"]["action"]["enum"])
        )
        # None of these destructive method names should appear as an action.
        for bad in ("register", "unregister", "deleteLocalAccountData",
                    "setPin", "addDevice", "quitGroup"):
            assert bad not in all_actions


# ===========================================================================
# Availability gating (env)
# ===========================================================================
class TestAvailability:
    def test_available_when_configured(self, signal_env):
        assert signal_tool.check_signal_tool_requirements() is True

    def test_unavailable_when_url_unset(self, signal_env):
        signal_env.delenv("SIGNAL_HTTP_URL", raising=False)
        assert signal_tool.check_signal_tool_requirements() is False

    def test_unavailable_when_account_unset(self, signal_env):
        signal_env.delenv("SIGNAL_ACCOUNT", raising=False)
        assert signal_tool.check_signal_tool_requirements() is False

    def test_core_handler_errors_when_unconfigured(self, signal_env, no_allowlist):
        signal_env.delenv("SIGNAL_HTTP_URL", raising=False)
        out = json.loads(signal_tool.signal_core_handler(action="version"))
        assert "error" in out
        # Message should mention configuration / the required env vars.
        assert "configured" in out["error"].lower() or "SIGNAL_HTTP_URL" in out["error"]

    def test_core_handler_does_not_call_rpc_when_unconfigured(
        self, signal_env, no_allowlist, rec
    ):
        signal_env.delenv("SIGNAL_HTTP_URL", raising=False)
        signal_tool.signal_core_handler(action="version")
        assert rec.calls == []


# ===========================================================================
# Unknown action
# ===========================================================================
class TestUnknownAction:
    def test_core_unknown_action_lists_available(self, signal_env, no_allowlist, rec):
        out = json.loads(signal_tool.signal_core_handler(action="frobnicate"))
        assert "error" in out
        assert "frobnicate" in out["error"]
        assert out["available_actions"] == sorted(signal_tool._CORE_ACTIONS)
        assert rec.calls == []

    def test_write_unknown_action_lists_available(self, signal_env, no_allowlist, rec):
        signal_env.setenv("SIGNAL_WRITE_TOOLS", "true")
        out = json.loads(signal_tool.signal_write_handler(action="frobnicate"))
        assert "error" in out
        assert out["available_actions"] == sorted(signal_tool._WRITE_ACTIONS)
        assert rec.calls == []

    def test_admin_unknown_action_lists_available(self, signal_env, no_allowlist, rec):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", "true")
        out = json.loads(signal_tool.signal_admin_handler(action="frobnicate"))
        assert "error" in out
        assert out["available_actions"] == sorted(signal_tool._ADMIN_ACTIONS)
        assert rec.calls == []


# ===========================================================================
# Admin gating
# ===========================================================================
class TestAdminGating:
    def test_admin_requirements_false_without_optin(self, signal_env):
        assert signal_tool._admin_enabled() is False
        assert signal_tool.check_signal_admin_requirements() is False

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "On", "Yes"])
    def test_admin_enabled_truthy_values(self, signal_env, val):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", val)
        assert signal_tool._admin_enabled() is True
        assert signal_tool.check_signal_admin_requirements() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "maybe"])
    def test_admin_disabled_falsy_values(self, signal_env, val):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", val)
        assert signal_tool._admin_enabled() is False

    def test_write_requirements_false_without_optin(self, signal_env):
        assert signal_tool.check_signal_write_requirements() is False

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "On", "Yes"])
    def test_write_enabled_truthy_values(self, signal_env, val):
        signal_env.setenv("SIGNAL_WRITE_TOOLS", val)
        assert signal_tool.check_signal_write_requirements() is True

    def test_write_handler_rejected_without_optin(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_receipt", recipient_id="+155****4444", target_timestamp=1
            )
        )
        assert "error" in out
        assert "SIGNAL_WRITE_TOOLS" in out["error"]
        assert rec.calls == []

    def test_admin_handler_rejected_without_optin(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_admin_handler(
                action="block_contact", recipient_id="+15551112222"
            )
        )
        assert "error" in out
        assert "SIGNAL_ADMIN_TOOLS" in out["error"]
        # Critical: the daemon must NOT be touched when admin is off.
        assert rec.calls == []

    def test_admin_handler_dispatches_when_enabled(self, signal_env, no_allowlist, rec):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", "true")
        out = json.loads(
            signal_tool.signal_admin_handler(
                action="block_contact", recipient_id="+15551112222"
            )
        )
        assert "error" not in out
        assert out["blocked"] is True
        assert rec.method == "block"
        # block routes recipient -> ["+15551112222"]
        assert rec.params["recipient"] == ["+15551112222"]


# ===========================================================================
# Action dispatch — method + params construction
# ===========================================================================
class TestCoreDispatch:
    @pytest.fixture(autouse=True)
    def _enable_write(self, signal_env):
        signal_env.setenv("SIGNAL_WRITE_TOOLS", "true")

    def test_version(self, signal_env, no_allowlist, rec):
        rec.result = "0.13.0"
        out = json.loads(signal_tool.signal_core_handler(action="version"))
        assert rec.method == "version"
        assert rec.params == {}
        assert out["version"] == "0.13.0"

    def test_list_contacts(self, signal_env, no_allowlist, rec):
        rec.result = [{"number": "+1"}, {"number": "+2"}]
        out = json.loads(signal_tool.signal_core_handler(action="list_contacts"))
        assert rec.method == "listContacts"
        assert rec.params["allRecipients"] is True
        assert rec.params["detailed"] is False
        assert out["count"] == 2

    def test_list_contacts_detailed(self, signal_env, no_allowlist, rec):
        rec.result = []
        signal_tool.signal_core_handler(action="list_contacts", detailed=True)
        assert rec.params["detailed"] is True

    def test_list_groups(self, signal_env, no_allowlist, rec):
        rec.result = [{"id": "g1"}]
        out = json.loads(signal_tool.signal_core_handler(action="list_groups"))
        assert rec.method == "listGroups"
        assert out["count"] == 1

    def test_send_reaction_with_group_id(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_reaction",
                group_id="grp==",
                emoji="👍",
                target_author="+15551112222",
                target_timestamp=1700000000000,
            )
        )
        assert rec.method == "sendReaction"
        assert rec.params["groupId"] == "grp=="
        assert "recipient" not in rec.params
        assert rec.params["emoji"] == "👍"
        assert rec.params["targetAuthor"] == "+15551112222"
        assert rec.params["targetTimestamp"] == 1700000000000
        assert out["sent"] is True

    def test_send_reaction_with_recipient_id(self, signal_env, no_allowlist, rec):
        signal_tool.signal_write_handler(
            action="send_reaction",
            recipient_id="+15553334444",
            emoji="🎉",
            target_author="+15551112222",
            target_timestamp=1700000000001,
        )
        assert rec.method == "sendReaction"
        assert rec.params["recipient"] == ["+15553334444"]
        assert "groupId" not in rec.params

    def test_send_reaction_group_beats_recipient(self, signal_env, no_allowlist, rec):
        """When both supplied, group routing wins (per _route)."""
        signal_tool.signal_write_handler(
            action="send_reaction",
            recipient_id="+15553334444",
            group_id="grp==",
            emoji="🔥",
            target_author="+1",
            target_timestamp=1,
        )
        assert rec.params["groupId"] == "grp=="
        assert "recipient" not in rec.params

    def test_send_reaction_remove_flag(self, signal_env, no_allowlist, rec):
        signal_tool.signal_write_handler(
            action="send_reaction",
            recipient_id="+15553334444",
            emoji="👍",
            target_author="+1",
            target_timestamp=2,
            remove_reaction=True,
        )
        assert rec.params["remove"] is True

    def test_create_poll(self, signal_env, no_allowlist, rec):
        opts = ["Pizza", "Tacos", "Sushi"]
        out = json.loads(
            signal_tool.signal_write_handler(
                action="create_poll",
                group_id="grp==",
                question="Dinner?",
                options=opts,
            )
        )
        assert rec.method == "sendPollCreate"
        assert rec.params["question"] == "Dinner?"
        assert rec.params["option"] == opts
        assert rec.params["groupId"] == "grp=="
        assert "noMulti" not in rec.params
        assert out["sent"] is True

    def test_create_poll_single_vote(self, signal_env, no_allowlist, rec):
        signal_tool.signal_write_handler(
            action="create_poll",
            recipient_id="+15553334444",
            question="Q",
            options=["a", "b"],
            single_vote=True,
        )
        assert rec.params["noMulti"] is True
        assert rec.params["recipient"] == ["+15553334444"]

    def test_send_receipt_defaults_type_read(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_receipt",
                recipient_id="+15553334444",
                target_timestamp=1700000000002,
            )
        )
        assert rec.method == "sendReceipt"
        # recipient is a plain string here (not array) per _send_receipt.
        assert rec.params["recipient"] == "+15553334444"
        assert rec.params["targetTimestamp"] == [1700000000002]
        assert isinstance(rec.params["targetTimestamp"], list)
        assert rec.params["type"] == "read"
        assert out["sent"] is True

    def test_send_receipt_explicit_type(self, signal_env, no_allowlist, rec):
        signal_tool.signal_write_handler(
            action="send_receipt",
            recipient_id="+15553334444",
            target_timestamp=42,
            receipt_type="viewed",
        )
        assert rec.params["type"] == "viewed"

    def test_get_contact(self, signal_env, no_allowlist, rec):
        rec.result = {"name": "Alice"}
        out = json.loads(
            signal_tool.signal_core_handler(
                action="get_contact", recipient_id="+15553334444"
            )
        )
        assert rec.method == "getContact"
        assert rec.params == {"contactAddress": "+15553334444"}
        assert out["contact"] == {"name": "Alice"}

    def test_get_user_status_recipient_array(self, signal_env, no_allowlist, rec):
        signal_tool.signal_core_handler(
            action="get_user_status", recipient_id="+15553334444"
        )
        assert rec.method == "getUserStatus"
        assert rec.params == {"recipient": ["+15553334444"]}

    def test_vote_poll(self, signal_env, no_allowlist, rec):
        signal_tool.signal_write_handler(
            action="vote_poll",
            group_id="grp==",
            poll_author="+1",
            poll_timestamp=123,
            vote_count=1,
            vote_options=[0, 2],
        )
        assert rec.method == "sendPollVote"
        assert rec.params["pollAuthor"] == "+1"
        assert rec.params["pollTimestamp"] == 123
        assert rec.params["voteCount"] == 1
        assert rec.params["option"] == [0, 2]
        assert rec.params["groupId"] == "grp=="


class TestAdminDispatch:
    @pytest.fixture(autouse=True)
    def _enable_admin(self, signal_env):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", "true")

    def test_block_contact(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(
            action="block_contact", recipient_id="+15551112222"
        )
        assert rec.method == "block"
        assert rec.params == {"recipient": ["+15551112222"]}

    def test_block_group(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(action="block_contact", group_id="grp==")
        assert rec.method == "block"
        assert rec.params == {"groupId": "grp=="}

    def test_trust_identity(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(
            action="trust_identity", recipient_id="+15551112222"
        )
        assert rec.method == "trust"
        assert rec.params["recipient"] == "+15551112222"
        assert rec.params["trustAllKnownKeys"] is True

    def test_update_group(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(
            action="update_group",
            group_id="grp==",
            name="New Name",
            members=["+1", "+2"],
        )
        assert rec.method == "updateGroup"
        assert rec.params["groupId"] == "grp=="
        assert rec.params["name"] == "New Name"
        assert rec.params["member"] == ["+1", "+2"]

    def test_remote_delete(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(
            action="remote_delete",
            recipient_id="+15553334444",
            target_timestamp=987654321,
        )
        assert rec.method == "remoteDelete"
        assert rec.params["targetTimestamp"] == 987654321
        assert rec.params["recipient"] == ["+15553334444"]

    def test_pin_message(self, no_allowlist, rec):
        signal_tool.signal_admin_handler(
            action="pin_message",
            group_id="grp==",
            target_author="+1",
            target_timestamp=555,
        )
        assert rec.method == "sendPinMessage"
        assert rec.params["targetAuthor"] == "+1"
        assert rec.params["targetTimestamp"] == 555
        assert rec.params["pinDuration"] == -1  # default permanent


# ===========================================================================
# Required-param validation
# ===========================================================================
class TestRequiredParams:
    @pytest.fixture(autouse=True)
    def _enable_write(self, signal_env):
        signal_env.setenv("SIGNAL_WRITE_TOOLS", "true")

    def test_create_poll_missing_options(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_write_handler(
                action="create_poll", group_id="grp==", question="Q"
            )
        )
        assert "error" in out
        assert "options" in out["error"]
        assert rec.calls == []

    def test_get_sticker_missing_id(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_core_handler(action="get_sticker", pack_id="pack1")
        )
        assert "error" in out
        assert "sticker_id" in out["error"]
        assert rec.calls == []

    def test_send_reaction_missing_target_timestamp(self, signal_env, no_allowlist, rec):
        """target_timestamp defaults to 0, which is falsy, so the missing-param
        check (``not kwargs.get(p)``) treats it as absent and errors.

        This documents the actual behavior: a legitimately-zero timestamp would
        also be rejected, but Signal timestamps are ms-since-epoch and never 0.
        """
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_reaction",
                recipient_id="+15553334444",
                emoji="👍",
                target_author="+1",
                # target_timestamp omitted -> default 0 -> treated as missing
            )
        )
        assert "error" in out
        assert "target_timestamp" in out["error"]
        assert rec.calls == []

    def test_send_reaction_zero_target_timestamp_also_rejected(
        self, signal_env, no_allowlist, rec
    ):
        """Explicit target_timestamp=0 is rejected too (0 is falsy)."""
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_reaction",
                recipient_id="+15553334444",
                emoji="👍",
                target_author="+1",
                target_timestamp=0,
            )
        )
        assert "error" in out
        assert "target_timestamp" in out["error"]
        assert rec.calls == []

    def test_get_contact_missing_recipient(self, signal_env, no_allowlist, rec):
        out = json.loads(signal_tool.signal_core_handler(action="get_contact"))
        assert "error" in out
        assert "recipient_id" in out["error"]
        assert rec.calls == []


# ===========================================================================
# Destination requirement
# ===========================================================================
class TestDestinationRequirement:
    @pytest.fixture(autouse=True)
    def _enable_write(self, signal_env):
        signal_env.setenv("SIGNAL_WRITE_TOOLS", "true")

    def test_send_reaction_no_destination(self, signal_env, no_allowlist, rec):
        """All required params present but neither recipient_id nor group_id."""
        out = json.loads(
            signal_tool.signal_write_handler(
                action="send_reaction",
                emoji="👍",
                target_author="+1",
                target_timestamp=123,
            )
        )
        assert "error" in out
        assert "recipient_id" in out["error"]
        assert "group_id" in out["error"]
        assert rec.calls == []

    def test_create_poll_no_destination(self, signal_env, no_allowlist, rec):
        out = json.loads(
            signal_tool.signal_write_handler(
                action="create_poll", question="Q", options=["a", "b"]
            )
        )
        assert "error" in out
        assert "recipient_id" in out["error"] and "group_id" in out["error"]
        assert rec.calls == []


# ===========================================================================
# Error serialization + phone redaction
# ===========================================================================
class TestErrorSerialization:
    def test_signal_rpc_error_is_redacted(self, signal_env, no_allowlist, monkeypatch):
        err = signal_rpc.SignalRPCError(
            {"code": -1, "message": "failed for +15551234567"}
        )

        def boom(method, params):
            raise err

        monkeypatch.setattr("tools.signal_tool._call", boom)
        out = json.loads(
            signal_tool.signal_core_handler(action="list_contacts")
        )
        assert "error" in out
        assert "+155****4567" in out["error"]
        assert "+15551234567" not in out["error"]
        # code is propagated through tool_error(..., code=e.code)
        assert out["code"] == -1

    def test_method_not_allowed_error_serialized(
        self, signal_env, no_allowlist, monkeypatch
    ):
        def boom(method, params):
            raise signal_rpc.SignalMethodNotAllowed("method 'foo' is not permitted")

        monkeypatch.setattr("tools.signal_tool._call", boom)
        out = json.loads(signal_tool.signal_core_handler(action="version"))
        assert "error" in out
        assert "not permitted" in out["error"]

    def test_generic_exception_serialized_and_redacted(
        self, signal_env, no_allowlist, monkeypatch
    ):
        def boom(method, params):
            raise RuntimeError("boom for +15551234567")

        monkeypatch.setattr("tools.signal_tool._call", boom)
        out = json.loads(signal_tool.signal_core_handler(action="version"))
        assert "error" in out
        assert "RuntimeError" in out["error"]
        assert "+155****4567" in out["error"]
        assert "+15551234567" not in out["error"]

    def test_redact_text_two_numbers(self):
        text = "from +15551234567 to +447911123456 now"
        redacted = signal_tool._redact_text(text)
        assert "+15551234567" not in redacted
        assert "+447911123456" not in redacted
        # First: +155 ... 4567 ; second: +447 ... 3456
        assert "+155****4567" in redacted
        assert "+447****3456" in redacted
        # Surrounding text is preserved.
        assert redacted.startswith("from ")
        assert redacted.endswith(" now")

    def test_redact_text_no_numbers_unchanged(self):
        assert signal_tool._redact_text("nothing to redact here") == \
            "nothing to redact here"


# ===========================================================================
# Config allowlist (signal.actions)
# ===========================================================================
class TestConfigAllowlist:
    def test_disabled_action_rejected(self, signal_env, monkeypatch, rec):
        monkeypatch.setattr(
            signal_tool, "_load_allowed_actions_config", lambda: ["list_contacts"]
        )
        out = json.loads(signal_tool.signal_core_handler(action="list_groups"))
        assert "error" in out
        assert "list_groups" in out["error"]
        assert "disabled" in out["error"].lower() or "signal.actions" in out["error"]
        assert rec.calls == []

    def test_allowed_action_dispatches(self, signal_env, monkeypatch, rec):
        rec.result = [{"number": "+1"}]
        monkeypatch.setattr(
            signal_tool, "_load_allowed_actions_config", lambda: ["list_contacts"]
        )
        out = json.loads(signal_tool.signal_core_handler(action="list_contacts"))
        assert "error" not in out
        assert rec.method == "listContacts"
        assert out["count"] == 1

    def test_empty_allowlist_rejects_everything(self, signal_env, monkeypatch, rec):
        monkeypatch.setattr(
            signal_tool, "_load_allowed_actions_config", lambda: []
        )
        out = json.loads(signal_tool.signal_core_handler(action="list_contacts"))
        assert "error" in out
        assert rec.calls == []

    def test_none_allowlist_allows_all(self, signal_env, monkeypatch, rec):
        monkeypatch.setattr(signal_tool, "_load_allowed_actions_config", lambda: None)
        signal_tool.signal_core_handler(action="list_groups")
        assert rec.method == "listGroups"


class TestAvatarCaching:
    """get_avatar decodes base64 media and caches it under the Hermes home."""

    def test_get_avatar_caches_to_hermes_dir(
        self, signal_env, no_allowlist, monkeypatch, tmp_path
    ):
        import base64 as _b64
        png = b"\x89PNG\r\n\x1a\n" + b"fakepngbody"
        b64 = _b64.b64encode(png).decode()

        # _call returns a base64 string (as signal-cli getAvatar may).
        monkeypatch.setattr("tools.signal_tool._call", lambda method, params: b64)
        # Redirect the cache dir into a tmp path so the test is hermetic and
        # only ever writes under the (faked) Hermes home.
        cache_dir = tmp_path / "cache" / "signal"
        monkeypatch.setattr(
            "hermes_constants.get_hermes_dir", lambda *a, **k: cache_dir
        )

        out = json.loads(
            signal_tool.signal_core_handler(action="get_avatar", recipient_id="+15551112222")
        )
        assert "error" not in out
        avatar = out["avatar"]
        assert "path" in avatar and avatar["bytes"] == len(png)
        # File was written under the faked Hermes cache dir with the PNG bytes.
        from pathlib import Path
        written = Path(avatar["path"])
        assert written.parent == cache_dir
        assert written.read_bytes() == png

    def test_get_avatar_requires_destination(self, signal_env, no_allowlist, rec):
        out = json.loads(signal_tool.signal_core_handler(action="get_avatar"))
        assert "error" in out
        assert rec.calls == []


class TestAdminConfirmation:
    """Tier-3 admin actions require interactive confirmation (default on)."""

    def _admin_env(self, signal_env, monkeypatch):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", "true")
        signal_env.setenv("SIGNAL_ADMIN_REQUIRE_CONFIRM", "true")  # force confirmation on
        monkeypatch.setattr("tools.approval.is_current_session_yolo_enabled", lambda: False)

    def test_denied_blocks_action(self, signal_env, no_allowlist, monkeypatch, rec):
        self._admin_env(signal_env, monkeypatch)
        monkeypatch.setattr("tools.approval.prompt_dangerous_approval", lambda *a, **k: "deny")
        out = json.loads(
            signal_tool.signal_admin_handler(action="block_contact", recipient_id="+15551112222")
        )
        assert "error" in out and "not executed" in out["error"]
        assert rec.calls == []  # no RPC was issued

    def test_approval_allows_action(self, signal_env, no_allowlist, monkeypatch, rec):
        self._admin_env(signal_env, monkeypatch)
        monkeypatch.setattr("tools.approval.prompt_dangerous_approval", lambda *a, **k: "once")
        out = json.loads(
            signal_tool.signal_admin_handler(action="block_contact", recipient_id="+15551112222")
        )
        assert "error" not in out
        assert rec.method == "block"

    def test_confirm_skipped_when_disabled(self, signal_env, no_allowlist, monkeypatch, rec):
        self._admin_env(signal_env, monkeypatch)
        signal_env.setenv("SIGNAL_ADMIN_REQUIRE_CONFIRM", "false")

        def _boom(*a, **k):
            raise AssertionError("prompt must not be called when confirmation disabled")

        monkeypatch.setattr("tools.approval.prompt_dangerous_approval", _boom)
        out = json.loads(
            signal_tool.signal_admin_handler(action="unblock_contact", recipient_id="+15551112222")
        )
        assert "error" not in out
        assert rec.method == "unblock"

    def test_confirm_skipped_in_yolo(self, signal_env, no_allowlist, monkeypatch, rec):
        signal_env.setenv("SIGNAL_ADMIN_TOOLS", "true")
        signal_env.setenv("SIGNAL_ADMIN_REQUIRE_CONFIRM", "true")  # confirmation on...
        monkeypatch.setattr("tools.approval.is_current_session_yolo_enabled", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("prompt must not be called in yolo mode")

        monkeypatch.setattr("tools.approval.prompt_dangerous_approval", _boom)
        out = json.loads(
            signal_tool.signal_admin_handler(action="block_contact", recipient_id="+15551112222")
        )
        assert "error" not in out
        assert rec.method == "block"

    def test_confirmation_failure_is_fail_closed(self, signal_env, no_allowlist, monkeypatch, rec):
        self._admin_env(signal_env, monkeypatch)

        def _raise(*a, **k):
            raise RuntimeError("approval backend down")

        monkeypatch.setattr("tools.approval.prompt_dangerous_approval", _raise)
        out = json.loads(
            signal_tool.signal_admin_handler(action="block_contact", recipient_id="+15551112222")
        )
        assert "error" in out and "not executed" in out["error"]
        assert rec.calls == []
