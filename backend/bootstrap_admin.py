"""Set up the first local administrator without storing a plaintext password."""
from __future__ import annotations

import getpass
import hashlib
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ADMIN_EMAIL = "frommahitoyou@gmail.com"


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(18)
    rounds = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${digest}"


def main() -> None:
    password = getpass.getpass(f"Set password for {ADMIN_EMAIL}: ")
    if len(password) < 8:
        raise SystemExit("Use a password with at least 8 characters.")
    confirm = getpass.getpass("Confirm administrator password: ")
    if password != confirm:
        raise SystemExit("Passwords did not match; no changes were made.")

    existing = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    values = {
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "ADMIN_PASSWORD_HASH": hash_password(password),
    }
    if not any(line.startswith("SESSION_SECRET=") and line.partition("=")[2].strip() for line in existing):
        values["SESSION_SECRET"] = secrets.token_urlsafe(48)
    output = []
    seen: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            if key not in seen:
                output.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    print(f"Administrator configured for {ADMIN_EMAIL}. Password hash and session secret saved to .env.")


if __name__ == "__main__":
    main()
