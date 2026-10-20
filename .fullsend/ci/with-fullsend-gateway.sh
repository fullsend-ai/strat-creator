#!/usr/bin/env bash
# rfe-autofixer's job-owned Podman/OpenShell pattern, with readiness and cleanup.
set -Eeuo pipefail
[[ "${1:-}" == -- && $# -gt 1 ]] || { echo 'usage: with-fullsend-gateway.sh -- command [args...]' >&2; exit 2; }
shift
: "${STRAT_PRIVATE_DIR:?}" "${CI_JOB_ID:?}" "${FULLSEND_OUTPUT_DIR:?}"
: "${FULLSEND_BASE_IMAGE:?}" "${OPENSHELL_SUPERVISOR_IMAGE:?}" "${CI_PROJECT_DIR:?}"
[[ "$CI_JOB_ID" =~ ^[0-9]+$ ]] || exit 2
SOCKET=/run/podman/podman.sock
NAME="strat-$CI_JOB_ID"
NETWORK="openshell-strat-$CI_JOB_ID"
PODMAN_PID= GATEWAY_PID=
mkdir -p "$STRAT_PRIVATE_DIR/gateway" /run/podman
[[ ! -S "$SOCKET" ]] || { echo 'Refusing to reuse an existing Podman service' >&2; exit 1; }
export XDG_CONFIG_HOME="$STRAT_PRIVATE_DIR/gateway/config"
export XDG_STATE_HOME="$STRAT_PRIVATE_DIR/gateway/state"
export XDG_DATA_HOME="$STRAT_PRIVATE_DIR/gateway/data"
mkdir -p "$XDG_CONFIG_HOME/openshell" "$XDG_STATE_HOME/openshell" "$XDG_DATA_HOME"
export CONTAINERS_STORAGE_CONF="$STRAT_PRIVATE_DIR/gateway/storage.conf"
cat >"$CONTAINERS_STORAGE_CONF" <<EOF
[storage]
driver = "vfs"
graphroot = "$STRAT_PRIVATE_DIR/gateway/storage"
runroot = "$STRAT_PRIVATE_DIR/gateway/runroot"
EOF

cleanup() {
  local rc=$? cleanup_rc=0
  trap - EXIT HUP INT TERM
  set +e
  if [[ -n "$GATEWAY_PID" ]]; then
    openshell gateway remove "$NAME" >>"$FULLSEND_OUTPUT_DIR/gateway-cleanup.log" 2>&1 || cleanup_rc=1
    kill -TERM "$GATEWAY_PID" 2>/dev/null
    wait "$GATEWAY_PID" 2>/dev/null
  fi
  if [[ -n "$PODMAN_PID" ]]; then
    # This service uses fresh storage private to this CI job.
    podman --url "unix://$SOCKET" rm -af >>"$FULLSEND_OUTPUT_DIR/gateway-cleanup.log" 2>&1 || cleanup_rc=1
    if podman --url "unix://$SOCKET" network inspect "$NETWORK" >/dev/null 2>&1; then
      podman --url "unix://$SOCKET" network rm "$NETWORK" >>"$FULLSEND_OUTPUT_DIR/gateway-cleanup.log" 2>&1 || cleanup_rc=1
    fi
    kill -TERM "$PODMAN_PID" 2>/dev/null
    wait "$PODMAN_PID" 2>/dev/null
  fi
  printf 'command_exit=%s\ncleanup_exit=%s\n' "$rc" "$cleanup_rc" >>"$FULLSEND_OUTPUT_DIR/gateway-cleanup.log"
  [[ "$rc" != 0 ]] || rc=$cleanup_rc
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for tool in podman openshell openshell-gateway curl; do command -v "$tool" >/dev/null; done
openshell --version | tee "$FULLSEND_OUTPUT_DIR/openshell-version.txt"
openshell-gateway --version | tee "$FULLSEND_OUTPUT_DIR/gateway-version.txt"
openshell --version | grep -Fq '0.0.112-rhaiv.0'
openshell-gateway --version | grep -Fq '0.0.112-rhaiv.0'
# The reference Docker-executor setup uses VFS without user-namespace mapping.
: > /etc/subuid
: > /etc/subgid
podman system service --time=0 "unix://$SOCKET" >"$FULLSEND_OUTPUT_DIR/podman.log" 2>&1 &
PODMAN_PID=$!
for ((i = 0; i < 30; i++)); do
  podman --url "unix://$SOCKET" info >/dev/null 2>&1 && break
  kill -0 "$PODMAN_PID" 2>/dev/null || exit 1
  sleep 1
done
podman --url "unix://$SOCKET" info >/dev/null
# Warm the same images used by the gateway and harness before sandbox creation.
podman --url "unix://$SOCKET" pull -q "$OPENSHELL_SUPERVISOR_IMAGE"
podman --url "unix://$SOCKET" pull -q "$FULLSEND_BASE_IMAGE"
export FULLSEND_SANDBOX_IMAGE="localhost/strat-fullsend:$CI_JOB_ID"
podman --url "unix://$SOCKET" build --quiet --pull=never \
  --build-arg "BASE_IMAGE=$FULLSEND_BASE_IMAGE" --tag "$FULLSEND_SANDBOX_IMAGE" \
  --file "${BASH_SOURCE[0]%/*}/fullsend-sandbox.Containerfile" \
  "${BASH_SOURCE[0]%/*}" >"$FULLSEND_OUTPUT_DIR/sandbox-build.log" 2>&1
podman --url "unix://$SOCKET" image inspect --format '{{.Id}}' "$FULLSEND_SANDBOX_IMAGE" \
  >"$FULLSEND_OUTPUT_DIR/sandbox-image.txt"

openshell-gateway generate-certs --output-dir "$XDG_STATE_HOME/openshell/tls" \
  --server-san 127.0.0.1 --server-san localhost --server-san host.containers.internal \
  >"$FULLSEND_OUTPUT_DIR/gateway-certgen.log" 2>&1
cat >"$STRAT_PRIVATE_DIR/gateway/gateway.toml" <<EOF
[openshell]
version = 1
[openshell.gateway]
bind_address = "0.0.0.0:17670"
compute_drivers = ["podman"]
[openshell.drivers.podman]
socket_path = "$SOCKET"
supervisor_image = "$OPENSHELL_SUPERVISOR_IMAGE"
network_name = "$NETWORK"
grpc_endpoint = "https://host.containers.internal:17670"
default_image = "$FULLSEND_BASE_IMAGE"
image_pull_policy = "missing"
guest_tls_ca = "$XDG_STATE_HOME/openshell/tls/ca.crt"
guest_tls_cert = "$XDG_STATE_HOME/openshell/tls/client/tls.crt"
guest_tls_key = "$XDG_STATE_HOME/openshell/tls/client/tls.key"
EOF
env -u KUBERNETES_SERVICE_HOST -u KUBERNETES_SERVICE_PORT -u KUBERNETES_PORT \
  openshell-gateway --config "$STRAT_PRIVATE_DIR/gateway/gateway.toml" \
  --bind-address 0.0.0.0 --health-port 17671 \
  --tls-cert "$XDG_STATE_HOME/openshell/tls/server/tls.crt" \
  --tls-key "$XDG_STATE_HOME/openshell/tls/server/tls.key" \
  --tls-client-ca "$XDG_STATE_HOME/openshell/tls/ca.crt" --enable-mtls-auth true \
  --db-url "sqlite:$STRAT_PRIVATE_DIR/gateway/openshell.db?mode=rwc" \
  --log-level info --drivers podman >"$FULLSEND_OUTPUT_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!
for ((i = 0; i < 90; i++)); do
  curl -fsS http://127.0.0.1:17671/healthz >/dev/null 2>&1 && break
  kill -0 "$GATEWAY_PID" 2>/dev/null || exit 1
  sleep 2
done
curl -fsS http://127.0.0.1:17671/healthz >/dev/null
openshell gateway add https://127.0.0.1:17670 --local --name "$NAME"
openshell gateway select "$NAME"
openshell settings set --global --key providers_v2_enabled --value true --yes
"$@"
