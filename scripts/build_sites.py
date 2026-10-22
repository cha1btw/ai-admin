"""Build the two public sites into dist/sites (pushed to the public repo cha1btw/ai-admin-sites).

    uv run python scripts/build_sites.py [out_dir]

landing/     the marketing landing (landing/index.html as is)
admin-demo/  the admin panel running on sample data in the browser (no server, nothing saved)

Only static files go out: no bot code, no keys, no real studio data.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "admin" / "static"
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
DEMO_BANNER = (
    '<div class="demo-banner">Демо-версія адмінки: студії й дані вигадані, зміни не зберігаються. '
    "Справжня адмінка працює на нашому сервері.</div>"
)
DEMO_CSS = (
    ".demo-banner{background:var(--accent-soft,#e3f1ec);color:var(--accent,#1f6f5c);"
    "font-size:.85rem;text-align:center;padding:.5rem 1rem;line-height:1.4}"
)
README = """# AI-адміністратор: сайти

Публічні статичні сайти проєкту. Код бота лежить в окремому приватному репозиторії, звідки ці
файли збирає `scripts/build_sites.py`. Тут нічого не редагуйте вручну.

- `landing/` — лендинг для студій.
- `admin-demo/` — демо адмінки засновників на вигаданих даних: працює в браузері без сервера,
  нічого не зберігає.

На Vercel це два проєкти з Root Directory `landing` і `admin-demo`.
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

    demo = out / "admin-demo"
    demo.mkdir()
    shutil.copy(STATIC / "admin.js", demo / "admin.js")
    css = (STATIC / "admin.css").read_text(encoding="utf-8") + "\n" + DEMO_CSS + "\n"
    (demo / "admin.css").write_text(css, encoding="utf-8")
    config = (ROOT / "studios" / "demo_studio.json").read_text(encoding="utf-8")
    script = (ROOT / "app" / "admin" / "demo" / "demo.js").read_text(encoding="utf-8")
    (demo / "demo.js").write_text(
        script.replace("const BASE = __DEMO_CONFIG__;", f"const BASE = {config.strip()};"),
        encoding="utf-8",
    )
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    page = page.replace("/admin/static/admin.css?v=__V__", "admin.css")
    page = page.replace(
        '<script src="/admin/static/admin.js?v=__V__"></script>',
        '<script src="demo.js"></script>\n  <script src="admin.js"></script>',
    )
    page = page.replace(
        "<title>Адмінка · AI-адміністратор</title>",
        "<title>Демо адмінки · AI-адміністратор</title>",
    )
    page = page.replace("  </header>", f"  </header>\n  {DEMO_BANNER}", 1)
    (demo / "index.html").write_text(page, encoding="utf-8")
    (demo / "vercel.json").write_text(json.dumps(VERCEL, indent=2), encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "sites"
    build(target)
    print(f"built {target}")
