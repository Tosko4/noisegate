# noisegate-hermes on npm

This npm package is only a thin installer wrapper for the Python package [`noisegate-hermes`](https://github.com/Tosko4/noisegate).

Noisegate itself is a Python Hermes Agent plugin and CLI. The Python package is canonical.

## Install Noisegate for Hermes

```bash
npx -p noisegate-hermes noisegate install-hermes
```

If your npm client does not resolve the single-bin shortcut, use the explicit bin name:

```bash
npx -p noisegate-hermes noisegate-hermes-installer install-hermes
```

The wrapper delegates to:

```bash
uvx --from noisegate-hermes==<this npm package version> noisegate install-hermes
```

## Security posture

- No `postinstall` scripts.
- No bundled Python implementation.
- No long-lived npm token is required when published through npm trusted publishing.
- Publish provenance should be enabled in CI.
- The package exists as the public `noisegate-hermes` npm installer wrapper and provides a safe install entrypoint.
