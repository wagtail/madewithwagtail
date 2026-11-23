import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from pipeline.proposal import Proposal
from pipeline.content import (
    output_paths,
    update_developer_profile,
    write_content_files,
)
from pipeline.publish import (
    PR_CREATED_LABEL,
    WAGTAIL_DETECTED_LABEL,
    commit_message,
    git_add_paths,
    pr_labels,
)
from process_submission import cmd_publish
from test_proposal import make_proposal_kwargs


def make_proposal(**overrides):
    return Proposal(**make_proposal_kwargs(**overrides))


def make_webp(size: tuple[int, int] = (1200, 996)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 10, 10)).save(buf, "WEBP")
    return buf.getvalue()


class TestWriteContentFiles:
    def test_writes_site_and_images(self, tmp_path):
        (tmp_path / "src" / "content" / "developers").mkdir(parents=True)
        p = make_proposal()
        written = write_content_files(p, tmp_path, make_webp(), make_webp((200, 100)))
        rel = {str(path.relative_to(tmp_path)) for path in written}
        assert "src/content/developers/example-co/example-site/index.md" in rel
        assert "src/content/developers/example-co/index.md" in rel
        assert (
            "src/content/developers/example-co/example-site/example-site.fill-1200x996.webp" in rel
        )
        assert "src/content/developers/example-co/example-co.max-120x120.webp" in rel

    def test_existing_developer_writes_less(self, tmp_path):
        (tmp_path / "src" / "content" / "developers").mkdir(parents=True)
        p = make_proposal(developer_exists=True, developer_slug="frojd")
        written = write_content_files(p, tmp_path, make_webp(), None)
        rel = {path.name for path in written}
        assert rel == {"index.md", "example-site.fill-1200x996.webp"}

    def test_rejects_wrong_dimension_screenshot(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "public").mkdir()
        with pytest.raises(ValueError, match="1200x996"):
            write_content_files(make_proposal(), tmp_path, make_webp((800, 600)), None)

    def test_rejects_non_webp(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "public").mkdir()
        buf = io.BytesIO()
        Image.new("RGB", (1200, 996)).save(buf, "PNG")
        with pytest.raises(ValueError, match="WEBP"):
            write_content_files(make_proposal(), tmp_path, buf.getvalue(), None)


class TestGitAddPaths:
    def test_excludes_missing_logo(self, tmp_path):
        # New-developer proposal whose logo was never written (no logo
        # artifact): the logo path must not reach `git add`.
        p = make_proposal()
        (tmp_path / "src" / "content" / "developers" / "example-co" / "example-site").mkdir(parents=True)
        (tmp_path / "src" / "content" / "developers" / "example-co" / "example-site" / "index.md").touch()
        (tmp_path / "src" / "content" / "developers" / "example-co" / "index.md").touch()
        (
            tmp_path
            / "src" / "content" / "developers" / "example-co" / "example-site"
            / "example-site.fill-1200x996.webp"
        ).touch()
        paths = git_add_paths(p, tmp_path)
        assert (
            tmp_path / "src/content/developers/example-co/example-co.max-120x120.webp" not in paths
        )
        assert len(paths) == 3

    def test_includes_logo_when_written(self, tmp_path):
        p = make_proposal()
        for rel in output_paths(p).values():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).touch()
        paths = git_add_paths(p, tmp_path)
        assert len(paths) == 4

    def test_existing_developer_paths_only(self, tmp_path):
        p = make_proposal(developer_exists=True, developer_slug="frojd")
        for rel in output_paths(p).values():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).touch()
        paths = git_add_paths(p, tmp_path)
        assert {path.name for path in paths} == {"index.md", "example-site.fill-1200x996.webp"}


