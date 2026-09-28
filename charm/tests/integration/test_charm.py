# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""Integration tests for the GoAlert charm."""

import json

import jubilant
import pytest
import requests
import yaml

TRAEFIK_APP = "traefik-k8s"


def _unit_address(juju: jubilant.Juju, unit: str) -> str:
    app = unit.split("/")[0]
    return juju.status().apps[app].units[unit].address


def _goalert_env(juju: jubilant.Juju, unit: str) -> dict[str, str]:
    plan = yaml.safe_load(juju.ssh(unit, "/charm/bin/pebble", "plan", container="app"))
    return plan["services"]["go"]["environment"]


def _leader(juju: jubilant.Juju, app: str) -> str:
    units = juju.status().apps[app].units
    return next(name for name, unit in units.items() if unit.leader)


def test_health(juju: jubilant.Juju, goalert: str):
    """
    arrange: GoAlert deployed and integrated with PostgreSQL.
    act: query the health endpoint of the unit.
    assert: GoAlert reports healthy, meaning it connected to the database and migrated it.
    """
    address = _unit_address(juju, f"{goalert}/0")

    response = requests.get(f"http://{address}:8080/health", timeout=10)

    assert response.status_code == 200


def test_metrics(juju: jubilant.Juju, goalert: str):
    """
    arrange: GoAlert deployed.
    act: scrape the Prometheus endpoint.
    assert: GoAlert metrics are exposed on the dedicated metrics port.
    """
    address = _unit_address(juju, f"{goalert}/0")

    response = requests.get(f"http://{address}:2112/metrics", timeout=10)

    assert response.status_code == 200
    assert "goalert_" in response.text


def test_create_admin_user_and_login(juju: jubilant.Juju, goalert: str):
    """
    arrange: GoAlert deployed.
    act: run create-admin-user and log in with the returned credentials.
    assert: the login succeeds and returns a session token.
    """
    unit = f"{goalert}/0"
    task = juju.run(unit, "create-admin-user", {"username": "admin", "email": "admin@example.com"})
    password = task.results["password"]
    address = _unit_address(juju, unit)
    public_url = _goalert_env(juju, unit)["GOALERT_PUBLIC_URL"]

    response = requests.post(
        f"http://{address}:8080/api/v2/identity/providers/basic",
        data={"username": "admin", "password": password, "noRedirect": "1"},
        headers={"Referer": public_url},
        timeout=10,
    )

    assert response.status_code == 200, response.text
    assert response.text


def test_create_admin_user_duplicate_fails(juju: jubilant.Juju, goalert: str):
    """
    arrange: an admin user named "admin" exists.
    act: run create-admin-user again with the same username.
    assert: the action fails.
    """
    with pytest.raises(jubilant.TaskError):
        juju.run(
            f"{goalert}/0",
            "create-admin-user",
            {"username": "admin", "email": "admin@example.com", "password": "another-password"},
        )


def test_scale_api_only(juju: jubilant.Juju, goalert: str):
    """
    arrange: a single GoAlert unit.
    act: add a unit.
    assert: only the leader runs the engine, the other unit runs API-only and is healthy.
    """
    juju.add_unit(goalert)
    juju.wait(
        lambda s: (
            jubilant.all_active(s, goalert)
            and len(s.apps[goalert].units) == 2
            and jubilant.all_agents_idle(s, goalert)
        ),
        error=lambda s: jubilant.any_error(s, goalert),
    )

    leader = _leader(juju, goalert)
    for unit in juju.status().apps[goalert].units:
        expected = "false" if unit == leader else "true"
        assert _goalert_env(juju, unit)["GOALERT_API_ONLY"] == expected
        response = requests.get(f"http://{_unit_address(juju, unit)}:8080/health", timeout=10)
        assert response.status_code == 200


def test_ingress(juju: jubilant.Juju, goalert: str):
    """
    arrange: GoAlert deployed.
    act: integrate with traefik-k8s.
    assert: GoAlert is reachable through the ingress path prefix and uses it as public URL.
    """
    juju.deploy(TRAEFIK_APP, channel="latest/stable", trust=True)
    juju.integrate(goalert, f"{TRAEFIK_APP}:ingress")
    juju.wait(
        lambda s: (
            jubilant.all_active(s, goalert, TRAEFIK_APP)
            and jubilant.all_agents_idle(s, goalert, TRAEFIK_APP)
        ),
        error=lambda s: jubilant.any_error(s, goalert, TRAEFIK_APP),
    )

    task = juju.run(f"{TRAEFIK_APP}/0", "show-proxied-endpoints")
    endpoints = json.loads(task.results["proxied-endpoints"])
    ingress_url = endpoints[goalert]["url"]
    prefix = ingress_url.split("/", 3)[3] if ingress_url.count("/") >= 3 else ""
    assert _goalert_env(juju, f"{goalert}/0")["GOALERT_PUBLIC_URL"] == ingress_url

    traefik_address = _unit_address(juju, f"{TRAEFIK_APP}/0")
    response = requests.get(f"http://{traefik_address}/{prefix}/health", timeout=10)

    assert response.status_code == 200
