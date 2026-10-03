"""Static checks of ``.github/workflows`` (the workflows cannot be run locally).

What these pin down: nothing crawls or deploys unless the owner has set the repository
variable ``PUBLISH_ENABLED``; the workflows call only the CLI; a crawl can never start on
a runner without the pipeline state; least-privilege tokens; pinned actions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import cli
from src.config import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STATE_ACTION = "./.github/actions/pipeline-state"
ACTION_FILE = REPO_ROOT / ".github" / "actions" / "pipeline-state" / "action.yml"
PIP_LOCKED = "python -m pip install --require-hashes -r requirements-lock.txt"
ACCEPT = "${{ inputs.accept_changes && '--accept-changes' || '' }}"
GATED = ["deploy_pages.yml", "scrape_and_update.yml"]
GATE = "if: vars.PUBLISH_ENABLED == 'true' && github.ref == 'refs/heads/main'"


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    """Job id -> its block (two-space indented keys under ``jobs:``)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z_-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _commands(text: str) -> list[str]:
    """Every shell line of every ``run:`` (single-line and block form)."""
    out: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run: (.*)$", line)
        if not m:
            continue
        if m.group(2).strip() != "|":
            out.append(m.group(2).strip())
            continue
        indent = len(m.group(1)) + 2
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(m.group(1)):
                break
            if nxt.strip():
                out.append(nxt[indent:].strip() if len(nxt) > indent else nxt.strip())
    return out


def test_expected_workflows() -> None:
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "deploy_pages.yml",
                                                           "scrape_and_update.yml"]


@pytest.mark.parametrize("name", GATED)
def test_every_job_is_gated_on_the_owner_opt_in(name: str) -> None:
    jobs = _jobs(_text(name))
    assert len(jobs) == 2 and "deploy" in jobs
    for job, block in jobs.items():
        assert f"    {GATE}\n" in block, f"{name}: job {job} is not gated"
        # the gate is the job-level condition, i.e. it stands before the steps
        assert block.index(GATE) < block.index("    steps:"), job


def test_triggers() -> None:
    deploy, scrape, ci = _text("deploy_pages.yml"), _text("scrape_and_update.yml"), _text("ci.yml")
    head = lambda t: t.split("\npermissions:", 1)[0].split("\non:\n", 1)[1]  # noqa: E731
    # the deploy workflow starts by hand only: a merge must not change the public site
    assert re.findall(r"^  ([a-z_]+):", head(deploy), flags=re.M) == ["workflow_dispatch"]
    assert re.findall(r"^  ([a-z_]+):", head(scrape), flags=re.M) == ["schedule",
                                                                     "workflow_dispatch"]
    crons = re.findall(r'cron: "([^"]+)"', scrape)
    assert crons == ["17 3 * 3-10 2", "17 3 1 1,2,11,12 *"]   # weekly in season, monthly off
    assert re.findall(r"^  ([a-z_]+):", head(ci), flags=re.M) == ["push", "pull_request"]
    for text in (deploy, scrape, ci):
        assert "pull_request_target" not in text and "workflow_run" not in text


@pytest.mark.parametrize("name", GATED + ["ci.yml"])
def test_only_cli_calls_in_the_workflows(name: str) -> None:
    """No shell besides the CLI, pytest and the install - not even `gh` (that is the
    local action's job)."""
    allowed = re.compile(
        r"^(python -m src\.cli [a-z-]+( .*)?|python -m pytest|"
        + re.escape(PIP_LOCKED) + r"|python -m pip install \$\{\{ matrix\.pip-args \}\})$")
    commands = _commands(_text(name))
    assert len(commands) >= 4
    for cmd in commands:
        assert allowed.match(cmd), f"{name}: unexpected shell: {cmd}"
    assert (PIP_LOCKED in commands) is (name in GATED)       # publishing jobs: hashed lock
    parser = cli.build_parser()
    for cmd in commands:
        if not cmd.startswith("python -m src.cli "):
            continue
        assert "--allow-empty" not in cmd and "--refresh" not in cmd and "--force" not in cmd
        argv = [a for a in cmd.split()[3:] if not a.startswith(("\"$", "${{", "&&", "||", "''",
                                                                "'--", "inputs.", "}}"))]
        args = parser.parse_args(argv + (["x"] if argv[0].startswith("state-") else []))
        assert args.command == argv[0]             # the CLI really has these options


