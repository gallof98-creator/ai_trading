"""Create a ready-to-use .env with freshly generated secrets (standard library only).

    docker run --rm -v "${PWD}:/w" -w /w python:3.12-slim python infrastructure/scripts/make_env.py

It never overwrites an existing .env. The admin password is printed once: write it down.
"""
import base64
import os
import secrets
import sys
from pathlib import Path

target = Path(".env")
if target.exists():
    sys.exit(".env already exists - delete it first if you really want a new one.")
email = sys.argv[1] if len(sys.argv) > 1 else "admin@example.com"
password = secrets.token_urlsafe(12)
target.write_text("\n".join([
    f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
    "QAT_ENVIRONMENT=production",
    f"QAT_JWT_SECRET={secrets.token_urlsafe(48)}",
    f"QAT_CREDENTIAL_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}",  # a valid Fernet key
    f"QAT_ADMIN_EMAIL={email}",
    f"QAT_ADMIN_PASSWORD={password}",
    "",
]), encoding="utf-8", newline="\n")
print("Created .env")
print(f"  Sign-in email:    {email}")
print(f"  Sign-in password: {password}")
print("Next: docker compose up -d --build   then open http://localhost:3000")
