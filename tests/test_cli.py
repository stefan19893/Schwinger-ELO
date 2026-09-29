"""CLI + config tests. Offline only; all output goes to tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import cli
from src.config import REPO_ROOT, SAMPLE_DATA_DIR, Config, load_config


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))


# ------------------------------------------------------------------ config
def test_defaults() -> None:
    cfg = load_config(env={})
    assert cfg.from_year == 2011
    assert cfg.request_delay_min == 0.5 and cfg.request_delay_max == 1.0
    assert cfg.k_factors["ESAF"] == 48 and cfg.k_factors["Kantonal"] == 24
    assert cfg.raw_dir == cfg.data_dir / "raw"
    assert cfg.db_path == cfg.data_dir / "schwingen.db"


def test_env_overrides_defaults() -> None:
    env = {"SCHWINGEN_FROM_YEAR": "2015", "SCHWINGEN_DATA_DIR": "/x/data",
           "SCHWINGEN_SAMPLE": "true", "SCHWINGEN_MOV_ALPHA": "0.25"}
    cfg = load_config(env=env)
    assert cfg.from_year == 2015
    assert cfg.data_dir == Path("/x/data")
    assert cfg.sample is True
    assert cfg.mov_alpha == 0.25


def test_flag_overrides_env() -> None:
    cfg = load_config({"from_year": 2020, "to_year": None}, env={"SCHWINGEN_FROM_YEAR": "2015"})
    assert cfg.from_year == 2020  # flag wins, None falls through
    assert cfg.to_year == Config().to_year


@pytest.mark.parametrize("env", [
    {"SCHWINGEN_FROM_YEAR": "abc"},
    {"SCHWINGEN_SAMPLE": "maybe"},
    {"SCHWINGEN_FROM_YEAR": "2030", "SCHWINGEN_TO_YEAR": "2020"},
    {"SCHWINGEN_REQUEST_DELAY_MIN": "2.0"},
])
def test_invalid_config_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_config(env=env)


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("argv", [
    ["--sample", "--data-dir", "/d", "all"],
    ["all", "--sample", "--data-dir", "/d"],
])
def test_global_options_before_or_after_subcommand(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.sample is True and args.data_dir == "/d" and args.command == "all"


def test_crawl_year_options() -> None:
    args = cli.build_parser().parse_args(["crawl", "--from-year", "2012", "--to-year", "2013"])
    cfg = cli.config_from_args(args)
    assert (cfg.from_year, cfg.to_year) == (2012, 2013)


def test_serve_port_option() -> None:
    args = cli.build_parser().parse_args(["serve", "--port", "8123"])
    assert cli.config_from_args(args).port == 8123


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["bogus"])


# ------------------------------------------------------------------ stages
@pytest.mark.parametrize("cmd", ["crawl", "parse", "clean", "elo"])
def test_stub_stages_succeed(cmd: str, tmp_path: Path) -> None:
    assert cli.main([cmd, "--sample", "--data-dir", str(tmp_path / "data")]) == 0


def test_all_sample_produces_dist(tmp_path: Path) -> None:
    rc = cli.main(["all", "--sample", "--data-dir", str(tmp_path / "data")])
    assert rc == 0
    index = tmp_path / "dist" / "index.html"
    assert index.is_file()
    html = index.read_text(encoding="utf-8")
    assert "Schwinger-ELO" in html
    assert 'href="/' not in html and 'src="/' not in html  # relative URLs only


def test_build_is_idempotent(tmp_path: Path) -> None:
    assert cli.main(["build"]) == 0
    assert cli.main(["build"]) == 0
    assert (tmp_path / "dist" / "index.html").is_file()


def test_build_refuses_unsafe_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(REPO_ROOT))
    assert cli.main(["build"]) == 1
    assert (REPO_ROOT / "src").is_dir()


def test_build_refuses_foreign_nonempty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(target))
    assert cli.main(["build"]) == 1
    assert (target / "keep.txt").is_file()


def test_build_replaces_previous_build_and_empty_dir(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()  # empty dir is fine
    assert cli.main(["build"]) == 0
    (dist / "stale.json").write_text("{}")
    assert cli.main(["build"]) == 0  # previous build (has marker) is replaced
    assert not (dist / "stale.json").exists()


def test_serve_without_dist_fails(tmp_path: Path) -> None:
    assert cli.main(["serve"]) == 1


@pytest.mark.parametrize("port", ["0", "99999", "-1"])
def test_serve_rejects_invalid_port(port: str) -> None:
    assert cli.main(["serve", "--port", port]) == 2


def test_serve_port_in_use_fails_cleanly(tmp_path: Path) -> None:
    import socket

    assert cli.main(["build"]) == 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert cli.main(["serve", "--port", str(port)]) == 1


# ------------------------------------------------------------------ sample isolation
def test_sample_uses_separate_data_dir() -> None:
    assert load_config({"sample": True}, env={}).data_dir == SAMPLE_DATA_DIR
    assert load_config(env={}).data_dir == REPO_ROOT / "data"
    # explicit data dir (flag or env) still wins
    assert load_config({"sample": True, "data_dir": "/d"}, env={}).data_dir == Path("/d")
    env = {"SCHWINGEN_SAMPLE": "1", "SCHWINGEN_DATA_DIR": "/e"}
    assert load_config(env=env).data_dir == Path("/e")


# ------------------------------------------------------------------ ignored options
@pytest.mark.parametrize("argv, warned", [
    (["parse", "--skip-crawl"], "--skip-crawl"),
    (["crawl", "--skip-crawl"], "--skip-crawl"),
    (["elo", "--refresh"], "--refresh"),
])
def test_warns_on_ignored_options(argv: list[str], warned: str, tmp_path: Path,
                                  caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main([*argv, "--data-dir", str(tmp_path)]) == 0
    assert any(warned in r.getMessage() and "no effect" in r.getMessage() for r in caplog.records)


def test_no_warning_for_scoped_options(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["all", "--sample", "--skip-crawl", "--refresh", "--data-dir", str(tmp_path)]) == 0
    assert not any("no effect" in r.getMessage() for r in caplog.records)


def test_user_agent_has_contact_url() -> None:
    assert "https://github.com/" in Config().user_agent