def test_state_transport_action() -> None:
    """The only `gh` calls: the draft check comes before the download and before the
    upload, and no generation is deleted before the new one is up."""
    text = ACTION_FILE.read_text(encoding="utf-8")
    allowed = re.compile(
        r"^(gh release (download|upload|delete-asset) \"\$STATE_TAG\" .*|"
        r"test \"\$STATE_MODE\" = \"download\" -o \"\$STATE_MODE\" = \"upload\"|"
        r"test \"\$\(gh release view \"\$STATE_TAG\" .*--json isDraft --jq \.isDraft\)\" = \"true\"|"
        r"for old in \"\$RUNNER_TEMP\"/state-in/schwingen-state-\*\.tar\.gz; do|done)$")
    shell = _commands(text)
    for cmd in shell:
        assert allowed.match(cmd), f"unexpected shell in the action: {cmd}"
    order = [next(i for i, c in enumerate(shell) if key in c)
             for key in ("--json isDraft", "gh release download", "gh release upload",
                         "gh release delete-asset")]
    assert order == sorted(order) and order[0] < 2 and "--clobber" not in text
    steps = re.split(r"\n    - name: ", text)[1:]
    assert len(steps) == 3 and "isDraft" in steps[0] and "if:" not in steps[0]   # always runs
    assert "if: inputs.mode == 'download'" in steps[1] and "gh release download" in steps[1]
    assert "if: inputs.mode == 'upload'" in steps[2] and "gh release upload" in steps[2]
    assert "using: composite" in text and "uses:" not in text and "python" not in text
    assert text.count("GH_TOKEN: ${{ inputs.token }}") == 3 and "github.token" not in text
    # the uploaded file is the one the workflows export
    target = '"$RUNNER_TEMP/state-out/schwingen-state-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.tar.gz"'
    assert target in text
    for name in GATED:
        assert f"python -m src.cli state-export {target}" in _commands(_text(name))


@pytest.mark.parametrize("name", GATED)
def test_no_crawl_without_state(name: str) -> None:
    text = _text(name)
    commands = [c for c in _commands(text) if c.startswith("python -m src.cli ")]
    stages = [c.split()[3] for c in commands]
    assert stages.index("state-import") == 0      # before anything that reads the data dir
    for cmd in commands:
        if cmd.split()[3] in ("crawl", "all"):
            assert "--require-state" in cmd, cmd
    if name == "deploy_pages.yml":                 # this workflow never talks to the source
        assert "crawl" not in stages and all("--skip-crawl" in c for c in commands
                                             if c.split()[3] == "all")
    else:
        assert stages == ["state-import", "crawl", "state-export", "all", "check-site",
                          "state-export"]
        assert "python -m src.cli all --skip-crawl --require-state" in commands
    assert any(c.startswith("python -m src.cli check-site --record") for c in commands)
    # download (draft checked) -> import -> ... -> last export -> upload (draft checked
    # again, immediately before) -> Pages artifact
    order = [text.index("mode: download"), text.index("state-import"),
             text.rindex("state-export"), text.index("mode: upload"),
             text.index("upload-pages-artifact")]
    assert order == sorted(order) and "--clobber" not in text and "gh " not in text
    assert text.count(f"uses: {STATE_ACTION}\n") == 2


@pytest.mark.parametrize("name", GATED)
def test_permissions_and_concurrency(name: str) -> None:
    text = _text(name)
    assert "\npermissions: {}\n" in text                       # nothing by default
    jobs = _jobs(text)
    deploy = jobs.pop("deploy")
    [build] = jobs.values()
    assert re.search(r"permissions:\n      contents: write +#[^\n]*\n    steps:", build)
    assert re.search(r"permissions:\n      pages: write\n      id-token: write\n", deploy)
    assert "contents:" not in deploy and "name: github-pages" in deploy
    assert "uses: actions/deploy-pages@v4" in deploy and "needs:" in deploy
    assert "persist-credentials: false" in build
    check_token_scope(text)
    assert "concurrency:\n  group: pipeline-state\n  cancel-in-progress: false\n" in text
    assert "timeout-minutes:" in build and "timeout-minutes:" in deploy
    assert 'python-version: "3.14"' in build and "cache: pip" in build


