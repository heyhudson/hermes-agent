"""Signal RPC capability registry and safe client for signal-cli JSON-RPC.

This module is the single source of truth for *which* signal-cli JSON-RPC
methods Hermes is permitted to invoke, classified by risk tier, with a hard
safety valve (:func:`assert_method_sendable`) that prevents account-destructive
or unrecognized methods from ever reaching the signal-cli daemon.

Design (mirrors the threat-tiering used elsewhere in Hermes):

  - **read**         Tier 1: safe read-only introspection (list/get).
  - **side_effect**  Tier 2: reversible messaging side-effects (send, react,
                     receipts, polls). Destination is supplied explicitly.
  - **admin**        Tier 3: account-state mutation (block, trust, group/contact
                     edits, remote delete, pin). Exposed only via the admin tool
                     when ``SIGNAL_ADMIN_TOOLS=true``.
  - **destructive**  Tier 4: account-destructive (register/unregister/delete
                     local data/PIN/device/number changes/account config).
                     **Never sendable** — classified here purely so denial is
                     explicit and test-covered.

The sendable allowlist is an *allowlist*, not a denylist: any method that is
not classified read/side_effect/admin (including unknown method names and every
Tier-4 method) is rejected by :func:`assert_method_sendable` before any network
call. This means a bug or injection in the tool layer cannot smuggle a
destructive method through to the daemon.

Two consumers:
  - ``gateway/platforms/signal.py`` imports :func:`assert_method_sendable` and
    calls it at the top of ``SignalAdapter._rpc()`` (defense in depth).
  - ``tools/signal_tool.py`` imports the registry + :func:`call_rpc` to expose a
    curated, tier-gated set of capabilities to the agent.

IMPORTANT: this module must NOT import ``gateway.platforms.signal`` — the
dependency is one-way (signal.py -> signal_rpc.py) to avoid import cycles.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk tiers
# ---------------------------------------------------------------------------
TIER_READ = "read"
TIER_SIDE_EFFECT = "side_effect"
TIER_ADMIN = "admin"
TIER_DESTRUCTIVE = "destructive"

#: Tiers whose methods Hermes is allowed to actually send to the daemon.
_SENDABLE_TIERS = frozenset({TIER_READ, TIER_SIDE_EFFECT, TIER_ADMIN})


@dataclass(frozen=True)
class SignalCapability:
    """Static description of one signal-cli JSON-RPC method.

    ``required`` lists user-facing JSON-RPC param names that must be present
    (excluding ``account``, which :func:`call_rpc` injects). ``needs_destination``
    means at least one of ``recipient`` / ``groupId`` must be supplied.
    ``exposable`` controls whether the method may back an agent-tool action
    (some sendable methods, e.g. ``send``/``getAttachment``, are adapter-internal
    and never exposed as discrete tool actions).
    """

    method: str
    category: str
    risk_tier: str
    required: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()
    needs_destination: bool = False
    exposable: bool = True
    summary: str = ""

    @property
    def requires_admin(self) -> bool:
        return self.risk_tier == TIER_ADMIN

    @property
    def sendable(self) -> bool:
        return self.risk_tier in _SENDABLE_TIERS


def _cap(method, category, tier, **kw) -> Tuple[str, SignalCapability]:
    return method, SignalCapability(method=method, category=category, risk_tier=tier, **kw)


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------
# Routing note: signal-cli accepts ``recipient`` as a single value or an array,
# and ``groupId`` as a string. The tool layer normalizes a friendly
# recipient_id/group_id into the correct shape before calling call_rpc().

SIGNAL_RPC_REGISTRY: Dict[str, SignalCapability] = dict([
    # --- Tier 1: read-only ------------------------------------------------
    _cap("listContacts", "contact", TIER_READ,
         optional=("recipient", "allRecipients", "blocked", "name", "detailed"),
         summary="List known contacts."),
    _cap("listGroups", "group", TIER_READ,
         optional=("groupId", "detailed"),
         summary="List groups the account is in."),
    _cap("listIdentities", "identity", TIER_READ,
         optional=("number",),
         summary="List known identities / safety numbers."),
    _cap("listDevices", "device", TIER_READ,
         summary="List linked devices."),
    _cap("getUserStatus", "contact", TIER_READ,
         required=("recipient",), optional=("username",),
         summary="Check whether numbers are registered on Signal."),
    _cap("getContact", "contact", TIER_READ,
         required=("contactAddress",),
         summary="Look up a single contact's profile name."),
    _cap("getAvatar", "contact", TIER_READ,
         optional=("contact", "profile", "groupId"),
         summary="Fetch a contact/profile/group avatar (base64)."),
    _cap("getSticker", "sticker", TIER_READ,
         required=("packId", "stickerId"),
         summary="Fetch a single sticker image (base64)."),
    _cap("listStickerPacks", "sticker", TIER_READ,
         summary="List installed sticker packs."),
    _cap("listCalls", "call", TIER_READ,
         summary="List call history."),
    _cap("version", "account", TIER_READ,
         summary="signal-cli version info."),
    # Adapter-internal read (not a discrete tool action):
    _cap("getAttachment", "message", TIER_READ,
         required=("id",), exposable=False,
         summary="Download an inbound attachment (adapter-internal)."),

    # --- Tier 2: reversible messaging side-effects ------------------------
    # send / sendTyping are adapter-internal (driven by the gateway send path),
    # kept sendable but not exposed as discrete tool actions.
    _cap("send", "message", TIER_SIDE_EFFECT,
         needs_destination=True, exposable=False,
         summary="Send a message (adapter-internal)."),
    _cap("sendTyping", "message", TIER_SIDE_EFFECT,
         needs_destination=True, exposable=False,
         summary="Typing indicator (adapter-internal)."),
    _cap("sendReaction", "message", TIER_SIDE_EFFECT,
         required=("emoji", "targetAuthor", "targetTimestamp"),
         needs_destination=True,
         summary="React to a message with an emoji."),
    _cap("sendReceipt", "message", TIER_SIDE_EFFECT,
         required=("recipient", "targetTimestamp"), optional=("type",),
         summary="Send a read/viewed receipt for a message."),
    _cap("sendPollCreate", "message", TIER_SIDE_EFFECT,
         required=("question", "option"), needs_destination=True,
         optional=("noMulti",),
         summary="Create a poll."),
    _cap("sendPollVote", "message", TIER_SIDE_EFFECT,
         required=("pollAuthor", "pollTimestamp", "voteCount"),
         needs_destination=True, optional=("option",),
         summary="Vote in a poll."),
    _cap("sendMessageRequestResponse", "contact", TIER_SIDE_EFFECT,
         required=("type",), needs_destination=True,
         summary="Accept or delete an incoming message request."),

    # --- Tier 3: admin / account-state mutation (gated) -------------------
    _cap("block", "contact", TIER_ADMIN,
         needs_destination=True,
         summary="Block a contact or group."),
    _cap("unblock", "contact", TIER_ADMIN,
         needs_destination=True,
         summary="Unblock a contact or group."),
    _cap("trust", "identity", TIER_ADMIN,
         required=("recipient",), optional=("trustAllKnownKeys", "verifiedSafetyNumber"),
         summary="Trust a contact's identity/safety number."),
    _cap("updateContact", "contact", TIER_ADMIN,
         required=("recipient",),
         optional=("name", "givenName", "familyName", "note", "expiration"),
         summary="Edit a contact's local name/note."),
    _cap("updateGroup", "group", TIER_ADMIN,
         required=("groupId",),
         optional=("name", "description", "member", "removeMember", "admin",
                   "removeAdmin", "expiration"),
         summary="Edit group name/description/members (where permitted)."),
    _cap("remoteDelete", "message", TIER_ADMIN,
         required=("targetTimestamp",), needs_destination=True,
         summary="Remote-delete a message the bot previously sent."),
    _cap("sendPinMessage", "message", TIER_ADMIN,
         required=("targetAuthor", "targetTimestamp"), needs_destination=True,
         optional=("pinDuration",),
         summary="Pin a message."),
    _cap("sendUnpinMessage", "message", TIER_ADMIN,
         required=("targetAuthor", "targetTimestamp"), needs_destination=True,
         summary="Unpin a message."),
    _cap("sendPollTerminate", "message", TIER_ADMIN,
         required=("pollTimestamp",), needs_destination=True,
         summary="Terminate a poll."),

    # --- Tier 4: account-destructive — NEVER sendable ---------------------
    # Listed explicitly so denial is documented and test-covered. They are not
    # in _SENDABLE_TIERS, so assert_method_sendable() rejects them.
    _cap("register", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Register the phone number on Signal."),
    _cap("verify", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Verify a registration code."),
    _cap("unregister", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Unregister the account from Signal."),
    _cap("deleteLocalAccountData", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Irreversibly delete local account data."),
    _cap("setPin", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Set the registration lock PIN."),
    _cap("removePin", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Remove the registration lock PIN."),
    _cap("addDevice", "device", TIER_DESTRUCTIVE, exposable=False,
         summary="Link a new device."),
    _cap("removeDevice", "device", TIER_DESTRUCTIVE, exposable=False,
         summary="Remove a linked device."),
    _cap("link", "device", TIER_DESTRUCTIVE, exposable=False,
         summary="Link this client as a secondary device."),
    _cap("startChangeNumber", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Begin a phone-number change."),
    _cap("finishChangeNumber", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Complete a phone-number change."),
    _cap("updateAccount", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Update account-level settings."),
    _cap("updateConfiguration", "account", TIER_DESTRUCTIVE, exposable=False,
         summary="Update account-wide privacy configuration."),
    _cap("updateDevice", "device", TIER_DESTRUCTIVE, exposable=False,
         summary="Rename a linked device."),
    _cap("joinGroup", "group", TIER_DESTRUCTIVE, exposable=False,
         summary="Join a group via invitation link."),
    _cap("quitGroup", "group", TIER_DESTRUCTIVE, exposable=False,
         summary="Leave (or delete) a group."),
])

# Methods deliberately ABSENT from the registry (and therefore unsendable, by
# the fail-closed allowlist) because Hermes has no use for them and several are
# risky: acceptCall/startCall/hangupCall/rejectCall (call control),
# addStickerPack/uploadStickerPack (mutate sticker state), sendAdminDelete
# (delete other members' messages), removeContact (delete contact data),
# sendContacts/sendSyncRequest/sendPaymentNotification, updateProfile,
# submitRateLimitChallenge, daemon/receive/jsonRpc (transport). To expose any of
# these in future, add an explicit registry entry with the correct risk tier —
# never rely on it "just working" because signal-cli supports it.

#: Methods Hermes is permitted to send to the daemon.
SENDABLE_METHODS = frozenset(
    m for m, cap in SIGNAL_RPC_REGISTRY.items() if cap.sendable
)

#: Explicit account-destructive denylist (subset of the registry). Kept as a
#: named export so callers/tests can assert the full set is unreachable.
DESTRUCTIVE_METHODS = frozenset(
    m for m, cap in SIGNAL_RPC_REGISTRY.items() if cap.risk_tier == TIER_DESTRUCTIVE
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class SignalMethodNotAllowed(ValueError):
    """Raised when a signal-cli method is not on the sendable allowlist.

    Subclasses ValueError so existing ``except ValueError`` paths keep working.
    """


class SignalRPCError(Exception):
    """Raised by :func:`call_rpc` when signal-cli returns a JSON-RPC error."""

    def __init__(self, error: Any):
        self.error = error
        if isinstance(error, dict):
            self.code = error.get("code")
            message = error.get("message") or json.dumps(error)
        else:
            self.code = None
            message = str(error)
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Safety valve + validation
# ---------------------------------------------------------------------------
def is_sendable(method: str) -> bool:
    """Return True if *method* may be sent to the signal-cli daemon."""
    return method in SENDABLE_METHODS


def assert_method_sendable(method: str, *, allow_admin: bool = False) -> None:
    """Raise :class:`SignalMethodNotAllowed` unless *method* may be sent.

    This is the hard safety valve: account-destructive (Tier 4) methods and any
    unrecognized method name are rejected *before* any network call. In addition,
    Tier-3 admin methods are rejected unless ``allow_admin=True`` — defense in
    depth so the low-level client refuses an admin method even if a read/write
    caller accidentally routes one to it. Only the admin tool path passes
    ``allow_admin=True``.
    """
    cap = SIGNAL_RPC_REGISTRY.get(method)
    if method not in SENDABLE_METHODS:
        tier = cap.risk_tier if cap else "unknown"
        raise SignalMethodNotAllowed(
            f"signal-cli method {method!r} is not permitted "
            f"(risk tier: {tier}). Account-destructive and unrecognized "
            f"methods are blocked and can never reach the daemon."
        )
    if cap is not None and cap.risk_tier == TIER_ADMIN and not allow_admin:
        raise SignalMethodNotAllowed(
            f"signal-cli method {method!r} is a Tier-3 admin action and "
            f"requires explicit admin context (allow_admin=True)."
        )


def validate_params(method: str, params: Dict[str, Any]) -> Optional[str]:
    """Return an error string if required params are missing, else ``None``.

    *params* are the user-supplied JSON-RPC params (``account`` is injected by
    :func:`call_rpc` and is not validated here). ``0`` and ``False`` count as
    present; only ``None`` / ``""`` / ``[]`` count as missing.
    """
    cap = SIGNAL_RPC_REGISTRY.get(method)
    if cap is None:
        return f"unknown signal method: {method}"

    def _missing(value: Any) -> bool:
        return value is None or value == "" or value == []

    missing = [p for p in cap.required if _missing(params.get(p))]
    if missing:
        return f"missing required parameter(s) for {method}: {', '.join(missing)}"

    if cap.needs_destination and _missing(params.get("recipient")) and _missing(params.get("groupId")):
        return f"{method} requires a destination: provide recipient or groupId"

    return None


# ---------------------------------------------------------------------------
# Synchronous JSON-RPC client (used by the tool layer)
# ---------------------------------------------------------------------------
def call_rpc(
    http_url: str,
    account: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout: int = 30,
    opener: Any = None,
    allow_admin: bool = False,
) -> Any:
    """Invoke a signal-cli JSON-RPC method over HTTP and return its result.

    Enforces :func:`assert_method_sendable` and :func:`validate_params` before
    making any network call. ``account`` is injected automatically (except for
    ``version``, which is not account-scoped).

    Raises:
        SignalMethodNotAllowed: method is not on the sendable allowlist, or is
            a Tier-3 admin method and ``allow_admin`` is False.
        ValueError: required params are missing.
        SignalRPCError: signal-cli returned a JSON-RPC error.
        urllib.error.URLError / OSError: transport failure.
    """
    params = dict(params or {})
    assert_method_sendable(method, allow_admin=allow_admin)
    err = validate_params(method, params)
    if err:
        raise ValueError(err)

    if method == "version":
        rpc_params: Dict[str, Any] = params
    else:
        rpc_params = {"account": account, **params}

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": rpc_params,
        "id": f"{method}-tool",
    }
    url = f"{http_url.rstrip('/')}/api/v1/rpc"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    _open = opener if opener is not None else urllib.request.urlopen
    with _open(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    data = json.loads(body)

    if isinstance(data, dict) and data.get("error"):
        raise SignalRPCError(data["error"])
    return data.get("result") if isinstance(data, dict) else data
