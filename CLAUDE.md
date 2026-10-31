# CLAUDE.md

> **For people and agents changing sealedstack itself.** If you are setting up or operating an infra
> repository that *uses* sealedstack, this file is not for you: read the `AGENTS.md` in your infra repo and
> [run it with an agent](docs/run-with-an-agent.md). Commands here (`just ...`) are for contributors.


See **[AGENTS.md](AGENTS.md)** for all guidance on working with this repository
(project overview, layout, the provider registry, how to add a provider, and the
validation gate).

Quick reference:

```sh
just validate     # schema + registry + compose lint
just providers    # browse the provider registry
just doctor       # required secrets/tools for your selected providers
```
