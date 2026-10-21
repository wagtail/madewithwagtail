# Task runner: https://github.com/casey/just
# Requires: `node` >= 22.12 (see .node-version), `npm`, and `just`.

# List all the justfile recipes.
help:
    just --list --list-prefix 'just '

# Install the dependencies and Git hooks.
install:
    npm ci
    prek install

# Lint the code with Biome, Stylelint, and prek.
lint:
    npm run lint

# Run all formatters.
format:
    npm run format

# Run the Astro type checker.
check:
    npm run check

# Build the production site to `dist/`.
build:
    npm run build

# Run the development server.
serve:
    npm run dev

# Run the submission pipeline tests.
test-submissions:
    uv run --with 'pytest, pydantic, pyyaml, python-slugify, httpx, pillow' pytest tests/submissions

# Refresh site listings (screenshots, technologies, canonical URLs).
refresh-sites *args:
    ./scripts/submissions/refresh_sites.py sites {{args}}

# Refresh developer profiles (logos, online profile links).
refresh-profiles *args:
    ./scripts/submissions/refresh_sites.py profiles {{args}}
