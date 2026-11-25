# Sites management

We want to showcase all kinds of sites built with Wagtail. We reserve the right to reject submissions for sites related to vice industries or other content that doesn’t align with our project values.

## Site submissions

Submit new sites and new developer profiles through the [site submission form](https://github.com/wagtail/madewithwagtail/issues/new?template=site-submission.yml). The submission then goes through our GitHub Actions submission workflow:

1. **Validates** the submission — checks the URL, generates slugs, and rejects duplicates.
2. **Renders** the site in a sandboxed, credential-free job to extract information about how the site is built.
3. **Publishes** a pull request with the new content for maintainer review — adding the site to the developer's profile, creating a new profile when needed, and updating an existing profile with any developer details provided in the submission.

Nothing is published automatically: a maintainer reviews and merges the pull request, which closes the original issue. The pipeline's logic lives in `scripts/submissions/pipeline/`, wired up by the `process_submission.py` and `refresh_sites.py` entry scripts.

The process is automated so we consistently detect technologies the site is built with and take high-quality screenshots. Site submissions are auto-rejected if the site isn’t built with Wagtail, or for other issues with the submissions. Rejected submissions stay open, labelled `needs triage` for a maintainer to follow up on.

## Periodic refresh

Listings go stale: sites move, screenshots age, technology stacks change. The
refresh pipeline re-scans published content and updates what can be updated
mechanically, reporting anything that needs a human decision.

- `just refresh-sites` — re-scans sites whose `latest_revision_created_at` is
  older than ~9 months (`--max-age-months` to change, `--all` for everything):
  fresh screenshot, refreshed `technologies`, canonical `site_url` after
  redirects. Dead sites and other surprises are reported, never auto-edited.
- `just refresh-profiles` — re-scans developer profiles the same way:
  refreshed logo and conservative `online_profiles` matches from the
  developer's `company_url`.

Both edit content files in place (bump `latest_revision_created_at` only when
something changed), and write one CSV report per run under
`local/refresh/<timestamp>/report.csv`. Add `--dry-run` to preview changes
without writing (with just, pass flags after `--`, e.g.
`just refresh-sites -- --dry-run`). After a run, visually compare replaced
screenshots and revert any that aren't meaningfully different, then commit
the rest — removing dead sites stays a manual decision.

## Archival

You can request removal of a site present under your developer profile. We also periodically review existing sites, and remove those that are no longer online or no longer built with Wagtail. We also remove developer profiles that have no any active sites (of their own or via `in_cooperation_with` references).
