# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""Unit tests for the GoAlert charm."""

import dataclasses

import ops
import pytest
from ops import testing


def _service_env(state: testing.State) -> dict[str, str]:
    container = state.get_container("app")
    return dict(container.plan.services["go"].environment)


def test_leader_runs_engine(ctx: testing.Context, state: testing.State):
    """
    arrange: a leader unit with postgresql and the encryption key configured.
    act: run config-changed.
    assert: GoAlert is configured from the charm state and runs the engine.
    """
    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == testing.ActiveStatus()
    env = _service_env(out)
    assert env["GOALERT_DB_URL"] == (
        "postgresql://relation-user:relation-password@postgresql-k8s-primary:5432/goalert-k8s"
    )
    assert env["GOALERT_DATA_ENCRYPTION_KEY"] == "super-secret-key"
    assert env["GOALERT_PUBLIC_URL"] == "http://goalert-k8s.test-model:8080"
    assert env["GOALERT_LISTEN"] == "0.0.0.0:8080"
    assert env["GOALERT_LISTEN_PROMETHEUS"] == "0.0.0.0:2112"
    assert env["GOALERT_API_ONLY"] == "false"
    assert env["GOALERT_JSON"] == "true"
    assert "APP_DATA_ENCRYPTION_KEY_VALUE" not in env
    assert out.opened_ports == {testing.TCPPort(8080)}


def test_non_leader_runs_api_only(ctx: testing.Context, state: testing.State):
    """
    arrange: a non-leader unit.
    act: run config-changed.
    assert: GoAlert runs in API-only mode.
    """
    out = ctx.run(ctx.on.config_changed(), dataclasses.replace(state, leader=False))

    assert _service_env(out)["GOALERT_API_ONLY"] == "true"


def test_health_check(ctx: testing.Context, state: testing.State):
    """
    arrange: a fully integrated unit.
    act: run config-changed.
    assert: a pebble health check against GoAlert's /health is added.
    """
    out = ctx.run(ctx.on.config_changed(), state)

    check = out.get_container("app").plan.checks["goalert-ready"]
    assert check.http == {"url": "http://localhost:8080/health"}


def test_custom_ports(ctx: testing.Context, state: testing.State):
    """
    arrange: app-port and metrics-port set to distinct values.
    act: run config-changed.
    assert: GoAlert listens on the configured ports.
    """
    state = dataclasses.replace(
        state, config={**state.config, "app-port": 8081, "metrics-port": 9102}
    )

    out = ctx.run(ctx.on.config_changed(), state)

    env = _service_env(out)
    assert env["GOALERT_LISTEN"] == "0.0.0.0:8081"
    assert env["GOALERT_LISTEN_PROMETHEUS"] == "0.0.0.0:9102"


def test_ingress_keeps_prefix(ctx: testing.Context, state: testing.State):
    """
    arrange: an ingress relation providing a URL with a path prefix.
    act: run ingress relation-changed.
    assert: the public URL and health check include the prefix and it isn't stripped.
    """
    ingress = testing.Relation(
        endpoint="ingress",
        interface="ingress",
        remote_app_data={"ingress": '{"url": "http://ingress.example/test-model-goalert-k8s"}'},
    )
    state = dataclasses.replace(state, relations={*state.relations, ingress})

    out = ctx.run(ctx.on.relation_changed(ingress), state)

    assert (
        _service_env(out)["GOALERT_PUBLIC_URL"] == "http://ingress.example/test-model-goalert-k8s"
    )
    assert out.get_container("app").plan.checks["goalert-ready"].http == {
        "url": "http://localhost:8080/test-model-goalert-k8s/health"
    }
    app_data = out.get_relation(ingress.id).local_app_data
    assert app_data["name"] == '"goalert-k8s"'
    assert app_data.get("strip-prefix", "false") == "false"


def test_missing_encryption_key_blocks(ctx: testing.Context, state: testing.State):
    """
    arrange: the data-encryption-key option is unset.
    act: run config-changed.
    assert: the unit is blocked.
    """
    out = ctx.run(ctx.on.config_changed(), dataclasses.replace(state, config={}))

    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "data-encryption-key" in out.unit_status.message


