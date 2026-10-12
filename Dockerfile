# sealedstack — the versioned tool image (D-031, spec-infra-repo-and-release §6.2).
#
# It is the toolbox base plus the sealedstack code at this version and a CLI
# entrypoint (`sealedstack <command>`). Users never clone this repo: their infra
# repo pins a version and runs this image through a generated `bin/sealedstack`.
#
# Build the toolbox base first, then this image. In CI the release workflow
# builds the toolbox, captures its digest and passes it here so the tool image
# is pinned to an immutable base:
#   docker build -t sealedstack-toolbox:dev toolbox
#   docker build -f Dockerfile -t sealedstack-tool \
#     --build-arg TOOLBOX_IMAGE=ghcr.io/<owner>/sealedstack-toolbox@sha256:... .
# `just tool` does the local variant: it builds the toolbox as
# `sealedstack-toolbox:dev` and passes that tag here.
#
# TOOLBOX_IMAGE defaults to the published toolbox for the repo owner, so a
# clean `docker build .` (or a CI build) works without a local-only tag.
ARG TOOLBOX_OWNER=anomalyco
ARG TOOLBOX_IMAGE=ghcr.io/${TOOLBOX_OWNER}/sealedstack-toolbox:latest
FROM ${TOOLBOX_IMAGE} AS tool

ENV SEALEDSTACK_CODE=/opt/sealedstack
WORKDIR /infra

# The code lives at a fixed path inside the image; the entrypoint resolves it
# from SEALEDSTACK_CODE. Real user config is never baked in — it arrives via the
# SEALEDSTACK_CONFIG mount. Only the sample files ship for `init` to copy.
COPY scripts/ /opt/sealedstack/scripts/
COPY tests/ /opt/sealedstack/tests/
COPY engine/ /opt/sealedstack/engine/
COPY registry/ /opt/sealedstack/registry/
COPY schemas/ /opt/sealedstack/schemas/
COPY tofu/ /opt/sealedstack/tofu/
COPY ansible/ /opt/sealedstack/ansible/
COPY examples/ /opt/sealedstack/examples/
COPY docs/ /opt/sealedstack/docs/
COPY config/*.example.yaml /opt/sealedstack/config/
COPY config/providers/*.example.yaml /opt/sealedstack/config/providers/
COPY VERSION README.md AGENTS.md ansible.cfg /opt/sealedstack/

RUN find /opt/sealedstack/scripts -maxdepth 1 -type f \
         \( -name '*.sh' -o -name '*.py' \) -exec chmod 0755 {} +; \
    printf '#!/bin/sh\nexec /opt/venv/bin/python3 -m sealedstack "$@"\n' \
      > /usr/local/bin/sealedstack \
    && chmod 0755 /usr/local/bin/sealedstack

# The Python engine is on the path; the console entrypoint is `sealedstack`.
ENV PYTHONPATH=/opt/sealedstack/engine

ENTRYPOINT ["sealedstack"]

LABEL org.opencontainers.image.title="sealedstack" \
      org.opencontainers.image.description="sealedstack: build a sealed origin from an infra repo" \
      org.opencontainers.image.source="https://github.com/anomalyco/sealedstack"
