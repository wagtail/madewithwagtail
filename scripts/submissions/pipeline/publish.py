"""GitHub PR and issue-comment builders, plus the git/gh step runner."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from slugify import slugify

from .content import output_paths

if TYPE_CHECKING:
    from .proposal import Proposal


PR_LABEL = "🤖 new site submission"
NEEDS_TRIAGE_LABEL = "needs-triage"
PR_CREATED_LABEL = "submission → PR created"
LIVE_SITE_URL = "https://madewithwagtail.org"

# Submitted text is shown as literal text in the summary table: submissions
# are public, so Markdown in a description could otherwise inject links,
# @mentions, or cross-references into a bot-authored PR body.
_TABLE_CELL_ESCAPES = str.maketrans(
    {
        "\\": r"\\",
        "|": r"\|",
        "*": r"\*",
        "_": r"\_",
        "[": r"\[",
        "]": r"\]",
        "(": r"\(",
        ")": r"\)",
        "#": r"\#",
        "@": r"\@",
        "~": r"\~",
        "`": r"\`",
        "<": "&lt;",
        ">": "&gt;",
        "&": "&amp;",
    }
)


def _table_cell_text(value: str) -> str:
    """Submitted text as a literal Markdown table cell.

    Escapes the cell separator and inline markup, so the text renders as
    written rather than as a link, mention, or reference. HTML angle
    brackets become entities: GitHub sanitises them anyway, but the cell
    then still shows markup that will reach the site's raw Markdown.
    """
    paragraphs = [
        " ".join(line.strip() for line in block.splitlines() if line.strip())
        .translate(_TABLE_CELL_ESCAPES)
        for block in re.split(r"\n\s*\n", value.strip())
    ]
    return "<br><br>".join(p for p in paragraphs if p)


def _profile_line(p: Proposal) -> str:
    """Developer table cell: name linked to their website, profile link after.

    The name links to the developer's own site so the row works for both
    new and existing profiles; the profile-page link only exists once the
    profile is live.
    """
    name_text = _table_cell_text(p.developer_name)
    if p.developer_url:
        name = f"[{name_text}]({p.developer_url})"
    elif p.developer_exists:
        name = f"[{name_text}]({LIVE_SITE_URL}/developers/{p.developer_slug}/)"
    else:
        name = name_text
    if p.developer_exists:
        suffix = (
            f" - [see profile page]({LIVE_SITE_URL}/developers/{p.developer_slug}/)"
        )
    else:
        suffix = " - new 🎉"
    return name + suffix


def _facet_links(values: list[str], facet: str) -> str:
    """Facet values linking to the live site's facet pages, as the site renders them."""
    if not values:
        return "_(none)_"
    return ", ".join(
        f"[{value}]({LIVE_SITE_URL}/sites/{facet}/{slugify(value)}/)"
        for value in values
    )


def _run_footer(run_url: str) -> str:
    """Small-print footer shared by the PR body and issue comments."""
    return f"<sub>View the [site submission workflow logs]({run_url}).</sub>"


def _detection_value(detection: dict) -> str:
    """Concise table-cell status: verdict plus the signals behind it."""
    if detection["is_wagtail"]:
        signals = "; ".join(detection["signals"]) or "Wagtail detected"
        return f"✅ {signals}"
    return "⚠️ No Wagtail signals detected"


def _committed_file_url(
    path: Path,
    repo_full_name: str,
    head_sha: str | None,
    line_count: int | None,
) -> str:
    """Raw deep link to a committed file, rendered as an inline file viewer.

    The L1-L<last> range makes GitHub render the file directly in the PR
    description; it needs the branch's HEAD SHA and the file's line count.
    The URL is emitted bare — reviewers asked for raw links, not markdown
    links — so GitHub auto-links the visible URL itself.
    """
    if head_sha is None:
        return "(SHA unavailable in dry-run)"
    last = line_count if line_count is not None else 1
    return (
        f"https://github.com/{repo_full_name}/blob/{head_sha}/{path}?plain=1#L1-L{last}"
    )


