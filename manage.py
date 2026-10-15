"""Server admin CLI.

  python manage.py invite [N]      create N one-time invite codes (default 1)
  python manage.py users           list registered users
  python manage.py revoke USER     log USER out everywhere (delete their tokens)
  python manage.py delete USER     delete a user account and all their data
"""
import os
import secrets
import sys
import time

os.environ.setdefault("MSG_NO_AUTOAPP", "1")
from app.main import open_db  # noqa: E402


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    db = open_db()
    cmd = argv[0]
    if cmd == "invite":
        for _ in range(int(argv[1]) if len(argv) > 1 else 1):
            code = secrets.token_urlsafe(12)
            db.execute("INSERT INTO invites VALUES(?,?,NULL)", (code, int(time.time())))
            print(code)
    elif cmd == "users":
        for r in db.execute("SELECT username, is_admin, created_at FROM users ORDER BY id"):
            print(f"{r['username']:<32} {'admin' if r['is_admin'] else '':<6} "
                  f"{time.strftime('%Y-%m-%d', time.localtime(r['created_at']))}")
    elif cmd == "revoke" and len(argv) == 2:
        n = db.execute("DELETE FROM tokens WHERE user_id=(SELECT id FROM users WHERE username=?)", (argv[1],)).rowcount
        print(f"revoked {n} session(s)")
    elif cmd == "delete" and len(argv) == 2:
        n = db.execute("DELETE FROM users WHERE username=?", (argv[1],)).rowcount
        print("deleted" if n else "no such user")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
