#!/usr/bin/env bash
# Copyright 2026 Emre
# See LICENSE file for licensing details.
#
# Disposable development environment for trying the GoAlert charm by hand.
#
# Creates a Multipass VM with MicroK8s and its own Juju controller, deploys COS Lite
# and GoAlert (with PostgreSQL), and exposes the GoAlert UI and Grafana to the host
# browser. Nothing is installed on the host apart from what Multipass already needs,
# and the host's Juju client and controllers are never used.
#
# Usage: scripts/dev-env.sh [all|up|build|deploy|refresh|info|snapshot|restore|shell|destroy]

set -euo pipefail

VM_NAME="${VM_NAME:-goalert-dev}"
VM_CPUS="${VM_CPUS:-8}"
VM_MEMORY="${VM_MEMORY:-16G}"
VM_DISK="${VM_DISK:-100G}"
VM_IMAGE="${VM_IMAGE:-24.04}"
SNAPSHOT_NAME="provisioned"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [command]

Commands:
  all       (default) up, build and deploy
  up        create the VM and set up MicroK8s, a local registry and a Juju controller
  build     copy the repo into the VM, pack the rock and charm, push the rock to the registry
  deploy    deploy COS Lite and GoAlert, connect them and create a GoAlert admin user
  refresh   rebuild and refresh the deployed GoAlert charm and image
  info      print URLs and credentials
  snapshot  snapshot the VM as "$SNAPSHOT_NAME" (stops and restarts it)
  restore   restore the "$SNAPSHOT_NAME" snapshot
  shell     open a shell in the VM (juju and microk8s.kubectl work there)
  destroy   delete the VM and everything in it

Settings (environment variables): VM_NAME=$VM_NAME VM_CPUS=$VM_CPUS VM_MEMORY=$VM_MEMORY
VM_DISK=$VM_DISK VM_IMAGE=$VM_IMAGE
EOF
}

vm_exists() { multipass info "$VM_NAME" >/dev/null 2>&1; }

require_vm() { vm_exists || die "VM '$VM_NAME' does not exist, run '$(basename "$0") up' first"; }

vm_ip() {
    multipass info "$VM_NAME" --format json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["info"][sys.argv[1]]["ipv4"][0])' "$VM_NAME"
}

# Run a script (read from stdin) inside the VM as the "ubuntu" user, in a login shell
# so that the PATH and group membership set up by concierge are picked up.
# Arguments are passed as KEY=VALUE environment variables.
# The script is passed as an argument, not piped, because tools such as rockcraft read
# stdin and would swallow the rest of it. Strict mode is enabled only after the login
# profile, which is not nounset-safe on Ubuntu (byobu).
vm_run() {
    local script
    script="$(cat)"
    multipass exec "$VM_NAME" -- env "$@" bash -l -c "set -euo pipefail
$script" </dev/null
}

cmd_up() {
    command -v multipass >/dev/null || die "multipass is not installed (sudo snap install multipass)"
    if vm_exists; then
        log "VM '$VM_NAME' already exists"
        multipass start "$VM_NAME" >/dev/null 2>&1 || true
    else
        log "Launching VM '$VM_NAME' ($VM_CPUS CPUs, $VM_MEMORY RAM, $VM_DISK disk)"
        multipass launch "$VM_IMAGE" --name "$VM_NAME" --cpus "$VM_CPUS" \
            --memory "$VM_MEMORY" --disk "$VM_DISK" --timeout 900
    fi

    log "Provisioning MicroK8s, Juju, charmcraft and rockcraft (this mirrors CI)"
    multipass transfer "$REPO_ROOT/charm/tests/integration/metallb-rbac.yaml" \
        "$VM_NAME:/home/ubuntu/metallb-rbac.yaml"
    vm_run <<'EOF'
if [ ! -f ~/.provisioned ]; then
    sudo snap install concierge --classic
    sudo concierge prepare -p microk8s --extra-snaps astral-uv
    sudo microk8s enable registry
    sudo microk8s kubectl apply -f ~/metallb-rbac.yaml
    sudo microk8s kubectl -n metallb-system rollout restart deploy/controller ds/speaker
    touch ~/.provisioned
fi
sudo microk8s kubectl -n container-registry rollout status deployment/registry --timeout=300s
juju controllers
EOF
    log "VM is ready. Tip: '$(basename "$0") snapshot' saves this clean state."
}

