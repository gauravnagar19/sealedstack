# sealedstack — single entrypoint (spec section 10).
# Every recipe runs inside the toolbox container; only Docker is needed on the host.
set shell := ["bash", "-euo", "pipefail", "-c"]

workdir := justfile_directory()
toolbox_image := "sealedstack-toolbox:" + env_var_or_default("SEALEDSTACK_TOOLBOX_TAG", "dev")
tool_image := "sealedstack-tool:" + env_var_or_default("SEALEDSTACK_TOOL_TAG", "dev")

# Env passed through into the toolbox when present in the invoking shell.
docker_env := "-e BWS_ACCESS_TOKEN -e GH_TOKEN -e CLOUDFLARE_API_TOKEN -e CLOUDFLARE_ACCOUNT_ID -e ROOT_PASSWORD -e SEALEDSTACK_CONFIG"
# Mount the SSH agent so bootstrap and site can use your key.
# Docker Desktop for macOS cannot bind-mount a host Unix socket; use its
# host-services socket instead (point SSH_AUTH_SOCK at Bitwarden's agent first).
ssh_mount := if os() == "macos" {
  "-v /run/host-services/ssh-auth.sock:/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent"
} else if env_var_or_default("SSH_AUTH_SOCK", "") != "" {
  "-v " + env_var("SSH_AUTH_SOCK") + ":/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent"
} else { "" }
docker_base := "docker run --rm --init -i " + docker_env + " " + ssh_mount + " -v \"" + workdir + ":/work\" -w /work"

default:
    @just --list

# Build the toolbox image (tofu, ansible, bws, just, check-jsonschema).
toolbox-build:
    docker build -t {{toolbox_image}} toolbox

# Build the tool image (toolbox + sealedstack code + CLI entrypoint).
tool: toolbox-build
    docker build -f Dockerfile -t {{tool_image}} --build-arg TOOLBOX_IMAGE={{toolbox_image}} .

# Alias for `just tool`: build the versioned tool image.
toolbox-image: tool

# Open an interactive shell inside the toolbox.
toolbox-shell: toolbox-build
    {{docker_base}} {{toolbox_image}} bash

# Schema-check config/, render the app manifests, and lint the compose rules.
validate: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/validate.py"

# Preflight: verify config, secrets, versions and provider access (engine `check`).
check: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=engine python3 -m sealedstack check"

# Run the engine CLI directly, e.g. `just ss debug providers`.
ss +args: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=engine python3 -m sealedstack {{args}}"

# Run the test suite (bats + pytest).
test: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "tests/run.sh"

# Render app manifests into .build/ (spec section 5.5).
render: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/render.py"

# Run once: create the state bucket (local state) and store its credentials.
state-init: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.stateinit"

# Mint a mesh auth key, run bootstrap.yml, then site.yml; print the firewall table.
# The public IP is a bootstrap secret (SERVER_IP_<NAME>), never an argument (D-049).
server-up name: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.server up {{name}}"

# Re-run site.yml (drift correction, upgrades).
server-sync name="": toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.server sync {{name}}"

# Render, then plan the edge stack, then the platform stack.
plan: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/validate.py && python3 scripts/render.py && python3 scripts/mesh-policy.py && PYTHONPATH=/work/engine python3 -m sealedstack.buildenv && PYTHONPATH=/work/engine python3 -m sealedstack.userauth && PYTHONPATH=/work/engine python3 -m sealedstack.edge plan && PYTHONPATH=/work/engine python3 -m sealedstack.platform plan"

# Render, then apply the edge stack, then the platform stack.
apply: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/validate.py && python3 scripts/render.py && python3 scripts/mesh-policy.py && PYTHONPATH=/work/engine python3 -m sealedstack.buildenv && PYTHONPATH=/work/engine python3 -m sealedstack.userauth && PYTHONPATH=/work/engine python3 -m sealedstack.edge apply && PYTHONPATH=/work/engine python3 -m sealedstack.platform apply && PYTHONPATH=/work/engine python3 -m sealedstack.postapply"

# Restore the latest (or a named) backup into the app's primary database.
# Refuses a non-empty database unless FORCE=1 (see sealedstack.db).
app-restore app backup_id="": toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.db restore {{app}} {{backup_id}}"

# End-to-end test on a throwaway server (creates a small VM, runs the flow, destroys it).
e2e: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.e2e"

# Restore the latest backup into a throwaway database and check it (v0.3).
db-drill app table="": toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.db drill {{app}} {{table}}"

# List the provider registry (all slots, or one: compute mesh ingress gate secrets backup platform user_auth ci).
providers slot="": toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/registry.py list {{slot}}"

# Report the verified-provider ratio and whether the v1.0 gate is met (>= 50%).
release-status: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/registry.py release-status"

# Check tokens, permissions, zone and ACL, and that provider requirements are met.
doctor: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "python3 scripts/registry.py check && python3 scripts/doctor.py"

# Add an app to config/apps.yaml and validate it.
app-add repo host access server="":
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.appadd --repo {{repo}} --host {{host}} --access {{access}}{{ if server != '' { ' --server ' + server } else { '' } }}"

# Publish deploy settings to each app repo (GitHub variables + secrets).
publish-app-settings:
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.publishsettings"

# Write secrets to a SOPS-encrypted file (slots: secrets: sops).
secrets-write: toolbox-build
    {{docker_base}} {{toolbox_image}} bash -lc "PYTHONPATH=/work/engine python3 -m sealedstack.sopssecrets"

# Interactive wizard that writes config/.
init: toolbox-build
    docker run --rm -it {{docker_env}} -v "{{workdir}}:/work" -w /work {{toolbox_image}} bash -lc "python3 scripts/init.py"
