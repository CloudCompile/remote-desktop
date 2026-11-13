"""Config schema and validation tests (#23)."""

import os

import pytest

from remote_desktop import config


# --------------------------------------------------------------- schema


def test_schema_rows_have_env_and_description():
    for spec in config.SCHEMA:
        assert spec.env, spec.name
        assert spec.description, f"{spec.env} needs a description"
        assert spec.kind, spec.env


def test_schema_covers_every_rd_env_used_in_code():
    # every os.environ.get("RD_*"/"VNC_*") call outside config.py would be a
    # regression: all settings must come from the schema
    import pathlib
    import re

    pkg = pathlib.Path(config.__file__).parent
    offenders = []
    pat = re.compile(r"""os\.environ(?:\.get)?[\[(]\s*["'](RD_|VNC_|REMOTE_CAM)""")
    for py in pkg.glob("*.py"):
        if py.name == "config.py":
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{py.name}:{i}")
    assert offenders == [], f"settings read outside config.py: {offenders}"


# --------------------------------------------------------------- parsing


def test_load_defaults():
    values, errs = config.load(env={})
    assert errs == []
    assert values["port"] == 8090
    assert values["max_connections"] == 64
    assert values["max_audio"] == 8
    assert values["max_media_in"] == 2
    assert values["geometry"] == "1280x800"
    assert values["dpi"] == 96


def test_load_parses_types_and_csv():
    values, errs = config.load(
        env={
            "RD_PORT": "9000",
            "RD_TRUSTED_PROXIES": "10.0.0.1, 10.0.0.2,",
            "RD_DPI": "144",
            "RD_MAX_AUDIO": "3",
        }
    )
    assert errs == []
    assert values["port"] == 9000
    assert values["trusted_proxies"] == {"10.0.0.1", "10.0.0.2"}
    assert values["dpi"] == 144
    assert values["max_audio"] == 3


def test_load_bad_value_falls_back_and_reports():
    values, errs = config.load(env={"RD_PORT": "not-a-port", "RD_DPI": "hundred"})
    assert values["port"] == 8090
    assert values["dpi"] == 96
    assert any("RD_PORT" in e for e in errs)
    assert any("RD_DPI" in e for e in errs)


def test_load_empty_env_var_uses_default():
    values, errs = config.load(env={"RD_PORT": ""})
    assert errs == []
    assert values["port"] == 8090


def test_config_file_with_env_override(tmp_path):
    cfg = tmp_path / "rd.conf"
    cfg.write_text("# comment\nRD_PORT=7777\nRD_GEOMETRY=1024x768\n\n")
    values, errs = config.load(env={"RD_CONFIG": str(cfg), "RD_PORT": "8888"})
    assert errs == []
    assert values["port"] == 8888  # real environment wins
    assert values["geometry"] == "1024x768"  # file fills the rest


def test_config_file_missing_is_an_error():
    values, errs = config.load(env={"RD_CONFIG": "/no/such/file.conf"})
    assert any("RD_CONFIG" in e for e in errs)


# -------------------------------------------------------------- validate


def test_validate_ok_on_defaults():
    values, _ = config.load(env={})
    assert config.validate(values) == []


@pytest.mark.parametrize(
    "env,fragment",
    [
        ({"RD_PORT": "70000"}, "RD_PORT"),
        ({"VNC_PORT": "0"}, "VNC_PORT"),
        ({"RD_GEOMETRY": "banana"}, "RD_GEOMETRY"),
        ({"RD_GEOMETRY": "100x100"}, "width"),
        ({"RD_DPI": "20"}, "RD_DPI"),
        ({"RD_LOG_LEVEL": "loud"}, "RD_LOG_LEVEL"),
        ({"RD_LOG_FORMAT": "xml"}, "RD_LOG_FORMAT"),
        ({"RD_MAX_AUDIO": "0"}, "RD_MAX_AUDIO"),
        ({"RD_HTTP_TIMEOUT": "0"}, "RD_HTTP_TIMEOUT"),
    ],
)
def test_validate_rejects_bad_values(env, fragment):
    values, _ = config.load(env=env)
    errs = config.validate(values)
    assert any(fragment in e for e in errs), errs


def test_validate_rejects_files_dir_that_is_a_file(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    values, _ = config.load(env={"RD_FILES_DIR": str(f)})
    assert any("RD_FILES_DIR" in e for e in config.validate(values))


def test_check_raises_config_error():
    values, _ = config.load(env={"RD_PORT": "99999"})
    with pytest.raises(config.ConfigError) as ei:
        config.check(values)
    assert "RD_PORT" in str(ei.value)


def test_geometry_wh():
    assert config.geometry_wh("1280x800") == (1280, 800)
    assert config.geometry_wh("bogus") == (None, None)


# ---------------------------------------------------------------- docs


def test_generated_env_doc(tmp_path, monkeypatch):
    # keep the repo copy pristine while testing the generator
    out = tmp_path / "CONFIGURATION.md"
    monkeypatch.setattr(config, "SCHEMA", config.SCHEMA)  # sanity: no mutation
    body_path = config.write_env_doc(str(out))
    text = open(body_path).read()
    assert "`RD_PORT`" in text
    assert "`8090`" in text
    assert "RD_PASSFILE" in text and "(redacted)" in text  # secrets never in docs
    assert os.path.expanduser("~") + "/Downloads" not in text  # portable ~/ paths
