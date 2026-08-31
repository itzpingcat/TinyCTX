"""
utils/sanitize.py — Content sanitization helpers.

Two independent sanitizers live here:

  sanitize_brackets()  — strip Unicode bracket look-alikes so user content
                          cannot spoof the 【author】: speaker prefix injected
                          in context.py.

  sanitize_special_tokens() — strip LLM special/control tokens (e.g.
                          <|im_start|>, <start_of_turn>, [INST]) so prompt
                          injection can't forge fake turn boundaries in the
                          context window.
"""

import re

# ---------------------------------------------------------------------------
# Bracket homoglyph sanitizer
# ---------------------------------------------------------------------------

# Homoglyph sanitizer — strip Unicode bracket look-alikes from user content
# so they cannot spoof the 【author】: speaker prefix injected in context.py.

_HOMOGLYPH_TABLE = str.maketrans({
    # Fullwidth
    '（': '(', '）': ')',   # （）
    '［': '[', '］': ']',   # ［］
    '｛': '{', '｝': '}',   # ｛｝
    '＜': '<', '＞': '>',   # ＜＞
    # CJK / mathematical angle brackets
    '⟨': '<', '⟩': '>',   # ⟨⟩
    '〈': '<', '〉': '>',   # 〈〉
    '《': '<', '》': '>',   # 《》
    '「': '[', '」': ']',   # 「」
    '『': '[', '』': ']',   # 『』
    '【': '[', '】': ']',   # 【】  ← the delimiters we use for labels
    '〔': '(', '〕': ')',   # 〔〕
    '〖': '[', '〗': ']',   # 〖〗
    '﹙': '{', '﹚': '}',   # ﹙﹚ small
    '﹛': '{', '﹜': '}',   # ﹛﹜ small
    '﹝': '[', '﹞': ']',   # ﹝﹞ small
    '❨': '(', '❩': ')',   # ❨❩ medium
    '❪': '(', '❫': ')',   # ❪❫
    '❬': '<', '❭': '>',   # ❬❭
    '❮': '(', '❯': ')',   # ❮❯ (arrow-heavy, close enough)
    '❰': '<', '❱': '>',   # ❰❱
    '❲': '[', '❳': ']',   # ❲❳
    '❴': '{', '❵': '}',   # ❴❵
})


def sanitize_brackets(text: str) -> str:
    """Replace Unicode bracket homoglyphs with their ASCII equivalents."""
    return text.translate(_HOMOGLYPH_TABLE)


# ---------------------------------------------------------------------------
# Special-token sanitizer
# ---------------------------------------------------------------------------
#
# Strategy: most model families' special tokens are a bare identifier
# wrapped in a delimiter pair — <|im_start|>, <|im_end|>, [TOOL_CALLS], etc.
# Rather than hand-writing a regex per token, we list the bare identifiers
# in SPECIAL_TOKEN_STRINGS and generate a regex that matches any of the
# common wrapper shapes: "<|X|>", "<|X", "<X|>", "<X>".
#
# Irregular tokens that don't fit that "identifier + wrapper" shape (e.g.
# Mistral's "[INST]" / "[/INST]", Nemotron's "<extra_id_N>") are hardcoded
# separately in _HARDCODED_PATTERNS.

# Bare token identifiers, wrapped by _build_token_regex() into every common
# delimiter shape: <|X|>, <|X, X|>, <X>.
SPECIAL_TOKEN_STRINGS = [
    # Qwen2.5 / Qwen family
    "im_start", "im_end", "endoftext",
    "vision_start", "vision_end", "vision_pad", "image_pad", "video_pad",
    "object_ref_start", "object_ref_end", "box_start", "box_end",
    "quad_start", "quad_end",
    "fim_prefix", "fim_middle", "fim_suffix", "fim_pad",
    "repo_name", "file_sep",

    # Gemma 2 / 3 / 4 family
    "bos", "eos", "pad", "unk",
    "start_of_turn", "end_of_turn", "turn", "boi", "eoi",

    # Harmony-style channel tokens (gpt-oss and similar)
    "channel", "tool_call", "tool_response", "thought",
    "analysis", "commentary", "final",

    # Granite 4.0+
    "start_of_role", "end_of_role", "end_of_text",

    # Nemotron generic sentinels
    "s", "/s",

    # Common chat / role delimiters (bare and piped variants)
    "user", "assistant", "system", "thinking", "reasoning", "tool", "response",
]

# Patterns that don't fit the "identifier wrapped in a uniform delimiter"
# shape — written out in full.
_HARDCODED_PATTERNS = [
    # Mistral V3 / Tekken
    r"\[\s*(?:INST|/?INST|AVAILABLE_TOOLS|/?AVAILABLE_TOOLS|TOOL_CALLS|"
    r"TOOL_RESULTS|/?TOOL_RESULTS|PREFIX|MIDDLE|SUFFIX)\s*\]",

    # Granite structured-output wrappers
    r"</?(?:tools|tool_call|tool_response|documents)>",

    # Nemotron <extra_id_N> sentinels
    r"<extra_id_\d+>",
]


def _build_token_regex(token_strings: list[str]) -> re.Pattern:
    """Build one combined regex matching any of token_strings in any of the
    common special-token wrapper shapes — opener "<" or "<|", independently
    paired with closer ">" or "|>":

        <X>   <X|>   <|X>   <|X|>   <|X ...>   </X>

    plus the hardcoded irregular patterns.
    """
    escaped = sorted((re.escape(t) for t in token_strings), key=len, reverse=True)
    alternation = "|".join(escaped)

    # Whitespace/slash around the identifier is bounded (\s{0,4}, not \s*)
    # to prevent catastrophic backtracking: two adjacent unbounded \s*
    # runs around an optional \/? let the engine try every way of
    # splitting a long run of whitespace between them, which is
    # exponential when the expected closing delimiter never shows up
    # (e.g. an attacker sending "<" followed by 50k spaces). Real special
    # tokens never contain more than a token or two of whitespace, so the
    # bound costs no coverage.
    wrapped = [
        rf"<\|(?:{alternation})[^>]*?\|?>",           # <|X|>, <|X ...>, <|X>
        rf"<\s{{0,4}}/?\s{{0,4}}(?:{alternation})\s{{0,4}}\|>",  # <X|>, </X|>
        rf"<\s{{0,4}}/?\s{{0,4}}(?:{alternation})\s{{0,4}}>",    # <X>, </X>
    ]

    all_patterns = wrapped + _HARDCODED_PATTERNS
    combined = "|".join(f"(?:{p})" for p in all_patterns)
    return re.compile(combined, re.IGNORECASE)


_SPECIAL_TOKEN_RE = _build_token_regex(SPECIAL_TOKEN_STRINGS)


def sanitize_special_tokens(text: str, extra_patterns: list[str] | None = None) -> str:
    """Strip known LLM special/control tokens from text, then collapse
    redundant horizontal whitespace left behind.

    extra_patterns, if given, is a list of additional raw regex patterns
    (e.g. loaded from a project-specific blacklist file) applied on top of
    the built-in set. These are NOT ReDoS-checked — only pass patterns from
    a trusted, operator-authored source (never derived from user/model
    content), and avoid adjacent unbounded quantifiers (e.g. \s*\s*,
    (a+)+) which can cause catastrophic backtracking on adversarial input.
    """
    pattern = _SPECIAL_TOKEN_RE
    if extra_patterns:
        combined = "|".join([_SPECIAL_TOKEN_RE.pattern] + [f"(?:{p})" for p in extra_patterns])
        pattern = re.compile(combined, re.IGNORECASE)

    cleaned = pattern.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned
