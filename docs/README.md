# Documentation

User and design documentation. See the project [README](../README.md) to get
started, and [AGENTS.md](../AGENTS.md) if you are extending the project.

## Get started

- [Getting started](getting-started.md): from an empty repo to a running app, with zero installs.
- [Run it with an agent](run-with-an-agent.md): who does what when an AI agent helps, phase by phase.
- [Prerequisites](prerequisites.md): the accounts, credentials and exact secret names.
- [Known limitations and caveats](limitations.md) — what does not work yet, and why.

## Configure and operate

- [Choosing a stack](choosing-a-stack.md) — the golden path, how the project guides each choice, and what is not checked.
- [Providers](providers.md) — the slot registry and how to plug in a provider.
- [Config repository](config-repo.md) — keep `config/` in a private repo (`SEALEDSTACK_CONFIG`).
- [Infra repo and the tool image](infra-repo.md) — the three-repo model, `bin/sealedstack`, upgrades.
- [CI/CD](ci-cd.md) — releases from GitHub, GitLab, Gitea and others.
- [Netcup firewall](netcup-firewall.md) — the manual rule table for the Netcup SCP adapter.

## Design

- [Architecture](architecture.md) — the detailed implementation-level design.
- [Design spec](spec.md) — the original architecture and contracts.
- [Decision log](decisions.md) — what was decided and why.
- [User experience (final)](user-experience.md) — every journey, with mockups, commands and defaults.

## Contributing

- [Development](development.md) — repo layout, tooling and the CI pipeline.
