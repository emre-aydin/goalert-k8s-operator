# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""Fixtures for GoAlert charm integration tests."""

import os
import pathlib
import platform
import typing

import jubilant
import pytest

APP_NAME = "goalert-k8s"
POSTGRESQL_APP = "postgresql-k8s"
HOST_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine(), "amd64"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add integration test options."""
    parser.addoption("--charm-path", default=os.environ.get("CHARM_PATH"))
    parser.addoption("--rock-image", default=os.environ.get("ROCK_IMAGE"))
    parser.addoption("--model", default=os.environ.get("JUJU_MODEL"))
    parser.addoption(
        "--keep-models", action="store_true", default=bool(os.environ.get("KEEP_MODELS"))
    )


@pytest.fixture(scope="session", name="charm_path")
def charm_path_fixture(pytestconfig: pytest.Config) -> pathlib.Path:
    """Path to the packed charm."""
    charm = pytestconfig.getoption("--charm-path")
    if not charm:
        charms = sorted(pathlib.Path(__file__).parents[2].glob("*.charm"))
        assert charms, "no charm found, run `charmcraft pack` or pass --charm-path"
        charm = charms[0]
    return pathlib.Path(charm).resolve()


@pytest.fixture(scope="session", name="rock_image")
def rock_image_fixture(pytestconfig: pytest.Config) -> str:
    """OCI image reference of the GoAlert rock."""
    image = pytestconfig.getoption("--rock-image")
    assert image, "pass --rock-image or set ROCK_IMAGE (e.g. localhost:32000/goalert:0.35.0)"
    return image


@pytest.fixture(scope="module", name="juju")
def juju_fixture(pytestconfig: pytest.Config) -> typing.Iterator[jubilant.Juju]:
    """Juju client bound to a (temporary) model."""
    model = pytestconfig.getoption("--model")
    if model:
        juju = jubilant.Juju(model=model)
        juju.wait_timeout = 20 * 60
        yield juju
        return
    with jubilant.temp_model(keep=pytestconfig.getoption("--keep-models")) as juju:
        juju.wait_timeout = 20 * 60
        # Juju schedules Kubernetes pods on amd64 nodes unless told otherwise.
        juju.model_constraints({"arch": HOST_ARCH})
        yield juju
        print(juju.debug_log(limit=1000))


@pytest.fixture(scope="module", name="goalert")
def goalert_fixture(juju: jubilant.Juju, charm_path: pathlib.Path, rock_image: str) -> str:
    """Deploy GoAlert integrated with PostgreSQL and return the application name."""
    status = juju.status()
    if APP_NAME in status.apps:
        return APP_NAME

    juju.deploy(POSTGRESQL_APP, channel="16/stable", trust=True)
    juju.deploy(charm_path, APP_NAME, resources={"app-image": rock_image})
    juju.wait(lambda s: jubilant.all_blocked(s, APP_NAME), timeout=10 * 60)

    secret = juju.add_secret("goalert-encryption-key", {"value": "integration-test-key"})
    juju.grant_secret(secret, APP_NAME)
    juju.config(APP_NAME, {"data-encryption-key": secret})
    juju.integrate(APP_NAME, POSTGRESQL_APP)
    juju.wait(
        lambda s: (
            jubilant.all_active(s, APP_NAME, POSTGRESQL_APP)
            and jubilant.all_agents_idle(s, APP_NAME, POSTGRESQL_APP)
        ),
        error=lambda s: jubilant.any_error(s, APP_NAME),
    )
    return APP_NAME
