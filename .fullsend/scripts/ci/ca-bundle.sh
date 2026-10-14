#!/usr/bin/env bash
# Preserve OpenShell's sandbox trust bundle and add the runner's local-service CA.
fullsend_prepare_ca_bundle() {
  local destination="${1:?destination bundle path is required}"
  local base_bundle="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
  local local_ca="/tmp/gitlab-ca.crt"

  if [[ "${FULLSEND_CA_BUNDLE_READY:-}" == 1 ]]; then
    return 0
  fi
  if [[ ! -r "$local_ca" ]]; then
    return 0
  fi
  if [[ ! -r "$base_bundle" ]]; then
    echo "ERROR: sandbox trust bundle is not readable: $base_bundle" >&2
    return 1
  fi

  cat "$base_bundle" "$local_ca" >"$destination"
  chmod 0600 "$destination"
  export SSL_CERT_FILE="$destination"
  export REQUESTS_CA_BUNDLE="$destination"
  export CURL_CA_BUNDLE="$destination"
  export GIT_SSL_CAINFO="$destination"
  export NODE_EXTRA_CA_CERTS="$destination"
  export FULLSEND_CA_BUNDLE_READY=1
}
