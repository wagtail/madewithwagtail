#!/usr/bin/env -S uv run --script
"""Triage showcase site submissions: validate, render, publish.

Subcommands:
    validate --issue-body <file> [--content-dir <dir>]     -> proposal JSON on stdout
    render   --proposal <file> --out-dir <dir> [--url URL] -> detection.json, screenshot.webp, logo.webp
    publish prepare --proposal <file> --detection <file>
              --screenshot <file> [--logo <file>] --repo-root <dir> [--dry-run]
    publish pr      --proposal <file> --detection <file> --repo-root <dir>
                    [--co-author <login> --co-author-id <id>] [--dry-run]

Exit codes: 0 success, 2 rejection (reasons JSON printed to stdout by
`validate`), 1 unexpected error.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.28",
#   "playwright==1.62.0",
#   "pillow>=11",
#   "pydantic>=2.10",
#   "python-slugify>=8",
#   "pyyaml>=6",
#   "wappalyzer>=2,<3",
# ]
# ///

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.browser import capture_screenshot
from pipeline.content import output_paths, site_markdown, write_content_files
from pipeline.detection import (
    classify_technologies,
    detect_wagtail,
    detection_result,
    wappalyzer_technologies,
)
from pipeline.logos import gather_logo_candidates, select_largest_logo
from pipeline.net import fetch_page, probe_admin_pages
from pipeline.proposal import Proposal, Rejection, build_proposal
from pipeline.publish import (
    BRANCH_PREFIX,
    PR_CREATED_LABEL,
    build_pr_body,
    build_pr_comment,
    commit_message,
    git_add_paths,
    pr_labels,
    run,
)

REJECTION_EXIT = 2
ERROR_EXIT = 1


def cmd_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="validate")
    parser.add_argument("--issue-body", required=True, type=Path)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument(
        "--content-dir", type=Path, default=Path("src/content/developers")
    )
    args = parser.parse_args(argv)
    body = args.issue_body.read_text(encoding="utf-8")
    try:
        proposal = build_proposal(body, args.issue_number, args.content_dir)
    except Rejection as rejection:
        print(json.dumps({"reasons": rejection.reasons}, indent=2, ensure_ascii=False))
        return REJECTION_EXIT
    print(proposal.model_dump_json(indent=2))
    return 0


def _file_line_count(path: Path) -> int:
    """Number of lines in a committed file, for deep-link ranges."""
    return len(path.read_text(encoding="utf-8").splitlines())


def cmd_render(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="render")
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    # --url smoke mode (local browser testing) skips the proposal entirely.
    proposal: Proposal | None = None
    if args.proposal:
        proposal = Proposal.model_validate_json(
            args.proposal.read_text(encoding="utf-8")
        )
    elif not args.url:
        print("render: either --proposal or --url is required", file=sys.stderr)
        return ERROR_EXIT
    target_url = args.url or proposal.site_url

    import httpx

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # follow_redirects=True for image/manifest GETs: candidate URLs 301
    # between www/apex origins routinely. fetch_page and probe_admin_pages
    # manage redirects manually with per-hop SSRF checks and are unaffected.
    with httpx.Client(follow_redirects=True) as client:
        final_url, html = fetch_page(client, target_url)
        signals = detect_wagtail(html)
        final_parts = urlsplit(final_url)
        origin = f"{final_parts.scheme}://{final_parts.hostname}"
        signals += probe_admin_pages(client, origin)

        logo_bytes: bytes | None = None
        if proposal is not None and not proposal.developer_exists:
            # Logo candidates come from the developer's own site (Developer
            # URL), never the submitted site. Best-effort: an unreachable
            # developer page simply yields no icon candidates.
            developer_html = ""
            if proposal.developer_url:
                try:
                    _, developer_html = fetch_page(client, proposal.developer_url)
                except Exception:
                    pass
            logo_bytes = select_largest_logo(
                client,
                gather_logo_candidates(
                    client, developer_html, proposal.developer_url, proposal.logo_url
                ),
            )
    # Wappalyzer scan (headless Chromium): technology fingerprints that
    # complement the Wagtail HTML detection above, which stays authoritative
    # for Wagtail itself. Best-effort: a failed scan yields no technologies.
    # Incompatible technologies are reported in "technologies" and gated by
    # a dedicated workflow job — never merged into "signals": is_wagtail
    # must stay a pure Wagtail-detection verdict.
    classified = classify_technologies(wappalyzer_technologies(final_url))
    detection = detection_result(signals, final_url, classified)
    (args.out_dir / "detection.json").write_text(
        json.dumps(detection, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    capture_screenshot(final_url, args.out_dir / "screenshot.webp")
    if logo_bytes:
        (args.out_dir / "logo.webp").write_bytes(logo_bytes)
    return 0


def cmd_publish(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="publish")
    parser.add_argument("stage", choices=["prepare", "pr"])
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--detection", type=Path)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--logo", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--co-author")
    parser.add_argument("--co-author-id")
    args = parser.parse_args(argv)
    proposal = Proposal.model_validate_json(args.proposal.read_text(encoding="utf-8"))

    if args.stage == "prepare":
        screenshot = args.screenshot.read_bytes()
        # A missing logo artifact is a legitimate no-logo outcome, not an
        # error: write_content_files treats empty bytes as "no logo".
        logo = args.logo.read_bytes() if args.logo and args.logo.exists() else b""
        technologies = {}
        if args.detection and args.detection.exists():
            technologies = (
                json.loads(args.detection.read_text(encoding="utf-8")).get(
                    "technologies"
                )
                or {}
            )
        if args.dry_run:
            for key, rel in output_paths(proposal).items():
                print(f"would write {rel}")
            print(site_markdown(proposal, technologies))
            return 0
        written = write_content_files(
            proposal, args.repo_root, screenshot, logo, technologies
        )
        for path in written:
            print(path)
        return 0

    # Stage "pr": branch, commit, push, PR, issue comment.
    # The PR body links the committed site entry at the pushed HEAD SHA, so
    # the commit must exist (and be pushed) before the body is built.
    if args.detection is None:
        parser.error("publish pr requires --detection")
    detection = json.loads(args.detection.read_text(encoding="utf-8"))
    branch = f"{BRANCH_PREFIX}{proposal.issue_number}"
    paths = output_paths(proposal)
    logo_committed = "logo" in paths and (args.repo_root / paths["logo"]).exists()

    body_file = args.repo_root / ".git" / "PR_BODY.md"

    if args.dry_run:
        # Placeholders keep the advertised local dry-run working without CI
        # env, git writes, or a real push.
        repo = os.environ.get("GITHUB_REPOSITORY", "<GITHUB_REPOSITORY>")
        run_url = os.environ.get("GITHUB_RUN_URL", "<GITHUB_RUN_URL>")
        head_sha = os.environ.get("GITHUB_HEAD_SHA")
        body = build_pr_body(
            proposal,
            detection,
            repo,
            branch,
            run_url,
            logo_committed=logo_committed,
            head_sha=head_sha,
        )
        print(f"would create branch {branch} and open a PR on {repo}")
        print(body)
        return 0

    run(["git", "checkout", "-B", branch])
    run(
        ["git", "add", *(str(path) for path in git_add_paths(proposal, args.repo_root))]
    )
    run(
        [
            "git",
            "commit",
            "-m",
            commit_message(proposal, args.co_author, args.co_author_id),
        ]
    )
    # The branch is fully regenerated from validated artifacts each run, so
    # force pushing keeps retries idempotent when the branch (and its PR)
    # already exist from a previous pipeline run. The lease expectation must
    # be explicit: the checkout only fetched the default branch, so no
    # remote-tracking ref exists for --force-with-lease to verify against.
    listing = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_sha = listing.stdout.split()[0] if listing.stdout.strip() else ""
    run(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
            "origin",
            branch,
        ]
    )

    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = os.environ["GITHUB_RUN_URL"]
    # The commit just pushed is HEAD of the current branch.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    head_sha = head.stdout.strip()
    entry_line_count = _file_line_count(args.repo_root / paths["site_md"])
    # Submissions that create or update a developer profile deep-link it
    # with the same file-viewer range.
    profile_line_count = (
        _file_line_count(args.repo_root / paths["developer_md"])
        if "developer_md" in paths
        else None
    )
    body = build_pr_body(
        proposal,
        detection,
        repo,
        branch,
        run_url,
        logo_committed=logo_committed,
        head_sha=head_sha,
        entry_line_count=entry_line_count,
        profile_line_count=profile_line_count,
    )
    body_file.write_text(body, encoding="utf-8")

    # The labels may not exist yet in the repository. Auto-created PRs carry
    # the same `submission → PR created` label as the issue they come from,
    # plus a Wagtail-detected label when the detection verdict is positive.
    labels = pr_labels(detection)
    for label in labels:
        run(["gh", "label", "create", label, "--color", "0e8a16", "--force"])
    # A retried submission (issue reopened) may already have an open PR for
    # the branch; update it in place instead of failing.
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
        check=True,
        capture_output=True,
        text=True,
    )
    existing = json.loads(result.stdout or "[]")
    if existing:
        pr_url = existing[0]["url"]
        run(["gh", "pr", "edit", pr_url, "--body-file", str(body_file)])
        run(["gh", "pr", "edit", pr_url, *[f"--add-label={label}" for label in labels]])
    else:
        # gh pr create prints the PR URL on stdout — capture it for the issue comment.
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"New site submission: {proposal.site_title}",
                "--body-file",
                str(body_file),
                "--head",
                branch,
                *[f"--label={label}" for label in labels],
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pr_url = result.stdout.strip().splitlines()[-1]
    # Retries must not stack duplicate comments on the issue: edit the
    # bot's most recent comment, creating one only if none exists yet.
    run(
        [
            "gh",
            "issue",
            "comment",
            str(proposal.issue_number),
            "--body",
            build_pr_comment(proposal, pr_url, run_url),
            "--edit-last",
            "--create-if-none",
        ]
    )
    run(
        [
            "gh",
            "issue",
            "edit",
            str(proposal.issue_number),
            "--add-label",
            PR_CREATED_LABEL,
        ]
    )
    body_file.unlink(missing_ok=True)
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, *argv = sys.argv[1:]
    try:
        if command == "validate":
            return cmd_validate(argv)
        if command == "render":
            return cmd_render(argv)
        if command == "publish":
            return cmd_publish(argv)
    except Rejection as rejection:  # defensive: cmd_validate already handles it
        print(json.dumps({"reasons": rejection.reasons}), file=sys.stderr)
        return REJECTION_EXIT
    except Exception:
        # Unexpected errors must stay distinguishable from a form rejection:
        # exit 1 with the traceback on stderr (never 2, never partial JSON on
        # stdout) so callers can triage crashes instead of closing on them.
        traceback.print_exc()
        return ERROR_EXIT
    print(f"Unknown command: {command}", file=sys.stderr)
    return ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
