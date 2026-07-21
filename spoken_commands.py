"""
Spoken symbol commands — convert dictated character names into the characters.

    "slash settings"                    -> "/settings"
    "ryan underscore murphy"            -> "ryan_murphy"
    "hashtag safety first"              -> "#safety first"
    "open bracket see attached close bracket" -> "(see attached)"

Deliberately conservative: only symbol NAMES that are almost never dictated as
literal prose. Plain punctuation words ("comma", "full stop", "period", "dot",
"question mark") are NOT converted — Parakeet already punctuates from prosody,
and those words appear in ordinary English far too often to rewrite safely
("over a period of time", "a question mark over the plan", "a new line of
products"). "at sign" is also excluded ("look at sign 4" in a safety audit);
say "at symbol" for @.

Enabled via Config.spoken_punctuation (default on).
"""

import re

_S = r"[ \t]*"  # horizontal space only — never swallow newlines

# Each entry: (spoken-name regex, symbol). Grouped by how the symbol binds to
# its neighbours. Order inside a group matters where names overlap
# ("forward slash" before "slash").
_JOIN_BOTH = [   # swallow space on both sides: "foo slash bar" -> "foo/bar"
    ("forward slash", "/"),
    ("back ?slash", "\\"),
    ("slash", "/"),
    ("underscore", "_"),
    ("hyphen", "-"),
    ("at symbol", "@"),
]
_JOIN_RIGHT = [  # swallow the following space: "hashtag safety" -> "#safety"
    ("hash ?tag", "#"),
    ("dollar sign", "$"),
    ("pound sign", "£"),
    ("euro sign", "€"),
    ("tilde", "~"),
    ("open square bracket", "["),
    ("open curly bracket", "{"),
    ("open brace", "{"),
    ("open bracket", "("),
    ("open paren(?:thesis)?", "("),
]
_JOIN_LEFT = [   # swallow the preceding space: "50 percent sign" -> "50%"
    ("percent sign", "%"),
    ("semicolon", ";"),
    ("close square bracket", "]"),
    ("close curly bracket", "}"),
    ("close brace", "}"),
    ("close bracket", ")"),
    ("close paren(?:thesis)?", ")"),
]
_SPACED = [      # plain word swap, spacing untouched: "A ampersand B" -> "A & B"
    ("ampersand", "&"),
    ("asterisk", "*"),
    ("plus sign", "+"),
    ("equals? sign", "="),
    ("pipe symbol", "|"),
    ("vertical bar", "|"),
    ("caret", "^"),
]

_RULES: list[tuple[re.Pattern, str]] = (
    [(re.compile(rf"{_S}\b(?:{p})\b{_S}", re.IGNORECASE), s) for p, s in _JOIN_BOTH]
    + [(re.compile(rf"\b(?:{p})\b{_S}", re.IGNORECASE), s) for p, s in _JOIN_RIGHT]
    + [(re.compile(rf"{_S}\b(?:{p})\b", re.IGNORECASE), s) for p, s in _JOIN_LEFT]
    + [(re.compile(rf"\b(?:{p})\b", re.IGNORECASE), s) for p, s in _SPACED]
)


def apply_spoken_commands(text: str) -> str:
    """Replace spoken symbol names with the symbols themselves."""
    if not text:
        return text
    out = text
    for pat, sym in _RULES:
        # Function replacement — literal symbol, immune to "\\"/"$" escaping.
        out = pat.sub(lambda m, s=sym: s, out)
    if out != text:
        out = re.sub(r" {2,}", " ", out)
        out = re.sub(r" +([.,!?;:])", r"\1", out)
        out = out.strip()
    return out
