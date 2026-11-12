"""Create a user:  python -m app.scripts.create_user EMAIL ROLE   (password is prompted)"""
from __future__ import annotations

import getpass
import sys

from ..core.config import get_settings
from ..database.base import Database
from ..security.roles import Role
from ..security.users import create_user


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    password = getpass.getpass("Password (min 12 chars): ")
    with Database(get_settings().database_url).session() as session:
        user = create_user(session, sys.argv[1], password, Role(sys.argv[2]), actor="cli")
    print(f"Created {user.email} ({user.role})")


if __name__ == "__main__":
    main()
