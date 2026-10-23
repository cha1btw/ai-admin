"""Build the public static site into dist/sites (pushed to the public repo cha1btw/ai-admin-sites).

    uv run python scripts/build_sites.py [out_dir]

landing/     the marketing landing (landing/index.html as is)

The admin panel is not published as a static site any more: it is the real one, served by the
API (`/admin`), and shows the studios of the bot's database. Only static files go out here:
no bot code, no keys, no real studio data.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERCEL = {
    "cleanUrls": True,
    "headers": [
        {
            "source": "/(.*)",
            "headers": [
                {"key": "X-Content-Type-Options", "value": "nosniff"},
                {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
            ],
        }
    ],
}
README = """# AI-адміністратор: сайти

Публічний статичний сайт проєкту. Код бота лежить в окремому приватному репозиторії, звідки ці
файли збирає `scripts/build_sites.py`. Тут нічого не редагуйте вручну.

- `landing/` — лендинг для студій.

Адмінка засновників тут більше не публікується: вона працює на сервері бота (`/admin`).
На Vercel це один проєкт з Root Directory `landing`.
"""


def build(out: Path) -> None:
    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / "README.md").write_text(README, encoding="utf-8")

    landing = out / "landing"
    landing.mkdir()
    shutil.copy(ROOT / "landing" / "index.html", landing / "index.html")
    (landing / "vercel.json").write_text(json.dumps(VERCEL, indent=2), encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "sites"
    build(target)
    print(f"built {target}")
