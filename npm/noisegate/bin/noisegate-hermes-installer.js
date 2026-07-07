#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const packageJson = JSON.parse(readFileSync(join(here, '..', 'package.json'), 'utf8'));
const pythonPackageSpec = `noisegate-hermes==${packageJson.version}`;

function help() {
  console.log(`Noisegate npm installer wrapper

This npm package is a thin installer for the Python package noisegate-hermes.
The Python package remains the canonical Noisegate implementation.

Usage:
  npx -p noisegate-hermes noisegate install-hermes [--dry-run] [noisegate install-hermes options]
  npx -p noisegate-hermes noisegate-hermes-installer install-hermes [options]

Security model:
  - no postinstall scripts
  - no bundled Python code
  - delegates to: uvx --from ${pythonPackageSpec} noisegate install-hermes ...
`);
}

if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
  help();
  process.exit(0);
}

if (args[0] !== 'install-hermes') {
  console.error('noisegate npm wrapper only supports: install-hermes');
  console.error('For the full CLI, install the Python package: noisegate-hermes');
  process.exit(2);
}

const uvx = process.env.NOISEGATE_UVX || 'uvx';
const result = spawnSync(
  uvx,
  ['--from', pythonPackageSpec, 'noisegate', 'install-hermes', ...args.slice(1)],
  { stdio: 'inherit' },
);

if (result.error) {
  if (result.error.code === 'ENOENT') {
    console.error('uvx was not found. Install uv first: https://docs.astral.sh/uv/');
  } else {
    console.error(`failed to run uvx: ${result.error.message}`);
  }
  process.exit(127);
}

process.exit(result.status ?? 1);
