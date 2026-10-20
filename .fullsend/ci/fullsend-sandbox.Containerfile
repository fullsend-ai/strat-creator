ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
# The workflow runs versioned Python helpers inside the sandbox as well as on
# the host. The published base supplies the qualified Claude 2.1.296 runtime.
RUN python3 -m pip install --break-system-packages --ignore-installed \
      jsonschema==4.26.0 pyyaml==6.0.3 certifi==2026.4.22 diff-match-patch==20241021 \
    && claude --version | grep -E '^2[.]1[.]296([[:space:]]|$)'
