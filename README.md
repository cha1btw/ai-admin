# AI-admin

AI administrator for Instagram and Telegram DMs of beauty studios. It answers clients from the
studio's own data, books real free slots and hands anything sensitive to the master.

Product rules and architecture: [CLAUDE.md](CLAUDE.md). Product behaviour and scenarios:
[docs/PRODUCT.md](docs/PRODUCT.md).

## Quick start

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker.

```bash
cp .env.example .env
make install
make db        # Postgres on localhost:5435
make migrate
make test      # unit + integration tests, no API keys needed
```
