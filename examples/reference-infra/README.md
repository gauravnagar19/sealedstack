# Sample infra config for the reference app

A minimal infra `config/` that places `examples/app-template` the way a real
infra repo would (one server, one owner-only app). CI validates the reference app
against it without any GitHub access:

```sh
SEALEDSTACK_CONFIG=examples/reference-infra/config \
SEALEDSTACK_LOCAL_APPS=example-owner/example=examples/app-template \
python3 scripts/validate.py
```

`SEALEDSTACK_LOCAL_APPS` maps an app repo to a local checkout, so the manifest
and compose file are read from disk instead of through the GitHub API.
