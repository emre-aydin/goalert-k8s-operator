# Copyright 2026 Emre
# See LICENSE file for licensing details.

"""GoAlert workload management on top of the paas-charm App."""

import typing
import urllib.parse

import ops
from paas_charm.app import App

SERVICE_NAME = "go"
GOALERT_BIN = "/usr/local/bin/goalert"
DEFAULT_METRICS_PORT = 2112
METRICS_PATH = "/metrics"
ENCRYPTION_KEY_CONFIG = "data_encryption_key"
ENCRYPTION_KEY_FIELD = "value"


class GoAlertApp(App):
    """paas-charm App that maps charm state to GoAlert's configuration."""

    def __init__(self, *, api_only: bool, metrics_port: int, **kwargs: typing.Any) -> None:
        """Initialize the instance.

        Args:
            api_only: whether this unit should run GoAlert in API-only mode (no engine).
            metrics_port: port GoAlert serves Prometheus metrics on.
            kwargs: passthrough to paas_charm.app.App.
        """
        super().__init__(**kwargs)
        self._api_only = api_only
        self._metrics_port = metrics_port

    def gen_environment(self) -> dict[str, str]:
        """Generate the GoAlert environment from the charm state.

        Returns:
            The environment variables for the GoAlert service.
        """
        env = super().gen_environment()
        encryption_key = self._charm_state.user_defined_config.get(ENCRYPTION_KEY_CONFIG)
        # Don't leak the key under a second, unused variable name.
        env.pop(
            f"{self.configuration_prefix}{ENCRYPTION_KEY_CONFIG.upper()}_"
            f"{ENCRYPTION_KEY_FIELD.upper()}",
            None,
        )
        if isinstance(encryption_key, typing.Mapping) and encryption_key.get(ENCRYPTION_KEY_FIELD):
            env["GOALERT_DATA_ENCRYPTION_KEY"] = str(encryption_key[ENCRYPTION_KEY_FIELD])
        if db_url := env.get("POSTGRESQL_DB_CONNECT_STRING"):
            env["GOALERT_DB_URL"] = db_url
        if public_url := env.get(f"{self.configuration_prefix}BASE_URL"):
            env["GOALERT_PUBLIC_URL"] = public_url
        env["GOALERT_LISTEN"] = f"0.0.0.0:{self._workload_config.port}"
        env["GOALERT_LISTEN_PROMETHEUS"] = f"0.0.0.0:{self._metrics_port}"
        env["GOALERT_API_ONLY"] = "true" if self._api_only else "false"
        env["GOALERT_JSON"] = "true"
        return env

    def _app_layer(self) -> ops.pebble.LayerDict:
        """Add a GoAlert health check to the paas-charm generated layer.

        Returns:
            The pebble layer definition for the application.
        """
        layer = super()._app_layer()
        prefix = urllib.parse.urlparse(self._charm_state.base_url or "").path.rstrip("/")
        layer["checks"] = {
            "goalert-ready": {
                "override": "replace",
                "level": "ready",
                "period": "10s",
                "threshold": 3,
                "http": {"url": f"http://localhost:{self._workload_config.port}{prefix}/health"},
            }
        }
        return layer
