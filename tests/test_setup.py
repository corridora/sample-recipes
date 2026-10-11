import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sample_setup", ROOT / "scripts/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_help_and_deploy_dryrun_need_no_dependencies_credentials_or_cloud_login(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "setup"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "scenario" in result.stdout
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"SAMPLE_MODE": "cloud", "GCP_PROJECT": "fake-project"}))
    result = subprocess.run([sys.executable, str(ROOT / "setup"), "deploy", "--config", str(config), "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "no cloud login" in result.stdout and "fake-project" in result.stdout


def test_secure_config_masks_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "STATE", tmp_path / "state")
    monkeypatch.setenv("DEMO_CONTROL_TOKEN", "fake-control")
    monkeypatch.setenv("CORRIDORA_INGEST_KEY", "fake-ingest")
    from argparse import Namespace
    config = setup.configure(Namespace(config=None, non_interactive=True))
    assert "fake-ingest" not in (setup.STATE / "config.json").read_text()
    assert json.loads((setup.STATE / "secrets.json").read_text())["CORRIDORA_INGEST_KEY"] == "fake-ingest"
    assert (setup.STATE / "secrets.json").stat().st_mode & 0o777 == 0o600
    assert setup.STATE.stat().st_mode & 0o777 == 0o700


def test_cloud_requires_keys_and_demo_operator_keys():
    config = dict(setup.DEFAULTS)
    config.update(SAMPLE_MODE="cloud", DEMO_CONTROL_TOKEN="control")
    with pytest.raises(ValueError, match="HTTPS"):
        setup.validate_config(config)
    config["PUBLIC_URL"] = "https://example.test"
    with pytest.raises(ValueError, match="Missing required"):
        setup.validate_config(config)


def test_reuse_resources_is_never_inferred_from_names(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import gcp
    monkeypatch.setattr(gcp, "STATE", tmp_path)
    config = dict(setup.DEFAULTS, GCP_PROJECT="fake")
    cloud = gcp.GCP(config)
    monkeypatch.setattr(cloud, "present", lambda command: True)
    with pytest.raises(ValueError, match="no ownership inventory"):
        cloud.ensure("topic", "already-exists", [], [], [])
    cloud.ensure("bucket", "selected-shared", [], [], [], reuse=True)
    assert cloud.inventory["resources"][0]["owned"] is False


def test_changed_cloud_owner_refuses_deletion(tmp_path, monkeypatch):
    import gcp
    monkeypatch.setattr(gcp, "STATE", tmp_path)
    cloud = gcp.GCP(dict(setup.DEFAULTS, GCP_PROJECT="fake"))
    monkeypatch.setattr(cloud, "command", lambda *a, **k: '{"labels":{"sample-owner":"someone-else"}}')
    with pytest.raises(ValueError, match="Ownership marker changed"):
        cloud.verify_owner({"name": "topic", "kind": "topic"})
