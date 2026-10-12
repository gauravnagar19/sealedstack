# Contributing

Thanks for helping. sealedstack is at **0.5 (pre-release)**; what is validated
and what's next are tracked in the private development repo.

## Ground rules

- **Never commit secrets.** `config/` holds no secrets (D-008); secrets live in
  Bitwarden. gitleaks runs in CI and as a pre-commit hook.
- **No user-specific values in generic code.** Admin user, domains and tokens
  come from `config/` and the registry.
- **Keep the layering:** OpenTofu never SSHes into a host; Ansible never calls a
  SaaS API except to fetch secrets.
- **One provider per slot until a second is needed** (D-012) — but the registry
  is deliberately broad; new providers are welcome as long as they validate.
- **Personal data must not reach git.** Commit/tag identity must be a GitHub
  no-reply address; `scripts/publish-check.sh --strict --metadata` fails on any
  other identity, and a `pre-push` hook blocks it (see below).

## Install the git hooks

Run this once after cloning (it symlinks the hooks into `.git/hooks/`):

```sh
scripts/install-hooks.sh
```

The `pre-push` hook refuses to push any ref other than `main`, `feat/*` and
`v*` tags, and refuses any commit or tag whose author/committer/tagger email is
not `@users.noreply.github.com` (or a known CI bot). It protects the clean
history and the no-reply identity; don't bypass it with `--no-verify`.

## Before you open a PR

Run the validation gate (everything is in `AGENTS.md`):

```sh
python3 scripts/registry.py check
python3 scripts/render.py --allow-unresolved && python3 scripts/validate.py
tofu fmt -check -recursive tofu
for s in state edge platform compute; do
  tofu -chdir=tofu/stacks/$s init -backend=false >/dev/null && tofu -chdir=tofu/stacks/$s validate
done
ansible-lint
```

- Adding a provider? Follow the recipes in [AGENTS.md](AGENTS.md); a registry
  entry plus a module/role/adapter, wired, with samples and secret names.
- Keep commit messages imperative and scoped (see `git log`).

## Development

- [docs/development.md](docs/development.md) — layout, tooling, the CI pipeline.
- [AGENTS.md](AGENTS.md) — the provider registry and how to extend it.
- [docs/architecture.md](docs/architecture.md) — how the pieces fit.

## Reporting issues

Include the `just validate` output, `just release-status`, your `config/` slots
(redact values), and the provider versions you selected.
