# issuebot

issuebot starts a coding agent for tasks on a board. For each task, issuebot does
these steps:

1. It finds a task that the board gives to your agent identity.
2. It claims the task.
3. It prepares a workspace.
4. It starts an agent CLI (Claude Code or Codex) to do the task.
5. It publishes the result.

issuebot has five plugin axes:

| Plugin axis | What it sets | Available |
|---|---|---|
| **source** | where the tasks come from, and what a run can report | `issuebear` |
| **workspace** | where issuebot prepares the working copy | `git`, `folder` |
| **environment** | which machine the agent runs on | `local`, `railway` |
| **harness** | which agent CLI issuebot starts | `claude`, `codex` |
| **sink** | where issuebot publishes a result | `github` |

Each plugin can add configuration keys, `issuebot doctor` checks, and commands.
`issuebot --help` shows the commands in this installation. The installed
`issuebear` source supplies the tasks for all connections. All connections use
the configured harness. You can make one connection for each board.
Connections can use different workspace, environment, and sink plugins.

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [How a task runs](#how-a-task-runs)
- [Configuration](#configuration)
- [Connections](#connections)
- [Workspaces](#workspaces)
- [What a run can report](#what-a-run-can-report)
- [Sinks](#sinks)
- [Environments](#environments)
- [Harnesses](#harnesses)
- [Monitor a run](#monitor-a-run)
- [Skills, plans and confirmation](#skills-plans-and-confirmation)
- [Workspace bootstrap](#workspace-bootstrap)
- [Housekeeping](#housekeeping)
- [Security](#security)

## Install

Each GitHub Release has an issuebot wheel for one stable version. With no
version, the installer uses GitHub's latest release. Give a stable `X.Y.Z`
version to install that specified release.

### Latest Release

```sh
curl -fsSL https://github.com/teamwebhq/issuebot-public/releases/latest/download/install.sh | sh
```

### Exact Release
```
curl -fsSL https://github.com/teamwebhq/issuebot-public/releases/download/v0.2.0/install.sh | sh -s -- 0.2.0
```

Before you start, make sure that the machine has `curl`. If necessary, the
script installs a pinned `uv`. Then, `uv` installs Python if necessary.

The script does these steps:

1. If you give no version, it gets the latest GitHub Release version.
2. If you give a version, it makes sure that the version has the stable `X.Y.Z`
   format.
3. It selects the versioned wheel for that release.
4. It installs a pinned `uv` if the machine has no `uv`.
5. It installs issuebot in `/usr/local/bin`, or in `~/.local/bin` if
   `/usr/local/bin` is not writable. To use a different directory, set
   `ISSUEBOT_BIN_DIR`. The script stops if that directory is not in your
   `PATH`.
6. It makes sure that the installed command reports the selected version.

You can run the script again. To update issuebot, run the script for the latest
release. To install a different release, give its stable `X.Y.Z` version.

```sh
issuebot --help             # make sure issuebot is on your PATH
issuebot version            # the version of the package
```

## Quickstart

```sh
# 1. Connect this installation to your board server
issuebot init

# 2. Make a connection: a board, a place to work, and where results go
issuebot connect

# 3. Claim and work tasks until you push Ctrl-C
issuebot listen
```

Run `issuebot init` to make the configuration file.

The command writes `~/.config/issuebot/config.toml`. It prompts for the Issuebear
URL, the agent PAT, and the harness. It also prompts for the harness executable
path. The command finds the API and MCP endpoints from the Issuebear URL. If it
cannot find them, it prompts for each endpoint. Before it writes the file, the
command makes sure that the PAT can get work.

For the `claude` harness, `issuebot init` tries to add the board MCP server to
Claude Code. The command uses the `user` scope. This setup lets you speak to the
board directly. To skip this setup, use the `--skip-harness-setup` option.

Run `issuebot connect` without `--name` and `--board` to start the wizard. The
wizard does these steps:

1. The `issuebear` source prompts for the organization, project, and board.
2. The wizard gives a default name for the connection.
3. It shows the installed environments and prompts you to select one.
4. It prompts for the settings of the selected environment. For `local`, it
   prompts for the mode and the source of the working copy. In `build` mode, it
   prompts for `none`, `branch`, or `worktree`. It also prompts for the folder
   or clone URL.
5. It prompts for update-base only for a task branch. Then, it prompts for plan
   confirmation and done-mode.
6. For each sink, it prompts you to select `no`, `required`, or `best-effort`.

You can also use flags with `issuebot connect`. Refer to
[Connections](#connections).

In script mode, give the **board id** with `--board`. The `issuebear` source
uses the **agent PAT** in the configuration. It gets only tasks that the board
gives to the agent identity of that PAT.

Run `issuebot listen` with no names to monitor all configured connections. Give
one or more names (`issuebot listen myproj otherproj`) to monitor only those
connections. The command monitors the configuration file. Thus, issuebot uses
a change from `issuebot connect` immediately. If the command cannot read a
change, it reports the error and continues with the active connections.

Run `issuebot doctor` to examine an installation. First, it makes sure that the
PAT can get work. It stops if the PAT cannot get work. Then, it examines the
harness executable. It also does the available plugin checks for each
connection. All checks after the PAT check are warnings.

## How a task runs

1. **Get work** — issuebot gets a work item from the source.
2. **Claim the task** — issuebot claims an assigned task. If a different listener
   has the claim, issuebot continues to the next work item. A mention uses its
   non-locking run from the source.
3. **Set the outputs** — issuebot sets the output kinds that the run can
   report. The agent prompt shows only these output kinds. Refer to
   [What a run can report](#what-a-run-can-report).
4. **Prepare the workspace** — issuebot makes the working copy or uses it again.
5. **Run the bootstrap** — issuebot runs the
   [`.issuebear.toml`](#workspace-bootstrap) bootstrap.
6. **Start the agent** — the harness starts the agent in the workspace. The
   source supplies the board MCP server with the agent PAT.
7. **Monitor the run** — issuebot shows a live feed. It sends heartbeats if the
   source supplies a run ID.
8. **Read the response** — the agent writes JSON to the path in the
   `$ISSUEBOT_RESPONSE` variable. issuebot reads this file. The run fails if
   the file is missing or incorrect.
9. **Commit changes** — if the run permits `changes`, the workspace commits the
   changed files.
10. **Record changes** — issuebot gets the change data from `git`.
11. **Push the branch** — issuebot pushes only if the branch head moved, `push`
    is `true`, and there is an `origin` remote.
12. **Do a check of the response** — issuebot makes sure that the run permits
    each output kind. It makes sure that each output has its necessary field. It
    permits a maximum of one decision. It rejects a `changes` output if the
    branch did not move.
13. **Send deliverables** — issuebot sends each deliverable to each sink that
    accepts its kind. It uses the sink sequence in the connection. All sinks
    get their applicable deliverables.
14. **Sink results** — issuebot fails the run if a required sink
    fails. It does not apply the decisions. A `best-effort` sink failure does
    not fail the run.
15. **Apply decisions** — issuebot sends each decision to the source.
16. **Report sink results** — the source adds a task comment if there is a sink
    result.
17. **Release the run** — issuebot releases the run as done or failed. The
    release data includes result text when applicable.

### Comments on the task

The agent uses task comments for answers and a summary of its work. After it
uses `ask_questions`, the agent adds one short comment. This comment only says
that the questions are in the form. It does not write the questions again.

After the agent completes its work, issuebot adds a comment if a sink has a
result. Thus, the comment can include a pull request URL. If there are no sink
results, issuebot adds no comment.

### The clarify loop

If the agent cannot continue without an answer, it uses `ask_questions`. This
command puts the questions in a question form and stops the run until you
answer. The agent adds one short task comment. This comment only says that the
questions are in the form. It does not write the questions again in the
comment.

The response file reports a `needs_input` output and its `question`. issuebot
sets the task to the awaiting-input status. Do these steps to continue:

1. Write your answer in a task comment.
2. Assign the task to the agent again.

The next run uses the task workspace again. If session resumption is active,
the next run also continues the same agent conversation. Refer to
[Session resumption](#session-resumption).

## Configuration

The configuration file is at `~/.config/issuebot/config.toml`. Set the
`$ISSUEBOT_CONFIG` variable to use a different path. The file contains your
credentials. Thus, issuebot writes it with the `0600` permissions. Keep these
permissions. The board server does not read this file.

The configuration has a small number of core keys. It also has keys for the five
plugin axes. The plugin keys have two structures:

- A table with the name of the plugin, for example `[git]`, `[claude]`, or
  `[connections.railway]`
- A bare key that the plugin claims, for example `git_init` or `board`.

If no installed plugin claims a key, issuebot does not load the configuration.

Core keys:

| Key | Default | Meaning |
|---|---|---|
| `harness` | — | Agent CLI that does the work. **Necessary** only if more than one harness is installed |
| `max_concurrent` | `1` | Maximum number of tasks that issuebot does at the same time for all connections. Restart issuebot after a change |
| `task_timeout_minutes` | not set | Maximum time for one run. If not set, there is no maximum |
| `update_command` | the latest-release [installer](#install) | Command that starts when the board sends an update control |
| `connections` | `[]` | Connections, with one array-of-tables entry for each connection |

The `harness` key and the `executor` key of each connection have no default. If
you do not give a name, issuebot uses the one installed harness or environment.
If more than one is installed, issuebot reports this:

```text
no harness named, and 3 are installed — set harness = "…" (known: claude, codex, fake)
connection 'web': no environment named, and 2 are installed — set executor = "…" (known: local, railway)
```

More than one harness and more than one environment are installed in this
build. Thus, write the two keys. `issuebot connect` writes `executor`. In script
mode, it stops if you do not give `--executor` and more than one environment is
installed. `issuebot init` writes `harness`. Refer to
[Environments](#environments).

The default `update_command` downloads `install.sh` from the latest GitHub
Release. issuebot starts the command without a shell. Write a different command
as a command string with its arguments. To use a pipeline, put it in
`sh -c '…'`.

An update waits for active tasks. issuebot stops new task claims and lets the
active tasks complete. Then, it does the update. The update control gives each
listener 30 minutes to complete its active work.

If a listener does not complete, issuebot does not update. It reports the
problem. The runner continues with the installed version.

A restart control stops active agents and does not wait.

A minimal configuration:

```toml
harness = "claude"

# The global table of the issuebear source: the board and the credential.
[issuebear]
api_url = "https://issuebear.example.com/api"
mcp_url = "https://issuebear.example.com/mcp"
pat = "ib_pat_xxx"

[[connections]]
name = "myproj"
board = "board-1"
folder = "/home/me/code/myproj"
executor = "local"
```

A configuration with a git workspace, a sink and three global tables:

```toml
harness = "claude"
max_concurrent = 2
task_timeout_minutes = 30

[issuebear]
api_url = "https://issuebear.example.com/api"
mcp_url = "https://issuebear.example.com/mcp"
pat = "ib_pat_xxx"
install_name = "laptop"              # the name of this installation on the dashboard
telemetry_interval_seconds = 15

[claude]
command = "/Users/you/.claude/local/claude"   # empty or absent = find it on the PATH
resume_sessions = true

[git]
worktree_root = "/var/tmp/issuebot/worktrees"   # default: <state dir>/worktrees
clone_root = "/var/tmp/issuebot/clones"         # default: <state dir>/clones

[github]
summary_model = "claude-haiku-4-5"   # the model for the pull request description

[[connections]]
name = "web"
board = "board-1"
folder = "/home/me/code/web"
executor = "local"
git_init = "worktree"        # the keys of git are on the connection
update_base = "merge"
confirm = true
done = "review"
sinks = ["github"]
```

### Validation

issuebot rejects a configuration that it cannot use. It reports the problems
that it finds together. issuebot examines these items:

- Unknown keys, with a possible correct key
- A key that a plugin claims when the connection does not use that plugin, for
  example `[connections.railway]` with the `local` executor
- The `source`, the `executor`, and each sink, which must be installed
- The type of each setting
- Rules between fields, for example the permitted combinations of `git_init`,
  `repo`, and `folder`
- Rules for sinks, for example a pushed-branch sink with `push = false`.

If issuebot cannot use a configuration, the command reports the problems after
`Config error in <path>:`. It exits with code 1. If there is no file, the
command tells you to run `issuebot init` first.

## Connections

A connection has one `source → workspace → environment → sinks` structure. The
core fields are `name`, `source`, `folder`, `executor`, and `sinks`. In a correct
configuration, a plugin claims each other field.

Use `issuebot connect` with flags in a script:

```sh
issuebot connect --name myproj --board <board-id> --folder /path/to/repo \
  --isolation worktree --update-base merge --confirm yes --sinks github
```

| Flag | Writes | Notes |
|---|---|---|
| `--name` | `name` | Use with `--board`. Without `--name` and `--board`, the wizard starts |
| `--board` | `board` | the key of the issuebear source |
| `--folder` | `folder` | Absolute path. The folder must be a directory |
| `--repo` | `repo` | Clone URL. Makes a new clone for each task. Do not use with `--folder` |
| `--isolation` | `git_init` | What to make in the working copy: `none` (default) makes nothing, `branch`, or `worktree`. Refer to [Workspaces](#workspaces) |
| `--branch-prefix` | `branch_prefix` | Default `issuebot/`. If you change the default, use `branch` or `worktree` isolation |
| `--update-base` | `update_base` | `none` (default), `rebase` or `merge` |
| `--mode` | `mode` | `build` (default) or `respond` |
| `--done` | `done` | `review` (default) or `complete` |
| `--confirm` | `confirm` | `yes` (default) or `no`. If `yes`, the agent waits for approval of the plan before it writes code |
| `--executor` | `executor` | Installed environment. `--help` shows the list. Necessary only if more than one is installed |
| `--sinks` | `sinks` | Repeatable and in sequence. `NAME[:best-effort]` |
| `--set` | a plugin setting | Repeatable. `<name>.<key>=<value>` |

Use `--set` for plugin connection settings that do not have flags:

```sh
issuebot connect --name web --board <board-id> \
  --repo https://github.com/org/web.git --isolation branch \
  --executor railway \
  --set railway.environment_id=<env-id> \
  --set railway.token=<token> \
  --sinks github
```

`issuebot connect --help` shows each key that you can use with `--set` in this
installation. It shows the permitted values and descriptions that each plugin
supplies.
issuebot examines each value immediately and rejects an incorrect value. For
example, it can report `Input should be 'isolated' or 'private'`. If a different
flag writes the same key, issuebot rejects the value. It also gives the flag
name, for example `--set git.git_init=…`.

Before it writes, `issuebot connect` uses the configuration rules to examine
the new connection. It also rejects a second connection to a board that this
agent has.

`issuebot disconnect --name <name>` removes a connection from the configuration
and tells the server. If there is no server response, the connection stays
removed from the configuration.

`issuebot connections` shows a summary of each connection:

```
$ issuebot connections
2 connections:

myproj  ·  board board-1  ·  /home/me/code/myproj
    mode           build
    isolation      worktree
    done           review
    confirm        yes
    update-base    merge
    branch-prefix  issuebot/
    sinks          github

docs  ·  board board-2  ·  /home/me/code/docs
    mode           respond
    isolation      none
    done           review
    confirm        yes
    update-base    none
    branch-prefix  issuebot/
    sinks          none
```

## Workspaces

You make two selections for the workspace. Each connection has the two
selections:

| | Setting | Answers |
|---|---|---|
| **Working-copy source** | `folder` or `repo` | a folder on this machine, or a clone from a URL |
| **Git preparation** | `git_init` | a task branch, a task worktree, or no task branch |

You can use each `git_init` value with `folder` or `repo`.

Set one of these working-copy source keys:

- `folder = "/path/to/repo"` — issuebot uses this folder and does not clone it.
  For a git workspace, this folder must be a git repository.
- `repo = "https://github.com/org/x.git"` — issuebot clones this URL. With
  `git_init = "branch"` or no `git_init`, it keeps one clone for each task. The
  clone root is `<state dir>/clones` or `[git] clone_root`. Use an HTTPS URL
  for a GitHub repository: issuebot gives each clone the `gh` CLI as its
  credential helper, and `gh` is the only GitHub credential an executor holds.
  An SSH URL needs a key and a known-hosts entry that an executor does not get.

With `repo` and `git_init = "worktree"`, issuebot keeps one clone for all task
worktrees. It makes the task worktree in `<state dir>/worktrees` or
`[git] worktree_root`.

If you set the two source keys, issuebot does not load the configuration.

The board sends the repository of each task's project with the task. If that
repository is not the `repo` of the connection, issuebot fails the run and puts
both URLs on the task. It does not change the configuration. Set `repo` to the
repository of the project, or connect the project to the correct repository.

issuebot makes no check for a connection with a `folder`, or for a task whose
project has no repository.

Use `git_init` to select the git preparation:

- If `git_init` is not set, issuebot uses the current branch. It does not commit
  or push changes. Thus, the run cannot report a `changes` output.
- `git_init = "branch"` — issuebot checks out a task branch in the working
  copy. The first task branch has the default name `issuebot/<ref>`.
- `git_init = "worktree"` — issuebot makes a worktree on a task branch. The
  first task branch has the default name `issuebot/<ref>`.

The git workspace also uses these keys:

- `branch_prefix` — sets the task branch prefix. The default is `issuebot/`.
- `update_base` — uses `none`, `rebase`, or `merge`. If there is an `origin`
  remote, a rebase or merge uses its default branch. The default is `none`.
- `push` — controls the push after a commit. The default is `true`.

Use `branch_prefix` and `update_base` only with `git_init`. issuebot rejects
these keys when `git_init` is not set.

If a run permits `changes`, issuebot commits all changed files. It pushes only
if `push` is `true` and there is an `origin` remote. A [sink](#sinks) can then
use the pushed branch.

If `push` is `false`, the work stays on this machine. issuebot rejects this
setting for a sink that uses a pushed branch.

A connection with only `folder` uses that folder and does not commit changes.
Set `folder_init = "copy"` to make a temporary copy for each task.

### Git workspace commands

Use the `issuebot git` group for git worktrees and clones. Run these commands
to see the applicable `list` and `prune` commands:

```sh
issuebot git --help
issuebot git worktree --help
issuebot git clone --help
```

Run these commands to list the managed working copies:

```sh
issuebot git worktree list
issuebot git clone list
```

Use a task ref or a selection option with a prune command. The commands do not
prune a dirty or unpushed working copy unless the command has the `--force`
option.

```sh
issuebot git worktree prune ISS-42
issuebot git worktree prune --all
issuebot git worktree prune --merged
issuebot git worktree prune ISS-42 --force

issuebot git clone prune ISS-42
issuebot git clone prune --all
issuebot git clone prune --merged
issuebot git clone prune ISS-42 --force
```

The worktree `--merged` option uses git branch ancestry. The clone `--merged`
option uses `gh pr view`.

### A task that runs again

A task can have more than one run. issuebot uses the applicable task branch
again:

- **The working copy is available.** issuebot uses it again. It fetches a clone
  on this machine. For a connection with a task branch, it fast-forwards the
  branch from `origin`.
- **The working copy is not available.** issuebot makes it again. If the task
  branch is on this machine or on `origin`, issuebot continues from that branch.

If the default branch contains a previous task branch, issuebot uses the next
branch name. It also uses the next name if a previous branch has a merged pull
request. For example, it can use `issuebot/<ref>-2`.

Workspace preparation fails if issuebot cannot fast-forward the task branch.
It also fails if an `update_base` rebase or merge causes a conflict. Correct
the branch. Then, assign the task to the agent again.

If the run permits `changes`, a preparation or bootstrap failure fails the run.
For other runs, issuebot uses the connection folder after a preparation
failure. Without a connection folder, the run fails.

For these other runs, a bootstrap failure does not fail the run. issuebot
starts the agent without the data that the bootstrap adds.

## What a run can report

The agent writes one JSON document to the path in the `$ISSUEBOT_RESPONSE`
variable. The document is `{"outputs": [...]}`. It can contain no outputs or
one or more outputs of four kinds.

| Kind | Contains | Category |
|---|---|---|
| `changes` | `summary` | deliverable |
| `answer` | `text` | deliverable |
| `needs_input` | `question` | decision |
| `handoff` | `assignee`, `note` | decision |

issuebot sends each deliverable to each sink that accepts its kind. It sends a
decision to the source. A run can report a maximum of one decision.

Two conditions decrease the output kinds that a run can report:

- **The kind of work.** An assignment can report all four kinds. An **@mention**
  cannot report `changes`. A connection with `mode = "respond"` also cannot
  report `changes`.
- **The workspace.** A [workspace](#workspaces) without a task branch cannot
  report `changes`.

The agent prompt shows only the permitted kinds. issuebot rejects each other
kind.

The `mode` setting controls the first condition. The `respond` mode permits all
kinds other than `changes`. The mode does not select the workspace.

The `respond` mode is not a sandbox. A run in `respond` mode can report only
kinds other than `changes`.
issuebot tells the agent not to change files and rejects a `changes` output.
The agent has its usual file tools and shell tools. Refer to
[Security](#security).

## Sinks

A sink publishes a deliverable. Sinks run on the controller and not in a
sandbox. Thus, issuebot does not send sink credentials to a sandbox.

Set the sinks for each connection, in the sequence that they run:

```sh
issuebot connect --name web --board <id> --repo <url> --isolation branch \
  --sinks github
```

A sink is **required** by default. If a required sink fails, the run fails and
issuebot does not apply its decisions. Add the `:best-effort` suffix to let the
task complete after a sink failure:

```sh
issuebot connect --name web --board <id> --folder /path --isolation branch \
  --sinks github:best-effort
```

In the configuration, write `sinks = ["github"]` or
`sinks = [{ name = "github", required = false }]`.

A sink receives only the output kinds that it accepts. The **`github`** sink
accepts only `changes` from a pushed branch. It does these steps:

1. **Do a check of change data** — the sink makes sure that the branch head is
   different from its base.
2. **Find the repository** — the sink uses the connection `repo` value. If
   there is no `repo` value, it uses the checkout `origin`.
3. **Do a check of the remote branch** — the sink uses the GitHub compare API.
   The branch head must be after its base.
4. **Make the description** — the harness makes the pull request title and body
   from the local diff. The `[github]
   summary_model` key sets the model.
5. **Find an open pull request** — the sink uses an open pull request that it
   finds for the branch.
6. **Open a pull request** — if the sink does not find an open pull request, it
   opens one with the `gh` CLI.

The sink cannot use the harness when the controller has no checkout. It also
cannot use it if the harness is missing, fails, or gives empty text. In these
cases, the sink uses the `changes` summary and `git diff --stat` data.

The GitHub API path includes the repository name. Each `gh pr` command uses
`-R owner/name`.

Install `gh`. Authenticate the `gh` CLI. `issuebot doctor` examines it.

A connection with no sinks keeps the work on the task branch.

## Environments

The environment sets which machine the agent runs on. Set it with `--executor`
on each connection. There is no default, and two environments are available.

**`local`** — the agent runs as a subprocess on the machine that runs
`issuebot listen`, in the workspace of the connection. This environment has no
settings.

**`railway`** — each task gets a new
[Railway sandbox](https://docs.railway.com/sandboxes). A sandbox is a temporary
Linux VM. issuebot makes it for the task and deletes it when the task ends.
`issuebot listen` stays on your machine and does these steps:

1. It claims the task.
2. It starts a sandbox.
3. It aligns the sandbox to the controller's exact released distribution
   version.
4. It runs `issuebot run-one` in the sandbox.
5. It sends the output to your terminal.
6. It gets the result.
7. It deletes the sandbox.

Railway execution requires the controller itself to run from an installed,
non-editable release wheel. A source checkout or editable installation is
rejected before issuebot allocates a sandbox. Local execution remains available
from a development checkout.

> **Note.** Railway Sandboxes change frequently. Make sure that
> `issuebot railway build-template` operates with your installed `railway` CLI.

### Railway prerequisites

1. A **Railway account** with sandbox access, and a project and an environment
   for the sandboxes.
2. The **`railway` CLI** on the `PATH` of the machine that runs
   `issuebot listen`.
3. A **Railway token** for each connection, set with `--set railway.token=…`.
   Thus one runner can use sandboxes in more than one project. A *project*
   token operates only in the environment that it was made for. Set
   `--set railway.token_kind=project|account` to tell the CLI which variable to
   read the token from: `RAILWAY_TOKEN` or `RAILWAY_API_TOKEN`. A connection
   with no token uses the variable that the `issuebot listen` process has.
4. The secrets of the agent, as **shared variables in that Railway
   environment**: `ANTHROPIC_API_KEY` for the `claude` harness, and `GH_TOKEN`
   to clone and to push. issuebot points to these shared variables, but does not
   read them. issuebot sends the board URLs and the agent PAT from your
   configuration.

`issuebot doctor` gives a warning if the CLI is absent, or if a railway
connection has no token and the environment has no token.

### Build the tooling template

A sandbox starts from a template with the tools that the agent needs: git, gh,
curl, node, npm and uv, plus the controller's exact release wheel. Build the
template one time for each Railway project, while running the controller from a
released wheel:

```sh
issuebot railway build-template                    # in the default project
issuebot railway build-template --connection web   # with the token of a connection
```

### Configure a railway connection

The wizard shows Railway when it asks where the tasks must run. For Railway,
the wizard sets a cloned working copy on a task branch. Then it asks for the
repository URL, the environment id, the network mode and the token of this
connection. If the CLI is not ready, the wizard gives a warning but continues.

To configure the connection manually, or in a script:

```toml
harness = "claude"
max_concurrent = 3          # railway tasks at the same time (default 1 = one task)

[issuebear]
api_url = "https://issuebear.example.com/api"
mcp_url = "https://issuebear.example.com/mcp"
pat = "ib_pat_xxx"

[[connections]]
name = "web"
board = "board-1"
executor = "railway"
repo = "https://github.com/org/web.git"
git_init = "branch"
done = "review"
sinks = ["github"]

[connections.railway]
environment_id = "<env-id>"   # necessary
network = "isolated"          # "isolated" (default) | "private"
token = "<token>"             # absent = use the variable of the runner
token_kind = "project"        # "project" (default) | "account"
```

With `network = "private"`, the sandbox joins the private network of the
environment. Then the agent can get access to your services, for example
`postgres.railway.internal`. Use this mode only if a task needs those services.

A railway connection must have a `[connections.railway]` table with an
`environment_id` key. Without the key, the configuration does not load.

You can use local connections and railway connections in one configuration. The
`executor` key of the connection sets the route for each task.

### Sandbox features

- **Warm starts** — the first run for a project caches a checkpoint with the
  repository and the installed dependencies. Subsequent runs start from the
  checkpoint and fetch the latest commits.
- **Concurrency** — issuebot runs a maximum of `max_concurrent` tasks at the
  same time, each one in its own sandbox. Your Railway plan can set a lower
  maximum.
- **Pause and resume** — if a run ends with `needs_input`, issuebot writes a
  checkpoint and deletes the sandbox. When the task comes back, the next run
  starts from that checkpoint. On the `claude` harness, the checkpoint also
  contains the agent conversation.
- **Version alignment** — at the start, the controller asks the sandbox which
  distribution version of issuebot it has. If the versions differ, the
  controller installs its own exact GitHub Release wheel and verifies the
  sandbox again before starting work.

issuebot always deletes the sandbox when the task ends. Delete the checkpoints
of paused tasks with these commands:

```sh
issuebot railway prune-checkpoints                 # more than 7 days old
issuebot railway prune-checkpoints --ttl-hours 24
```

> **Caution.** The Free plan of Railway sets a maximum idle timeout of 5
> minutes for a sandbox. This is too short for an agent run. Use the Hobby plan
> or the Pro plan.

## Harnesses

The harness starts one agent CLI. Select the harness at `issuebot init`, or set
the `harness` key in the configuration.

- **`claude`** — Claude Code without a terminal (`claude -p …`). issuebot uses
  `--strict-mcp-config`, thus the agent has only the board MCP and not your
  other MCP servers. issuebot also uses `--dangerously-skip-permissions`,
  because an unattended runner cannot give approvals, and
  `--output-format stream-json` for the live feed and the log.
- **`codex`** — Codex without a terminal (`codex exec …`).

The CLI must be on your `PATH`. `issuebot doctor` examines it. If the CLI is
not on your `PATH`, give the path at `issuebot init`, or set the path in the
table of the harness:

```toml
# fragment: one table of a full configuration
[claude]
command = "/Users/you/.claude/local/claude"
```

If the `command` key is not set, issuebot finds the name of the harness on the
`PATH`.

Each start of the agent writes a temporary MCP configuration file. The file
contains the MCP server of the board, with the name of your source. Thus the
board tools of the agent are `mcp__issuebear__get_task`,
`mcp__issuebear__add_comment` and so on. Use this prefix in your own skills and
prompts. A [bootstrap](#workspace-bootstrap) can add more servers. issuebot
merges the servers of the source last, thus a repository cannot replace them.

The harness also reads a run log. `issuebot logs` asks the configured harness to
read the log. If issuebot cannot find a harness, it shows the raw lines.

### Session resumption

By default, each run starts a new agent that reads the comments of the task for
context. On the `claude` harness, issuebot can keep the session id of each task
and continue that session:

```toml
# fragment: one table of a full configuration
[claude]
resume_sessions = true
```

issuebot writes the session ids to `<state dir>/sessions.json` with the `0600`
permissions. To use a different directory, set the `$ISSUEBOT_STATE` variable.
If a session is expired, issuebot starts a new agent. Use
`issuebot claude session list` and `issuebot claude session prune` to manage
the sessions.

The `resume_sessions` key controls the local environment. In a Railway sandbox,
issuebot always keeps the session of a paused task and continues it on the next
run. Only `claude` has sessions. Thus a codex run in a sandbox restores the
worktree, but starts a new conversation.

## Monitor a run

`issuebot listen` shows a live feed in your terminal while a task runs. It also
writes the full transcript to a log for each run, gives a warning if the agent
stops to write, and stops correctly at Ctrl-C.

### The live feed

Each event of the agent is one line: 🔧 for a tool, 💬 for text from the agent,
and ✓ for the final result of the run.

```text
▶ ISS-42 — working in /path/to/repo
  log: /home/me/.local/state/issuebot/logs/ISS-42-20260629-200000.jsonl
  🔧 Read: src/issuebot/runner.py
  🔧 Bash: uv run pytest -q
  💬 Found the failing case — fixing the off-by-one in the claim loop.
  🔧 Edit: src/issuebot/runner.py
  ✓ Done — posted a summary comment and reassigned for review.
✓ ISS-42 done in 92s
```

The feed starts with a `▶` line and the path of the [log](#logs) of that run.
It shows the elapsed time, and ends with `✓` or `✗`. If you listen on more than
one board, each line starts with the ref of the task. The feed goes to
**stderr**, thus it does not pollute stdout.

### Stall warning

If there is no output for a long time, issuebot gives a warning:

```text
⚠ ISS-42 — no output for 90s (elapsed 240s) — still running; Ctrl-C to abort
```

issuebot repeats the warning, with new numbers, until the agent writes again.

### Ctrl-C

Ctrl-C stops the agent and releases the run. Thus no claim stays on the board.
A second Ctrl-C exits immediately.

### Hard timeout

By default, a run ends only when the agent exits, or when you stop it. To set a
maximum time in minutes, use this key:

```toml
# fragment: one key of a full configuration
task_timeout_minutes = 30
```

If a run is longer than the maximum, issuebot stops it, releases it, and
classifies it as timed out.

### Logs

issuebot always writes the full output of the harness to this file:

```
~/.local/state/issuebot/logs/<ref>-<timestamp>.jsonl
```

issuebot obeys the `$XDG_STATE_HOME` variable. Use `tail -f` on the file, or
use these commands:

```sh
issuebot logs                 # the recent runs, the newest first
issuebot logs ISS-42          # the latest run of that ref, as the feed
issuebot logs ISS-42 --raw    # the latest run of that ref, as raw lines
issuebot logs ISS-42 -n 100   # the last 100 lines (0 = all the lines)
issuebot logs -f              # follow the run that is active now
issuebot logs ISS-42 -f       # follow the latest run of that ref
```

With `-f` and no ref, issuebot follows the run that the runner reports. If
there is no active run, it follows the most recent run on the disk.

### Status

`issuebot status` shows each configured connection. If a `listen` process runs
on this machine, the command also shows its phase, its task, and the log path
of that run:

```
$ issuebot status
Runner: active (pid 8421, v0.1.0, updated 3s ago).

myproj  board-1  /home/me/code/myproj  working  ISS-42  /home/me/.local/state/issuebot/logs/ISS-42-20260629-200000.jsonl
docs  board-2  /home/me/code/docs  waiting  —
```

The command reads a local status file that the listener writes. There is no
request to the server, thus the command operates offline and in a different
terminal. If the file is absent or old, no runner is active. issuebot shows the
connections.

## Skills, plans and confirmation

On the `claude` harness, issuebot loads four board skills into each agent with
`--plugin-dir`. Your own skills stay available.

- **`board-brainstorming`** — if the scope, the requirements or the approach of
  a task are not clear, the agent writes its questions on the task as a form.
  The agent uses multiple choice when it can. Then it assigns the task back.
- **`board-implementing`** — if the requirements are clear, the agent writes its
  plan on the task, works test-first, obeys the `CLAUDE.md` or `AGENTS.md` file
  of the repository, writes comments as it works, and obeys the done-mode.
- **`board-planning`** — if the agent finds work that the task does not cover,
  the agent writes a new task and does not make the branch larger. The skill
  shows the agent where work goes: the plan, a checklist, a sub-task or a new
  task. It also gives the shape of a task description, in three parts — why the
  work is necessary, the expected outcome, then the technical detail. People who
  do not read code read the first two parts.
- **`writing-pull-requests`** — the shape of a pull request that a person can
  review: the reason for the change first, then the changes, the tests, and the
  items to examine by hand.

The first three skills are only for the `claude` harness. On the `codex`
harness, the prompt gives the same instructions.

`writing-pull-requests` is different. The pull request text comes from a
separate agent call that has no tools and loads no skills, thus issuebot puts
the text of that skill directly in the prompt. To change how issuebot writes
pull requests, change that one file.

The board gives the agent three tools:

- **the plan** — `set_plan` records what the agent will do. The task holds one
  plan. The agent changes that plan, and does not write a new comment. Thus the
  activity log shows the history. The agent always writes a plan.
- **questions** — `ask_questions` writes the questions of the agent on the task
  as a form. The agent always asks, and does not guess.
- **confirmation** — `request_confirmation` asks the person if the agent can
  continue: Yes or No. If the answer is No, the person writes what to change.

The `confirm` setting of a connection controls the confirmation:

- `yes` (default) — the agent writes the plan and waits for approval before it
  writes code.
- `no` — the agent writes the plan and continues. The agent can still use
  `request_confirmation` for an operation that it cannot undo.

The `done` setting of a connection controls the last step:

- `review` (default) — the agent writes a summary and assigns the task back for
  review. The agent does not complete the task.
- `complete` — the agent writes a summary and completes the task.

## Workspace bootstrap

A repository can set how issuebot prepares its workspace. To do this, commit a
`.issuebear.toml` file to the root of the repository. The file is optional.

```toml
# fragment: this is .issuebear.toml in a target repository, not the runner configuration
[bootstrap]
# Shell commands that run in sequence in the workspace before the agent starts.
setup = ["uv sync", "npm ci"]

[bootstrap.env]            # exported for the setup commands and for the agent
NODE_ENV = "test"

[[bootstrap.mcp]]          # more MCP servers, merged below the board MCP
name = "chrome-devtools"
command = "npx"
args = ["-y", "chrome-devtools-mcp@latest"]

[bootstrap.plugins]        # more --plugin-dir entries, relative to the repository
dirs = [".claude/plugins/browser"]
```

`[[bootstrap.mcp]]` also accepts an HTTP form: `type = "http"`, `url = "…"` and
`headers = { … }`.

**Times.** The `setup` commands run when issuebot makes the workspace, and
again after a change to the `[bootstrap]` table. The `env`, `mcp` and `plugins`
data apply at each start of the agent.

**Trust.** The setup commands run at the trust level of the agent, in the same
workspace and with the same permissions. Use issuebot only with repositories
that you trust.

**Failure.** If the file is incorrect, or if a setup command fails, the task
fails and the agent does not start. If the run cannot report `changes`,
issuebot uses the folder of the connection and the run continues.

## Housekeeping

```sh
issuebot git worktree list
issuebot git worktree prune <ref>… | --all | --merged [--force]

issuebot git clone list
issuebot git clone prune <ref>… | --all | --merged [--force]

issuebot claude session list
issuebot claude session prune <task-id>… | --all | --completed

issuebot railway prune-checkpoints [--ttl-hours N] [--connection NAME]
```

Each `prune` command needs a selector. A `prune` command refuses a workspace
with uncommitted changes or unpushed commits. To delete it, use
`issuebot git worktree prune --force`. The `issuebot git clone prune --merged`
command uses `gh pr view` to find the work that is complete. The
`issuebot claude session prune --completed` command deletes the entries of
tasks that are complete on the board.

issuebot deletes nothing automatically. The clones and the worktrees stay until
you delete them.

## Security

- **The PAT stays on your machine**, in `~/.config/issuebot/config.toml` with
  the `0600` permissions. The server does not read your configuration. Use a
  dedicated agent PAT that you can revoke, not a personal token.
- **`ps` can show the PAT during the setup.** On the `claude` harness,
  `issuebot init` runs `claude mcp add --header "Authorization: Bearer <pat>"`,
  which puts the PAT in a command-line argument. This is a risk on a machine
  that more than one person uses.
- **The agent runs unattended and asks for no approvals.** The `claude` harness
  uses `--dangerously-skip-permissions`, thus the agent can change files and
  run commands in its workspace. Use issuebot only with a workspace and a branch
  that the agent can safely change.
- **The `respond` mode is a restriction on the report, not on the tools.** Refer
  to [What a run can report](#what-a-run-can-report). The agent keeps its file
  tools and shell tools. For full containment, use a sandbox environment for the
  connection.
- **A `.issuebear.toml` file runs shell commands** in the workspace, at the
  trust level of the agent. It can also add MCP servers. Use issuebot only with
  repositories that you trust.
- **Use one agent identity for each machine.** A runner uses one agent PAT. Do
  not share the PAT.
- **A session id is a resumption token.** With `resume_sessions = true`,
  issuebot writes the session ids to `sessions.json` with the `0600`
  permissions. Delete the file to make the session ids invalid.
- **Sink credentials stay on the controller.** A sink does not run in a sandbox,
  thus its credential does not go to a sandbox. A sandbox gets the board URLs
  and the agent PAT from your configuration, and the shared variables of the
  Railway environment: `GH_TOKEN` and `ANTHROPIC_API_KEY`.

---

To work on issuebot, or to write a plugin, refer to
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