cmd_build() {
    require_vm
    log "Copying the repository into the VM"
    multipass exec "$VM_NAME" -- bash -c 'rm -rf ~/goalert && mkdir -p ~/goalert ~/artifacts'
    tar -C "$REPO_ROOT" --exclude-vcs --exclude=.tox --exclude='.venv*' --exclude=venv \
        --exclude=build --exclude='*.charm' --exclude='*.rock' --exclude=__pycache__ -cf - . |
        multipass exec "$VM_NAME" -- tar -C /home/ubuntu/goalert -xf -

    log "Packing the rock and charm, and pushing the rock to the local registry"
    vm_run <<'EOF'
arch=$(dpkg --print-architecture)
cd ~/goalert/rock
rockcraft pack
rock=$(ls -t ./*_"$arch".rock | head -1)
image="localhost:32000/goalert:dev-$(date +%Y%m%d%H%M%S)"
rockcraft.skopeo --insecure-policy copy --dest-tls-verify=false "oci-archive:$rock" "docker://$image"
mv "$rock" ~/artifacts/
echo "$image" > ~/artifacts/image

cd ~/goalert/charm
charmcraft pack
mv ./goalert-k8s_*"$arch"*.charm ~/artifacts/goalert-k8s.charm
echo "Built ~/artifacts/goalert-k8s.charm and $image"
EOF
}

cmd_deploy() {
    require_vm
    local ip
    ip="$(vm_ip)"
    vm_run "VM_IP=$ip" <<'EOF'
[ -f ~/artifacts/image ] || { echo "nothing built yet, run the build command first" >&2; exit 1; }
arch=$(dpkg --print-architecture)
image=$(cat ~/artifacts/image)

model_exists() { juju models --format json | python3 -c 'import json, sys; sys.exit(sys.argv[1] not in [m["short-name"] for m in json.load(sys.stdin)["models"]])' "$1"; }
app_exists() { juju status -m "$1" --format json | python3 -c 'import json, sys; sys.exit(sys.argv[1] not in json.load(sys.stdin).get("applications", {}))' "$2"; }
offer_exists() { juju offers -m cos --format json | python3 -c 'import json, sys; sys.exit(sys.argv[1] not in json.load(sys.stdin))' "$1"; }
relation_exists() { juju status -m goalert --format json | python3 -c '
import json, sys
app, endpoint = sys.argv[1].split(":")
rels = json.load(sys.stdin)["applications"].get(app, {}).get("relations", {})
sys.exit(endpoint not in rels)' "$1"; }
integrate() { relation_exists "$1" || juju integrate -m goalert "$1" "$2"; }

echo "==> Deploying COS Lite in model 'cos'"
model_exists cos || juju add-model cos
juju set-model-constraints -m cos "arch=$arch"
app_exists cos prometheus || juju deploy -m cos cos-lite --trust
# Ingress URLs use the VM address, so the host browser can open them.
juju config -m cos traefik external_hostname="$VM_IP"
offer_exists prometheus-scrape || juju offer cos.prometheus:metrics-endpoint prometheus-scrape
offer_exists loki-logging || juju offer cos.loki:logging loki-logging
offer_exists grafana-dashboards || juju offer cos.grafana:grafana-dashboard grafana-dashboards
offer_exists traefik-ingress || juju offer cos.traefik:ingress traefik-ingress

echo "==> Deploying GoAlert and PostgreSQL in model 'goalert'"
model_exists goalert || juju add-model goalert
juju set-model-constraints -m goalert "arch=$arch"
app_exists goalert postgresql-k8s || juju deploy -m goalert postgresql-k8s --channel 16/stable --trust
app_exists goalert goalert-k8s ||
    juju deploy -m goalert ~/artifacts/goalert-k8s.charm goalert-k8s --resource "app-image=$image"
if ! juju secrets -m goalert --format json | grep -q '"goalert-encryption-key"'; then
    secret=$(juju add-secret -m goalert goalert-encryption-key value="$(openssl rand -hex 32)")
    juju grant-secret -m goalert goalert-encryption-key goalert-k8s
    juju config -m goalert goalert-k8s data-encryption-key="$secret"
fi
integrate goalert-k8s:postgresql postgresql-k8s
integrate goalert-k8s:ingress admin/cos.traefik-ingress
integrate goalert-k8s:metrics-endpoint admin/cos.prometheus-scrape
integrate goalert-k8s:logging admin/cos.loki-logging
integrate goalert-k8s:grafana-dashboard admin/cos.grafana-dashboards

echo "==> Waiting for both models to settle (COS Lite takes a while on first deploy)"
for model in cos goalert; do
    juju wait-for model "$model" --timeout 40m \
        --query='forEach(applications, app => app.status == "active")'
done

echo "==> Forwarding port 80 of the VM to Traefik"
sudo systemctl stop goalert-dev-ingress.service 2>/dev/null || true
sudo systemctl reset-failed goalert-dev-ingress.service 2>/dev/null || true
sudo systemd-run --unit goalert-dev-ingress -p Restart=always -p RestartSec=5 \
    /snap/bin/microk8s kubectl -n cos port-forward --address 0.0.0.0 svc/traefik-lb 80:80

if [ ! -f ~/artifacts/admin.json ]; then
    echo "==> Creating the GoAlert admin user"
    juju run -m goalert goalert-k8s/leader create-admin-user \
        username=admin email=admin@example.com --format json |
        python3 -c 'import json, sys; print(json.dumps(next(iter(json.load(sys.stdin).values()))["results"]))' \
        > ~/artifacts/admin.json
    chmod 600 ~/artifacts/admin.json
fi
EOF
    cmd_info
}

cmd_refresh() {
    cmd_build
    log "Refreshing goalert-k8s"
    vm_run <<'EOF'
juju refresh -m goalert goalert-k8s --path ~/artifacts/goalert-k8s.charm \
    --resource "app-image=$(cat ~/artifacts/image)"
juju wait-for application -m goalert goalert-k8s --timeout 20m --query='status=="active"'
EOF
}

cmd_info() {
    require_vm
    local ip
    ip="$(vm_ip)"
    vm_run "VM_IP=$ip" <<'EOF'
results() { python3 -c 'import json, sys; r = next(iter(json.load(sys.stdin).values()))["results"]; print(r[sys.argv[1]])' "$1"; }
cat <<INFO

GoAlert     http://$VM_IP/goalert-goalert-k8s
            user: $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["username"])' ~/artifacts/admin.json 2>/dev/null || echo "(not created yet)")
            password: $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["password"])' ~/artifacts/admin.json 2>/dev/null || echo "(not created yet)")
Grafana     http://$VM_IP/cos-grafana
            user: admin
            password: $(juju run -m cos grafana/leader get-admin-password --format json 2>/dev/null | results admin-password || echo "(unavailable)")
Catalogue   http://$VM_IP/cos-catalogue  (links to Prometheus, Alertmanager and the rest)

In the VM ('scripts/dev-env.sh shell'): juju status -m goalert, juju status -m cos
INFO
EOF
}

cmd_snapshot() {
    require_vm
    log "Stopping '$VM_NAME' and taking snapshot '$SNAPSHOT_NAME'"
    multipass stop "$VM_NAME"
    multipass snapshot "$VM_NAME" --name "$SNAPSHOT_NAME"
    multipass start "$VM_NAME"
}

cmd_restore() {
    require_vm
    log "Restoring '$VM_NAME' to snapshot '$SNAPSHOT_NAME'"
    multipass stop "$VM_NAME"
    multipass restore --destructive "$VM_NAME.$SNAPSHOT_NAME"
    multipass start "$VM_NAME"
}

cmd_shell() {
    require_vm
    multipass shell "$VM_NAME"
}

cmd_destroy() {
    require_vm
    read -r -p "Delete VM '$VM_NAME' and everything in it? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { log "Aborted"; exit 0; }
    multipass delete --purge "$VM_NAME"
    log "Deleted '$VM_NAME'"
}

main() {
    local command="${1:-all}"
    case "$command" in
        all) cmd_up; cmd_build; cmd_deploy ;;
        up | build | deploy | refresh | info | snapshot | restore | shell | destroy) "cmd_$command" ;;
        -h | --help | help) usage ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
