"""Profiles: one checkout, one directory per product.

harvey.paths resolves the profile once, at import, so each case runs in a
fresh interpreter with HARVEY_PROFILE / HARVEY_PROFILES_DIR set, the same
way the CLI sets them before importing anything else.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harvey.paths import PROJECT_ROOT


def run_py(code: str, profiles_dir: Path, profile: str = "", cwd: Path | None = None):
    env = {**os.environ, "HARVEY_PROFILES_DIR": str(profiles_dir), "PYTHONUTF8": "1"}
    env.pop("HARVEY_PROFILE", None)
    if profile:
        env["HARVEY_PROFILE"] = profile
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(cwd or PROJECT_ROOT), timeout=60,
    )


def run_cli(args: list[str], profiles_dir: Path):
    env = {**os.environ, "HARVEY_PROFILES_DIR": str(profiles_dir), "PYTHONUTF8": "1"}
    env.pop("HARVEY_PROFILE", None)
    return subprocess.run(
        [sys.executable, "-c", "from harvey.cli import main; main()", *args],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(PROJECT_ROOT), timeout=60,
    )


@pytest.fixture
def profiles(tmp_path):
    root = tmp_path / "profiles"
    cargo = root / "cargo"
    (cargo / "skills").mkdir(parents=True)
    import yaml

    cfg = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    cfg["persona"]["company"] = "Rowan"
    cfg["product"]["name"] = "Rowan Cargo"
    cfg["product"]["description"] = "Software para transitarias y agentes de carga"
    (cargo / "harvey.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (cargo / "skills" / "product_knowledge.md").write_text(
        "# Cargo · conocimiento\nExpedientes, contenedores y aduanas.", encoding="utf-8"
    )
    shared = root / "_shared" / "skills"
    shared.mkdir(parents=True)
    (shared / "house_rules.md").write_text("Escribe en castellano.", encoding="utf-8")
    (shared / "email_frameworks.md").write_text("Marcos en castellano.", encoding="utf-8")
    (root / "erp").mkdir()
    (root / "erp" / "harvey.yaml").write_text("persona: {}\n", encoding="utf-8")
    (root / "empty").mkdir()  # no harvey.yaml: not listed
    return root


def test_no_profile_keeps_the_classic_layout(profiles):
    out = run_py("""
        import json
        from harvey import paths
        print(json.dumps([paths.PROFILE, str(paths.PROFILE_DIR), str(paths.DATA_DIR)]))
    """, profiles)
    assert out.returncode == 0, out.stderr
    profile, profile_dir, data_dir = json.loads(out.stdout)
    assert profile == ""
    assert Path(profile_dir) == PROJECT_ROOT
    assert Path(data_dir) == PROJECT_ROOT / "data"


def test_profile_moves_config_data_and_db(profiles):
    out = run_py("""
        import json
        from harvey import paths, state, config, dashboard
        from harvey.integrations import gmail
        print(json.dumps({
            "data": str(paths.DATA_DIR),
            "db": str(state.DB_PATH),
            "config": config._find_config_file(),
            "dash_db": str(dashboard.DB_PATH),
            "dash_token": str(dashboard.TOKEN_FILE),
            "gmail": str(gmail.TOKEN_FILE),
            "product": config.load_config().product.name,
        }))
    """, profiles, profile="cargo")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    cargo = profiles / "cargo"
    assert Path(got["data"]) == cargo / "data"
    assert Path(got["db"]) == cargo / "data" / "harvey.db"
    assert Path(got["config"]) == cargo / "harvey.yaml"
    assert Path(got["dash_db"]) == cargo / "data" / "harvey.db"
    assert Path(got["dash_token"]) == cargo / "data" / "dashboard-token"
    assert Path(got["gmail"]) == cargo / "data" / "gmail_token.json"
    assert got["product"] == "Rowan Cargo"


def test_profile_never_falls_back_to_another_config(profiles, tmp_path):
    # Even with a harvey.yaml in the working directory, a profile without
    # its own config must fail rather than prospect for the wrong product.
    (profiles / "sga").mkdir()
    (tmp_path / "harvey.yaml").write_text("persona: {}\n", encoding="utf-8")
    out = run_py("""
        from harvey import config
        try:
            config._find_config_file()
        except config.ConfigFileNotFoundError as e:
            print("missing:", e)
    """, profiles, profile="sga", cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("missing:")
    assert "sga" in out.stdout


def test_profile_env_overrides_shared_env(profiles):
    (profiles / "cargo" / ".env").write_text("SMTP_USERNAME=cargo@rowan.test\n", encoding="utf-8")
    out = run_py("""
        from harvey.config import load_env
        print(load_env().smtp_username)
    """, profiles, profile="cargo")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "cargo@rowan.test"


def test_knowledge_lookup_profile_then_shared_then_repo(profiles):
    out = run_py("""
        import json
        from harvey.brain import Brain
        b = Brain.__new__(Brain)
        writer = b.load_skills_for_agent("writer")
        print(json.dumps({
            "product": b.load_skill("product_knowledge"),
            "frameworks": b.load_skill("email_frameworks"),
            "methodology_from_repo": bool(b.load_skill("sales_methodology")),
            "rules_first": writer.find("Escribe en castellano.") < writer.find("Marcos en castellano."),
            "rules_in_writer": "Escribe en castellano." in writer,
            "prompt": b.load_prompt("writer")[:20],
        }))
    """, profiles, profile="cargo")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["product"].startswith("# Cargo · conocimiento")  # profile, UTF-8 intact
    assert got["frameworks"] == "Marcos en castellano."  # shared override
    assert got["methodology_from_repo"] is True  # shipped skill still used
    assert got["rules_in_writer"] and got["rules_first"]
    assert got["prompt"]  # shipped prompt when no override exists


def test_house_rules_absent_adds_nothing(profiles):
    out = run_py("""
        from harvey.brain import Brain
        b = Brain.__new__(Brain)
        print("house_rules" in b.load_skills_for_agent("scout"))
    """, profiles)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("bad", ["../etc", "Cargo", "a/b", "c:\\x", "-x", ""])
def test_invalid_profile_names_are_refused(bad):
    from harvey.paths import ProfileError, validate_profile_name

    with pytest.raises(ProfileError):
        validate_profile_name(bad)


def test_cli_lists_profiles(profiles):
    out = run_cli(["profiles"], profiles)
    assert out.returncode == 0, out.stderr
    assert "cargo" in out.stdout and "erp" in out.stdout
    assert "empty" not in out.stdout and "_shared" not in out.stdout


def test_cli_marks_active_profile(profiles):
    out = run_cli(["--profile", "erp", "profiles"], profiles)
    assert out.returncode == 0, out.stderr
    assert "* erp" in out.stdout


def test_cli_unknown_profile_exits_2(profiles):
    out = run_cli(["--profile", "nope", "status"], profiles)
    assert out.returncode == 2
    assert "not found" in out.stdout


def test_cli_invalid_profile_exits_2(profiles):
    out = run_cli(["--perfil", "../x", "status"], profiles)
    assert out.returncode == 2
    assert "Invalid profile name" in out.stdout