# ------------------------------------------------------- dangerous edits (review S3)
def check_token_scope(text: str) -> None:
    """The token reaches the state action only: never a step that runs project code,
    never a job- or workflow-level environment."""
    assert "GH_TOKEN" not in text and "GITHUB_TOKEN" not in text and "secrets." not in text
    steps = re.split(r"\n      - ", text.split("\njobs:\n", 1)[1])
    holders = [s for s in steps if "github.token" in s or re.search(r"^ +token:", s, re.M)]
    assert len(holders) == 2
    for step in holders:
        assert f"uses: {STATE_ACTION}\n" in step and "run:" not in step
        assert step.count("github.token") == 1 and "          token: ${{ github.token }}" in step
    assert text.count("github.token") == 2
    # an `env:` above the steps (workflow or job level) would hand its values to every
    # step, project code included; the only step environment is the browser switch
    for line in re.findall(r"^ *env:.*$", text, flags=re.M):
        assert line == "        env:", f"env outside a step: {line!r}"
    assert re.findall(r"^        env:\n((?:          .*\n)+)", text, flags=re.M) == \
        ['          SCHWINGEN_REQUIRE_BROWSER: "1"\n']


EXPECTED_USES = {
    "deploy_pages.yml": ["actions/checkout@v5", "actions/setup-python@v6", STATE_ACTION,
                         STATE_ACTION, "actions/upload-pages-artifact@v4",
                         "actions/deploy-pages@v4"],
    "scrape_and_update.yml": ["actions/checkout@v5", "actions/setup-python@v6", STATE_ACTION,
                              STATE_ACTION, "actions/upload-pages-artifact@v4",
                              "actions/deploy-pages@v4"],
    "ci.yml": ["actions/checkout@v5", "actions/setup-python@v6"],
}


def check_uses(name: str, text: str) -> None:
    """Exactly these actions, in this order: an added upload step (e.g. an artifact of
    `data/`, readable by every signed-in user of a public repository) is an error."""
    assert re.findall(r"uses: (\S+)", text) == EXPECTED_USES[name]
    assert "upload-artifact" not in text and "actions/cache" not in text


def check_pages_artifact(text: str) -> None:
    """The Pages artifact is `dist` and nothing else (`.` would publish the checkout,
    `data` the pipeline state)."""
    assert text.count("upload-pages-artifact") == 1
    assert ("      - uses: actions/upload-pages-artifact@v4\n        with:\n"
            "          path: dist\n\n") in text
    assert re.findall(r"^ *path: *(.*)$", text, flags=re.M) == ["dist"]
    assert "data/" not in text and "data\n" not in text.split("upload-pages-artifact")[1]


def check_accept_changes(text: str) -> None:
    """`--accept-changes` only through the manual input: a scheduled run has no inputs,
    so the expression is empty there and the guard is never overridden unattended."""
    assert text.count("--accept-changes") == 1 and text.count(ACCEPT) == 1
    assert f"run: python -m src.cli check-site --record {ACCEPT}\n" in text
    assert text.count("accept_changes") == 2          # the input and its one use
    head = text.split("\npermissions:", 1)[0]
    assert re.search(r"  workflow_dispatch:\n    inputs:\n      accept_changes:\n"
                     r"(?:        .*\n)+?        type: boolean\n        default: false\n", head)
    assert "SCHWINGEN_" not in text.replace("SCHWINGEN_REQUIRE_BROWSER", "")   # no setting
    #                                             (age, noindex, tolerances) is changed in YAML


@pytest.mark.parametrize("name", GATED)
def test_dangerous_edits_checks_pass_on_the_real_files(name: str) -> None:
    text = _text(name)
    check_token_scope(text)
    check_uses(name, text)
    check_pages_artifact(text)
    check_accept_changes(text)


JOB_ENV = ("    permissions:\n      contents: write",
           "    env:\n      GH_TOKEN: ${{ github.token }}\n    permissions:\n      contents: write")
ARTIFACT_STEP = ("      - uses: actions/upload-pages-artifact@v4\n",
                 "      - uses: actions/upload-artifact@v4\n        with:\n          name: state\n"
                 "          path: data/\n      - uses: actions/upload-pages-artifact@v4\n")


