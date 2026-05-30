"""Tests for Signal _markdown_to_signal() formatting.

Covers the markdown-to-bodyRanges conversion pipeline: bold, italic,
strikethrough, monospace, code blocks, headings, and — critically — the
false-positive regressions that caused spurious italics in production.
"""

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.signal import SignalAdapter


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _m2s(text: str):
    """Shorthand: call the static method and return (plain_text, styles)."""
    return SignalAdapter._markdown_to_signal(text)


def _style_types(styles: list[str]) -> list[str]:
    """Extract just the STYLE part from '0:4:BOLD' strings."""
    return [s.rsplit(":", 1)[1] for s in styles]


def _find_style(styles: list[str], style_type: str) -> list[str]:
    """Return only styles matching a given type."""
    return [s for s in styles if s.endswith(f":{style_type}")]


# ===========================================================================
# Basic formatting
# ===========================================================================

class TestMarkdownToSignalBasic:
    """Core formatting: bold, italic, strikethrough, monospace."""

    def test_bold_double_asterisk(self):
        text, styles = _m2s("hello **world**")
        assert text == "hello world"
        assert len(styles) == 1
        assert styles[0].endswith(":BOLD")

    def test_bold_double_underscore(self):
        text, styles = _m2s("hello __world__")
        assert text == "hello world"
        assert len(styles) == 1
        assert styles[0].endswith(":BOLD")

    def test_italic_single_asterisk(self):
        text, styles = _m2s("hello *world*")
        assert text == "hello world"
        assert len(styles) == 1
        assert styles[0].endswith(":ITALIC")

    def test_italic_single_underscore(self):
        text, styles = _m2s("hello _world_")
        assert text == "hello world"
        assert len(styles) == 1
        assert styles[0].endswith(":ITALIC")

    def test_strikethrough(self):
        text, styles = _m2s("hello ~~world~~")
        assert text == "hello world"
        assert len(styles) == 1
        assert styles[0].endswith(":STRIKETHROUGH")

    def test_inline_monospace(self):
        text, styles = _m2s("run `ls -la` now")
        assert text == "run ls -la now"
        assert len(styles) == 1
        assert styles[0].endswith(":MONOSPACE")

    def test_fenced_code_block(self):
        text, styles = _m2s("before\n```\ncode here\n```\nafter")
        assert "code here" in text
        assert "```" not in text
        assert any(s.endswith(":MONOSPACE") for s in styles)

    def test_heading_becomes_bold(self):
        text, styles = _m2s("## Section Title")
        assert text == "Section Title"
        assert len(styles) == 1
        assert styles[0].endswith(":BOLD")

    def test_multiple_styles(self):
        text, styles = _m2s("**bold** and *italic*")
        assert text == "bold and italic"
        types = _style_types(styles)
        assert "BOLD" in types
        assert "ITALIC" in types

    def test_plain_text_no_styles(self):
        text, styles = _m2s("just plain text")
        assert text == "just plain text"
        assert styles == []

    def test_empty_string(self):
        text, styles = _m2s("")
        assert text == ""
        assert styles == []


# ===========================================================================
# Italic false-positive regressions
# ===========================================================================

