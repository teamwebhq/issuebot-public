"""Contract tests for immutable GitHub Release automation."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    """Read one workflow after producing a useful failure when it is absent."""
    path = WORKFLOWS / name
    assert path.is_file(), f"{path.relative_to(ROOT)} is absent"
    return path.read_text()


def test_pull_requests_validate_a_unique_version_and_run_project_checks() -> None:
    """Weakening PR CI must not admit an unpublishable version or unchecked code."""
    workflow = _workflow("pull-request.yml")

    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "jobs:\n  check:" in workflow
    assert "fetch-depth: 0" in workflow
    assert "git fetch origin" in workflow
    assert '"${{ github.base_ref }}:refs/remotes/origin/${{ github.base_ref }}" --tags' in workflow
    assert 'tools/release_version.py --base-ref "origin/${{ github.base_ref }}"' in workflow
    assert "uv run pytest -q" in workflow
    assert "./tools/check.sh" in workflow


def test_required_check_rejects_sha_like_head_names_before_checkout() -> None:
    """A head name that suppresses the release event must never become mergeable."""
    workflow = _workflow("pull-request.yml")
    guard = workflow.index("Reject SHA-like head branch")
    checkout = workflow.index("actions/checkout@v4")
    guard_step = workflow[guard:checkout]

    assert guard < checkout
    assert "HEAD_REF: ${{ github.head_ref }}" in guard_step
    assert "^[0-9a-fA-F]{7,64}$" in guard_step
    assert "exit 1" in guard_step


def test_release_runs_for_each_closed_main_pull_request_without_a_push_trigger() -> None:
    """Commit skip directives must not suppress the event that authorizes release."""
    workflow = _workflow("release.yml")
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "pull_request_target:" in trigger
    assert "branches: [main]" in trigger
    assert "types: [closed]" in trigger
    assert "push:" not in trigger
    assert "if: github.event.pull_request.merged == true" in workflow


def test_release_checks_out_only_the_merged_main_commit() -> None:
    """A privileged target workflow must never execute the unmerged PR head."""
    workflow = _workflow("release.yml")

    assert "RELEASE_SHA: ${{ github.event.pull_request.merge_commit_sha }}" in workflow
    assert "ref: ${{ github.event.pull_request.merge_commit_sha }}" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"' in workflow
    assert "github.event.pull_request.head.sha" not in workflow


def test_release_revalidates_the_version_against_previous_main() -> None:
    """A merged version must differ from main immediately before that merge."""
    workflow = _workflow("release.yml")

    assert "git rev-list --parents -n 1 HEAD" in workflow
    assert 'base_ref="$(git rev-parse HEAD^1)"' in workflow
    assert 'tools/release_version.py --base-ref "$base_ref"' in workflow


def test_merged_code_builds_and_publishes_one_verified_release_wheel() -> None:
    """Release CI must publish only a checked immutable wheel and its installer."""
    workflow = _workflow("release.yml")

    assert "contents: write" in workflow
    assert "group: issuebot-release" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "tools/release_version.py" in workflow
    assert "uv run pytest -q" in workflow
    assert "./tools/check.sh" in workflow
    assert "uv build --wheel" in workflow
    assert 'test -f "$WHEEL"' in workflow
    assert 'if git rev-parse --verify --quiet "refs/tags/v$VERSION"; then' in workflow
    assert 'gh release create "v$VERSION" "$WHEEL" install.sh --draft' in workflow
    assert 'gh release edit "v$VERSION" --draft=false --latest' in workflow
    assert workflow.index("Create release draft") < workflow.index("Publish release")


def test_release_serialization_preserves_every_pending_merge() -> None:
    """Only merged jobs queue, and every pending merged version remains serialized."""
    workflow = _workflow("release.yml")
    jobs = workflow.index("jobs:")
    release_job = workflow[workflow.index("  release:", jobs) :]
    condition = release_job.index("    if: github.event.pull_request.merged == true")
    concurrency = release_job.index("    concurrency:")
    steps = release_job.index("    steps:")

    assert "\nconcurrency:" not in workflow[:jobs]
    assert condition < concurrency < steps
    assert "      group: issuebot-release" in release_job
    assert "      cancel-in-progress: false" in release_job
    assert "      queue: max" in release_job


def test_release_absence_probe_does_not_hide_github_errors() -> None:
    """Only an explicit 404 means absence; auth, network, and API errors must fail."""
    workflow = _workflow("release.yml")

    assert "gh api --include" in workflow
    assert "set +e" in workflow
    assert "api_status=$?" in workflow
    assert "set -e" in workflow
    assert "404)" in workflow
    assert "2??)" in workflow
    assert 'exit "$api_status"' in workflow
    assert "! gh release view" not in workflow


def test_release_atomically_reserves_and_verifies_the_tag_before_the_draft() -> None:
    """A same-SHA tag created by another actor must still make this run fail."""
    workflow = _workflow("release.yml")
    reserve = workflow.index("Reserve release tag")
    create = workflow.index("Create release draft")
    reservation = workflow[reserve:create]

    assert reserve < create
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs"' in reservation
    assert '-f ref="refs/tags/v$VERSION"' in reservation
    assert '-f sha="$RELEASE_SHA"' in reservation
    assert 'gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/v$VERSION"' in reservation
    assert "--jq '.object.sha'" in reservation
    assert 'test "$remote_target" = "$RELEASE_SHA"' in reservation
    assert "git push" not in reservation
    assert "--verify-tag" in workflow[create:]


def test_release_parses_the_exact_wheel_metadata_version() -> None:
    """Substring matches must not accept a malformed or different wheel version."""
    workflow = _workflow("release.yml")

    assert "from email.parser import Parser" in workflow
    assert 'metadata["Version"] == version' in workflow
    assert 'f"Version: {v}\\n"' not in workflow


def test_repository_workflows_do_not_perform_pull_request_merges() -> None:
    """No checked-in workflow may bypass the event-emitting actor ruleset."""
    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    merge_operations = (
        r"\bgh\s+pr\s+merge\b",
        r"pulls/[^\n]*/merge\b",
        r"\bmergePullRequest\b",
    )

    for path in paths:
        workflow = path.read_text()
        for operation in merge_operations:
            assert re.search(operation, workflow, re.IGNORECASE) is None, (
                f"{path.name} contains a pull-request merge operation matching {operation!r}"
            )
