# GoAlert Kubernetes charm

A [Juju](https://juju.is) Kubernetes charm for [GoAlert](https://github.com/target/goalert),
the on-call scheduling, automated escalation and notification platform.

The charm is built with the 12-factor [`paas-charm`](https://github.com/canonical/paas-charm)
Go framework. The workload runs from a custom rock, built with the rockcraft `go-framework` extension.

## Repository layout

| Path | Contents |
| --- | --- |
| `rock/` | `rockcraft.yaml` for the GoAlert rock. It installs the pinned upstream release binary and verifies its checksum. |
| `charm/` | The charm: `charmcraft.yaml`, `src/`, vendored charm libraries in `lib/`, and tests. |
| `.github/workflows/` | CI (lint, static checks, unit and integration tests) and Charmhub publishing. |

## Features

- **PostgreSQL** (`postgresql` endpoint, `postgresql_client` interface): required. The charm is blocked until this integration exists.
- **Data encryption key** from a Juju user secret, shared by all units.
- **Ingress** through `traefik-k8s`. The path prefix is kept, and GoAlert's public URL follows the ingress URL.
- **Horizontal scaling**: the leader unit runs the GoAlert engine and the other units run with `--api-only`, as upstream recommends. The role follows leadership changes.
- **Observability**:
  - Prometheus scraping (`metrics-endpoint`)
  - Loki log forwarding (`logging`, JSON logs)
  - Grafana dashboard endpoint
- **`create-admin-user` action** to bootstrap the first administrator.
- A Pebble readiness check against GoAlert's `/health` endpoint.

## Deploy

```shell
juju deploy postgresql-k8s --channel 16/stable --trust
juju deploy goalert-k8s --resource app-image=<rock image>

# The encryption key must never change after GoAlert has stored data.
secret=$(juju add-secret goalert-encryption-key value="$(openssl rand -hex 32)")
juju grant-secret goalert-encryption-key goalert-k8s
juju config goalert-k8s data-encryption-key="$secret"

juju integrate goalert-k8s postgresql-k8s

# Optional
juju deploy traefik-k8s --trust && juju integrate goalert-k8s traefik-k8s
juju integrate goalert-k8s:metrics-endpoint prometheus-k8s
juju integrate goalert-k8s:logging loki-k8s

# Create the first admin (a password is generated if you don't pass one)
juju run goalert-k8s/leader create-admin-user username=admin email=admin@example.com
```

GoAlert needs the `pgcrypto` extension. It is a trusted extension, so GoAlert creates it
on first start using the credentials from the relation.

Configure everything else in the GoAlert admin UI: SMTP, Twilio, Slack, OIDC and similar.

## Configuration

| Option | Default | Notes |
| --- | --- | --- |
| `data-encryption-key` | (required) | Juju secret ID. The secret must contain a `value` key. |
| `app-port` | `8080` | HTTP port GoAlert listens on. |
| `metrics-port` | `8080` | Prometheus port. GoAlert serves metrics on its own listener, so if this equals `app-port` the charm uses `2112`. |
| `metrics-path` | `/metrics` | Ignored: GoAlert always serves `/metrics`. |

The extension-provided `app-secret-key`/`app-secret-key-id` options and the `rotate-secret-key`
action have no effect on GoAlert.

### Scaling

```shell
juju scale-application goalert-k8s 3
```

Only the leader processes schedules and notifications. When leadership moves, the new leader
switches to engine mode straight away (`leader-elected`). The previous leader switches to API-only
mode on its next `update-status` hook. Running two engines briefly is safe in GoAlert.

### Encryption key rotation

Not supported yet. Changing the secret's content makes existing encrypted data unreadable.
GoAlert's `--data-encryption-key-old` flow is not exposed.

## Development

The unit tests need no Juju or charmcraft. The `go-framework` extension metadata is expanded in
`tests/unit/conftest.py`.

```shell
cd charm
tox -e lint,static,unit
```

For the integration tests you need a Juju controller on MicroK8s with the `registry` addon. On
macOS, a Multipass VM works: `sudo concierge prepare -p microk8s`. On MicroK8s 1.36, also apply
`charm/tests/integration/metallb-rbac.yaml` and restart MetalLB. Without it, MetalLB never assigns
Traefik an IP.

```shell
(cd rock && rockcraft pack)
rockcraft.skopeo --insecure-policy copy --dest-tls-verify=false \
  oci-archive:rock/goalert_0.35.0_amd64.rock docker://localhost:32000/goalert:0.35.0
(cd charm && charmcraft pack)
cd charm
CHARM_PATH=$PWD/goalert-k8s_amd64.charm ROCK_IMAGE=localhost:32000/goalert:0.35.0 tox -e integration
```

To upgrade GoAlert, bump `GOALERT_VERSION`, the per-architecture SHA256 values and `version` in
`rock/rockcraft.yaml`. The SHA256 values are shown on the upstream release page.

### Publishing

`.github/workflows/publish.yaml` runs the tests, then for amd64 and arm64:

1. Packs the rock and the charm.
2. Uploads the rock as the `app-image` resource.
3. Releases the charm to `latest/edge`.

It needs a `CHARMHUB_TOKEN` repository secret, created with `charmcraft login --export`.
Register the `goalert-k8s` name on Charmhub first.
