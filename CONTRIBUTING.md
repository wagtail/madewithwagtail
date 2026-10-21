# Contributing guidelines

Thank you for your interest in this project! Made with Wagtail is a showcase of sites built with [Wagtail](https://wagtail.org), generated as a fully static site with [Astro](https://astro.build) from Markdown content collections.

## Quick start

Requirements: Node, `npm`, [`just`](https://github.com/casey/just), and [`prek`](https://prek.j178.dev/).

```sh
git clone git+https://github.com/wagtail/madewithwagtail
cd madewithwagtail

# Install the dependencies and Git hooks.
just install

# Start the development server at http://localhost:4321/madewithwagtail/.
just serve
```

`just install` installs dependencies and sets up the Git pre-commit hooks without formatting project files. Run `just format` when you want to apply formatting.

Other useful commands:

```sh
just build            # Build the production site to `dist/`.
just check            # Run the Astro type checker.
just lint             # Run all linters (Biome, Stylelint, prek).
just format           # Run all formatters.
just test-submissions # Run the submission pipeline tests.
just help             # List all the justfile recipes.
```

## How the site works

- **Pages and components** live in `src/pages` and `src/components` (Astro components).
- **Content** lives in `src/content`:
  - `src/content/sites` – one Markdown file per showcased site.
  - `src/content/developers` – one Markdown file per Wagtail developer/agency.
- The schema for both collections is defined in `src/content.config.ts`.
- **Stylesheets** are Sass files under `src/styles`, compiled with the [Wagtail style guidelines](https://github.com/wagtail/stylelint-config-wagtail) enforced via Stylelint.
- **SVG icons** are defined as an inline sprite in `src/layouts/BaseLayout.astro`, used via the `<Icon>` component.

Content changes are picked up automatically by the dev server. Data is regenerated at build time — there is no database.

## Site submissions

See how we manage site submissions in our docs: [Sites management](./docs/sites-management.md).

## Quality assurance

Here are the available tooling scripts for the project:

```sh
just build   # Build the production site to `dist/`.
just check   # Run the Astro type checker.
just format  # Run all formatters.
just help    # List all the justfile recipes.
just install # Install the dependencies and Git hooks.
just lint    # Run all linters.
just serve   # Run the development server at localhost:4321.
```

Tooling used:

- [Biome](https://biomejs.dev) lints and formats the Astro, TypeScript, and JSON code (`npm run lint:format`).
- [Stylelint](https://stylelint.io) with [@wagtail/stylelint-config-wagtail](https://github.com/wagtail/stylelint-config-wagtail) lints the stylesheets, with [Prettier](https://prettier.io) for their formatting (`npm run lint:css`).
- [`astro check`](https://docs.astro.build/en/reference/cli-reference/#astro-check) type-checks the project (`just check`).
- [`prek`](https://prek.j178.dev/) runs all checks as git pre-commit hooks, and in CI.

## Code review

Create a pull request with your changes so that they can be reviewed by a maintainer. Ensure that you give a summary with the purpose of the change and any steps that the reviewer needs to take to test your work.

All CI checks must pass before a pull request can be merged.

## Deployment

The site is deployed to GitHub Pages on every push to `main`, via GitHub Actions (see [.github/workflows](./.github/workflows)). Dependency updates are automated with [Renovate](https://docs.renovatebot.com/).