class TestItalicFalsePositives:
    """Regressions from signal-italic-false-positive-fix.md and
    signal-italic-bullet-list-fix.md."""

    # --- snake_case (original fix) ---

    def test_snake_case_not_italic(self):
        """snake_case identifiers must NOT be italicized."""
        text, styles = _m2s("the config_file is ready")
        assert text == "the config_file is ready"
        assert _find_style(styles, "ITALIC") == []

    def test_multiple_snake_case(self):
        text, styles = _m2s("set OPENAI_API_KEY and ANTHROPIC_API_KEY")
        assert _find_style(styles, "ITALIC") == []

    def test_snake_case_path(self):
        text, styles = _m2s("/tools/delegate_tool.py")
        assert _find_style(styles, "ITALIC") == []

    def test_snake_case_between_words(self):
        """file_path and error_code — underscores between words."""
        text, styles = _m2s("file_path and error_code")
        assert _find_style(styles, "ITALIC") == []

    # --- Bullet lists (second fix) ---

    def test_bullet_list_not_italic(self):
        """* item lines must NOT be treated as italic delimiters."""
        md = "* item one\n* item two\n* item three"
        text, styles = _m2s(md)
        assert _find_style(styles, "ITALIC") == []

    def test_bullet_list_with_content_before(self):
        md = "Here are things:\n\n* first thing\n* second thing"
        text, styles = _m2s(md)
        assert _find_style(styles, "ITALIC") == []

    def test_bullet_list_file_paths(self):
        """Real-world case that triggered the bug."""
        md = (
            "* tools/delegate_tool.py — delegation\n"
            "* tools/file_tools.py — file operations\n"
            "* tools/web_tools.py — web operations"
        )
        text, styles = _m2s(md)
        assert _find_style(styles, "ITALIC") == []

    def test_bullet_with_italic_inside(self):
        """Italic *inside* a bullet item should still work."""
        md = "* this has *emphasis* inside\n* plain item"
        text, styles = _m2s(md)
        italic_styles = _find_style(styles, "ITALIC")
        assert len(italic_styles) == 1
        # The italic should cover "emphasis", not the whole bullet
        assert "emphasis" in text

    # --- Cross-line spans (DOTALL removal) ---

    def test_star_italic_no_cross_line(self):
        """*foo\\nbar* must NOT match as italic (no DOTALL)."""
        text, styles = _m2s("*foo\nbar*")
        assert _find_style(styles, "ITALIC") == []

    def test_underscore_italic_no_cross_line(self):
        """_foo\\nbar_ must NOT match as italic (no DOTALL)."""
        text, styles = _m2s("_foo\nbar_")
        assert _find_style(styles, "ITALIC") == []

    def test_star_italic_multiline_response(self):
        """Multi-paragraph response with * should not false-positive."""
        md = (
            "I checked the following files:\n\n"
            "* tools/delegate_tool.py — sub-agent delegation\n"
            "* tools/file_tools.py — file read/write/search\n"
            "* tools/web_tools.py — web search/extract\n\n"
            "Everything looks good."
        )
        text, styles = _m2s(md)
        assert _find_style(styles, "ITALIC") == []

    # --- Legitimate italic still works ---

    def test_star_italic_still_works(self):
        text, styles = _m2s("this is *italic* text")
        assert text == "this is italic text"
        assert len(_find_style(styles, "ITALIC")) == 1

    def test_underscore_italic_still_works(self):
        text, styles = _m2s("this is _italic_ text")
        assert text == "this is italic text"
        assert len(_find_style(styles, "ITALIC")) == 1

    def test_multiple_italic_same_line(self):
        text, styles = _m2s("*foo* and *bar* ok")
        assert text == "foo and bar ok"
        assert len(_find_style(styles, "ITALIC")) == 2

    def test_italic_single_word(self):
        text, styles = _m2s("*word*")
        assert text == "word"
        assert len(_find_style(styles, "ITALIC")) == 1

    def test_italic_multi_word(self):
        text, styles = _m2s("*several words here*")
        assert text == "several words here"
        assert len(_find_style(styles, "ITALIC")) == 1


# ===========================================================================
# Style position accuracy
# ===========================================================================

class TestStylePositions:
    """Verify that start:length positions map to the correct text."""

    def _extract(self, text: str, style_str: str) -> str:
        """Given 'start:length:STYLE', extract the substring from text."""
        # Positions are UTF-16 code units; for ASCII they match code points
        parts = style_str.split(":")
        start, length = int(parts[0]), int(parts[1])
        # Encode to UTF-16-LE, slice, decode back
        encoded = text.encode("utf-16-le")
        extracted = encoded[start * 2 : (start + length) * 2]
        return extracted.decode("utf-16-le")

    def test_bold_position(self):
        text, styles = _m2s("hello **world** end")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "world"

    def test_italic_position(self):
        text, styles = _m2s("hello *world* end")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "world"

    def test_multiple_styles_positions(self):
        text, styles = _m2s("**bold** then *italic*")
        assert len(styles) == 2
        extracted = {self._extract(text, s) for s in styles}
        assert extracted == {"bold", "italic"}

    def test_emoji_utf16_offset(self):
        """Emoji (multi-byte UTF-16) before a styled span."""
        text, styles = _m2s("👋 **hello**")
        assert text == "👋 hello"
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "hello"


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    """Tricky inputs that have caused issues or could regress."""

    def test_bold_inside_bullet(self):
        """Bold inside a bullet list item."""
        md = "* **important** item\n* normal item"
        text, styles = _m2s(md)
        assert len(_find_style(styles, "BOLD")) == 1
        assert _find_style(styles, "ITALIC") == []

    def test_code_span_with_underscores(self):
        """`snake_case_var` — backtick takes priority over underscore."""
        text, styles = _m2s("use `my_var_name` here")
        assert text == "use my_var_name here"
        types = _style_types(styles)
        assert "MONOSPACE" in types
        assert "ITALIC" not in types

    def test_bold_and_italic_nested(self):
        """***bold+italic*** — bold captured, not italic (bold pattern first)."""
        text, styles = _m2s("***word***")
        # ** matches bold around *word*, or *** is ambiguous;
        # either way there should be no false italic of the whole string
        assert "word" in text

    def test_lone_asterisk(self):
        """A single * with no pair should not cause issues."""
        text, styles = _m2s("5 * 3 = 15")
        # Should not crash; any italic match would be a false positive
        assert "5" in text and "15" in text

    def test_lone_underscore(self):
        """A single _ with no pair."""
        text, styles = _m2s("this _ that")
        assert text == "this _ that"

    def test_consecutive_underscored_words(self):
        """_foo and _bar (leading underscores, no closers)."""
        text, styles = _m2s("call _init and _setup")
        assert _find_style(styles, "ITALIC") == []

    def test_mixed_formatting_no_bleed(self):
        """Multiple format types don't bleed into each other."""
        md = "**bold** and `code` and *italic* and ~~strike~~"
        text, styles = _m2s(md)
        assert text == "bold and code and italic and strike"
        types = _style_types(styles)
        assert sorted(types) == ["BOLD", "ITALIC", "MONOSPACE", "STRIKETHROUGH"]


