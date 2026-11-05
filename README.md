# einvoice-nrs

The NRS MBS toolkit used by our SI (`einvoice-platform`) and our APP (`einvoice-app`). It holds
only what both services need from NRS, so neither depends on the other:

| Part | What it is |
|---|---|
| `einvoice_nrs.client` | The NRS API client (`x-api-key` / `x-api-secret`, NRS's error shape, exact amounts). |
| `einvoice_nrs.codes` | NRS's invoice code lists, checked against `manifest.json` when loaded. |
| `einvoice_nrs.fake` | A fake NRS for tests and offline work (extra `fake`; never deployed). |
| `scripts/fetch_nrs_code_lists.py` | Downloads the code lists from the NRS sandbox (review the diff, then commit). |
| `scripts/nrs_sandbox_probe.py` | Asks the NRS sandbox how it really behaves; never signs. |

## Using it from a service

The services pin a released version (a git tag), so a change here reaches them only when they
move the pin:

```toml
[project]
dependencies = ["einvoice-nrs"]

[dependency-groups]
dev = ["einvoice-nrs[fake]"]

[tool.uv.sources]
einvoice-nrs = { git = "https://github.com/SCI-Applications/einvoice-nrs", tag = "v0.1.0" }
```

Run the fake NRS from a service (port 8090, the packaged `config.json`):

```bash
uv run python -m einvoice_nrs.fake
```

## Working on it

```bash
uv sync
uv run pytest
uvx pre-commit run --all-files
```

Releasing: change `version` in `pyproject.toml` and `__version__` in `src/einvoice_nrs/__init__.py`,
merge to `main`, tag the merged commit `vX.Y.Z`, then move the pin in each service.
