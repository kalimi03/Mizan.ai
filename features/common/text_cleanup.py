"""
Mizan.ai — cleanup for raw LaTeX math notation in model replies.

QwenBrain/QwenLite default to LaTeX (\\[ \\], \\frac{}{}, \\times, \\text{},
etc.) for anything involving a formula, even when explicitly told not to in
the system prompt — this is a stubborn habit from math-heavy pretraining
data, not something a prompt instruction reliably suppresses on its own
(confirmed live: the prompt-only fix in features/explainer/prompts.py did
not stop it). Every frontend page renders replies as plain text, not
rendered math, so raw LaTeX shows up as broken-looking backslash-and-brace
soup. This is a safety-net cleanup applied to every model reply, on top of
(not instead of) the prompt instruction.

Not a full LaTeX parser — just the patterns actually observed in real
replies (basic formulas: fractions, multiplication, text wrappers, math
delimiters). Nested/advanced LaTeX may not fully clean up, but that's not
what these models are generating in practice.
"""

import re

_REPLACEMENTS = [
    (re.compile(r"\\\[|\\\]"), ""),                          # \[ \]  display math delimiters
    (re.compile(r"\\\(|\\\)"), ""),                           # \( \)  inline math delimiters
    (re.compile(r"\$\$|\$"), ""),                             # $$ ... $$ / $ ... $  dollar math delimiters
    (re.compile(r"\\text\{([^{}]*)\}"), r"\1"),               # \text{X} -> X
    (re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}"), r"(\1/\2)"),  # \frac{a}{b} -> (a/b)
    (re.compile(r"\\times"), "x"),
    (re.compile(r"\\div"), "/"),
    (re.compile(r"\\cdot"), "x"),
    (re.compile(r"\\left|\\right"), ""),
]


def strip_latex_math(text: str) -> str:
    if not text:
        return text
    cleaned = text
    for pattern, replacement in _REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    # Removed delimiters often leave a stray leading/trailing space on
    # their line (e.g. "\[ VAT = ..." -> " VAT = ...") — trim per line
    # rather than the whole string, so paragraph breaks survive.
    return "\n".join(line.strip() for line in cleaned.split("\n"))
