#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const script = join(here, 'bin', 'noisegate-hermes-installer.js');
const packageJson = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
const pythonPackageSpec = `noisegate-hermes==${packageJson.version}`;

function run(args, env = {}) {
  return spawnSync(process.execPath, [script, ...args], {
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

const help = run(['--help']);
if (help.status !== 0 || !help.stdout.includes(`uvx --from ${pythonPackageSpec} noisegate install-hermes`)) {
  console.error(help.stdout);
  console.error(help.stderr);
  throw new Error('help output did not document the delegated uvx command');
}

const delegated = run(['install-hermes', '--dry-run'], { NOISEGATE_UVX: '/bin/echo' });
if (delegated.status !== 0) {
  console.error(delegated.stdout);
  console.error(delegated.stderr);
  throw new Error('delegation smoke failed');
}

const expected = `--from ${pythonPackageSpec} noisegate install-hermes --dry-run`;
if (delegated.stdout.trim() !== expected) {
  console.error(`expected: ${expected}`);
  console.error(`actual: ${delegated.stdout.trim()}`);
  throw new Error('delegated uvx arguments changed');
}
