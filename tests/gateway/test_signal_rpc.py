"""Tests for the Signal RPC capability registry and safe JSON-RPC client.

Covers ``gateway/platforms/signal_rpc.py``:

  - the risk-tiered :data:`SIGNAL_RPC_REGISTRY` and derived sets
    (:data:`SENDABLE_METHODS`, :data:`DESTRUCTIVE_METHODS`),
  - the hard safety valve :func:`assert_method_sendable` (Tier-4 and unknown
    methods can never reach the daemon),
  - :func:`validate_params` required/destination handling,
  - :func:`call_rpc` payload construction, account injection, result parsing,
    and JSON-RPC error surfacing.

All HTTP is mocked via an injected ``opener`` — no real network or Signal
account is touched.
"""

import json

import pytest

from gateway.platforms.signal_rpc import (
    DESTRUCTIVE_METHODS,
    SENDABLE_METHODS,
    SIGNAL_RPC_REGISTRY,
    TIER_ADMIN,
    TIER_DESTRUCTIVE,
    TIER_READ,
    TIER_SIDE_EFFECT,
    SignalMethodNotAllowed,
    SignalRPCError,
    assert_method_sendable,
    call_rpc,
    is_sendable,
    validate_params,
)


# ---------------------------------------------------------------------------
# HTTP mocking helpers (per the task spec)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class _FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        return _FakeResp(self.payload)


HTTP_URL = "http://localhost:8080"
ACCOUNT = "+15551234567"

#: The full Tier-4 account-destructive set the user requires to be blocked.
EXPECTED_DESTRUCTIVE = {
    "register",
    "unregister",
    "deleteLocalAccountData",
    "setPin",
    "removePin",
    "addDevice",
    "removeDevice",
    "link",
    "startChangeNumber",
    "finishChangeNumber",
    "updateAccount",
    "updateConfiguration",
    "updateDevice",
    "joinGroup",
    "quitGroup",
}


# ===========================================================================
# Tier constants & registry shape
# ===========================================================================
class TestRegistryShape:
    def test_tier_constants(self):
        assert TIER_READ == "read"
        assert TIER_SIDE_EFFECT == "side_effect"
        assert TIER_ADMIN == "admin"
        assert TIER_DESTRUCTIVE == "destructive"

    def test_registry_keys_match_method_attr(self):
        for name, cap in SIGNAL_RPC_REGISTRY.items():
            assert cap.method == name

    def test_sendable_excludes_destructive(self):
        # SENDABLE and DESTRUCTIVE are disjoint.
        assert SENDABLE_METHODS.isdisjoint(DESTRUCTIVE_METHODS)

    def test_sendable_is_union_of_safe_tiers(self):
        expected = {
            m
            for m, cap in SIGNAL_RPC_REGISTRY.items()
            if cap.risk_tier in (TIER_READ, TIER_SIDE_EFFECT, TIER_ADMIN)
        }
        assert set(SENDABLE_METHODS) == expected


# ===========================================================================
# Destructive (Tier-4) denial
# ===========================================================================
class TestDestructiveMethods:
    def test_expected_tier4_set_present(self):
        """The user's full Tier-4 set must be classified destructive."""
        assert EXPECTED_DESTRUCTIVE <= set(DESTRUCTIVE_METHODS)

    @pytest.mark.parametrize("method", sorted(DESTRUCTIVE_METHODS))
    def test_destructive_not_sendable(self, method):
        assert method not in SENDABLE_METHODS
        assert is_sendable(method) is False

    @pytest.mark.parametrize("method", sorted(DESTRUCTIVE_METHODS))
    def test_destructive_assert_raises(self, method):
        with pytest.raises(SignalMethodNotAllowed):
            assert_method_sendable(method)

    def test_signal_method_not_allowed_is_value_error(self):
        assert issubclass(SignalMethodNotAllowed, ValueError)

    def test_unknown_method_rejected(self):
        with pytest.raises(SignalMethodNotAllowed):
            assert_method_sendable("evilMethod")

    def test_unknown_method_is_not_sendable(self):
        assert is_sendable("evilMethod") is False