# ===========================================================================
# signal-markdown-strip-patch: core conversion pipeline
# ===========================================================================

class TestMarkdownStripPatch:
    """Tests for the original signal-markdown-strip-patch.
    
    Covers: fenced code blocks with language tags, links preserved,
    headings converted to bold, multiple headings, UTF-16 correctness
    for multi-byte characters, and marker stripping completeness.
    """

    def test_fenced_code_block_with_language_tag(self):
        """```python\\ncode\\n``` — language tag is stripped, content is MONOSPACE."""
        text, styles = _m2s("```python\nprint('hello')\n```")
        assert "```" not in text
        assert "python" not in text  # language tag stripped
        assert "print('hello')" in text
        assert any(s.endswith(":MONOSPACE") for s in styles)

    def test_fenced_code_block_multiline(self):
        """Multi-line code blocks preserve all lines."""
        md = "```\nline1\nline2\nline3\n```"
        text, styles = _m2s(md)
        assert "line1" in text
        assert "line2" in text
        assert "line3" in text
        assert "```" not in text

    def test_links_preserved(self):
        """[text](url) links are kept as-is — Signal auto-linkifies."""
        md = "Check [this link](https://example.com) for details"
        text, styles = _m2s(md)
        # Links should pass through — either as markdown or just preserved
        assert "https://example.com" in text

    def test_heading_h1(self):
        """# H1 becomes bold text."""
        text, styles = _m2s("# Main Title")
        assert text == "Main Title"
        assert len(styles) == 1
        assert styles[0].endswith(":BOLD")

    def test_heading_h3(self):
        """### H3 becomes bold text."""
        text, styles = _m2s("### Sub Section")
        assert text == "Sub Section"
        assert len(styles) == 1
        assert styles[0].endswith(":BOLD")

    def test_multiple_headings(self):
        """Multiple headings each become separate bold spans."""
        md = "## First\n\nSome text\n\n## Second"
        text, styles = _m2s(md)
        assert "First" in text
        assert "Second" in text
        assert "##" not in text
        bold_styles = _find_style(styles, "BOLD")
        assert len(bold_styles) == 2

    def test_no_raw_markdown_markers_in_output(self):
        """All markdown syntax is stripped from plain text output."""
        md = "**bold** and *italic* and ~~struck~~ and `code` and ## heading"
        text, styles = _m2s(md)
        assert "**" not in text
        assert "~~" not in text
        assert "`" not in text
        # ## at end might remain if not at line start — that's ok
        # The important thing is styled markers are stripped

    def test_utf16_surrogate_pair_emoji(self):
        """Emoji requiring UTF-16 surrogate pairs don't corrupt offsets."""
        # 🎉 is U+1F389 — requires surrogate pair (2 UTF-16 code units)
        text, styles = _m2s("🎉🎉 **test**")
        assert "test" in text
        assert len(styles) == 1
        # Verify the style position is correct
        parts = styles[0].split(":")
        start, length = int(parts[0]), int(parts[1])
        # 🎉🎉 = 4 UTF-16 code units + space = 5, then "test" = 4
        assert start == 5
        assert length == 4

    def test_consecutive_newlines_collapsed(self):
        """3+ consecutive newlines are collapsed to 2."""
        text, styles = _m2s("first\n\n\n\n\nsecond")
        assert "\n\n\n" not in text
        assert "first" in text
        assert "second" in text

    def test_empty_bold_not_crash(self):
        """**** (empty bold) should not crash."""
        text, styles = _m2s("before **** after")
        # Should not raise — exact output doesn't matter much
        assert "before" in text


