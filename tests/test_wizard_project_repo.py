"""When a Parade project names a GitHub repository, the wizard uses it.

The project's repo wins outright — there is no "or type a different one"
branch, because a connection pointed at a different repo than the project it
reads tasks from produces PRs that never appear on those tasks."""

from issuebot import wizard as core
from issuebot.plugins.sources.issuebear import wizard as source_wizard


def _choose_first(_label, options, *, to_label=None):
    """Stand-in for core's numbered picker: always take the first option."""
    return options[0]


class _Client:
    """A board server with one org, one project and one board."""

    def __init__(self, project):
        self._project = project

    def list_organisations(self):
        return [{"id": "org-1", "name": "Acme"}]

    def list_projects(self, org_id):
        return [self._project]

    def list_boards(self, project_id):
        return [{"id": "board-1", "name": "Dev"}]


def test_connection_carries_the_projects_repo():
    """The source already walks to the project, so the repo costs no extra
    question and no extra call."""
    project = {
        "id": "proj-1",
        "name": "Web",
        "github_repo": {
            "full_name": "acme/web",
            "ssh_url": "git@github.com:acme/web.git",
            "clone_url": "https://github.com/acme/web.git",
            "html_url": "https://github.com/acme/web",
        },
    }

    result = source_wizard.connection(_Client(project), choose=_choose_first)

    assert result["repo"] == "https://github.com/acme/web.git"
    assert result["board"] == "board-1"


def test_connection_reports_no_repo_when_the_project_has_none():
    """Most projects are not linked; the wizard must still ask in that case."""
    project = {"id": "proj-1", "name": "Web", "github_repo": None}

    result = source_wizard.connection(_Client(project), choose=_choose_first)

    assert result["repo"] is None


def test_connection_tolerates_an_older_board_server():
    """A Parade that predates the GitHub connector sends no such key at all."""
    project = {"id": "proj-1", "name": "Web"}

    result = source_wizard.connection(_Client(project), choose=_choose_first)

    assert result["repo"] is None


def test_core_does_not_ask_for_a_repo_when_the_source_supplied_one(monkeypatch):
    """The whole point: connecting a project that names a repo asks one fewer
    question, and cannot be answered wrongly."""
    asked = []
    monkeypatch.setattr(core, "_prompt_repo", lambda: asked.append("asked") or "typed-url")

    prompt = core._repo_prompter("git@github.com:acme/web.git")

    assert prompt() == "git@github.com:acme/web.git"
    assert asked == []


def test_core_falls_back_to_asking_when_the_project_has_no_repo(monkeypatch):
    """No repo means the wizard still has to ask, same as always."""
    monkeypatch.setattr(core, "_prompt_repo", lambda: "git@github.com:acme/typed.git")

    prompt = core._repo_prompter(None)

    assert prompt() == "git@github.com:acme/typed.git"


def test_connection_reports_no_repo_when_the_project_names_only_ssh():
    """A run environment holds gh credentials and nothing else — no SSH key,
    no known-hosts entry — so an SSH URL is not a remote it can clone. Better
    to ask than to configure a connection that fails at the clone."""
    project = {
        "id": "proj-1",
        "name": "Web",
        "github_repo": {"ssh_url": "git@github.com:acme/web.git"},
    }

    result = source_wizard.connection(_Client(project), choose=_choose_first)

    assert result["repo"] is None
