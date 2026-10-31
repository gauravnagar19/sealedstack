# Documentation

These pages are for **using** sealedstack: setting up an infra repo, running it, and working
with an AI agent while you do. The way sealedstack is built, and why, is kept apart in
[maintainers/](maintainers/README.md); read that only if you are changing sealedstack itself.

**New here?** Read, in this order: [getting started](getting-started.md), then
[prerequisites](prerequisites.md). Working with an agent? Add
[run it with an agent](run-with-an-agent.md).

## Get started

- [Getting started](getting-started.md): from an empty repo to a running app, with zero installs.
- [Run it with an agent](run-with-an-agent.md): who does what when an AI agent helps, phase by phase.
- [Prerequisites](prerequisites.md): the accounts, credentials and exact secret names.
- [Known limitations and caveats](limitations.md): what does not work yet, and why.

## Choose and configure

- [Choosing a stack](choosing-a-stack.md): the golden path, how the project guides each choice, and what is not checked.
- [Providers](providers.md): the slot catalog, status and per-provider setup.
- [Config repository](config-repo.md): keep `config/` in a private repo (`SEALEDSTACK_CONFIG`).
- [Infra repo and the tool image](infra-repo.md): the three-repo model, `bin/sealedstack`, upgrades.
- [CI/CD](ci-cd.md): releases from GitHub, GitLab, Gitea and others.
- [Netcup firewall](netcup-firewall.md): the manual rule table for the Netcup SCP adapter.
- [Netcup reinstall](netcup-reinstall.md): wipe and reinstall a Netcup server from CI.

## Reference

- [User experience (final)](user-experience.md): every journey, with mockups, commands and defaults.

## For maintainers

[maintainers/](maintainers/README.md): architecture, design spec, decision log, development notes and
how to add a provider. Not needed to use sealedstack.
