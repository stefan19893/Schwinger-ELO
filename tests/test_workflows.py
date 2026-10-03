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
def test_only_cli_calls_and_state_transport(name: str) -> None:
    allowed = re.compile(
        r"^(python -m src\.cli [a-z-]+( .*)?|python -m pytest|"
        r"python -m pip install -r requirements\.txt|"
        r"gh release (view|download|upload|delete-asset) \"\$STATE_TAG\" .*|"
        r"test \"\$\(gh release view \"\$STATE_TAG\" .*--json isDraft --jq \.isDraft\)\" = \"true\"|"
        r"for old in \"\$RUNNER_TEMP\"/state-in/schwingen-state-\*\.tar\.gz; do|done)$")
    commands = _commands(_text(name))
    assert len(commands) >= 4
    for cmd in commands:
        assert allowed.match(cmd), f"{name}: unexpected shell: {cmd}"
    parser = cli.build_parser()
    for cmd in commands:
        if not cmd.startswith("python -m src.cli "):
            continue
        assert "--allow-empty" not in cmd and "--refresh" not in cmd and "--force" not in cmd
        argv = [a for a in cmd.split()[3:] if not a.startswith(("\"$", "${{", "&&", "||", "''",
                                                                "'--", "inputs.", "}}"))]
        args = parser.parse_args(argv + (["x"] if argv[0].startswith("state-") else []))
        assert args.command == argv[0]             # the CLI really has these options


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
    # the draft check comes first, and no generation is deleted before the new one is up
    shell = _commands(text)
    order = [next(i for i, c in enumerate(shell) if key in c)
             for key in ("--json isDraft", "gh release download", "gh release upload",
                         "gh release delete-asset")]
    assert order == sorted(order) and "--clobber" not in text


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
    # the token reaches the gh steps only, never a step that runs project code
    for step in re.split(r"\n      - ", build)[1:]:
        if "GH_TOKEN" in step:
            assert "python" not in step and "gh release" in step
    assert "secrets." not in text and "github.token" in text
    assert "concurrency:\n  group: pipeline-state\n  cancel-in-progress: false\n" in text
    assert "timeout-minutes:" in build and "timeout-minutes:" in deploy
    assert 'python-version: "3.14"' in build and "cache: pip" in build


def test_ci_is_read_only_and_requires_the_browser() -> None:
    text = _text("ci.yml")
    assert "permissions:\n  contents: read\n" in text and "write" not in text
    assert 'SCHWINGEN_REQUIRE_BROWSER: "1"' in text and "persist-credentials: false" in text
    assert "PUBLISH_ENABLED" not in text and "GH_TOKEN" not in text and "pages" not in text
    commands = _commands(text)
    assert commands == ["python -m pip install -r requirements.txt", "python -m pytest",
                        "python -m src.cli all --sample", "python -m src.cli check-site --sample"]


@pytest.mark.parametrize("name", GATED + ["ci.yml"])
def test_actions_are_pinned(name: str) -> None:
    uses = re.findall(r"uses: (\S+)", _text(name))
    assert uses
    for ref in uses:
        assert re.fullmatch(r"actions/[a-z-]+@(v\d+|[0-9a-f]{40})", ref), ref
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
        update.index("gh release upload") < update.index("upload-pages-artifact")
    # the Pages artifact is only made by a fully successful run (no `if:` on that step)
    assert "if:" not in update.split("upload-pages-artifact")[1]
