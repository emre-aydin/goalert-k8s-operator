# GoAlert Kubernetes charm

A [Juju](https://juju.is) Kubernetes charm for [GoAlert](https://github.com/target/goalert),
the on-call scheduling, automated escalation and notification platform.

The charm is built with the 12-factor [`paas-charm`](https://github.com/canonical/paas-charm)
Go framework. The workload runs from a custom rock, built with the rockcraft `go-framework` extension.

## Repository layout

| Path | Contents |
| --- | --- |
| `rock/` | `rockcraft.yaml` for the GoAlert rock. It installs the pinned release binary from the [emre-aydin/goalert](https://github.com/emre-aydin/goalert) fork (upstream GoAlert plus Mattermost notifications) and verifies its checksum. |
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

## Receiving alerts from Alertmanager

The charm's integrations send GoAlert's own metrics, logs and dashboard to COS. They do not route
COS alerts into GoAlert. To page people from Prometheus or Loki alert rules, point Alertmanager at
GoAlert's built-in Alertmanager webhook. No charm relation is involved.

1. In GoAlert, create an escalation policy and a service. On the service, add an integration key
   of type **Prometheus Alertmanager** and copy the URL it shows
   (`.../api/v2/alertmanager/incoming?token=<key>`).
2. Configure Alertmanager with a webhook receiver for that URL. From inside the cluster, use
   GoAlert's service address. Keep the path prefix, because GoAlert expects it:

   ```yaml
   # am.yaml
   route:
     receiver: goalert
     group_by: [juju_model, juju_application, alertname]
     routes:
       # COS fires Watchdog all the time to prove alerting works. Keep it out of GoAlert,
       # or send it to a GoAlert heartbeat monitor instead.
       - matchers: ['alertname="Watchdog"']
         receiver: blackhole
   receivers:
     - name: blackhole
     - name: goalert
       webhook_configs:
         - url: http://goalert-k8s.<model>.svc.cluster.local:8080/<model>-goalert-k8s/api/v2/alertmanager/incoming?token=<key>
           send_resolved: true
   ```

   ```shell
   juju config -m cos alertmanager config_file=@am.yaml
   ```

   The path prefix is the ingress path, `/<model>-<app>`. Without ingress, there is no prefix.

### Testing it

Post an alert straight to Alertmanager. This tests the Alertmanager to GoAlert path without
waiting for a real alert rule to fire:

```shell
curl -X POST http://<alertmanager>/api/v2/alerts -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"GoAlertTest","severity":"critical"},"annotations":{"summary":"Test alert"}}]'
```

After `group_wait` (30 seconds by default), the alert appears under **Alerts**, or on the service's
**Alerts** page. The Alerts list may be filtered to favorited services or to active alerts. If no
alert appears, look at `alertmanager_notifications_failed_total{integration="webhook"}` on
Alertmanager's `/metrics`, and check that the integration key belongs to the service you are viewing.

To resolve the test alert, post the same labels with an `endsAt` in the past:

```shell
curl -X POST http://<alertmanager>/api/v2/alerts -H 'Content-Type: application/json' \
  -d "[{\"labels\":{\"alertname\":\"GoAlertTest\",\"severity\":\"critical\"},\"endsAt\":\"$(date -u -d '-1 min' +%Y-%m-%dT%H:%M:%SZ)\"}]"
```

### What happens when an alert resolves

- With `send_resolved: true`, Alertmanager sends a `resolved` notification and GoAlert **closes**
  the matching alert. Escalation stops. Users are only told about the close if they enabled status
  updates on their contact method.
- Without `send_resolved`, alerts stay open in GoAlert until someone closes them.
- GoAlert matches alerts on Alertmanager's group key. Every alert sharing the same `group_by`
  labels becomes **one** GoAlert alert, and that alert closes only when the whole group has
  resolved.
- It only works one way: acknowledging or closing an alert in GoAlert does not silence anything
  in Alertmanager. While the condition still fires, Alertmanager re-sends it every
  `repeat_interval` (4 hours by default). If you closed the alert by hand, that re-send opens a
  new one. Use an Alertmanager silence to stop the source.
- An alert that resolves and fires again (flapping) closes the first GoAlert alert and opens a new
  one, which escalates from the start. A `for:` duration on the rule, or a longer
  `group_interval`, makes noisy alerts calmer.

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
  oci-archive:rock/goalert_0.35.0-emre-aydin.1_amd64.rock docker://localhost:32000/goalert:0.35.0-emre-aydin.1
(cd charm && charmcraft pack)
cd charm
CHARM_PATH=$PWD/goalert-k8s_amd64.charm ROCK_IMAGE=localhost:32000/goalert:0.35.0-emre-aydin.1 tox -e integration
```

To upgrade GoAlert, bump `GOALERT_VERSION`, the per-architecture SHA256 values and `version` in
`rock/rockcraft.yaml`. `GOALERT_REPO` picks the GitHub repository the release comes from. The
SHA256 values are shown on the release page.

### Try it in a disposable VM

`scripts/dev-env.sh` sets up a Multipass VM where you can use the charm by hand together with
COS Lite. The only thing it needs on the host is Multipass. The VM has its own MicroK8s, local
registry and Juju controller, so your host's Juju client and controllers are never used.

```shell
scripts/dev-env.sh          # up + build + deploy, about 30-45 minutes the first time
scripts/dev-env.sh info     # URLs and credentials
scripts/dev-env.sh refresh  # rebuild and refresh GoAlert after a change
scripts/dev-env.sh destroy  # delete the VM
```

The script deploys COS Lite in the `cos` model, and GoAlert with PostgreSQL in the `goalert` model.
GoAlert connects to COS across models: ingress through COS's Traefik, metrics, logs and the Grafana
dashboard. Traefik uses the VM's IP as its external hostname, and port 80 of the VM forwards to it,
so your host browser can open:

- GoAlert: `http://<VM IP>/goalert-goalert-k8s`. The script creates an `admin` user and `info` prints its password.
- Grafana: `http://<VM IP>/cos-grafana`. The user is `admin` and `info` prints its password. GoAlert's dashboard is the go-framework's "Go Operator" dashboard.
- Catalogue: `http://<VM IP>/cos-catalogue`

Other commands: `up`, `build`, `deploy`, `snapshot`/`restore` (save and return to the freshly
provisioned VM) and `shell`. `VM_NAME`, `VM_CPUS` (8), `VM_MEMORY` (16G) and `VM_DISK` (100G) override
the defaults.

### Publishing

`.github/workflows/publish.yaml` runs the tests, then for amd64 and arm64:

1. Packs the rock and the charm.
2. Uploads the rock as the `app-image` resource.
3. Releases the charm to `latest/edge`.

It needs a `CHARMHUB_TOKEN` repository secret, created with `charmcraft login --export`.
Register the `goalert-k8s` name on Charmhub first.

## License

This charm and rock are licensed under the [Apache License 2.0](LICENSE).
GoAlert itself is also licensed under Apache 2.0 (see [upstream](https://github.com/target/goalert/blob/master/LICENSE.md)).
