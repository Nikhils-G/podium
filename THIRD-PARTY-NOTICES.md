# Third-party notices

Podium itself is MIT-licensed (see [`LICENSE`](LICENSE)). The image serves these third-party
files unchanged, from the container, never from a CDN:

| Component | Where | License |
|---|---|---|
| htmx 2.0.11 | `src/podium/static/js/htmx.min.js` | Zero-Clause BSD (0BSD) |
| Swagger UI 5 (swagger-ui-dist) | `src/podium/static/vendor/swagger-ui/` | Apache License 2.0 (full text in that folder) |
| Inter | `src/podium/static/fonts/inter-*.woff2` | SIL Open Font License 1.1 (`LICENSE-Inter.txt`) |
| Newsreader | `src/podium/static/fonts/newsreader-*.woff2` | SIL Open Font License 1.1 (`LICENSE-Newsreader.txt`) |
| JetBrains Mono | `src/podium/static/fonts/jetbrains-mono-*.woff2` | SIL Open Font License 1.1 (`LICENSE-JetBrainsMono.txt`) |

Python packages are installed from PyPI when the image is built, at the exact versions pinned in
`uv.lock`, and keep their own licenses. The direct dependencies:

| Package | License |
|---|---|
| FastAPI, SQLAlchemy, Alembic, Pydantic, pydantic-settings, argon2-cffi | MIT |
| Uvicorn, Starlette, Jinja2, segno | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| psycopg (with psycopg-binary) | LGPL-3.0-only, used unmodified as a library |

`uv export --no-dev` lists every transitive package; each one's license is in its PyPI metadata.
