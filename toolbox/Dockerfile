# sealedstack toolbox (spec section 3).
# Every sealedstack command runs inside this image so the host only needs Docker.
# All tools are pinned to exact versions (spec section 13, "Supply chain") and
# every downloaded binary is checked against its published sha256 before use.
#
# This file is reusable as the base for the tool image (repo root Dockerfile):
# the `base` stage holds the pinned toolchain, and `toolbox` is the dev image you
# get from `docker build toolbox`. The tool image adds the sealedstack code and
# the CLI entrypoint on top of it.
#
# Supply chain notes:
# - The base is digest-pinned. Renovate's `pinDigests` keeps it current.
# - Each tool has a version ARG (bumped by Renovate) and one sha256 per
#   architecture. Renovate bumps the versions only, so update the matching
#   *_SHA256_* values from the project's release checksums when it does.
FROM debian:12-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS base

ARG JUST_VERSION=1.36.0
ARG TOFU_VERSION=1.13.1
ARG BWS_VERSION=2.1.0
ARG SOPS_VERSION=3.9.1
ARG GITLEAKS_VERSION=8.21.2
ARG ANSIBLE_CORE_VERSION=2.17.5
ARG ANSIBLE_POSIX_VERSION=1.5.4
ARG ANSIBLE_LINT_VERSION=24.9.2
ARG CHECK_JSONSCHEMA_VERSION=0.29.3

# Pinned sha256 digests of the downloaded release assets, per architecture.
ARG JUST_SHA256_AMD64=bc7c9f377944f8de9cd0418b11d2955adebfa25a488c0b5e3dd2d2c0e9d732da
ARG JUST_SHA256_ARM64=bb3886b15e2cbcb9c0eb19956297d36de4eaef45b89d3f5fa5d1fc4ed3b5b51d
ARG TOFU_SHA256_AMD64=8ccbc6f8ee21d2827715f3c6e08a9b3e0209b1e62057c05067ef117e047c1a80
ARG TOFU_SHA256_ARM64=b9614df40575cc3fc10a8a25025b7245d961da279f715ea3efff4ddae8e6938a
ARG BWS_SHA256_AMD64=f59ee150e42b82128d437087e9bac920053c6bfddcb960d20ce9386e5ac9bba6
ARG BWS_SHA256_ARM64=eb0f1ae61d1c3b74244d2841233276e05c77e8be4da197ed90fc6248387005e1
ARG SOPS_SHA256_AMD64=cd795109851c3a483bbaa66d15d19ddb2227ac0352b39e25d96b67d51edb6225
ARG SOPS_SHA256_ARM64=bc946fef11dbe199587adac567037b69374c4202f928ca138443539efc85b357

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        less \
        make \
        openssh-client \
        python3 \
        python3-venv \
        rsync \
        shellcheck \
        unzip; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    python3 -m venv "${VIRTUAL_ENV}"; \
    "${VIRTUAL_ENV}/bin/pip" install --upgrade pip; \
    "${VIRTUAL_ENV}/bin/pip" install \
        "ansible-core==${ANSIBLE_CORE_VERSION}" \
        "ansible-lint==${ANSIBLE_LINT_VERSION}" \
        "check-jsonschema==${CHECK_JSONSCHEMA_VERSION}" \
        "awscli==1.35.15" \
        "jsonschema==4.23.0" \
        "json5==0.9.25" \
        "pynacl==1.5.0" \
        "pyyaml==6.0.2" \
        "pytest==8.3.3"; \
    "${VIRTUAL_ENV}/bin/ansible-galaxy" collection install \
        "ansible.posix:==${ANSIBLE_POSIX_VERSION}"; \
    printf '%s\n' 'export PATH="/opt/venv/bin:$PATH"' > /etc/profile.d/venv.sh; \
    chmod 0644 /etc/profile.d/venv.sh

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) just_arch=x86_64;  tofu_arch=amd64; bws_arch=x86_64; sops_arch=amd64; gl_arch=x64; \
               just_sha="${JUST_SHA256_AMD64}"; tofu_sha="${TOFU_SHA256_AMD64}"; \
               bws_sha="${BWS_SHA256_AMD64}"; sops_sha="${SOPS_SHA256_AMD64}" ;; \
        arm64) just_arch=aarch64; tofu_arch=arm64; bws_arch=aarch64; sops_arch=arm64; gl_arch=arm64; \
               just_sha="${JUST_SHA256_ARM64}"; tofu_sha="${TOFU_SHA256_ARM64}"; \
               bws_sha="${BWS_SHA256_ARM64}"; sops_sha="${SOPS_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    tmp="$(mktemp -d)"; \
    verify() { echo "$2  $1" | sha256sum -c -; }; \
    \
    curl -fsSL -o "${tmp}/just.tar.gz" "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${just_arch}-unknown-linux-musl.tar.gz"; \
    verify "${tmp}/just.tar.gz" "${just_sha}"; \
    tar -xzf "${tmp}/just.tar.gz" -C "${tmp}"; \
    install -m 0755 "${tmp}/just" /usr/local/bin/just; \
    \
    curl -fsSL -o "${tmp}/tofu.zip" "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${tofu_arch}.zip"; \
    verify "${tmp}/tofu.zip" "${tofu_sha}"; \
    unzip -q "${tmp}/tofu.zip" -d "${tmp}/tofu"; \
    install -m 0755 "${tmp}/tofu/tofu" /usr/local/bin/tofu; \
    \
    curl -fsSL -o "${tmp}/bws.zip" "https://github.com/bitwarden/sdk/releases/download/bws-v${BWS_VERSION}/bws-${bws_arch}-unknown-linux-musl-${BWS_VERSION}.zip"; \
    verify "${tmp}/bws.zip" "${bws_sha}"; \
    unzip -q "${tmp}/bws.zip" -d "${tmp}/bws"; \
    install -m 0755 "${tmp}/bws/bws" /usr/local/bin/bws; \
    \
    curl -fsSL -o "${tmp}/sops" "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${sops_arch}"; \
    verify "${tmp}/sops" "${sops_sha}"; \
    install -m 0755 "${tmp}/sops" /usr/local/bin/sops; \
    \
    base="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}"; \
    gl="gitleaks_${GITLEAKS_VERSION}_linux_${gl_arch}.tar.gz"; \
    curl -fsSL -o "${tmp}/${gl}" "${base}/${gl}"; \
    curl -fsSL -o "${tmp}/gitleaks.checksums" "${base}/gitleaks_${GITLEAKS_VERSION}_checksums.txt"; \
    ( cd "${tmp}" && grep " ${gl}$" gitleaks.checksums | sha256sum -c - ); \
    tar -xzf "${tmp}/${gl}" -C "${tmp}"; \
    install -m 0755 "${tmp}/gitleaks" /usr/local/bin/gitleaks; \
    \
    rm -rf "${tmp}"

WORKDIR /work

# The developer toolbox image (`docker build toolbox`). No ENTRYPOINT here so
# contributors can run `docker run … <image> bash -lc …`; the published tool
# image adds the sealedstack CLI as its entrypoint.
FROM base AS toolbox

LABEL org.opencontainers.image.title="sealedstack-toolbox" \
      org.opencontainers.image.description="tofu, ansible, bws, just, check-jsonschema"
