"""Signal control-surface tools for the Hermes agent.

Exposes a curated, tier-gated slice of the signal-cli JSON-RPC surface as two
action-dispatch tools (mirroring ``tools/discord_tool.py``):

  * ``signal``        — Tier 1 read-only + Tier 2 reversible messaging actions.
                        Available whenever Signal is configured.
  * ``signal_admin``  — Tier 3 account-state mutations (block, trust, group /
                        contact edits, remote delete, pin). **Disabled unless
                        ``SIGNAL_ADMIN_TOOLS=true``.**

All method routing goes through :mod:`gateway.platforms.signal_rpc`, whose
allowlist guarantees that account-destructive (Tier 4) and unrecognized methods
can never reach the signal-cli daemon — even via a bug here. Account-destructive
operations (register/unregister/deleteLocalAccountData/PIN/device/number-change/
account-config) are intentionally **not** represented as actions in either tool.

Like the Discord tool, this talks to the signal-cli daemon directly (HTTP
JSON-RPC) using ``SIGNAL_HTTP_URL`` + ``SIGNAL_ACCOUNT`` from the environment;
it does not depend on the running gateway adapter instance.
"""

import base64
import contextvars
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from gateway.platforms import signal_rpc
from gateway.platforms.signal_rpc import SignalMethodNotAllowed, SignalRPCError
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + phone redaction
# ---------------------------------------------------------------------------
def _get_signal_config() -> Tuple[Optional[str], Optional[str]]:
    """Return (http_url, account) from the environment, or (None, None)."""
    http_url = (os.getenv("SIGNAL_HTTP_URL") or "").strip() or None
    account = (os.getenv("SIGNAL_ACCOUNT") or "").strip() or None
    return http_url, account


def check_signal_tool_requirements() -> bool:
    """Signal tools are available only when the daemon URL + account are set."""
    http_url, account = _get_signal_config()
    return bool(http_url and account)


def _admin_enabled() -> bool:
    return os.getenv("SIGNAL_ADMIN_TOOLS", "").strip().lower() in {"true", "1", "yes", "on"}


def check_signal_admin_requirements() -> bool:
    """Admin tool requires Signal configured AND the explicit admin opt-in."""
    return check_signal_tool_requirements() and _admin_enabled()


def _write_enabled() -> bool:
    return os.getenv("SIGNAL_WRITE_TOOLS", "").strip().lower() in {"true", "1", "yes", "on"}


def check_signal_write_requirements() -> bool:
    """Write tool (messaging side-effects) requires Signal configured AND opt-in.

    Side-effect actions (receipts, reactions, polls) can target arbitrary
    recipients, so they are kept off the default toolset and gated behind an
    explicit SIGNAL_WRITE_TOOLS opt-in rather than shipping on by default.
    """
    return check_signal_tool_requirements() and _write_enabled()


# Set True only while dispatching a signal_admin action, so the low-level RPC
# client (signal_rpc.call_rpc, via _call) admits Tier-3 admin methods. Any other
# call path (read/write) leaves this False, so an admin method routed there by a
# bug is rejected at the client — defense in depth beyond the tool gating.
_signal_admin_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "signal_admin_ctx", default=False
)


def _admin_confirm_required() -> bool:
    """Whether Tier-3 admin actions need interactive confirmation (default on)."""
    return os.getenv("SIGNAL_ADMIN_REQUIRE_CONFIRM", "true").strip().lower() not in {
        "false", "0", "no", "off",
    }


def _admin_action_summary(action: str, kwargs: Dict[str, Any]) -> str:
    """One-line human summary of an admin action for the confirmation prompt."""
    bits = []
    for key in ("recipient_id", "group_id", "target_author", "target_timestamp",
                "name", "poll_timestamp"):
        val = kwargs.get(key)
        if val:
            bits.append(f"{key}={val}")
    return f"{action}({', '.join(bits)})"