def test_ungranted_encryption_key_blocks(ctx: testing.Context, state: testing.State):
    """
    arrange: data-encryption-key refers to a secret the charm can't access.
    act: run config-changed.
    assert: the unit is blocked with a helpful message.
    """
    out = ctx.run(
        ctx.on.config_changed(),
        dataclasses.replace(
            state, config={"data-encryption-key": "secret:cvh7kruupa1s46bqvuig"}, secrets=set()
        ),
    )

    assert out.unit_status == testing.BlockedStatus(
        "data-encryption-key secret not found or not granted"
    )


def test_encryption_key_without_value_blocks(ctx: testing.Context, state: testing.State):
    """
    arrange: data-encryption-key secret lacks the 'value' key.
    act: run config-changed.
    assert: the unit is blocked.
    """
    secret = testing.Secret(tracked_content={"key": "oops"})
    state = dataclasses.replace(state, config={"data-encryption-key": secret.id}, secrets={secret})

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == testing.BlockedStatus(
        "data-encryption-key secret must contain a non-empty 'value' key"
    )


def test_missing_postgresql_blocks(
    ctx: testing.Context, state: testing.State, postgresql_relation: testing.Relation
):
    """
    arrange: no postgresql relation.
    act: run config-changed.
    assert: the unit is blocked on the missing integration.
    """
    state = dataclasses.replace(state, relations=state.relations - {postgresql_relation})

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == testing.BlockedStatus("missing integrations: postgresql")


def test_leader_elected_promotes_engine(ctx: testing.Context, state: testing.State):
    """
    arrange: a unit running in API-only mode that becomes leader.
    act: run leader-elected.
    assert: the unit now runs the engine.
    """
    out = ctx.run(ctx.on.config_changed(), dataclasses.replace(state, leader=False))
    assert _service_env(out)["GOALERT_API_ONLY"] == "true"

    out = ctx.run(ctx.on.leader_elected(), dataclasses.replace(out, leader=True))

    assert _service_env(out)["GOALERT_API_ONLY"] == "false"


def test_update_status_demotes_former_leader(ctx: testing.Context, state: testing.State):
    """
    arrange: a unit running the engine that lost leadership.
    act: run update-status.
    assert: the unit switches to API-only mode.
    """
    out = ctx.run(ctx.on.config_changed(), state)
    assert _service_env(out)["GOALERT_API_ONLY"] == "false"

    out = ctx.run(ctx.on.update_status(), dataclasses.replace(out, leader=False))

    assert _service_env(out)["GOALERT_API_ONLY"] == "true"


def test_metrics_endpoint_uses_goalert_metrics_port(ctx: testing.Context, state: testing.State):
    """
    arrange: a metrics-endpoint relation.
    act: run relation-joined.
    assert: Prometheus scrapes GoAlert's dedicated metrics port.
    """
    metrics = testing.Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")
    state = dataclasses.replace(state, relations={*state.relations, metrics})

    out = ctx.run(ctx.on.relation_joined(metrics), state)

    jobs = out.get_relation(metrics.id).local_app_data["scrape_jobs"]
    assert '"*:2112"' in jobs
    assert '"metrics_path": "/metrics"' in jobs


@pytest.mark.parametrize("password", [None, "my-password"])
def test_create_admin_user(ctx: testing.Context, state: testing.State, password: str | None):
    """
    arrange: a fully integrated unit.
    act: run the create-admin-user action.
    assert: goalert add-user is executed and the credentials are returned.
    """
    container = state.get_container("app")
    container = dataclasses.replace(
        container,
        execs={testing.Exec(["/usr/local/bin/goalert", "add-user"], return_code=0)},
    )
    state = dataclasses.replace(state, containers={container})
    params = {"username": "admin", "email": "admin@example.com"}
    if password:
        params["password"] = password

    ctx.run(ctx.on.action("create-admin-user", params=params), state)

    results = ctx.action_results
    assert results is not None
    assert results["username"] == "admin"
    assert results["email"] == "admin@example.com"
    if password:
        assert "password" not in results
    else:
        assert len(results["password"]) >= 24
    execution = ctx.exec_history["app"][0]
    assert execution.command[:3] == ["/usr/local/bin/goalert", "add-user", "--admin"]
    expected_password = password or results["password"]
    assert execution.command[-2:] == ["--pass", expected_password]
    assert execution.environment["GOALERT_DATA_ENCRYPTION_KEY"] == "super-secret-key"
    assert execution.environment["GOALERT_DB_URL"].startswith("postgresql://")