# ===========================================================================
# Sendable methods (adapter-internal + exposed)
# ===========================================================================
class TestSendableMethods:
    @pytest.mark.parametrize(
        "method",
        ["send", "sendReaction", "sendTyping", "getAttachment", "getContact", "listContacts"],
    )
    def test_adapter_methods_sendable(self, method):
        assert is_sendable(method) is True
        assert method in SENDABLE_METHODS
        # assert_method_sendable returns None (does not raise).
        assert assert_method_sendable(method) is None

    def test_admin_exposable_methods_require_admin(self):
        """Every admin-tier method that is exposable reports requires_admin."""
        admin_exposable = [
            cap
            for cap in SIGNAL_RPC_REGISTRY.values()
            if cap.risk_tier == TIER_ADMIN and cap.exposable
        ]
        # Sanity: the registry actually has some.
        assert admin_exposable
        for cap in admin_exposable:
            assert cap.requires_admin is True

    def test_non_admin_does_not_require_admin(self):
        read_cap = SIGNAL_RPC_REGISTRY["listContacts"]
        side_cap = SIGNAL_RPC_REGISTRY["sendReaction"]
        assert read_cap.requires_admin is False
        assert side_cap.requires_admin is False

    def test_sendable_property(self):
        assert SIGNAL_RPC_REGISTRY["listContacts"].sendable is True
        assert SIGNAL_RPC_REGISTRY["block"].sendable is True
        assert SIGNAL_RPC_REGISTRY["register"].sendable is False


# ===========================================================================
# validate_params
# ===========================================================================
class TestValidateParams:
    def test_unknown_method(self):
        assert validate_params("evilMethod", {}) == "unknown signal method: evilMethod"

    def test_missing_required_mentions_params(self):
        err = validate_params("getSticker", {})
        assert err is not None
        assert "packId" in err
        assert "stickerId" in err

    def test_missing_one_required(self):
        err = validate_params("getSticker", {"packId": "abc"})
        assert err is not None
        assert "stickerId" in err
        assert "packId" not in err

    def test_required_present_ok(self):
        assert validate_params("getSticker", {"packId": "abc", "stickerId": "1"}) is None

    def test_empty_string_counts_as_missing(self):
        err = validate_params("getSticker", {"packId": "", "stickerId": "1"})
        assert err is not None
        assert "packId" in err

    def test_empty_list_counts_as_missing(self):
        err = validate_params("getUserStatus", {"recipient": []})
        assert err is not None
        assert "recipient" in err

    def test_zero_counts_as_present(self):
        # voteCount=0 should be accepted; destination supplied via recipient.
        assert validate_params(
            "sendPollVote",
            {
                "pollAuthor": "x",
                "pollTimestamp": 123,
                "voteCount": 0,
                "recipient": "+15550001111",
            },
        ) is None

    def test_false_counts_as_present(self):
        # A required param whose value is False must not be flagged missing.
        # sendMessageRequestResponse requires "type" and a destination.
        assert validate_params(
            "sendMessageRequestResponse",
            {"type": False, "recipient": "+15550001111"},
        ) is None

    def test_needs_destination_missing(self):
        err = validate_params(
            "sendReaction",
            {"emoji": "👍", "targetAuthor": "+1", "targetTimestamp": 1},
        )
        assert err is not None
        assert "recipient" in err
        assert "groupId" in err

    def test_needs_destination_recipient_ok(self):
        assert validate_params(
            "sendReaction",
            {
                "emoji": "👍",
                "targetAuthor": "+1",
                "targetTimestamp": 1,
                "recipient": "+15550001111",
            },
        ) is None

    def test_needs_destination_group_ok(self):
        assert validate_params(
            "sendReaction",
            {
                "emoji": "👍",
                "targetAuthor": "+1",
                "targetTimestamp": 1,
                "groupId": "grp==",
            },
        ) is None

    def test_no_required_no_destination_method_ok(self):
        assert validate_params("listDevices", {}) is None


# ===========================================================================
# call_rpc — safety valve runs before any HTTP
# ===========================================================================
class TestCallRpcSafetyValve:
    def test_destructive_blocked_before_http(self):
        opener = _FakeOpener({"result": "should-not-happen"})
        with pytest.raises(SignalMethodNotAllowed):
            call_rpc(HTTP_URL, ACCOUNT, "unregister", {}, opener=opener)
        # The opener must NEVER have been called.
        assert opener.calls == []

    def test_unknown_method_blocked_before_http(self):
        opener = _FakeOpener({"result": "nope"})
        with pytest.raises(SignalMethodNotAllowed):
            call_rpc(HTTP_URL, ACCOUNT, "evilMethod", {}, opener=opener)
        assert opener.calls == []

    def test_missing_params_raises_value_error_before_http(self):
        opener = _FakeOpener({"result": "nope"})
        with pytest.raises(ValueError) as exc:
            call_rpc(HTTP_URL, ACCOUNT, "getSticker", {}, opener=opener)
        # A plain ValueError (validation), not the SignalMethodNotAllowed subclass.
        assert not isinstance(exc.value, SignalMethodNotAllowed)
        assert opener.calls == []