def build_pr_body(
    p: Proposal,
    detection: dict,
    repo_full_name: str,
    branch: str,
    run_url: str,
    logo_committed: bool | None = None,
    head_sha: str | None = None,
    entry_line_count: int | None = None,
    profile_line_count: int | None = None,
) -> str:
    paths = output_paths(p)
    # Whether the logo image was actually written to the branch. None keeps
    # the default "the proposal expects one" for callers that don't know.
    logo_expected = logo_committed if logo_committed is not None else "logo" in paths
    # 300x249 is the 1200x996 capture scaled down; GitHub renders raw
    # width/height img attributes inside PR descriptions.
    screenshot_cell = (
        f'<img src="https://raw.githubusercontent.com/{repo_full_name}/{branch}/{paths["screenshot"]}"'
        ' width="300" height="249" alt="Screenshot of the new site">'
    )
    lines = [
        f"Closes #{p.issue_number}. Auto-generated PR via the [site submission workflow]"
        "(https://github.com/wagtail/madewithwagtail/blob/main/CONTRIBUTING.md#site-submissions)"
        f" ([view logs]({run_url})).",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Site | <{p.site_url}> |",
        f"| Developer | {_profile_line(p)} |",
        f"| Description | {_table_cell_text(p.site_description)} |",
    ]
    if p.similar_developers:
        links = ", ".join(
            f"[{slug}]({LIVE_SITE_URL}/developers/{slug}/)"
            for slug in p.similar_developers
        )
        lines.append(
            f"| Similar profiles | {links} — check this is not a duplicate |"
        )
    lines += [
        f"| Sector | {_facet_links(p.sector, 'sector')} |",
        f"| Site type | {_facet_links(p.site_type, 'type')} |",
        f"| Capabilities | {_facet_links(p.capability, 'capability')} |",
        f"| Detection | {_detection_value(detection)} |",
        f"| Screenshot | {screenshot_cell} |",
    ]
    if not p.developer_exists and logo_expected:
        logo_cell = (
            f'<img src="https://raw.githubusercontent.com/{repo_full_name}/{branch}/{paths["logo"]}"'
            ' width="120" alt="Logo of the developer">'
        )
        lines.append(f"| Logo | {logo_cell} |")
    lines += [
        f"| Local preview | `/developers/{p.developer_slug}/{p.site_slug}` |",
        "",
        "### Site page",
        "",
        _committed_file_url(
            paths["site_md"], repo_full_name, head_sha, entry_line_count
        ),
        "",
    ]
    if "developer_md" in paths:
        lines += [
            (
                "### Developer profile page"
                if not p.developer_exists
                else "### Developer profile update"
            ),
            "",
            _committed_file_url(
                paths["developer_md"], repo_full_name, head_sha, profile_line_count
            ),
            "",
        ]
    if p.other_notes:
        lines += [
            "### Submitter notes",
            "",
            p.other_notes,
            "",
        ]
    lines += [*_detected_technologies_section(detection)]
    lines += [
        "### Reviewer checklist",
        "",
        "- [ ] Site is live and built with Wagtail",
        "- [ ] Screenshot shows the site (not a cookie banner or login page)",
        "- [ ] Sector, site type, and capabilities are sensible",
        "- [ ] Description reads well",
        "- [ ] Developer details are correct"
        + (" (new profile: check the logo)" if not p.developer_exists else ""),
    ]
    return "\n".join(lines)


def _detected_technologies_section(detection: dict) -> list[str]:
    """'Detected technologies' PR section listing the Wappalyzer findings.

    Incompatible technologies never reach a PR: the workflow's
    reject-technologies job closes those submissions first, so only
    complementary and unclassified findings are reportable here.
    """
    technologies = detection.get("technologies") or {}
    complementary = technologies.get("complementary") or []
    other = technologies.get("other") or []
    if not (complementary or other):
        return [
            "### Detected technologies",
            "",
            "_None detected — the Wappalyzer scan found no reportable technologies._",
            "",
        ]
    lines = ["### Detected technologies", ""]
    if complementary:
        lines.append("- ✅ Complementary: " + ", ".join(complementary))
    if other:
        lines.append("- Other: " + ", ".join(other))
    lines.append("")
    return lines


def git_add_paths(p: Proposal, repo_root: Path) -> list[Path]:
    """Content paths that exist on disk — a missing logo is legitimate, and
    `git add` on a pathspec that matches nothing fails the publish stage."""
    return [
        path
        for rel in output_paths(p).values()
        if (repo_root / rel).exists()
        for path in [repo_root / rel]
    ]


def build_pr_comment(p: Proposal, pr_url: str, run_url: str) -> str:
    return (
        f"Opened pull request {pr_url} with this submission. "
        f"The issue auto-closes when the PR is merged.\n\n"
        f"Workflow run (artifacts): {run_url}\n" + _run_footer(run_url)
    )


def build_rejection_comment(reasons: list[str], run_url: str) -> str:
    bullets = "\n".join(f"- {reason}" for reason in reasons)
    return (
        "Thanks for your submission! Unfortunately it could not be processed:\n\n"
        f"{bullets}\n\n"
        "A maintainer will follow up (the issue is labelled needs-triage). "
        "Feel free to update the submission details here in the meantime.\n"
        + _run_footer(run_url)
    )


def build_failure_comment(stage: str, error: str, run_url: str) -> str:
    return (
        f"The submission pipeline failed at the **{stage}** stage: {error}\n\n"
        f"Check the [workflow run]({run_url}) for details — a maintainer will follow up "
        f"(labelled {NEEDS_TRIAGE_LABEL}).\n" + _run_footer(run_url)
    )


BRANCH_PREFIX = "submission/issue-"


def commit_message(
    proposal: Proposal, co_author: str | None, co_author_id: str | None
) -> str:
    """Commit subject plus a Co-authored-by trailer crediting the issue author."""
    message = f"Add site submission from issue #{proposal.issue_number}"
    if co_author and co_author_id:
        message += (
            f"\n\nCo-authored-by: {co_author} "
            f"<{co_author_id}+{co_author}@users.noreply.github.com>"
        )
    return message


def run(args: list[str]) -> None:
    """Run a subprocess with list args (never a shell) and fail loudly."""
    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}")
