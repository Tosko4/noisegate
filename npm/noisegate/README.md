# noisegate-hermes on npm

This npm package is only a thin installer wrapper for the Python package [`noisegate-hermes`](https://github.com/Tosko4/noisegate).

Noisegate itself is a Python Hermes Agent plugin and CLI. The Python package is canonical.

## Install or update Noisegate for Hermes

```bash
npx -p noisegate-hermes noisegate install-hermes
```

Use the same command for first install and updates. It finds `hermes` on `PATH`, verifies the launcher resolves to a Hermes Python console script or supported Hermes shim inside a virtual environment, installs the matching `noisegate-hermes` Python package there, enables the `noisegate` plugin, removes any stale disabled entry, and runs `noisegate doctor`. Native Windows launchers are opaque binaries, so Noisegate validates those by requiring an adjacent virtual-environment Python.

Preview the exact commands first. Dry-run mode does not run the install/enable/doctor commands and does not restart or reload Hermes:

```bash
npx -p noisegate-hermes noisegate install-hermes --dry-run
```

If your npm client does not resolve the single-bin shortcut, use the explicit bin name:

```bash
npx -p noisegate-hermes noisegate-hermes-installer install-hermes
```

The wrapper delegates to:

```bash
uvx --from noisegate-hermes==<this npm package version> noisegate install-hermes
```

If Hermes is running as a long-lived gateway/service, restart or reload that Hermes process through your normal maintenance flow after installing so the plugin/config change is picked up. Avoid interrupting in-flight agent work.

## Security posture

- No `postinstall` scripts.
- No bundled Python implementation.
- No long-lived npm token is required when published through npm trusted publishing.
- Publish provenance should be enabled in CI.
- The package exists as the public `noisegate-hermes` npm installer wrapper and provides a safe install entrypoint.
