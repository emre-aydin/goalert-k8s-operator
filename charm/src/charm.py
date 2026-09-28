#!/usr/bin/env python3
# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""GoAlert Kubernetes charm."""

import logging
import pathlib
import secrets
import typing

import ops
import paas_charm.go
from paas_charm.app import App, WorkloadConfig
from paas_charm.charm_state import CharmState
from paas_charm.charm_utils import block_if_invalid_data
from paas_charm.exceptions import CharmConfigInvalidError
from paas_charm.go.charm import GoConfig
from paas_charm.utils import build_validation_error_message, config_get_with_secret
from pydantic import BaseModel, ValidationError

from goalert import (
    DEFAULT_METRICS_PORT,
    ENCRYPTION_KEY_FIELD,
    GOALERT_BIN,
    METRICS_PATH,
    SERVICE_NAME,
    GoAlertApp,
)

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_OPTION = "data-encryption-key"


class GoAlertCharm(paas_charm.go.Charm):
    """GoAlert charm built on the paas-charm Go framework."""

    def __init__(self, *args: typing.Any) -> None:
        """Initialize the instance.

        Args:
            args: passthrough to CharmBase.
        """
        super().__init__(*args)
        # GoAlert handles its URL prefix itself (derived from the public URL) and
        # returns 404 for requests without it, so the ingress must not strip it.
        self._ingress._strip_prefix = False  # pylint: disable=protected-access
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.on.update_status, self._on_update_status_sync_role)
        self.framework.observe(self.on.create_admin_user_action, self._on_create_admin_user_action)

    @property
    def _metrics_port(self) -> int:
        """Port for GoAlert's Prometheus listener.

        GoAlert serves metrics on a dedicated listener, so it can't share the app port.
        """
        port = self.config.get("app-port")
        metrics_port = self.config.get("metrics-port")
        if isinstance(metrics_port, int) and metrics_port > 0 and metrics_port != port:
            return metrics_port
        return DEFAULT_METRICS_PORT

    @property
    def _api_only(self) -> bool:
        """Whether this unit runs in API-only mode; only the leader runs the engine."""
        return not self.unit.is_leader()

    @property
    def _workload_config(self) -> WorkloadConfig:
        """Return the workload config with GoAlert's metrics endpoint.

        This is evaluated during charm initialization, so invalid config must not raise
        here; the hook handlers report it as a blocked status instead.
        """
        try:
            port = typing.cast(GoConfig, self.get_framework_config()).port
        except CharmConfigInvalidError:
            port = GoConfig.model_fields["port"].default
        return WorkloadConfig(
            framework=self._framework_name,
            port=port,
            base_dir=pathlib.Path("/app"),
            app_dir=pathlib.Path("/app"),
            state_dir=self._state_dir,
            service_name=SERVICE_NAME,
            log_files=[],
            unit_name=self.unit.name,
            metrics_target=f"*:{self._metrics_port}",
            metrics_path=METRICS_PATH,
        )

    def _create_app(self) -> App:
        """Build the GoAlert App instance.

        Returns:
            A new GoAlertApp instance.
        """
        return GoAlertApp(
            container=self._container,
            charm_state=self._create_charm_state(),
            workload_config=self._workload_config,
            database_migration=self._database_migration,
            api_only=self._api_only,
            metrics_port=self._metrics_port,
        )

    def get_framework_config(self) -> BaseModel:
        """Return the framework related configurations.

        paas-charm resolves every secret-typed option here, which runs during charm
        initialization; an ungranted data-encryption-key secret would then crash every
        hook. The framework config doesn't use that option, so it is skipped and
        validated separately in _create_charm_state.

        Raises:
            CharmConfigInvalidError: if charm config is not valid.

        Returns:
             Framework related configurations.
        """
        config: dict[str, typing.Any] = {}
        for key in self.config.keys():
            if key == ENCRYPTION_KEY_OPTION:
                continue
            value = config_get_with_secret(self, key)
            config[key] = (
                value.get_content(refresh=True) if isinstance(value, ops.Secret) else value
            )
        try:
            return self.framework_config_class.model_validate(config)
        except ValidationError as exc:
            error_messages = build_validation_error_message(exc)
            logger.error(error_messages.long)
            raise CharmConfigInvalidError(error_messages.short) from exc

    def _create_charm_state(self) -> CharmState:
        """Create the charm state, validating the data encryption key secret.

        Returns:
            New CharmState.

        Raises:
            CharmConfigInvalidError: if the encryption key secret is unusable.
        """
        secret_id = self.config.get(ENCRYPTION_KEY_OPTION)
        if secret_id:
            try:
                content = self.model.get_secret(id=str(secret_id)).get_content(refresh=True)
            except ops.SecretNotFoundError as exc:
                raise CharmConfigInvalidError(
                    f"{ENCRYPTION_KEY_OPTION} secret not found or not granted"
                ) from exc
            except ops.ModelError as exc:
                raise CharmConfigInvalidError(
                    f"cannot read {ENCRYPTION_KEY_OPTION} secret, is it granted?"
                ) from exc
            if not content.get(ENCRYPTION_KEY_FIELD):
                raise CharmConfigInvalidError(
                    f"{ENCRYPTION_KEY_OPTION} secret must contain a non-empty "
                    f"'{ENCRYPTION_KEY_FIELD}' key"
                )
        return super()._create_charm_state()

    @block_if_invalid_data
    def _on_leader_elected(self, _: ops.LeaderElectedEvent) -> None:
        """Promote this unit to run the GoAlert engine."""
        self.restart()

    @block_if_invalid_data
    def _on_update_status_sync_role(self, _: ops.UpdateStatusEvent) -> None:
        """Converge the engine/API-only role of this unit with its leadership.

        A unit that loses leadership receives no dedicated event, so this catches it.
        """
        if not self._container.can_connect():
            return
        service = self._container.get_plan().services.get(SERVICE_NAME)
        if service is None:
            return
        current = service.environment.get("GOALERT_API_ONLY")
        desired = "true" if self._api_only else "false"
        if current is not None and current != desired:
            logger.info("GoAlert API-only mode changed to %s, restarting", desired)
            self.restart()

    def _on_create_admin_user_action(self, event: ops.ActionEvent) -> None:
        """Create a GoAlert admin user with basic authentication.

        Args:
            event: the action event.
        """
        if not self._container.can_connect():
            event.fail("workload container is not ready")
            return
        if not self._secret_storage.is_initialized:
            event.fail("charm is still initializing")
            return
        try:
            env = self._gen_environment()
        except CharmConfigInvalidError as exc:
            event.fail(f"invalid charm configuration: {exc.msg}")
            return
        if "GOALERT_DB_URL" not in env:
            event.fail("postgresql integration is not ready")
            return
        if "GOALERT_DATA_ENCRYPTION_KEY" not in env:
            event.fail(f"{ENCRYPTION_KEY_OPTION} is not configured")
            return

        username = str(event.params["username"])
        email = str(event.params["email"])
        password = str(event.params.get("password") or "")
        generated = not password
        if generated:
            password = secrets.token_urlsafe(24)

        command = [
            GOALERT_BIN,
            "add-user",
            "--admin",
            "--user",
            username,
            "--email",
            email,
            "--pass",
            password,
        ]
        process = self._container.exec(
            command,
            environment={
                "GOALERT_DB_URL": env["GOALERT_DB_URL"],
                "GOALERT_DATA_ENCRYPTION_KEY": env["GOALERT_DATA_ENCRYPTION_KEY"],
            },
            user=self._workload_config.user,
            group=self._workload_config.group,
            combine_stderr=True,
            timeout=120,
        )
        try:
            process.wait_output()
        except ops.pebble.ExecError as exc:
            output = typing.cast(str, exc.stdout or "").replace(password, "***")
            logger.error("goalert add-user failed: %s", output)
            event.fail(f"failed to create admin user: {output.strip()}")
            return
        except (ops.pebble.ChangeError, ops.pebble.APIError) as exc:
            logger.error("goalert add-user could not be run: %s", exc)
            event.fail("failed to run goalert add-user in the workload container")
            return

        results = {"username": username, "email": email}
        if generated:
            results["password"] = password
        event.set_results(results)


if __name__ == "__main__":
    ops.main(GoAlertCharm)
