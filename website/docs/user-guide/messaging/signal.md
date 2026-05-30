---
sidebar_position: 6
title: "Signal"
description: "Set up Hermes Agent as a Signal messenger bot via signal-cli daemon"
---

# Signal Setup

Hermes connects to Signal through the [signal-cli](https://github.com/AsamK/signal-cli) daemon running in HTTP mode. The adapter streams messages in real-time via SSE (Server-Sent Events) and sends responses via JSON-RPC.

Signal is the most privacy-focused mainstream messenger — end-to-end encrypted by default, open-source protocol, minimal metadata collection. This makes it ideal for security-sensitive agent workflows.

:::info No New Python Dependencies
The Signal adapter uses `httpx` (already a core Hermes dependency) for all communication. No additional Python packages are required. You just need signal-cli installed externally.
:::

---

## Prerequisites

- **signal-cli** — Java-based Signal client ([GitHub](https://github.com/AsamK/signal-cli))
- **Java 17+** runtime — required by signal-cli
- **A phone number** with Signal installed (for linking as a secondary device)

### Installing signal-cli

```bash
# macOS
brew install signal-cli

# Linux (download latest release)
VERSION=$(curl -Ls -o /dev/null -w %{url_effective} \
  https://github.com/AsamK/signal-cli/releases/latest | sed 's/^.*\/v//')
curl -L -O "https://github.com/AsamK/signal-cli/releases/download/v${VERSION}/signal-cli-${VERSION}.tar.gz"
sudo tar xf "signal-cli-${VERSION}.tar.gz" -C /opt
sudo ln -sf "/opt/signal-cli-${VERSION}/bin/signal-cli" /usr/local/bin/
```

:::caution
signal-cli is **not** in apt or snap repositories. The Linux install above downloads directly from [GitHub releases](https://github.com/AsamK/signal-cli/releases).
:::

---

## Step 1: Link Your Signal Account

Signal-cli works as a **linked device** — like WhatsApp Web, but for Signal. Your phone stays the primary device.

```bash
# Generate a linking URI (displays a QR code or link)
signal-cli link -n "HermesAgent"
```

1. Open **Signal** on your phone
2. Go to **Settings → Linked Devices**
3. Tap **Link New Device**
4. Scan the QR code or enter the URI

---

## Step 2: Start the signal-cli Daemon

```bash
# Replace +1234567890 with your Signal phone number (E.164 format)
signal-cli --account +1234567890 daemon --http 127.0.0.1:8080
```

:::tip
Keep this running in the background. You can use `systemd`, `tmux`, `screen`, or run it as a service.
:::

Verify it's running:

```bash
curl http://127.0.0.1:8080/api/v1/check
# Should return: {"versions":{"signal-cli":...}}
```

---

## Step 3: Configure Hermes

The easiest way:

```bash
hermes gateway setup
```

Select **Signal** from the platform menu. The wizard will:

1. Check if signal-cli is installed
2. Prompt for the HTTP URL (default: `http://127.0.0.1:8080`)
3. Test connectivity to the daemon
4. Ask for your account phone number
5. Configure allowed users and access policies

### Manual Configuration

Add to `~/.hermes/.env`:

```bash
# Required
SIGNAL_HTTP_URL=http://127.0.0.1:8080
SIGNAL_ACCOUNT=+1234567890

# Security (recommended)
SIGNAL_ALLOWED_USERS=+1234567890,+0987654321    # Comma-separated E.164 numbers or UUIDs

# Optional
SIGNAL_GROUP_ALLOWED_USERS=groupId1,groupId2     # Enable groups (omit to disable, * for all)
SIGNAL_HOME_CHANNEL=+1234567890                  # Default delivery target for cron jobs
```

Then start the gateway:

```bash
hermes gateway              # Foreground
hermes gateway install      # Install as a user service
sudo hermes gateway install --system   # Linux only: boot-time system service
```

---

## Access Control

### DM Access

DM access follows the same pattern as all other Hermes platforms:

1. **`SIGNAL_ALLOWED_USERS` set** → only those users can message
2. **No allowlist set** → unknown users get a DM pairing code (approve via `hermes pairing approve signal CODE`)
3. **`SIGNAL_ALLOW_ALL_USERS=true`** → anyone can message (use with caution)

### Group Access

Group access is controlled by the `SIGNAL_GROUP_ALLOWED_USERS` env var:

| Configuration | Behavior |
|---------------|----------|
| Not set (default) | All group messages are ignored. The bot only responds to DMs. |
| Set with group IDs | Only listed groups are monitored (e.g., `groupId1,groupId2`). |
| Set to `*` | The bot responds in any group it's a member of. |

---

## Features

### Attachments

The adapter supports sending and receiving media in both directions.

**Incoming** (user → agent):

- **Images** — PNG, JPEG, GIF, WebP (auto-detected via magic bytes)
- **Audio** — MP3, OGG, WAV, M4A (voice messages transcribed if Whisper is configured)
- **Documents** — PDF, ZIP, and other file types

**Outgoing** (agent → user):

The agent can send media files via `MEDIA:` tags in responses. The following delivery methods are supported:

- **Images** — `send_multiple_images` and `send_image_file` send PNG, JPEG, GIF, WebP as native Signal attachments
- **Voice** — `send_voice` sends audio files (OGG, MP3, WAV, M4A, AAC) as attachments
- **Video** — `send_video` sends MP4 video files
- **Documents** — `send_document` sends any file type (PDF, ZIP, etc.)

All outgoing media goes through Signal's standard attachment API. Unlike some platforms, Signal does not distinguish between voice messages and file attachments at the protocol level.

Attachment size limit: **100 MB** (both directions).
:::warning
**Signal servers will rate-limit attachment uploads**, the adapter uses a scheduler for multiple image sending that batches images in groups of 32 and throttles uploads to match the Signal server policy.
:::

### Native Formatting, Reply Quotes, and Reactions

Signal messages render with **native formatting** instead of literal markdown characters. The adapter converts markdown into Signal `bodyRanges` (`textStyles`) so the text shows up with real styling on the recipient's client rather than as visible `**` / `` ` `` markers:

| Markdown | Signal style |
|----------|--------------|
| `**bold**` or `__bold__` | BOLD |
| `*italic*` or `_italic_` | ITALIC |
| `~~strike~~` | STRIKETHROUGH |
| `` `code` `` and ```` ```fenced``` ```` | MONOSPACE |
| `# Heading` … `###### Heading` | BOLD |
| `\|\|spoiler\|\|` | SPOILER |

Offsets are computed in **UTF-16 code units** (the unit the Signal protocol uses), so emoji (including astral-plane / surrogate-pair codepoints), CJK text, combining marks, and ZWJ sequences are positioned correctly. The spoiler parser requires non-space content immediately inside the bars (`||hidden||`), so logical-OR in code (`a || b || c`) is **not** mistaken for a spoiler, and `||` inside inline code is left untouched.

**Graceful fallback.** If signal-cli rejects the style params (e.g. an older build), the send is automatically retried once as plain text so the message is still delivered rather than lost. A one-line INFO is logged when this happens.

**Reply quotes.** Replies natively quote the message that triggered them — the same affordance Signal users see when they tap "Reply". The adapter captures the triggering message when a turn starts and attaches it to the first message of the reply (works in DMs and groups). Proactive/cron/home-channel sends never quote. Set `SIGNAL_REPLY_QUOTE=false` to turn this off. Callers can also pass an explicit `quote_timestamp` + `quote_author` (+ optional `quote_message`) in the send `metadata`, which takes precedence; Hermes never fabricates a quote author.

**Reactions.** The agent reacts to messages via the standard reaction API (also exposed as `signal` → `send_reaction`); reactions surface in Signal as emoji reactions on the referenced message rather than as extra text.

Formatting and reactions require no additional config — they ship on by default in recent signal-cli builds.

### Typing Indicators

The bot sends typing indicators while processing messages, refreshing every 8 seconds.

### Phone Number Redaction

All phone numbers are automatically redacted in logs:
- `+15551234567` → `+155****4567`
- This applies to both Hermes gateway logs and the global redaction system

### Note to Self (Single-Number Setup)

If you run signal-cli as a **linked secondary device** on your own phone number (rather than a separate bot number), you can interact with Hermes through Signal's "Note to Self" feature.

Just send a message to yourself from your phone — signal-cli picks it up and Hermes responds in the same conversation.

**How it works:**
- "Note to Self" messages arrive as `syncMessage.sentMessage` envelopes
- The adapter detects when these are addressed to the bot's own account and processes them as regular inbound messages
- Echo-back protection (sent-timestamp tracking) prevents infinite loops — the bot's own replies are filtered out automatically

**No extra configuration needed.** This works automatically as long as `SIGNAL_ACCOUNT` matches your phone number.

### Health Monitoring

The adapter monitors the SSE connection and automatically reconnects if:
- The connection drops (with exponential backoff: 2s → 60s)
- No activity is detected for 120 seconds (pings signal-cli to verify)

---

## Signal Control-Surface Tools

Beyond sending and receiving, the agent can drive a curated slice of signal-cli through two action-dispatch tools. Both talk to the local signal-cli daemon over HTTP JSON-RPC and require `SIGNAL_HTTP_URL` + `SIGNAL_ACCOUNT`. Every method is routed through an **allowlist** — account-destructive and unrecognized methods can never reach the daemon (see [Safety Tiers](#safety-tiers)).

### `signal` tool (read + messaging)

Bundled with the `hermes-signal` toolset (no extra config). Call with an `action` plus action-specific params (use `recipient_id` for DMs, `group_id` for groups; message ids are millisecond timestamps):

| Action | Purpose |
|--------|---------|
| `version` | signal-cli version |
| `list_contacts` / `list_groups` | enumerate contacts / groups |
| `get_group_info` | details for one `group_id` |
| `get_contact` | profile name for a `recipient_id` |
| `get_user_status` | check whether a number is registered on Signal |
| `list_identities` | identities / safety numbers |
| `list_devices` | linked devices |
| `get_avatar` / `get_sticker` | fetch media (cached under the Hermes home) |
| `list_sticker_packs` | installed sticker packs |
| `list_calls` | call history |
| `send_receipt` | send a read / viewed receipt |
| `send_reaction` | react to a message (`emoji`, `target_author`, `target_timestamp`) |
| `create_poll` / `vote_poll` | create or vote in a poll |
| `send_message_request_response` | accept / delete an incoming message request |

### `signal_admin` tool (account-state mutations)

**Disabled by default.** Enable with `SIGNAL_ADMIN_TOOLS=true` *and* the `signal_admin` toolset. Even if the schema is reached, the handler refuses unless the flag is set.

**Interactive confirmation.** On top of the env gate, every admin action prompts for confirmation before it runs (the shared Hermes approval prompt — `[o]nce / [s]ession / [d]eny`). If approval can't be obtained (e.g. a headless session with no approval UI) the action is denied (fail-closed). `--yolo` sessions auto-approve. Set `SIGNAL_ADMIN_REQUIRE_CONFIRM=false` to run admin actions unattended.

| Action | Purpose |
|--------|---------|
| `block_contact` / `unblock_contact` | block / unblock a contact or group |
| `trust_identity` | trust an identity / safety number |
| `update_contact` | edit a contact's local name / note |
| `update_group` | edit group name / description / members |
| `remote_delete` | remote-delete a message the bot previously sent |
| `pin_message` / `unpin_message` | pin / unpin a message |
| `terminate_poll` | terminate a poll |

Errors are returned as JSON with phone numbers redacted. You can further restrict the exposed actions with a `signal.actions` config list (comma-separated or YAML list), mirroring `discord.server_actions`.

### Safety Tiers

| Tier | Examples | Exposure |
|------|----------|----------|
| **1 — read-only** | list/get contacts, groups, identities, devices, avatars, stickers, user status | `signal` tool |
| **2 — messaging side-effect** | receipts, reactions, polls, message-request response | `signal` tool (destination is explicit; DM/group allowlists still apply) |
| **3 — account-state mutation** | block/unblock, trust, group/contact edits, remote-delete, pin/unpin | `signal_admin` only, `SIGNAL_ADMIN_TOOLS=true` |
| **4 — account-destructive** | register, unregister, delete local data, PIN, device, number-change, account config, join/quit group | **Never exposed; blocked at the RPC allowlist before any network call** |

### Intentionally Unsupported (account-destructive)

The following signal-cli commands are deliberately **not** exposed by any tool and are blocked from being sent to the daemon, because they can destroy or hijack the account. Run them yourself with the `signal-cli` CLI if you ever need them:

`register`, `verify`, `unregister`, `deleteLocalAccountData`, `setPin`, `removePin`, `addDevice`, `removeDevice`, `link`, `startChangeNumber`, `finishChangeNumber`, `updateAccount`, `updateConfiguration`, `updateDevice`, `joinGroup`, `quitGroup`.

Arbitrary / free-form RPC method execution is never available to the agent — only the curated actions above.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Cannot reach signal-cli"** during setup | Ensure signal-cli daemon is running: `signal-cli --account +YOUR_NUMBER daemon --http 127.0.0.1:8080` |
| **Messages not received** | Check that `SIGNAL_ALLOWED_USERS` includes the sender's number in E.164 format (with `+` prefix) |
| **"signal-cli not found on PATH"** | Install signal-cli and ensure it's in your PATH, or use Docker |
| **Connection keeps dropping** | Check signal-cli logs for errors. Ensure Java 17+ is installed. |
| **Group messages ignored** | Configure `SIGNAL_GROUP_ALLOWED_USERS` with specific group IDs, or `*` to allow all groups. |
| **Bot responds to no one** | Configure `SIGNAL_ALLOWED_USERS`, use DM pairing, or explicitly allow all users through gateway policy if you want broader access. |
| **Duplicate messages** | Ensure only one signal-cli instance is listening on your phone number |
| **`signal_admin` actions rejected** | Set `SIGNAL_ADMIN_TOOLS=true` and enable the `signal_admin` toolset. Account-destructive commands remain blocked by design. |
| **Signal tools "not configured"** | The `signal`/`signal_admin` tools need `SIGNAL_HTTP_URL` + `SIGNAL_ACCOUNT` in the agent's environment, same as the gateway. |

---

## Security

:::warning
**Always configure access controls.** The bot has terminal access by default. Without `SIGNAL_ALLOWED_USERS` or DM pairing, the gateway denies all incoming messages as a safety measure.
:::

- Phone numbers are redacted in all log output
- Use DM pairing or explicit allowlists for safe onboarding of new users
- Keep groups disabled unless you specifically need group support, or allowlist only the groups you trust
- Signal's end-to-end encryption protects message content in transit
- The signal-cli session data in `~/.local/share/signal-cli/` contains account credentials — protect it like a password

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SIGNAL_HTTP_URL` | Yes | — | signal-cli HTTP endpoint |
| `SIGNAL_ACCOUNT` | Yes | — | Bot phone number (E.164) |
| `SIGNAL_ALLOWED_USERS` | No | — | Comma-separated phone numbers/UUIDs |
| `SIGNAL_GROUP_ALLOWED_USERS` | No | — | Group IDs to monitor, or `*` for all (omit to disable groups) |
| `SIGNAL_ALLOW_ALL_USERS` | No | `false` | Allow any user to interact (skip allowlist) |
| `SIGNAL_HOME_CHANNEL` | No | — | Default delivery target for cron jobs |
| `SIGNAL_ADMIN_TOOLS` | No | `false` | Enable the `signal_admin` tool (block/trust/group edits/remote-delete/pin). Account-destructive commands stay blocked regardless. |
| `SIGNAL_ADMIN_REQUIRE_CONFIRM` | No | `true` | Require interactive confirmation before each `signal_admin` action. Set `false` for unattended admin use. |
| `SIGNAL_REPLY_QUOTE` | No | `true` | Natively quote the triggering message in replies. Set `false` to disable. |