# ===========================================================================
# call_rpc — happy path + payload construction
# ===========================================================================
class TestCallRpcHappyPath:
    def _captured_payload(self, opener):
        assert len(opener.calls) == 1
        request = opener.calls[0]
        return json.loads(request.data.decode("utf-8")), request

    def test_payload_and_account_injection(self):
        opener = _FakeOpener({"result": ["alice", "bob"]})
        result = call_rpc(
            HTTP_URL,
            ACCOUNT,
            "listContacts",
            {"detailed": True},
            opener=opener,
        )
        assert result == ["alice", "bob"]

        payload, request = self._captured_payload(opener)
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "listContacts"
        assert payload["id"] == "listContacts-tool"
        # account injected, original params preserved.
        assert payload["params"]["account"] == ACCOUNT
        assert payload["params"]["detailed"] is True

    def test_request_url_and_method(self):
        opener = _FakeOpener({"result": None})
        call_rpc(HTTP_URL, ACCOUNT, "listDevices", {}, opener=opener)
        _, request = self._captured_payload(opener)
        assert request.full_url == "http://localhost:8080/api/v1/rpc"
        assert request.method == "POST"

    def test_trailing_slash_in_url_normalized(self):
        opener = _FakeOpener({"result": None})
        call_rpc("http://localhost:8080/", ACCOUNT, "listDevices", {}, opener=opener)
        _, request = self._captured_payload(opener)
        assert request.full_url == "http://localhost:8080/api/v1/rpc"

    def test_none_params_defaults_to_empty(self):
        opener = _FakeOpener({"result": "ok"})
        call_rpc(HTTP_URL, ACCOUNT, "listDevices", opener=opener)
        payload, _ = self._captured_payload(opener)
        # account injected even with no params provided.
        assert payload["params"] == {"account": ACCOUNT}

    def test_result_returned(self):
        opener = _FakeOpener({"result": {"deviceCount": 2}})
        result = call_rpc(HTTP_URL, ACCOUNT, "listDevices", {}, opener=opener)
        assert result == {"deviceCount": 2}

    def test_missing_result_key_returns_none(self):
        opener = _FakeOpener({"jsonrpc": "2.0", "id": "listDevices-tool"})
        result = call_rpc(HTTP_URL, ACCOUNT, "listDevices", {}, opener=opener)
        assert result is None

    def test_timeout_forwarded(self):
        captured = {}

        class _RecordingOpener(_FakeOpener):
            def __call__(self, request, timeout=None):
                captured["timeout"] = timeout
                return super().__call__(request, timeout=timeout)

        opener = _RecordingOpener({"result": None})
        call_rpc(HTTP_URL, ACCOUNT, "listDevices", {}, timeout=99, opener=opener)
        assert captured["timeout"] == 99


# ===========================================================================
# call_rpc — version is not account-scoped
# ===========================================================================
class TestCallRpcVersion:
    def test_version_does_not_inject_account(self):
        opener = _FakeOpener({"result": {"version": "0.13.0"}})
        result = call_rpc(HTTP_URL, ACCOUNT, "version", {}, opener=opener)
        assert result == {"version": "0.13.0"}

        assert len(opener.calls) == 1
        payload = json.loads(opener.calls[0].data.decode("utf-8"))
        assert payload["method"] == "version"
        assert "account" not in payload["params"]
        assert payload["params"] == {}


# ===========================================================================
# call_rpc — JSON-RPC error surfacing
# ===========================================================================
class TestCallRpcError:
    def test_error_raises_signal_rpc_error(self):
        opener = _FakeOpener({"error": {"code": -1, "message": "boom"}})
        with pytest.raises(SignalRPCError) as exc:
            call_rpc(HTTP_URL, ACCOUNT, "listContacts", {}, opener=opener)
        assert exc.value.code == -1
        assert exc.value.message == "boom"
        # The HTTP call did happen (error came from the daemon response).
        assert len(opener.calls) == 1

    def test_error_str_uses_message(self):
        opener = _FakeOpener({"error": {"code": 7, "message": "nope"}})
        with pytest.raises(SignalRPCError) as exc:
            call_rpc(HTTP_URL, ACCOUNT, "listContacts", {}, opener=opener)
        assert str(exc.value) == "nope"

    def test_error_without_message_falls_back_to_json(self):
        opener = _FakeOpener({"error": {"code": 9}})
        with pytest.raises(SignalRPCError) as exc:
            call_rpc(HTTP_URL, ACCOUNT, "listContacts", {}, opener=opener)
        assert exc.value.code == 9
        # message falls back to a JSON dump of the error dict.
        assert "9" in exc.value.message
