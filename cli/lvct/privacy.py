"""Shared client/server minimisation for voluntary support diagnostics."""
from __future__ import annotations
import re
from . import config
from .console import clean

TOKEN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gd_pat_[A-Za-z0-9_]{20,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|\b\d{5,}:[A-Za-z0-9_-]{20,})')
KEY_VALUE = re.compile(r'''(?i)(["']?(?:[a-z0-9]+[._-])*(?:password|passphrase|token|secret|session|api[_-]?key|api[_-]?hash|authorization|cookie)(?:[._-][a-z0-9]+)*["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''')
EMAIL = re.compile(r'\b[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PATH = re.compile(r'''(?:[A-Za-z]:[\\/]|/)(?:[^\s"'<>:,;]+[\\/])*[^\s"'<>:,;]*''')
URL = re.compile(r'https?://[^\s<>"\x27]+', re.I)
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])')
IPV6 = re.compile(r'(?<![\w])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?![\w])', re.I)
PHONE = re.compile(r'(?<!\w)\+\d[\d ()-]{7,}\d')
NUMERIC_ID = re.compile(r'(?<![\w])\d{5,}(?![\w])')
PRIVATE_KEY = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', re.S)


def scrub(text: str, values: dict[str, str] | None = None, *, limit: int = 2000) -> str:
    text = config.redact(clean(text), values)
    text = PRIVATE_KEY.sub('[private key removed]', text)
    text = TOKEN.sub('[credential removed]', text)
    text = KEY_VALUE.sub(lambda m: m.group(1) + '[hidden]', text)
    text = URL.sub('[url]', text)
    text = EMAIL.sub('[email]', text)
    text = PHONE.sub('[phone]', text)
    text = IPV4.sub('[ip]', text)
    text = IPV6.sub('[ip]', text)
    text = PATH.sub('[path]', text)
    text = NUMERIC_ID.sub('[id]', text)
    return text[:limit]
