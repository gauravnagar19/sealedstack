# sealedstack upgrade PR template

The upgrade bot (or you) opens a PR in the **infra repo** that changes exactly
one line and fills in this body. Nothing upgrades on its own.

---

## sealedstack {{from}} → {{to}}

This PR bumps `sealedstack.version` (and the workflow ref in
`.github/workflows/sealedstack.yml`, kept in lockstep by the version-pin check).

### What changed

{{release_notes}}

### Impact

`plan` runs automatically against the new image and shows any change here:

```
{{plan_diff}}
```

### Advisories

{{advisories}}

### Roll back

Revert this PR. The version pin is the only thing that changed, so a revert
returns the infra repo to {{from}} with no other edits.

---

Merge when the plan is empty or understood, then run **Up** and approve in the
`production` environment.