class TestProfileUpdate:
    """Existing-profile submissions update the profile when details are provided."""

    def make_profile(self, tmp_path):
        profile = tmp_path / "src" / "content" / "developers" / "frojd" / "index.md"
        profile.parent.mkdir(parents=True)
        profile.write_text(
            "---\n"
            "title: Fröjd\n"
            'first_published_at: "2017-03-14T05:42:09.633251+13:00"\n'
            'latest_revision_created_at: "2022-01-08T14:48:21.885038+13:00"\n'
            "location: Stockholm, Sweden\n"
            "company_url: https://www.frojd.se/\n"
            "github_user: frojd\n"
            "online_profiles: []\n"
            "---\n"
            "\n"
            "Fröjd is a full service web agency.\n",
            encoding="utf-8",
        )
        return profile

    def proposal(self, **overrides):
        return make_proposal(
            developer_exists=True,
            developer_slug="frojd",
            developer_name="Fröjd",
            **overrides,
        )

    def test_updates_provided_fields_and_preserves_the_rest(self, tmp_path):
        profile = self.make_profile(tmp_path)
        p = self.proposal(developer_location="Gothenburg, Sweden", lat="57.7087")
        written = write_content_files(p, tmp_path, make_webp(), None)
        assert profile in written
        text = profile.read_text(encoding="utf-8")
        assert "location: Gothenburg, Sweden" in text
        assert "lat: '57.7087'" in text
        # Untouched fields and the profile text stay as they were.
        assert "github_user: frojd" in text
        assert "company_url: https://www.frojd.se/" in text
        assert "title: Fröjd" in text
        assert "Fröjd is a full service web agency." in text

    def test_revision_timestamp_bumped(self, tmp_path):
        profile = self.make_profile(tmp_path)
        p = self.proposal(developer_location="Gothenburg, Sweden")
        write_content_files(p, tmp_path, make_webp(), None)
        text = profile.read_text(encoding="utf-8")
        assert "latest_revision_created_at: '2026-08-05T00:00:00+00:00'" in text
        assert "2017-03-14T05:42:09" in text  # first_published_at untouched

    def test_no_fields_means_no_profile_write(self, tmp_path):
        # A Developer name alone must still work without touching the profile.
        profile = self.make_profile(tmp_path)
        before = profile.read_text(encoding="utf-8")
        written = write_content_files(self.proposal(), tmp_path, make_webp(), None)
        assert profile not in written
        assert profile.read_text(encoding="utf-8") == before

    def test_identical_values_are_a_no_op(self, tmp_path):
        profile = self.make_profile(tmp_path)
        before = profile.read_text(encoding="utf-8")
        p = self.proposal(developer_location="Stockholm, Sweden", github_user="frojd")
        written = write_content_files(p, tmp_path, make_webp(), None)
        assert profile not in written
        assert profile.read_text(encoding="utf-8") == before

    def test_output_paths_include_profile_only_when_updating(self):
        assert "developer_md" not in output_paths(self.proposal())
        assert "developer_md" in output_paths(
            self.proposal(developer_location="Gothenburg, Sweden")
        )

    def test_missing_profile_is_skipped(self, tmp_path):
        p = self.proposal(developer_location="Gothenburg, Sweden")
        assert (
            update_developer_profile(tmp_path / "nope" / "index.md", p) is False
        )


class TestCommitMessage:
    def test_credits_issue_author(self):
        message = commit_message(make_proposal(), "thibaudcolas", "1234567")
        assert message.startswith("Add site submission from issue #42")
        assert (
            "Co-authored-by: thibaudcolas <1234567+thibaudcolas@users.noreply.github.com>"
            in message
        )

    def test_trailer_set_off_by_blank_line(self):
        message = commit_message(make_proposal(), "thibaudcolas", "1234567")
        assert message.endswith(
            "\n\nCo-authored-by: thibaudcolas <1234567+thibaudcolas@users.noreply.github.com>"
        )

    def test_no_co_author_omits_trailer(self):
        message = commit_message(make_proposal(), None, None)
        assert "Co-authored-by" not in message

    def test_missing_id_omits_trailer(self):
        message = commit_message(make_proposal(), "thibaudcolas", None)
        assert "Co-authored-by" not in message


class TestPrLabels:
    def test_wagtail_detected_gets_both_labels(self):
        assert pr_labels({"is_wagtail": True, "signals": ["wagtail-app"]}) == [
            PR_CREATED_LABEL,
            WAGTAIL_DETECTED_LABEL,
        ]

    def test_no_detection_gets_only_pr_created(self):
        assert pr_labels({"is_wagtail": False, "signals": []}) == [PR_CREATED_LABEL]

    def test_missing_verdict_gets_only_pr_created(self):
        assert pr_labels({}) == [PR_CREATED_LABEL]


class TestCmdPublishPrepare:
    def test_missing_logo_file_writes_without_logo(self, tmp_path, capsys):
        """The render job only writes logo.webp when one is discoverable, so
        `--logo rendered/logo.webp` may point at a nonexistent file for a
        new-developer submission. That must produce a logo-less PR, not a
        crash (which would route the submission to needs-triage)."""
        (tmp_path / "repo" / "src" / "content" / "developers").mkdir(parents=True)
        proposal_file = tmp_path / "proposal.json"
        proposal_file.write_text(make_proposal().model_dump_json())
        screenshot_file = tmp_path / "screenshot.webp"
        screenshot_file.write_bytes(make_webp())

        code = cmd_publish(
            [
                "prepare",
                "--proposal", str(proposal_file),
                "--screenshot", str(screenshot_file),
                "--logo", str(tmp_path / "logo.webp"),  # does not exist
                "--repo-root", str(tmp_path / "repo"),
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        written = {line for line in out.splitlines() if line.startswith("src/")}
        assert "src/content/developers/example-co/example-co.max-120x120.webp" not in written
        assert (
            tmp_path
            / "repo" / "src" / "content" / "developers" / "example-co" / "example-site"
            / "example-site.fill-1200x996.webp"
        ).exists()
        assert not (
            tmp_path / "repo" / "src" / "content" / "developers" / "example-co" / "example-co.max-120x120.webp"
        ).exists()
