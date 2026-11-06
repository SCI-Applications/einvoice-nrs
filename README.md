# einvoice-nrs — the NRS toolkit

The NRS MBS toolkit used by our SI ([einvoice-si](https://github.com/SCI-Applications/einvoice-si))
and our APP ([einvoice-app](https://github.com/SCI-Applications/einvoice-app)). It holds only what
both services need from NRS, so neither depends on the other. Public: no secrets, no database,
no real data.

- Whole project, NRS facts, rules, timeline: `einvoicing_context.md`
- Decisions and progress: `log.md` (start with its "Start here" block)

## Folder structure

```
src/einvoice_nrs/
  client.py          the NRS API client: x-api-key / x-api-secret, NRS's error shape, amounts
                     sent as exact JSON numbers, signs exactly the bytes it is given
  codes/             NRS's invoice code lists (JSON) + manifest.json (counts and sha256 checked
                     on load) + the loader
  fake/              a fake NRS (FastAPI) for tests, offline work and the test server:
                     __init__.py (the routes; GET /fake-nrs says "I am the fake"),
                     __main__.py (run it), config.json (its test business and key pair)
tests/               the client against the fake; the code lists
scripts/             fetch_nrs_code_lists.py (download the lists from the sandbox; review, commit),
                     nrs_sandbox_probe.py (ask the sandbox how it behaves; never signs)
render.yaml          the fake NRS as a Render web service
```

## Using it from a service

The services pin an exact version, so a change here reaches them only when they move the pin:

```toml
[project]
dependencies = ["einvoice-nrs"]

[dependency-groups]
dev = ["einvoice-nrs[fake]"]

[tool.uv.sources]
einvoice-nrs = { git = "https://github.com/SCI-Applications/einvoice-nrs", rev = "<commit>" }
```

## The fake NRS

- **On the Mac** (port 8090, the packaged `config.json`): `uv run python -m einvoice_nrs.fake`
- **On Render** (for the test server): Render dashboard → New → Blueprint → this repo; it reads
  `render.yaml`. Set `FAKE_NRS_API_KEY` / `FAKE_NRS_API_SECRET` in Render (never in git): the
  fake then accepts only that pair, which the server also holds in Vault at `secret/nrs/fake`.
  Check it: `GET https://<address>/fake-nrs` → `{"fake_nrs": true, ...}`. The free plan sleeps
  after ~15 minutes idle; the first call then takes up to a minute.
- It answers entity, irn/validate, validate, sign and confirm in NRS's shapes; the config can
  reject IRNs, simulate failures or delays. It never talks to NRS.

**Security:** the key pair in `config.json` is public (this repo is public), so any public copy
must set its own pair in Render; rotate it in Render and Vault together if it leaks. The key and
renewal rules for the whole system are in `einvoicing_context.md` §6a.

## Working on it

```bash
uv sync
uv run pytest
uvx pre-commit run --all-files
```

**Releasing:** bump `version` in `pyproject.toml` and `__version__` in `src/einvoice_nrs/__init__.py`,
PR to `main`, Muhammad merges (and tags `vX.Y.Z` in GitHub), then move the pin in each service.