def _confirm_admin_action(action: str, summary: str) -> Optional[str]:
    """Return ``None`` if the admin action may proceed, else a denial reason.

    Confirmation is on by default (SIGNAL_ADMIN_REQUIRE_CONFIRM); operators can
    disable it for unattended use. Yolo sessions auto-approve. Any failure to
    obtain an explicit approval fails closed (the action is denied). Reuses the
    shared approval primitive so it integrates with CLI/TUI approval UIs.
    """
    if not _admin_confirm_required():
        return None
    try:
        from tools import approval
        if approval.is_current_session_yolo_enabled():
            return None
        choice = approval.prompt_dangerous_approval(
            command=f"signal_admin {summary}",
            description=f"Signal admin action '{action}' (mutates account/contact/group state)",
            allow_permanent=False,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on any approval error
        logger.warning("signal_admin confirmation unavailable: %s", exc)
        return "confirmation mechanism unavailable; admin action denied (fail-closed)"
    if choice == "deny":
        return "user denied the admin action"
    return None


_PHONE_RE = re.compile(r"\+\d{7,15}")


def _redact_text(text: str) -> str:
    """Redact E.164 phone numbers embedded anywhere in *text* for safe output."""
    from gateway.platforms.helpers import redact_phone
    return _PHONE_RE.sub(lambda m: redact_phone(m.group(0)), str(text))


# ---------------------------------------------------------------------------
# Low-level call + media caching
# ---------------------------------------------------------------------------
def _call(method: str, params: Dict[str, Any]) -> Any:
    """Invoke a signal-cli RPC method via the allowlisted client.

    Tier-3 admin methods only go through when an admin action is being
    dispatched (see _signal_admin_ctx); read/write paths cannot send them.
    """
    http_url, account = _get_signal_config()
    return signal_rpc.call_rpc(
        http_url, account, method, params, allow_admin=_signal_admin_ctx.get()
    )


def _route(params: Dict[str, Any], recipient_id: str, group_id: str,
           *, as_array: bool = True) -> Dict[str, Any]:
    """Add Signal destination routing (groupId beats recipient)."""
    if group_id:
        params["groupId"] = group_id
    elif recipient_id:
        params["recipient"] = [recipient_id] if as_array else recipient_id
    return params


def _guess_media_ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _cache_media(result: Any) -> Any:
    """If *result* (or its ``data`` field) is base64 media, cache it under the
    Hermes home and return the local path; otherwise return it unchanged.

    Uses get_hermes_dir() so files only ever land inside the Hermes profile.
    """
    raw = result.get("data") if isinstance(result, dict) else result
    if not isinstance(raw, str):
        return result
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return result  # not base64 (e.g. a plain string / daemon-side path)
    if not data:
        return result
    import hashlib
    from hermes_constants import get_hermes_dir
    cache_dir = get_hermes_dir("cache/signal", "signal_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (hashlib.sha256(data).hexdigest()[:16] + _guess_media_ext(data))
    path.write_bytes(data)
    return {"path": str(path), "bytes": len(data)}


# ---------------------------------------------------------------------------
# Core (Tier 1-2) action implementations
# ---------------------------------------------------------------------------
def _version(**_kw) -> Dict[str, Any]:
    return {"version": _call("version", {})}


def _list_contacts(detailed: bool = False, **_kw) -> Dict[str, Any]:
    res = _call("listContacts", {"allRecipients": True, "detailed": bool(detailed)})
    return {"contacts": res, "count": len(res) if isinstance(res, list) else None}


def _list_groups(detailed: bool = False, **_kw) -> Dict[str, Any]:
    res = _call("listGroups", {"detailed": bool(detailed)})
    return {"groups": res, "count": len(res) if isinstance(res, list) else None}


def _get_group_info(group_id: str = "", **_kw) -> Dict[str, Any]:
    res = _call("listGroups", {"groupId": group_id, "detailed": True})
    if isinstance(res, list):
        res = res[0] if res else None
    return {"group": res}


def _get_contact(recipient_id: str = "", **_kw) -> Dict[str, Any]:
    return {"contact": _call("getContact", {"contactAddress": recipient_id})}


def _get_user_status(recipient_id: str = "", **_kw) -> Dict[str, Any]:
    return {"status": _call("getUserStatus", {"recipient": [recipient_id]})}


def _list_identities(number: str = "", **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if number:
        params["number"] = number
    return {"identities": _call("listIdentities", params)}


def _list_devices(**_kw) -> Dict[str, Any]:
    return {"devices": _call("listDevices", {})}


def _get_avatar(recipient_id: str = "", group_id: str = "", **_kw) -> Dict[str, Any]:
    if group_id:
        params = {"groupId": group_id}
    elif recipient_id:
        params = {"contact": recipient_id}
    else:
        raise ValueError("get_avatar requires recipient_id or group_id")
    return {"avatar": _cache_media(_call("getAvatar", params))}


def _get_sticker(pack_id: str = "", sticker_id: str = "", **_kw) -> Dict[str, Any]:
    res = _call("getSticker", {"packId": pack_id, "stickerId": sticker_id})
    return {"sticker": _cache_media(res)}


def _list_sticker_packs(**_kw) -> Dict[str, Any]:
    return {"sticker_packs": _call("listStickerPacks", {})}


def _list_calls(**_kw) -> Dict[str, Any]:
    return {"calls": _call("listCalls", {})}


def _send_receipt(recipient_id: str = "", target_timestamp: int = 0,
                  receipt_type: str = "read", **_kw) -> Dict[str, Any]:
    params = {
        "recipient": recipient_id,
        "targetTimestamp": [int(target_timestamp)],
        "type": receipt_type or "read",
    }
    return {"sent": True, "result": _call("sendReceipt", params)}


def _send_reaction(recipient_id: str = "", group_id: str = "", emoji: str = "",
                   target_author: str = "", target_timestamp: int = 0,
                   remove_reaction: bool = False, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "emoji": emoji,
        "targetAuthor": target_author,
        "targetTimestamp": int(target_timestamp),
    }
    if remove_reaction:
        params["remove"] = True
    _route(params, recipient_id, group_id)
    return {"sent": True, "result": _call("sendReaction", params)}


def _create_poll(recipient_id: str = "", group_id: str = "", question: str = "",
                 options: Optional[List[str]] = None, single_vote: bool = False,
                 **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"question": question, "option": list(options or [])}
    if single_vote:
        params["noMulti"] = True
    _route(params, recipient_id, group_id)
    return {"sent": True, "result": _call("sendPollCreate", params)}


def _vote_poll(recipient_id: str = "", group_id: str = "", poll_author: str = "",
               poll_timestamp: int = 0, vote_options: Optional[List[int]] = None,
               vote_count: int = 1, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "pollAuthor": poll_author,
        "pollTimestamp": int(poll_timestamp),
        "voteCount": int(vote_count),
    }
    if vote_options:
        params["option"] = [int(o) for o in vote_options]
    _route(params, recipient_id, group_id)
    return {"sent": True, "result": _call("sendPollVote", params)}


def _message_request_response(recipient_id: str = "", group_id: str = "",
                              response_type: str = "", **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": response_type}
    _route(params, recipient_id, group_id)
    return {"sent": True, "result": _call("sendMessageRequestResponse", params)}


# ---------------------------------------------------------------------------
# Admin (Tier 3) action implementations
# ---------------------------------------------------------------------------
def _block(recipient_id: str = "", group_id: str = "", **_kw) -> Dict[str, Any]:
    return {"blocked": True, "result": _call("block", _route({}, recipient_id, group_id))}


def _unblock(recipient_id: str = "", group_id: str = "", **_kw) -> Dict[str, Any]:
    return {"unblocked": True, "result": _call("unblock", _route({}, recipient_id, group_id))}


def _trust_identity(recipient_id: str = "", trust_all_known_keys: bool = True,
                    **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"recipient": recipient_id}
    # Default to trusting all known keys; a verified safety number path could be
    # added later. We never silently auto-trust without an explicit call.
    params["trustAllKnownKeys"] = bool(trust_all_known_keys)
    return {"trusted": True, "result": _call("trust", params)}


def _update_contact(recipient_id: str = "", name: str = "", note: str = "",
                    **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"recipient": recipient_id}
    if name:
        params["name"] = name
    if note:
        params["note"] = note
    return {"updated": True, "result": _call("updateContact", params)}


def _update_group(group_id: str = "", name: str = "", description: str = "",
                  members: Optional[List[str]] = None,
                  remove_members: Optional[List[str]] = None, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"groupId": group_id}
    if name:
        params["name"] = name
    if description:
        params["description"] = description
    if members:
        params["member"] = list(members)
    if remove_members:
        params["removeMember"] = list(remove_members)
    return {"updated": True, "result": _call("updateGroup", params)}


def _remote_delete(recipient_id: str = "", group_id: str = "",
                   target_timestamp: int = 0, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"targetTimestamp": int(target_timestamp)}
    _route(params, recipient_id, group_id)
    return {"deleted": True, "result": _call("remoteDelete", params)}


def _pin_message(recipient_id: str = "", group_id: str = "", target_author: str = "",
                 target_timestamp: int = 0, pin_duration: int = -1,
                 **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "targetAuthor": target_author,
        "targetTimestamp": int(target_timestamp),
        "pinDuration": int(pin_duration),
    }
    _route(params, recipient_id, group_id)
    return {"pinned": True, "result": _call("sendPinMessage", params)}


def _unpin_message(recipient_id: str = "", group_id: str = "", target_author: str = "",
                   target_timestamp: int = 0, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "targetAuthor": target_author,
        "targetTimestamp": int(target_timestamp),
    }
    _route(params, recipient_id, group_id)
    return {"unpinned": True, "result": _call("sendUnpinMessage", params)}


def _terminate_poll(recipient_id: str = "", group_id: str = "",
                    poll_timestamp: int = 0, **_kw) -> Dict[str, Any]:
    params: Dict[str, Any] = {"pollTimestamp": int(poll_timestamp)}
    _route(params, recipient_id, group_id)
    return {"terminated": True, "result": _call("sendPollTerminate", params)}


# ---------------------------------------------------------------------------
# Action tables + metadata
# ---------------------------------------------------------------------------
# Tier 1: read-only. Default-on in the hermes-signal toolset.
_CORE_ACTIONS = {
    "version": _version,
    "list_contacts": _list_contacts,
    "list_groups": _list_groups,
    "get_group_info": _get_group_info,
    "get_contact": _get_contact,
    "get_user_status": _get_user_status,
    "list_identities": _list_identities,
    "list_devices": _list_devices,
    "get_avatar": _get_avatar,
    "get_sticker": _get_sticker,
    "list_sticker_packs": _list_sticker_packs,
    "list_calls": _list_calls,
}

# Tier 2: reversible messaging side-effects with an arbitrary destination.
# Off by default — only exposed via the signal_write tool when SIGNAL_WRITE_TOOLS
# is set, so they can't bypass the operator's outbound expectations silently.
_WRITE_ACTIONS = {
    "send_receipt": _send_receipt,
    "send_reaction": _send_reaction,
    "create_poll": _create_poll,
    "vote_poll": _vote_poll,
}

# Tier 3: account/contact/group state mutation. Gated by SIGNAL_ADMIN_TOOLS +
# interactive confirmation. message_request_response lives here because
# accept/delete mutates contact/message-request state.
_ADMIN_ACTIONS = {
    "block_contact": _block,
    "unblock_contact": _unblock,
    "trust_identity": _trust_identity,
    "update_contact": _update_contact,
    "update_group": _update_group,
    "remote_delete": _remote_delete,
    "pin_message": _pin_message,
    "unpin_message": _unpin_message,
    "terminate_poll": _terminate_poll,
    "send_message_request_response": _message_request_response,
}

# action -> (signature, one-line description) for the schema description block.
_CORE_MANIFEST: List[Tuple[str, str, str]] = [
    ("version", "()", "signal-cli version"),
    ("list_contacts", "(detailed?)", "list known contacts"),
    ("list_groups", "(detailed?)", "list groups"),
    ("get_group_info", "(group_id)", "details for one group"),
    ("get_contact", "(recipient_id)", "look up a contact's profile name"),
    ("get_user_status", "(recipient_id)", "check if a number is on Signal"),
    ("list_identities", "(number?)", "list identities / safety numbers"),
    ("list_devices", "()", "list linked devices"),
    ("get_avatar", "(recipient_id|group_id)", "fetch an avatar (cached locally)"),
    ("get_sticker", "(pack_id, sticker_id)", "fetch a sticker (cached locally)"),
    ("list_sticker_packs", "()", "list installed sticker packs"),
    ("list_calls", "()", "list call history"),
]

_WRITE_MANIFEST: List[Tuple[str, str, str]] = [
    ("send_receipt", "(recipient_id, target_timestamp, receipt_type?)", "send a read/viewed receipt"),
    ("send_reaction", "(recipient_id|group_id, emoji, target_author, target_timestamp, remove_reaction?)", "react to a message"),
    ("create_poll", "(recipient_id|group_id, question, options[])", "create a poll"),
    ("vote_poll", "(recipient_id|group_id, poll_author, poll_timestamp, vote_options[], vote_count)", "vote in a poll"),
]

_ADMIN_MANIFEST: List[Tuple[str, str, str]] = [
    ("block_contact", "(recipient_id|group_id)", "block a contact/group"),
    ("unblock_contact", "(recipient_id|group_id)", "unblock a contact/group"),
    ("trust_identity", "(recipient_id, trust_all_known_keys?)", "trust an identity/safety number"),
    ("update_contact", "(recipient_id, name?, note?)", "edit a contact's local name/note"),
    ("update_group", "(group_id, name?, description?, members?, remove_members?)", "edit a group"),
    ("remote_delete", "(recipient_id|group_id, target_timestamp)", "remote-delete a bot-sent message"),
    ("pin_message", "(recipient_id|group_id, target_author, target_timestamp, pin_duration?)", "pin a message"),
    ("unpin_message", "(recipient_id|group_id, target_author, target_timestamp)", "unpin a message"),
    ("terminate_poll", "(recipient_id|group_id, poll_timestamp)", "terminate a poll"),
    ("send_message_request_response", "(recipient_id|group_id, response_type=accept|delete)", "accept/delete a message request"),
]

# Required friendly params per action (destination handled separately).
_REQUIRED_PARAMS: Dict[str, List[str]] = {
    "get_contact": ["recipient_id"],
    "get_user_status": ["recipient_id"],
    "get_sticker": ["pack_id", "sticker_id"],
    "get_group_info": ["group_id"],
    "send_receipt": ["recipient_id", "target_timestamp"],
    "send_reaction": ["emoji", "target_author", "target_timestamp"],
    "create_poll": ["question", "options"],
    "vote_poll": ["poll_author", "poll_timestamp", "vote_count"],
    "send_message_request_response": ["response_type"],
    "trust_identity": ["recipient_id"],
    "update_contact": ["recipient_id"],
    "update_group": ["group_id"],
    "remote_delete": ["target_timestamp"],
    "pin_message": ["target_author", "target_timestamp"],
    "unpin_message": ["target_author", "target_timestamp"],
    "terminate_poll": ["poll_timestamp"],
}

# Actions that require an explicit recipient_id or group_id destination.
_NEEDS_DESTINATION = frozenset({
    "send_reaction", "create_poll", "vote_poll", "send_message_request_response",
    "block_contact", "unblock_contact", "remote_delete", "pin_message",
    "unpin_message", "terminate_poll",
})

# Fixed kwargs surface passed to every handler (mirrors discord's defaults).
_HANDLER_DEFAULTS: Dict[str, Any] = {
    "recipient_id": "", "group_id": "", "number": "",
    "emoji": "", "target_author": "", "target_timestamp": 0,
    "question": "", "options": None, "single_vote": False,
    "poll_author": "", "poll_timestamp": 0, "vote_options": None, "vote_count": 1,
    "pack_id": "", "sticker_id": "", "receipt_type": "read",
    "remove_reaction": False, "response_type": "",
    "name": "", "description": "", "note": "", "members": None,
    "remove_members": None, "trust_all_known_keys": True, "pin_duration": -1,
    "detailed": False,
}


# ---------------------------------------------------------------------------
# Config allowlist (signal.actions) — mirrors discord.server_actions
# ---------------------------------------------------------------------------
def _load_allowed_actions_config() -> Optional[List[str]]:
    """Read ``signal.actions`` from user config; None means all actions allowed."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.debug("signal: could not load config (%s); allowing all actions.", exc)
        return None

    raw = (cfg.get("signal") or {}).get("actions")
    if raw is None or raw == "":
        return None

    if isinstance(raw, str):
        names = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        logger.warning("signal.actions: unexpected type %s; ignoring.", type(raw).__name__)
        return None

    known = set(_CORE_ACTIONS) | set(_WRITE_ACTIONS) | set(_ADMIN_ACTIONS)
    valid = [n for n in names if n in known]
    invalid = [n for n in names if n not in known]
    if invalid:
        logger.warning("signal.actions: unknown action(s) ignored: %s", ", ".join(invalid))
    return valid


# ---------------------------------------------------------------------------
# Schema construction
# ---------------------------------------------------------------------------
_PROPERTIES: Dict[str, Any] = {
    "recipient_id": {"type": "string", "description": "Recipient phone number (E.164) or Signal UUID."},
    "group_id": {"type": "string", "description": "Signal group ID (base64). Beats recipient_id when both given."},
    "number": {"type": "string", "description": "Phone number filter (list_identities)."},
    "emoji": {"type": "string", "description": "Reaction emoji (single grapheme)."},
    "target_author": {"type": "string", "description": "Phone/UUID of the target message's author."},
    "target_timestamp": {"type": "integer", "description": "Target message timestamp in ms (Signal message id)."},
    "question": {"type": "string", "description": "Poll question (create_poll)."},
    "options": {"type": "array", "items": {"type": "string"}, "description": "Poll option labels (create_poll)."},
    "single_vote": {"type": "boolean", "description": "Disallow multiple selections (create_poll)."},
    "poll_author": {"type": "string", "description": "Phone/UUID of the poll creator (vote_poll/terminate_poll)."},
    "poll_timestamp": {"type": "integer", "description": "Poll creation timestamp in ms."},
    "vote_options": {"type": "array", "items": {"type": "integer"}, "description": "Selected option indexes (vote_poll)."},
    "vote_count": {"type": "integer", "description": "Vote sequence number, increment per re-vote (vote_poll)."},
    "pack_id": {"type": "string", "description": "Sticker pack id (get_sticker)."},
    "sticker_id": {"type": "string", "description": "Sticker id within the pack (get_sticker)."},
    "receipt_type": {"type": "string", "enum": ["read", "viewed"], "description": "Receipt type (send_receipt)."},
    "remove_reaction": {"type": "boolean", "description": "Remove instead of add the reaction (send_reaction)."},
    "response_type": {"type": "string", "enum": ["accept", "delete"], "description": "Message-request response (send_message_request_response)."},
    "name": {"type": "string", "description": "New name (update_group / update_contact)."},
    "description": {"type": "string", "description": "New group description (update_group)."},
    "note": {"type": "string", "description": "Local contact note (update_contact)."},
    "members": {"type": "array", "items": {"type": "string"}, "description": "Members to add (update_group)."},
    "remove_members": {"type": "array", "items": {"type": "string"}, "description": "Members to remove (update_group)."},
    "trust_all_known_keys": {"type": "boolean", "description": "Trust all known keys (trust_identity)."},
    "pin_duration": {"type": "integer", "description": "Pin duration in seconds, -1 for permanent (pin_message)."},
    "detailed": {"type": "boolean", "description": "Return detailed records (list_contacts/list_groups)."},
}


def _build_schema(manifest: List[Tuple[str, str, str]], tool_name: str,
                  intro: str) -> Dict[str, Any]:
    actions = [name for name, _, _ in manifest]
    manifest_block = "\n".join(f"  {n}{sig}  — {desc}" for n, sig, desc in manifest)
    description = f"{intro}\n\nAvailable actions:\n{manifest_block}"
    properties = {"action": {"type": "string", "enum": actions}}
    properties.update(_PROPERTIES)
    return {
        "name": tool_name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": ["action"],
        },
    }


_CORE_INTRO = (
    "Read-only introspection of Signal via the local signal-cli daemon: "
    "contacts, groups, identities, devices, avatars, stickers, user status, "
    "call history. Use recipient_id for DMs and group_id for groups; message "
    "ids are millisecond timestamps."
)
_WRITE_INTRO = (
    "Signal messaging side-effects (receipts, reactions, polls) via signal-cli. "
    "These send to an explicit recipient_id/group_id, so they are enabled only "
    "when SIGNAL_WRITE_TOOLS=true. Use recipient_id for DMs and group_id for "
    "groups; message ids are millisecond timestamps."
)
_ADMIN_INTRO = (
    "Administrative Signal actions that mutate account state (block, trust, "
    "edit groups/contacts, remote-delete bot messages, pin, message-request "
    "responses). Enabled only when SIGNAL_ADMIN_TOOLS=true and each action "
    "prompts for confirmation. Account-destructive operations are never available."
)

_CORE_SCHEMA = _build_schema(_CORE_MANIFEST, "signal", _CORE_INTRO)
_WRITE_SCHEMA = _build_schema(_WRITE_MANIFEST, "signal_write", _WRITE_INTRO)
_ADMIN_SCHEMA = _build_schema(_ADMIN_MANIFEST, "signal_admin", _ADMIN_INTRO)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def _run_signal_action(action: str, valid_actions: Dict[str, Any],
                       tool_label: str, **kwargs: Any) -> str:
    if not check_signal_tool_requirements():
        return tool_error("Signal is not configured (SIGNAL_HTTP_URL + SIGNAL_ACCOUNT required).")

    fn = valid_actions.get(action)
    if not fn:
        return tool_error(f"Unknown action: {action}", available_actions=sorted(valid_actions))

    allowlist = _load_allowed_actions_config()
    if allowlist is not None and action not in allowlist:
        return tool_error(
            f"Action '{action}' is disabled by config (signal.actions). "
            f"Allowed: {', '.join(allowlist) if allowlist else '<none>'}"
        )

    missing = [p for p in _REQUIRED_PARAMS.get(action, []) if not kwargs.get(p)]
    if missing:
        return tool_error(f"Missing required parameters for '{action}': {', '.join(missing)}")

    if action in _NEEDS_DESTINATION and not (kwargs.get("recipient_id") or kwargs.get("group_id")):
        return tool_error(f"Action '{action}' requires recipient_id or group_id.")

    # Tier-3 admin actions require interactive confirmation (on top of the
    # SIGNAL_ADMIN_TOOLS env gate) — checked after validation, right before the
    # mutating call. Fail-closed if approval can't be obtained.
    admin_token = None
    if tool_label == "signal_admin":
        denial = _confirm_admin_action(action, _admin_action_summary(action, kwargs))
        if denial:
            return tool_error(f"signal_admin action '{action}' not executed: {denial}")
        # Admit Tier-3 methods at the low-level client only for this dispatch.
        admin_token = _signal_admin_ctx.set(True)

    try:
        # Success payloads are returned to the authorized agent unredacted on
        # purpose: surfacing contact numbers / group ids IS the point of these
        # read actions (e.g. look up a contact, then message them). Phone
        # numbers are only redacted in *error* strings below, mirroring how the
        # adapter redacts logs but not the message data it processes.
        return tool_result(fn(**kwargs))
    except SignalMethodNotAllowed as e:
        logger.error("signal %s: blocked method for action '%s': %s", tool_label, action, e)
        return tool_error(_redact_text(str(e)))
    except SignalRPCError as e:
        logger.warning("signal %s RPC error on '%s': %s", tool_label, action, e)
        return tool_error(_redact_text(e.message), code=e.code)
    except ValueError as e:
        return tool_error(_redact_text(str(e)))
    except Exception as e:  # noqa: BLE001 — uniform tool error envelope
        logger.exception("signal %s action '%s' failed", tool_label, action)
        return tool_error(_redact_text(f"{type(e).__name__}: {e}"))
    finally:
        if admin_token is not None:
            _signal_admin_ctx.reset(admin_token)


def signal_core_handler(action: str = "", **kwargs: Any) -> str:
    return _run_signal_action(action, _CORE_ACTIONS, "signal", **kwargs)


def signal_write_handler(action: str = "", **kwargs: Any) -> str:
    # Defense in depth: refuse messaging side-effects unless explicitly enabled.
    if not _write_enabled():
        return tool_error(
            "signal_write is disabled. Set SIGNAL_WRITE_TOOLS=true to enable "
            "Signal messaging side-effect actions (receipts, reactions, polls)."
        )
    return _run_signal_action(action, _WRITE_ACTIONS, "signal_write", **kwargs)


def signal_admin_handler(action: str = "", **kwargs: Any) -> str:
    # Defense in depth: even if the schema leaks past the check_fn gate, the
    # handler refuses admin actions unless the explicit opt-in is set.
    if not _admin_enabled():
        return tool_error(
            "signal_admin is disabled. Set SIGNAL_ADMIN_TOOLS=true to enable "
            "administrative Signal actions."
        )
    return _run_signal_action(action, _ADMIN_ACTIONS, "signal_admin", **kwargs)


def _make_handler(handler_fn):
    """Registry-compatible adapter: pull the fixed kwargs surface from args."""
    return lambda args, **_kw: handler_fn(
        action=args.get("action", ""),
        **{k: args.get(k, v) for k, v in _HANDLER_DEFAULTS.items()},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
registry.register(
    name="signal",
    toolset="signal",
    schema=_CORE_SCHEMA,
    handler=_make_handler(signal_core_handler),
    check_fn=check_signal_tool_requirements,
    requires_env=["SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"],
)

registry.register(
    name="signal_write",
    toolset="signal_write",
    schema=_WRITE_SCHEMA,
    handler=_make_handler(signal_write_handler),
    check_fn=check_signal_write_requirements,
    requires_env=["SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_WRITE_TOOLS"],
)

registry.register(
    name="signal_admin",
    toolset="signal_admin",
    schema=_ADMIN_SCHEMA,
    handler=_make_handler(signal_admin_handler),
    check_fn=check_signal_admin_requirements,
    requires_env=["SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_ADMIN_TOOLS"],
)
