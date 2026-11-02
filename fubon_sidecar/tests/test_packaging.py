"""Static checks for the deployment contract: image platform, SDK pins, compose wiring."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

SIDECAR = Path(__file__).resolve().parents[1]
REPO = SIDECAR.parent


def test_fugle_marketdata_is_pinned_to_2_4_1():
    pyproject = tomllib.loads((SIDECAR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["optional-dependencies"]["sdk"] == ["fugle-marketdata==2.4.1"]
    assert all("fubon" not in dep for dep in pyproject["project"]["dependencies"])


def test_dockerfile_is_amd64_and_installs_vendored_wheel_with_pin():
    dockerfile = (SIDECAR / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM --platform=linux/amd64 ", dockerfile, re.MULTILINE)
    assert "COPY vendor/ /tmp/vendor/" in dockerfile
    assert 'pip install --no-cache-dir "$wheel" ".[sdk]"' in dockerfile
    assert "USER sidecar" in dockerfile


def test_wheel_and_certificates_are_ignored_by_git_and_docker():
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "fubon_sidecar/vendor/*" in gitignore
    assert "fubon_sidecar/certs/*" in gitignore
    dockerignore = (SIDECAR / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "certs/" in dockerignore


def test_compose_service_is_internal_profile_gated_and_healthchecked():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["fubon-marketdata"]

    assert service["profiles"] == ["taiwan"]
    assert service["platform"] == "linux/amd64"
    assert "ports" not in service
    assert service["networks"] == ["quantdinger-network"]
    assert service["healthcheck"]["test"] == ["CMD", "python", "-m", "fubon_sidecar.healthcheck"]
    assert any(volume.endswith(":/run/fubon:ro") for volume in service["volumes"])
    for key in ("FUBON_ID", "FUBON_PASSWORD", "FUBON_API_KEY", "FUBON_CERT_PATH",
                "FUBON_CERT_PASSWORD", "FUBON_SIDECAR_TOKEN"):
        assert key in service["environment"]
    # Credentials come from the environment only; the project .env is never mounted.
    assert not any(".env" in volume for volume in service["volumes"])


def test_env_example_has_empty_placeholders_for_secrets():
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if line.startswith("FUBON_"))
    for key in ("FUBON_ID", "FUBON_PASSWORD", "FUBON_API_KEY", "FUBON_CERT_PASSWORD", "FUBON_SIDECAR_TOKEN"):
        assert values[key] == ""
    assert values["FUBON_CERT_PATH"].startswith("/run/fubon/")
