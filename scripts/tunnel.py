"""Temporary public HTTPS address for the local API (admin panel, webhooks), via Cloudflare quick
tunnel. Development only: the address changes on every start and works while this runs.

    make run      # the API on :8000
    make tunnel   # prints the address and saves it to .public_url
    /admin        # in Owner Bot (founders only): one-time login link on that address

Needs `cloudflared` (brew install cloudflared). The file is removed when the tunnel stops, so
the bot never hands out a dead address.
"""

import re
import subprocess
import sys
from pathlib import Path

PORT = 8000
URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
FILE = Path(__file__).resolve().parent.parent / ".public_url"


def main() -> int:
    FILE.unlink(missing_ok=True)  # an address left by a crashed run is dead
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        print("cloudflared not found: brew install cloudflared", file=sys.stderr)
        return 1
    try:
        for line in proc.stdout:
            match = URL.search(line)
            if match and not FILE.exists():
                FILE.write_text(match.group(0), encoding="utf-8")
                print(f"\nAdmin panel: {match.group(0)}/admin")
                print("Get a login link: /admin in Owner Bot\n")
    except KeyboardInterrupt:
        pass
    finally:
        FILE.unlink(missing_ok=True)
        proc.terminate()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