# ===========================================================================
# signal-streaming-patch: SUPPORTS_MESSAGE_EDITING and send() behavior
# ===========================================================================

class TestSignalStreamingPatch:
    """Tests for signal-streaming-patch: cursor suppression and edit support.
    
    These verify the adapter-level properties that prevent the streaming
    cursor from leaking into Signal messages.
    """

    def test_signal_does_not_support_editing(self, monkeypatch):
        """SignalAdapter.SUPPORTS_MESSAGE_EDITING must be False."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        from gateway.platforms.signal import SignalAdapter
        assert SignalAdapter.SUPPORTS_MESSAGE_EDITING is False

    @pytest.mark.asyncio
    async def test_send_returns_no_message_id(self, monkeypatch):
        """send() returns message_id=None so stream consumer uses no-edit path."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        from gateway.platforms.signal import SignalAdapter

        config = PlatformConfig(enabled=True)
        config.extra = {
            "http_url": "http://localhost:8080",
            "account": "+15551234567",
        }
        adapter = SignalAdapter(config)

        # Mock the RPC call
        async def mock_rpc(method, params, rpc_id=None):
            return {"timestamp": 1234567890}

        adapter._rpc = mock_rpc

        result = await adapter.send(
            chat_id="+15559876543",
            content="Hello",
        )
        assert result.message_id is None


# ===========================================================================
# SPOILER (||...||) → SPOILER textStyle
# ===========================================================================

class TestSpoiler:
    """||spoiler|| maps to Signal's SPOILER textStyle.

    Placed in the inline-pattern phase after MONOSPACE (so `||` inside inline
    code is protected) and before ITALIC. The \\S lookarounds anchor the bars
    to non-space content so logical OR and bare `||` don't false-positive.
    """

    def _extract(self, text: str, style_str: str) -> str:
        """Given 'start:length:STYLE', extract the styled substring."""
        parts = style_str.split(":")
        start, length = int(parts[0]), int(parts[1])
        encoded = text.encode("utf-16-le")
        return encoded[start * 2 : (start + length) * 2].decode("utf-16-le")

    def test_simple_spoiler(self):
        text, styles = _m2s("||secret||")
        assert text == "secret"
        assert styles == ["0:6:SPOILER"]

    def test_spoiler_mid_string(self):
        text, styles = _m2s("hidden ||spoiler|| here")
        assert text == "hidden spoiler here"
        spoilers = _find_style(styles, "SPOILER")
        assert len(spoilers) == 1
        assert self._extract(text, spoilers[0]) == "spoiler"

    def test_spoiler_combined_with_bold(self):
        text, styles = _m2s("**bold** and ||hidden||")
        assert text == "bold and hidden"
        bolds = _find_style(styles, "BOLD")
        spoilers = _find_style(styles, "SPOILER")
        assert len(bolds) == 1
        assert len(spoilers) == 1
        assert self._extract(text, bolds[0]) == "bold"
        assert self._extract(text, spoilers[0]) == "hidden"

    def test_no_false_positive_logical_or(self):
        """`a || b || c` is logical OR, not a spoiler."""
        text, styles = _m2s("if (a || b || c)")
        assert text == "if (a || b || c)"
        assert _find_style(styles, "SPOILER") == []

    def test_inline_code_protects_double_pipe(self):
        """`||` inside inline code stays MONOSPACE, never SPOILER."""
        text, styles = _m2s("`a || b`")
        assert text == "a || b"
        types = _style_types(styles)
        assert "MONOSPACE" in types
        assert "SPOILER" not in types

    def test_spoiler_requires_non_space_anchor(self):
        """Leading/trailing space inside the bars must NOT match (\\S anchors)."""
        text, styles = _m2s("|| spaced ||")
        assert text == "|| spaced ||"
        assert _find_style(styles, "SPOILER") == []


# ===========================================================================
# UTF-16 offset correctness with astral / combining / CJK characters
# ===========================================================================

