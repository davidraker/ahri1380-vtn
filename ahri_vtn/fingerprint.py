"""
OpenADR 2.0b certificate fingerprints (Profile Specification 10.5.1): the last 10 bytes of the
SHA-256 of the DER certificate, upper-case hex pairs separated by colons (29 characters).
Kept free of other imports so the configuration module can validate fingerprints too.
"""
from __future__ import annotations

import hashlib

HEX = set("0123456789ABCDEF")


def openadr_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(44, 64, 2))


def sha256_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, 64, 2))


def normalize_fingerprint(value: str) -> str:
    """
    Accept the 29-character OpenADR form or a full SHA-256 fingerprint (with or without colons or
    spaces, any case) and return the OpenADR form. Raises ValueError for anything else.
    """
    cleaned = "".join(ch for ch in str(value).upper() if ch not in ": \t\r\n")
    if not cleaned or any(ch not in HEX for ch in cleaned):
        raise ValueError("A fingerprint consists of hexadecimal digits (XX:XX:...:XX)")
    if len(cleaned) == 20:
        return ":".join(cleaned[i:i + 2] for i in range(0, 20, 2))
    if len(cleaned) == 64:
        return ":".join(cleaned[i:i + 2] for i in range(44, 64, 2))
    raise ValueError("A fingerprint is the 10-byte OpenADR form (XX:XX:...:XX, 29 characters) or a full "
                     "SHA-256 fingerprint (32 bytes)")
