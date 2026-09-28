# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""Fixtures for GoAlert charm unit tests."""

import copy
import pathlib
import typing

import pytest
import yaml
from ops import testing

from charm import GoAlertCharm

CHARM_DIR = pathlib.Path(__file__).parents[2]

ROCK_LAYER = {
    "services": {
        "go": {
            "override": "replace",
            "startup": "enabled",
            "command": "goalert",
            "user": "_daemon_",
            "working-dir": "/app",
        },
    }
}


def expanded_charmcraft() -> dict:
    """Return charmcraft.yaml with the go-framework extension (paas-charm 1.x) applied.

    Mirrors what `charmcraft expand-extensions` produces, so unit tests can run
    without charmcraft installed.
    """
    charmcraft = yaml.safe_load((CHARM_DIR / "charmcraft.yaml").read_text())
    charmcraft.pop("extensions", None)
    charmcraft["assumes"] = ["k8s-api"]
    charmcraft["containers"] = {"app": {"resource": "app-image"}}
    charmcraft["resources"] = {"app-image": {"type": "oci-image"}}
    charmcraft["peers"] = {"secret-storage": {"interface": "secret-storage"}}
    charmcraft.setdefault("requires", {}).update(
        {
            "logging": {"interface": "loki_push_api"},
            "ingress": {"interface": "ingress", "limit": 1},
        }
    )
    charmcraft.setdefault("provides", {}).update(
        {
            "metrics-endpoint": {"interface": "prometheus_scrape"},
            "grafana-dashboard": {"interface": "grafana_dashboard"},
        }
    )
    charmcraft.setdefault("actions", {})["rotate-secret-key"] = {
        "description": "Rotate the secret key."
    }
    charmcraft.setdefault("config", {}).setdefault("options", {}).update(
        {
            "app-port": {"type": "int", "default": 8080},
            "metrics-port": {"type": "int", "default": 8080},
            "metrics-path": {"type": "string", "default": "/metrics"},
            "app-secret-key": {"type": "string"},
            "app-secret-key-id": {"type": "secret"},
        }
    )
    return charmcraft


@pytest.fixture(name="ctx")
def ctx_fixture(tmp_path: pathlib.Path) -> typing.Iterator[testing.Context]:
    """Scenario context rooted at an expanded copy of the charm metadata."""
    charmcraft = expanded_charmcraft()
    (tmp_path / "charmcraft.yaml").write_text(yaml.safe_dump(charmcraft))
    meta = copy.deepcopy(charmcraft)
    actions = meta.pop("actions")
    config = meta.pop("config")
    yield testing.Context(
        GoAlertCharm,
        meta=meta,
        actions=actions,
        config=config,
        charm_root=tmp_path,
        juju_version="3.6.14",
    )


@pytest.fixture(name="encryption_secret")
def encryption_secret_fixture() -> testing.Secret:
    """User secret holding the GoAlert data encryption key."""
    return testing.Secret(tracked_content={"value": "super-secret-key"})


@pytest.fixture(name="postgresql_relation")
def postgresql_relation_fixture() -> testing.Relation:
    """PostgreSQL relation with credentials published by the provider."""
    return testing.Relation(
        endpoint="postgresql",
        interface="postgresql_client",
        remote_app_data={
            "database": "goalert-k8s",
            "endpoints": "postgresql-k8s-primary:5432",
            "username": "relation-user",
            "password": "relation-password",
        },
    )


@pytest.fixture(name="container")
def container_fixture() -> testing.Container:
    """Workload container with the rock's default pebble layer."""
    return testing.Container(
        name="app",
        can_connect=True,
        _base_plan=copy.deepcopy(ROCK_LAYER),
    )


@pytest.fixture(name="peer_relation")
def peer_relation_fixture() -> testing.PeerRelation:
    """Return an initialized paas-charm secret storage peer relation."""
    return testing.PeerRelation("secret-storage", local_app_data={"go_secret_key": "paas-secret"})


@pytest.fixture(name="state")
def state_fixture(
    encryption_secret: testing.Secret,
    postgresql_relation: testing.Relation,
    container: testing.Container,
    peer_relation: testing.PeerRelation,
) -> testing.State:
    """Return the state of a fully integrated leader unit."""
    return testing.State(
        leader=True,
        config={"data-encryption-key": encryption_secret.id},
        secrets={encryption_secret},
        relations={postgresql_relation, peer_relation},
        containers={container},
        model=testing.Model(name="test-model"),
    )