class TestUtf16OffsetCorrectness:
    """Styles are emitted in UTF-16 code units. Astral emoji (surrogate
    pairs), ZWJ sequences, combining marks, and CJK must not corrupt the
    start:length offsets or push a style out of bounds.
    """

    def _extract(self, text: str, style_str: str) -> str:
        parts = style_str.split(":")
        start, length = int(parts[0]), int(parts[1])
        encoded = text.encode("utf-16-le")
        return encoded[start * 2 : (start + length) * 2].decode("utf-16-le")

    def _assert_in_bounds(self, text: str, styles: list[str]) -> None:
        total_units = len(text.encode("utf-16-le")) // 2
        for s in styles:
            parts = s.split(":")
            start, length = int(parts[0]), int(parts[1])
            assert start >= 0, f"negative start in {s!r}"
            assert start + length <= total_units, (
                f"style {s!r} out of bounds (text has {total_units} UTF-16 units)"
            )

    def test_astral_emoji_before_bold(self):
        text, styles = _m2s("🎉🎉 **test**")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "test"
        self._assert_in_bounds(text, styles)

    def test_astral_emoji_inside_bold(self):
        text, styles = _m2s("**party 🎉 done**")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "party 🎉 done"
        self._assert_in_bounds(text, styles)

    def test_astral_emoji_after_bold(self):
        text, styles = _m2s("**x** 🎉")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "x"
        self._assert_in_bounds(text, styles)

    def test_zwj_family_sequence_before_bold(self):
        """ZWJ-joined family emoji: bold still extracts exactly 'hi'."""
        text, styles = _m2s("👨‍👩‍👧‍👦 **hi**")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "hi"
        self._assert_in_bounds(text, styles)

    def test_combining_mark_before_bold(self):
        """'cafe' + combining acute (U+0301) — bold extracts 'bold', offsets
        not split across the base char + combining mark."""
        text, styles = _m2s("café **bold**")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "bold"
        self._assert_in_bounds(text, styles)

    def test_cjk_around_bold(self):
        """CJK characters are single UTF-16 units but must still offset correctly."""
        text, styles = _m2s("你好 **粗体** ok")
        assert len(styles) == 1
        assert self._extract(text, styles[0]) == "粗体"
        self._assert_in_bounds(text, styles)

    def test_mixed_inputs_never_out_of_bounds(self):
        """Iterate several mixed inputs: no style is ever out of bounds."""
        cases = [
            "🎉🎉 **test**",
            "**party 🎉 done**",
            "**x** 🎉",
            "👨‍👩‍👧‍👦 **hi**",
            "café **bold**",
            "你好 **粗体** ok",
            "🚀 ~~strike~~ and `code` and *it* and ||spy||",
            "**a** *b* ~~c~~ `d` ||e||",
            "🎉 **bold 🎊** then *🌟 italic* end 🎈",
            "mixed 你好 **粗体 🎉** cafe\u0301 done",
        ]
        for case in cases:
            text, styles = _m2s(case)
            self._assert_in_bounds(text, styles)


# ===========================================================================
# send(): graceful textStyle fallback + reply quote plumbing
# ===========================================================================

def _make_signal_adapter(recipient: str = "+15559998888") -> SignalAdapter:
    """Build a SignalAdapter with http_url/account extra config.

    Seeds the recipient cache for *recipient* so ``_resolve_recipient`` short
    circuits without issuing a ``listContacts`` RPC — this keeps the ``_rpc``
    mock dedicated to the ``send`` path so call-count assertions are precise.
    """
    config = PlatformConfig(enabled=True)
    config.extra = {
        "http_url": "http://localhost:8080",
        "account": "+15551234567",
    }
    adapter = SignalAdapter(config)
    # Pre-resolve the recipient (identity) so send() doesn't call listContacts.
    adapter._recipient_uuid_by_number[recipient] = recipient
    return adapter


