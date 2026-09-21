#!/usr/bin/env python3
"""Generate a fresh AES-256 (Fernet) ENCRYPTION_KEY.

Usage:
    python scripts/generate_cipher_key.py [--urlsafe]
"""

from __future__ import annotations

import base64
import os
import sys


def main() -> None:
    raw = os.urandom(32)
    key = base64.urlsafe_b64encode(raw).decode("ascii")
    print(f"ENCRYPTION_KEY={key}")
    print("", file=sys.stderr)
    print(
        "→ Put it in your .env file and BACK IT UP somewhere safe. "
        "Losing this key makes stored tokens unrecoverable.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