@pytest.mark.parametrize("name", GATED)
@pytest.mark.parametrize("check, old, new", [
    # the four edits of the Phase 6 review that left the old tests green
    ("token", *JOB_ENV),
    ("uses", *ARTIFACT_STEP),
    ("pages", "          path: dist\n", "          path: .\n"),
    ("accept", f"check-site --record {ACCEPT}", "check-site --record --accept-changes"),
    # and their neighbours
    ("token", "\npermissions: {}\n", "\nenv:\n  GH_TOKEN: ${{ github.token }}\n\npermissions: {}\n"),
    ("token", "        run: python -m pytest\n",
     "        run: python -m pytest\n      - run: python -m src.cli build\n        env:\n"
     "          GH_TOKEN: ${{ github.token }}\n"),
    ("pages", "          path: dist\n", "          path: data\n"),
    ("uses", "      - uses: actions/upload-pages-artifact@v4\n",
     "      - uses: actions/cache@v4\n        with:\n          path: data\n          key: s\n"
     "      - uses: actions/upload-pages-artifact@v4\n"),
    ("accept", f"check-site --record {ACCEPT}",
     "check-site --record ${{ github.event_name == 'schedule' && '--accept-changes' || '' }}"),
    ("accept", "        run: python -m pytest\n",
     "        run: python -m pytest\n        env:\n          SCHWINGEN_PUBLISH_MIN_AGE: \"0\"\n"),
])
def test_dangerous_edits_are_caught(name: str, check: str, old: str, new: str) -> None:
    text = _text(name)
    assert text.count(old) >= 1
    edited = text.replace(old, new, 1)
    assert edited != text
    with pytest.raises(AssertionError):
        {"token": check_token_scope, "pages": check_pages_artifact,
         "accept": check_accept_changes,
         "uses": lambda t: check_uses(name, t)}[check](edited)


def test_ci_is_read_only_and_requires_the_browser() -> None:
    text = _text("ci.yml")
    assert "permissions:\n  contents: read\n" in text and "write" not in text
    assert 'SCHWINGEN_REQUIRE_BROWSER: "1"' in text and "persist-credentials: false" in text
    assert "PUBLISH_ENABLED" not in text and "GH_TOKEN" not in text and "pages" not in text
    commands = _commands(text)
    assert commands == ["python -m pip install ${{ matrix.pip-args }}", "python -m pytest",
                        "python -m src.cli all --sample", "python -m src.cli check-site --sample"]
    # both ways of installing are exercised: the ranges on the oldest Python, the hashed
    # lock of the publishing workflows on the deployment's Python
    assert ('- python-version: "3.11"\n            pip-args: -r requirements.txt\n') in text
    assert ('- python-version: "3.14"\n            pip-args: --require-hashes -r '
            'requirements-lock.txt\n') in text
    check_uses("ci.yml", text)


@pytest.mark.parametrize("name", GATED + ["ci.yml"])
def test_actions_are_pinned(name: str) -> None:
    uses = re.findall(r"uses: (\S+)", _text(name))
    assert uses
    for ref in uses:
        assert ref == STATE_ACTION or \
            re.fullmatch(r"actions/[a-z-]+@(v\d+|[0-9a-f]{40})", ref), ref
    assert "ubuntu-latest" not in _text(name)


def test_partial_failure_keeps_the_crawl() -> None:
    """After a failed crawl or rebuild the snapshot taken right after the crawl is still
    uploaded, so the next run does not request the same pages again."""
    text = _text("scrape_and_update.yml")
    assert ("if: always() && steps.state.outcome == 'success' && steps.crawl.outcome != "
            "'skipped'") in text
    assert "if: always() && steps.snapshot.outcome == 'success'" in text
    update = _jobs(text)["update"]
    assert update.index("id: snapshot") < update.index("all --skip-crawl") < \
        update.index("check-site") < update.index("Export the final state") < \
        update.index("mode: upload") < update.index("upload-pages-artifact")
    # ... and the upload step (the action) carries that condition
    assert ("        if: always() && steps.snapshot.outcome == 'success'\n"
            f"        uses: {STATE_ACTION}\n        with:\n          mode: upload\n") in update
    # the Pages artifact is only made by a fully successful run (no `if:` on that step)
    assert "if:" not in update.split("upload-pages-artifact")[1]


# --------------------------------------------------------------------------- lock file
def test_lock_file_pins_and_hashes_every_requirement() -> None:
    """`requirements-lock.txt` (scripts/make_lock.py): one exact version and one sha256 per
    distribution, and every range of requirements.txt is met by its pin."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    text = (REPO_ROOT / "requirements-lock.txt").read_text(encoding="utf-8")
    body = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    pins = dict(re.fullmatch(r"([a-z0-9-]+)==(\S+) \\", l).groups() for l in body[0::2])
    assert len(pins) == len(body) // 2 >= 20
    for line in body[1::2]:
        assert re.fullmatch(r"    --hash=sha256:[0-9a-f]{64}  # \S.*", line), line
    wanted = [Requirement(l.split("#")[0].strip())
              for l in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
              if l.split("#")[0].strip()]
    assert len(wanted) >= 10
    for req in wanted:
        name = canonicalize_name(req.name)
        assert name in pins, f"{name} is not in the lock - run scripts/make_lock.py"
        assert req.specifier.contains(pins[name], prereleases=True), (name, pins[name])