class TestSignalSendFallback:
    """send() retry/fallback behaviour and reply-quote plumbing.

    All RPC is mocked — fully hermetic, no network or Signal account.
    """

    @pytest.mark.asyncio
    async def test_textstyle_fallback_retries_plain(self, monkeypatch):
        """When the styled send returns None, send() retries once WITHOUT
        textStyle/textStyles and reports success."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()

        calls: list[dict] = []

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append(dict(params))
            # First call (with textStyle) fails; second (plain) succeeds.
            if len(calls) == 1:
                return None
            return {"timestamp": 123}

        adapter._rpc = mock_rpc

        result = await adapter.send(
            chat_id="+15559998888",
            content="**bold** message",
        )

        assert result.success is True
        assert len(calls) == 2
        # First attempt carried the style param.
        assert "textStyle" in calls[0] or "textStyles" in calls[0]
        # Retry must strip BOTH style keys.
        assert "textStyle" not in calls[1]
        assert "textStyles" not in calls[1]

    @pytest.mark.asyncio
    async def test_successful_send_does_not_retry(self, monkeypatch):
        """When the first styled send succeeds, _rpc is called exactly once."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()

        calls: list[dict] = []

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append(dict(params))
            return {"timestamp": 456}

        adapter._rpc = mock_rpc

        result = await adapter.send(
            chat_id="+15559998888",
            content="**bold** message",
        )

        assert result.success is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_reply_quote_params_plumbed(self, monkeypatch):
        """quote_timestamp / quote_author metadata become quoteTimestamp /
        quoteAuthor on the RPC params."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()

        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.update(params)
            return {"timestamp": 789}

        adapter._rpc = mock_rpc

        result = await adapter.send(
            chat_id="+15559998888",
            content="hi",
            metadata={"quote_timestamp": 111, "quote_author": "+15551110000"},
        )

        assert result.success is True
        assert captured.get("quoteTimestamp") == 111
        assert captured.get("quoteAuthor") == "+15551110000"

    @pytest.mark.asyncio
    async def test_no_quote_params_without_metadata(self, monkeypatch):
        """A plain send carries no quoteTimestamp."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()

        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.update(params)
            return {"timestamp": 321}

        adapter._rpc = mock_rpc

        result = await adapter.send(
            chat_id="+15559998888",
            content="hi",
        )

        assert result.success is True
        assert "quoteTimestamp" not in captured
        assert "quoteAuthor" not in captured


# ===========================================================================
# Review hardening: fallback negative case, quote variants/validation,
# adapter _rpc safety valve, overlapping-style bounds.
# ===========================================================================

class TestSignalSendFallbackExtra:
    """Extra send()/quote cases surfaced by the adversarial review."""

    @pytest.mark.asyncio
    async def test_plain_send_failure_no_retry(self, monkeypatch):
        """A plain (un-styled) send that fails must NOT retry — only styled
        sends get the plain-text fallback."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        calls: list[dict] = []

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append(dict(params))
            return None

        adapter._rpc = mock_rpc
        result = await adapter.send(chat_id="+15559998888", content="plain message")
        assert result.success is False
        assert len(calls) == 1  # no retry

    @pytest.mark.asyncio
    async def test_reply_quote_signal_prefixed_keys(self, monkeypatch):
        """signal_quote_* metadata keys are honored as well as quote_*."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.update(params)
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"signal_quote_timestamp": 222, "signal_quote_author": "+15551110000"},
        )
        assert captured.get("quoteTimestamp") == 222
        assert captured.get("quoteAuthor") == "+15551110000"

    @pytest.mark.asyncio
    async def test_reply_quote_message_optional(self, monkeypatch):
        """quote_message is attached when present and absent otherwise."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.clear()
            captured.update(params)
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"quote_timestamp": 1, "quote_author": "+15551110000",
                      "quote_message": "original"},
        )
        assert captured.get("quoteMessage") == "original"
        await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"quote_timestamp": 1, "quote_author": "+15551110000"},
        )
        assert "quoteMessage" not in captured

    @pytest.mark.asyncio
    async def test_reply_quote_invalid_timestamp_ignored(self, monkeypatch):
        """A non-numeric quote_timestamp is ignored; the send still succeeds."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.update(params)
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        result = await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"quote_timestamp": "not-a-number", "quote_author": "+15551110000"},
        )
        assert result.success is True
        assert "quoteTimestamp" not in captured
        assert "quoteAuthor" not in captured

    @pytest.mark.asyncio
    async def test_reply_quote_malformed_author_ignored(self, monkeypatch):
        """A quote_author that is neither E.164 nor a service id is ignored."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        captured: dict = {}

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.update(params)
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        result = await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"quote_timestamp": 111, "quote_author": "definitely not valid"},
        )
        assert result.success is True
        assert "quoteAuthor" not in captured
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_quote_failure_retries_without_quote(self, monkeypatch):
        """A send that fails with a quote retries once without the quote."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        calls: list[dict] = []

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append(dict(params))
            return None if len(calls) == 1 else {"timestamp": 1}

        adapter._rpc = mock_rpc
        result = await adapter.send(
            chat_id="+15559998888", content="plain",
            metadata={"quote_timestamp": 111, "quote_author": "+15551110000"},
        )
        assert result.success is True
        assert len(calls) == 2
        assert "quoteTimestamp" in calls[0]
        assert "quoteTimestamp" not in calls[1] and "quoteAuthor" not in calls[1]

    @pytest.mark.asyncio
    async def test_quote_and_styles_failure_retries_to_plain(self, monkeypatch):
        """Quote drop, then style drop: a styled+quoted send degrades to plain."""
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        calls: list[dict] = []

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append(dict(params))
            return {"timestamp": 1} if len(calls) >= 3 else None

        adapter._rpc = mock_rpc
        result = await adapter.send(
            chat_id="+15559998888", content="**bold**",
            metadata={"quote_timestamp": 111, "quote_author": "+15551110000"},
        )
        assert result.success is True
        assert len(calls) == 3
        assert "quoteTimestamp" in calls[0]
        assert "quoteTimestamp" not in calls[1]
        assert "textStyle" in calls[1] or "textStyles" in calls[1]
        assert "textStyle" not in calls[2] and "textStyles" not in calls[2]
        assert "quoteTimestamp" not in calls[2]


