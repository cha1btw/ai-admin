# Studio configs

- `demo_studio.json` — the public demo studio (made-up data). It mirrors the landing demo.
- `private/` — real studios. Ignored by git: keep them out of the repository.

The schema with every field and validation rule is `app/tenants/schema.py`. The bot sees only
the public projection (`make tenant-preview TENANT=<slug>`); `internal_notes` and payment
requisites never reach the model.

```bash
make tenant-validate FILE=studios/private/<slug>.json
make tenant-load FILE=studios/private/<slug>.json
```
