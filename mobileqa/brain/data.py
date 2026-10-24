"""Test-data generator for form fields. The same values a seasoned QA engineer types
by reflex: the happy path, the empty string, the 10,000-character string, the emoji,
the SQL, the negative number, the leap day."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from ..driver.base import Element


@dataclass
class Input:
    value: str
    kind: str          # valid | empty | boundary | invalid | hostile | unicode
    expect_accept: bool | None   # None = either way is fine, but must not crash


_LONG = "A" * 2000
_UNICODE = "Ünïcødé 测试 日本語 العربية 🚀🔥 ‏RTL‎ Z̷̢a̶l̸g̕o"
_HOSTILE = ["' OR 1=1 --", "<script>alert(1)</script>", "{{7*7}}", "%s%s%s%n", "../../etc/passwd",
            "\u0000null", "Robert'); DROP TABLE users;--", "=1+1", "${jndi:ldap://x}"]


def classify(el: Element) -> str:
    hint = " ".join([el.label, el.resource_id, el.text]).lower()
    if el.password or "password" in hint or "passcode" in hint:
        return "password"
    if "email" in hint or "e-mail" in hint:
        return "email"
    if "phone" in hint or "mobile" in hint or "tel" in hint:
        return "phone"
    if re.search(r"\b(zip|postal|postcode)\b", hint):
        return "zip"
    if re.search(r"\b(date|dob|birth)\b", hint):
        return "date"
    if re.search(r"\b(amount|price|qty|quantity|number|count|age|weight|height)\b", hint):
        return "number"
    if "search" in hint or "find" in hint:
        return "search"
    if re.search(r"\b(url|website|link)\b", hint):
        return "url"
    if re.search(r"\b(name|first|last|user)\b", hint):
        return "name"
    if re.search(r"\b(card|cvv|cvc|pan)\b", hint):
        return "card"
    if re.search(r"\b(pin|otp|code)\b", hint):
        return "code"
    return "text"


VALID = {
    "password": "Str0ng!Pass#2026",
    "email": "qa.agent+test@example.com",
    "phone": "+1 630 555 0142",
    "zip": "60585",
    "date": "02/29/2024",
    "number": "42",
    "search": "test",
    "url": "https://example.com",
    "name": "Alex Rivera-O'Neil",
    "card": "4242424242424242",
    "code": "123456",
    "text": "The quick brown fox",
}

INVALID = {
    "password": ["123", " "],
    "email": ["not-an-email", "a@b", "user@@example.com", "user@.com", " spaced@example.com "],
    "phone": ["abc", "123", "+1 (630) 555-01420000000"],
    "zip": ["ABCDE", "1", "000000000"],
    "date": ["13/45/2024", "02/30/2023", "00/00/0000", "2024-99-99"],
    "number": ["-1", "0", "1e309", "99999999999999999999", "1.000000001", "abc", "٣٤"],
    "search": [],
    "url": ["htp:/broken", "javascript:alert(1)", "http://"],
    "name": ["", "   "],
    "card": ["1234567890123456", "4242 4242 4242 424"],
    "code": ["000000", "12", "abcdef"],
    "text": [],
}


def inputs_for(el: Element, login: dict[str, str] | None = None, include_hostile: bool = True) -> list[Input]:
    kind = classify(el)
    out: list[Input] = []
    # Real credentials if we have them for this field
    if login:
        for k, v in login.items():
            if k.lower() in " ".join([el.label, el.resource_id]).lower() or (kind == k.lower()):
                out.append(Input(v, "valid", True))
                break
    if not out:
        out.append(Input(VALID[kind], "valid", True))
    out.append(Input("", "empty", False if kind in ("password", "email") else None))
    out += [Input(v, "invalid", False) for v in INVALID.get(kind, [])]
    out.append(Input(_LONG, "boundary", None))
    out.append(Input(_UNICODE, "unicode", None))
    out.append(Input(" leading and trailing  ", "boundary", None))
    if include_hostile and kind not in ("password",):
        out.append(Input(random.choice(_HOSTILE), "hostile", None))
    return out


def valid_value(el: Element, login: dict[str, str] | None = None) -> str:
    return inputs_for(el, login, include_hostile=False)[0].value