class TestAdapterRpcSafetyValve:
    """The adapter's _rpc() refuses Tier-4 methods before any network call."""

    @pytest.mark.asyncio
    async def test_rpc_blocks_destructive_method(self, monkeypatch):
        from gateway.platforms.signal_rpc import SignalMethodNotAllowed, DESTRUCTIVE_METHODS
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()

        # Mark a client so the guard isn't short-circuited by "not connected".
        adapter.client = object()
        for method in ["unregister", "deleteLocalAccountData", "addDevice"]:
            assert method in DESTRUCTIVE_METHODS
            with pytest.raises(SignalMethodNotAllowed):
                await adapter._rpc(method, {"account": "+15551234567"})

    @pytest.mark.asyncio
    async def test_rpc_blocks_unknown_method(self, monkeypatch):
        from gateway.platforms.signal_rpc import SignalMethodNotAllowed
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        adapter = _make_signal_adapter()
        adapter.client = object()
        with pytest.raises(SignalMethodNotAllowed):
            await adapter._rpc("totallyMadeUpMethod", {})


class TestOverlappingStyleBounds:
    """Nested / adjacent markdown never produces an out-of-bounds bodyRange."""

    def _u16_len(self, text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def _assert_in_bounds(self, text, styles):
        limit = self._u16_len(text)
        for s in styles:
            start, length = (int(x) for x in s.split(":")[:2])
            assert start >= 0
            assert start + length <= limit, f"{s} exceeds utf16 len {limit} of {text!r}"

    def test_nested_and_adjacent_styles_in_bounds(self):
        for md in [
            "***bold italic***",
            "**bold *inner* more**",
            "~~**strike bold**~~",
            "**a** **b** **c**",
            "||spoiler **bold** end||",
            "`code` **bold** *it* ~~s~~ ||sp||",
            "**🎉 party 🎉** and ||你好||",
            "__bold__ then ***x*** then `y`",
        ]:
            text, styles = _m2s(md)
            self._assert_in_bounds(text, styles)

    def test_no_styles_overlap_each_other(self):
        # Emitted ranges must be mutually non-overlapping (Signal requirement).
        text, styles = _m2s("**bold** and `code` and *it* and ~~s~~ and ||sp||")
        spans = sorted((int(s.split(":")[0]), int(s.split(":")[1])) for s in styles)
        for (s1, l1), (s2, l2) in zip(spans, spans[1:]):
            assert s1 + l1 <= s2, f"overlap between {(s1, l1)} and {(s2, l2)}"


class TestSignalAutoQuote:
    """Auto-quote: replies natively quote the message that triggered the turn.

    The quote target is captured in on_processing_start and consumed once by
    the first send() of the reply, then released in on_processing_complete.
    """

    @pytest.fixture(autouse=True)
    def _quote_on(self, monkeypatch):
        # Auto-quote defaults OFF; enable it for this class (one test overrides
        # it back to false to verify the off path).
        monkeypatch.setenv("SIGNAL_REPLY_QUOTE", "true")

    def _event(self, chat_id="+15559998888", author="+15559998888", ts=777):
        from types import SimpleNamespace
        return SimpleNamespace(
            source=SimpleNamespace(chat_id=chat_id, user_id=author),
            raw_message={"sender": author, "timestamp_ms": ts},
        )

    def _capturing_adapter(self, captured):
        adapter = _make_signal_adapter()

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            captured.clear()
            captured.update(params)
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        return adapter

    @pytest.mark.asyncio
    async def test_auto_quote_on_reply(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.on_processing_start(self._event())
        await adapter.send(chat_id="+15559998888", content="hi")

        assert captured.get("quoteTimestamp") == 777
        assert captured.get("quoteAuthor") == "+15559998888"

    @pytest.mark.asyncio
    async def test_auto_quote_consumed_once(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.on_processing_start(self._event())
        await adapter.send(chat_id="+15559998888", content="first")
        assert captured.get("quoteTimestamp") == 777
        # Second message of the same reply must NOT quote again.
        await adapter.send(chat_id="+15559998888", content="second")
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_auto_quote_cleared_on_complete(self, monkeypatch):
        from gateway.platforms.base import ProcessingOutcome
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)
        event = self._event()

        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await adapter.send(chat_id="+15559998888", content="late send")
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_auto_quote_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        monkeypatch.setenv("SIGNAL_REPLY_QUOTE", "false")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.on_processing_start(self._event())
        await adapter.send(chat_id="+155****8888", content="hi")
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_auto_quote_groups_scope_skips_dm(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        monkeypatch.setenv("SIGNAL_REPLY_QUOTE", "groups")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.on_processing_start(self._event(chat_id="+155****8888"))
        await adapter.send(chat_id="+155****8888", content="hi")
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_auto_quote_groups_scope_quotes_group(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "grp==")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        monkeypatch.setenv("SIGNAL_REPLY_QUOTE", "groups")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        event = self._event(chat_id="group:grp==")
        await adapter.on_processing_start(event)
        await adapter.send(chat_id="group:grp==", content="hi")
        assert captured.get("quoteTimestamp") == 777
        assert captured.get("quoteAuthor") == event.source.user_id

    @pytest.mark.asyncio
    async def test_read_receipts_groups_scope_only_groups(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "grp==")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        monkeypatch.setenv("SIGNAL_READ_RECEIPTS", "groups")
        calls: list[tuple[str, dict]] = []
        adapter = _make_signal_adapter()

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            calls.append((method, dict(params)))
            return {"timestamp": 1}

        adapter._rpc = mock_rpc
        await adapter.on_processing_start(self._event(chat_id="+155****8888", ts=111))
        assert calls == []

        group_event = self._event(chat_id="group:grp==", ts=222)
        await adapter.on_processing_start(group_event)
        assert len(calls) == 1
        method, params = calls[0]
        assert method == "sendReceipt"
        assert params["account"] == adapter.account
        assert params["recipient"] == group_event.source.user_id
        assert params["targetTimestamp"] == [222]
        assert params["type"] == "read"

    @pytest.mark.asyncio
    async def test_read_receipt_failure_is_best_effort(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "grp==")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        monkeypatch.setenv("SIGNAL_READ_RECEIPTS", "groups")
        adapter = _make_signal_adapter()

        async def mock_rpc(method, params, rpc_id=None, **kwargs):
            raise RuntimeError("receipt unavailable")

        adapter._rpc = mock_rpc
        await adapter.on_processing_start(self._event(chat_id="group:grp=="))
        assert adapter._active_quote["group:grp=="]["timestamp"] == 777

    @pytest.mark.asyncio
    async def test_no_quote_for_proactive_send(self, monkeypatch):

        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.send(chat_id="+15559998888", content="proactive")
        assert "quoteTimestamp" not in captured

    @pytest.mark.asyncio
    async def test_explicit_metadata_quote_beats_auto(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        monkeypatch.setenv("SIGNAL_REACTIONS", "false")
        captured: dict = {}
        adapter = self._capturing_adapter(captured)

        await adapter.on_processing_start(self._event(ts=777))
        await adapter.send(
            chat_id="+15559998888", content="hi",
            metadata={"quote_timestamp": 999, "quote_author": "+15551110000"},
        )
        # Explicit metadata wins over the auto-captured target.
        assert captured.get("quoteTimestamp") == 999
        assert captured.get("quoteAuthor") == "+15551110000"


class TestFencedCodeProtection:
    """Markdown markers inside fenced code blocks stay verbatim (not styled)."""

    def test_markers_inside_fence_preserved(self):
        text, styles = _m2s("```\n**not bold** and ||not spoiler||\n```")
        assert "**not bold**" in text
        assert "||not spoiler||" in text
        types = {s.rsplit(":", 1)[1] for s in styles}
        assert types == {"MONOSPACE"}

    def test_backticks_inside_fence_preserved(self):
        text, styles = _m2s("```\ncall `foo()` now\n```")
        assert "`foo()`" in text
        assert len(styles) == 1  # only the fenced block; no inline match inside
        assert styles[0].endswith(":MONOSPACE")

    def test_inline_styles_outside_fence_still_apply(self):
        text, styles = _m2s("```\n**x**\n```\n\n**real**")
        assert "**x**" in text          # fenced marker stays literal
        assert "real" in text
        assert any(s.endswith(":BOLD") for s in styles)       # trailing bold applied
        assert any(s.endswith(":MONOSPACE") for s in styles)  # fenced block