def test_create_admin_user_failure(ctx: testing.Context, state: testing.State):
    """
    arrange: goalert add-user fails.
    act: run the create-admin-user action.
    assert: the action fails with the command output and the password redacted.
    """
    container = dataclasses.replace(
        state.get_container("app"),
        execs={
            testing.Exec(
                ["/usr/local/bin/goalert", "add-user"],
                return_code=1,
                stdout="error: username 'admin' already exists (pass s3cret)",
            )
        },
    )
    state = dataclasses.replace(state, containers={container})

    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(
            ctx.on.action(
                "create-admin-user",
                params={"username": "admin", "email": "a@example.com", "password": "s3cret"},
            ),
            state,
        )

    assert "already exists" in exc_info.value.message
    assert "s3cret" not in exc_info.value.message


def test_create_admin_user_requires_database(
    ctx: testing.Context, state: testing.State, postgresql_relation: testing.Relation
):
    """
    arrange: no postgresql relation.
    act: run the create-admin-user action.
    assert: the action fails.
    """
    state = dataclasses.replace(state, relations=state.relations - {postgresql_relation})

    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(
            ctx.on.action(
                "create-admin-user", params={"username": "admin", "email": "a@example.com"}
            ),
            state,
        )

    assert exc_info.value.message == "postgresql integration is not ready"


def test_pebble_ready_without_container_connection(ctx: testing.Context, state: testing.State):
    """
    arrange: the workload container is not reachable.
    act: run config-changed.
    assert: the unit waits for pebble.
    """
    container = dataclasses.replace(state.get_container("app"), can_connect=False)

    out = ctx.run(ctx.on.config_changed(), dataclasses.replace(state, containers={container}))

    assert out.unit_status == ops.WaitingStatus("Waiting for pebble ready")


def test_invalid_framework_config_blocks(ctx: testing.Context, state: testing.State):
    """
    arrange: an invalid app-port.
    act: run config-changed.
    assert: the unit is blocked on the invalid option.
    """
    state = dataclasses.replace(state, config={**state.config, "app-port": 0})

    out = ctx.run(ctx.on.config_changed(), state)

    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "app-port" in out.unit_status.message


def test_update_status_without_container_connection(ctx: testing.Context, state: testing.State):
    """
    arrange: the workload container is not reachable.
    act: run update-status.
    assert: the charm doesn't touch the workload.
    """
    container = dataclasses.replace(state.get_container("app"), can_connect=False)

    out = ctx.run(ctx.on.update_status(), dataclasses.replace(state, containers={container}))

    assert out.get_container("app").layers == {}


@pytest.mark.parametrize(
    "change, message",
    [
        pytest.param(
            {"container_connect": False}, "workload container is not ready", id="no-container"
        ),
        pytest.param({"peer_empty": True}, "charm is still initializing", id="no-peer-data"),
        pytest.param(
            {"no_key": True},
            "invalid charm configuration: data-encryption-key secret not found or not granted",
            id="ungranted-key",
        ),
    ],
)
def test_create_admin_user_preconditions(
    ctx: testing.Context,
    state: testing.State,
    peer_relation: testing.PeerRelation,
    change: dict,
    message: str,
):
    """
    arrange: a unit that isn't ready to run the action.
    act: run the create-admin-user action.
    assert: the action fails with an explanation.
    """
    if change.get("container_connect") is False:
        container = dataclasses.replace(state.get_container("app"), can_connect=False)
        state = dataclasses.replace(state, containers={container})
    if change.get("peer_empty"):
        empty_peer = dataclasses.replace(peer_relation, local_app_data={})
        state = dataclasses.replace(
            state, relations=(state.relations - {peer_relation}) | {empty_peer}
        )
    if change.get("no_key"):
        state = dataclasses.replace(state, secrets=set())

    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(
            ctx.on.action(
                "create-admin-user", params={"username": "admin", "email": "a@example.com"}
            ),
            state,
        )

    assert exc_info.value.message == message
