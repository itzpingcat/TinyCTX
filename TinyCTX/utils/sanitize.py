"""
utils/sanitize.py — Unicode sanitization helpers.
"""

# Homoglyph sanitizer — strip Unicode bracket look-alikes from user content
# so they cannot spoof the 【author】: speaker prefix injected in context.py.

_HOMOGLYPH_TABLE = str.maketrans({
    # Fullwidth
    '\uFF08': '(', '\uFF09': ')',   # （）
    '\uFF3B': '[', '\uFF3D': ']',   # ［］
    '\uFF5B': '{', '\uFF5D': '}',   # ｛｝
    '\uFF1C': '<', '\uFF1E': '>',   # ＜＞
    # CJK / mathematical angle brackets
    '\u27E8': '<', '\u27E9': '>',   # ⟨⟩
    '\u3008': '<', '\u3009': '>',   # 〈〉
    '\u300A': '<', '\u300B': '>',   # 《》
    '\u300C': '[', '\u300D': ']',   # 「」
    '\u300E': '[', '\u300F': ']',   # 『』
    '\u3010': '[', '\u3011': ']',   # 【】  ← the delimiters we use for labels
    '\u3014': '(', '\u3015': ')',   # 〔〕
    '\u3016': '[', '\u3017': ']',   # 〖〗
    '\uFE59': '{', '\uFE5A': '}',   # ﹙﹚ small
    '\uFE5B': '{', '\uFE5C': '}',   # ﹛﹜ small
    '\uFE5D': '[', '\uFE5E': ']',   # ﹝﹞ small
    '\u2768': '(', '\u2769': ')',   # ❨❩ medium
    '\u276A': '(', '\u276B': ')',   # ❪❫
    '\u276C': '<', '\u276D': '>',   # ❬❭
    '\u276E': '(', '\u276F': ')',   # ❮❯ (arrow-heavy, close enough)
    '\u2770': '<', '\u2771': '>',   # ❰❱
    '\u2772': '[', '\u2773': ']',   # ❲❳
    '\u2774': '{', '\u2775': '}',   # ❴❵
})


def sanitize_brackets(text: str) -> str:
    """Replace Unicode bracket homoglyphs with their ASCII equivalents."""
    return text.translate(_HOMOGLYPH_TABLE)
