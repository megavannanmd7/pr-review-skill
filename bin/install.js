#!/usr/bin/env node
'use strict';

/**
 * Install the pr-review skill for one or more AI coding tools.
 *
 * Node replacement for install.sh / install.ps1: same destinations, same
 * messages, one script instead of two platform-specific ones. Copies core/
 * to ~/.pr-review-skill/core and drops a small adapter into each selected
 * tool's own skill/command directory. Every adapter points back at that one
 * core, so there is never more than one copy of the rubric or the scripts.
 *
 *   npx pr-review-skill install --all
 *   npx pr-review-skill install --platform claude-code,cursor
 *   npx pr-review-skill doctor
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const HOME = os.homedir();
const CORE_DEST = path.join(HOME, '.pr-review-skill', 'core');
const KNOWN_PLATFORMS = ['antigravity', 'claude-code', 'cursor', 'gemini-cli'];

function usage() {
  console.log('Usage: pr-review-skill install --all');
  console.log('       pr-review-skill install --platform antigravity,claude-code,cursor,gemini-cli');
  console.log('       pr-review-skill doctor');
}

function parseArgs(argv) {
  const [cmd, ...rest] = argv;
  const opts = { all: false, platform: null };
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (a === '--all') {
      opts.all = true;
    } else if (a === '--platform') {
      opts.platform = rest[++i];
    } else if (a.startsWith('--platform=')) {
      opts.platform = a.slice('--platform='.length);
    } else {
      console.error(`unknown argument: ${a}`);
      process.exit(2);
    }
  }
  return { cmd, opts };
}

// Skip __pycache__ dirs and .pyc files -- they're local build artifacts, not
// part of the skill, and copying them stale into every install is pointless.
function skipCacheFiles(srcPath) {
  const parts = srcPath.split(path.sep);
  return !parts.includes('__pycache__') && !srcPath.endsWith('.pyc');
}

function copyDir(src, dest) {
  fs.rmSync(dest, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.cpSync(src, dest, { recursive: true, filter: skipCacheFiles });
  console.log(`  -> ${dest}`);
}

function copyFile(src, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
  console.log(`  -> ${dest}`);
}

function installCore() {
  console.log('Installing shared core...');
  copyDir(path.join(ROOT, 'core'), CORE_DEST);
}

function installAdapter(platform) {
  console.log(`Installing adapter: ${platform}`);
  switch (platform) {
    case 'antigravity': {
      // Antigravity indexes skills as a folder containing SKILL.md, in two places
      // (IDE and CLI).
      const dirs = [
        path.join(HOME, '.gemini', 'config', 'skills', 'pr-review'),
        path.join(HOME, '.gemini', 'antigravity-cli', 'skills', 'pr-review'),
      ];
      for (const d of dirs) {
        fs.rmSync(d, { recursive: true, force: true });
        copyFile(path.join(ROOT, 'adapters', 'antigravity', 'SKILL.md'), path.join(d, 'SKILL.md'));
      }
      break;
    }
    case 'claude-code': {
      const d = path.join(HOME, '.claude', 'skills', 'pr-review');
      fs.rmSync(d, { recursive: true, force: true });
      copyFile(path.join(ROOT, 'adapters', 'claude-code', 'SKILL.md'), path.join(d, 'SKILL.md'));
      break;
    }
    case 'cursor':
      copyFile(
        path.join(ROOT, 'adapters', 'cursor', 'pr-review.md'),
        path.join(HOME, '.cursor', 'commands', 'pr-review.md'),
      );
      break;
    case 'gemini-cli':
      copyFile(
        path.join(ROOT, 'adapters', 'gemini-cli', 'pr-review.toml'),
        path.join(HOME, '.gemini', 'commands', 'pr-review.toml'),
      );
      break;
    default:
      console.error(`unknown platform: ${platform} (expected one of: ${KNOWN_PLATFORMS.join(', ')})`);
      process.exit(2);
  }
}

function tryRun(cmd, args) {
  try {
    return { ok: true, out: execFileSync(cmd, args, { encoding: 'utf8' }) };
  } catch (err) {
    return { ok: false, err };
  }
}

function doctor() {
  console.log('Checking prerequisites...');

  const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
  const py = tryRun(pyCmd, ['--version']);
  if (py.ok) {
    console.log(`  ${pyCmd}: ${py.out.trim()} (needed to run the skill, not to install it)`);
  } else {
    console.warn(`  WARNING: ${pyCmd} not found on PATH. Needed at review time (pr_fetch.py/pr_post.py), not for this install step.`);
  }

  const gh = tryRun('gh', ['--version']);
  if (gh.ok) {
    console.log(`  gh: ${gh.out.split('\n')[0]}`);
    const auth = tryRun('gh', ['auth', 'status']);
    if (!auth.ok) console.warn('  WARNING: gh is installed but not authenticated. Run: gh auth login');
  } else {
    console.warn('  WARNING: GitHub CLI not found. Install it (winget install --id GitHub.cli / brew install gh), then run: gh auth login');
  }
}

function main() {
  if (!fs.existsSync(path.join(ROOT, 'core', 'REVIEW.md'))) {
    console.error('Cannot find core/REVIEW.md next to this package -- the install looks corrupted.');
    process.exit(1);
  }

  const { cmd, opts } = parseArgs(process.argv.slice(2));

  if (cmd === 'doctor') {
    doctor();
    return;
  }

  if (cmd !== 'install') {
    usage();
    process.exit(cmd ? 2 : 1);
  }

  let platforms;
  if (opts.all) {
    platforms = KNOWN_PLATFORMS;
  } else if (opts.platform) {
    platforms = opts.platform.split(',').map((s) => s.trim()).filter(Boolean);
  } else {
    usage();
    process.exit(1);
  }

  const unknown = platforms.filter((p) => !KNOWN_PLATFORMS.includes(p));
  if (unknown.length) {
    console.error(`unknown platform(s): ${unknown.join(', ')} (expected one or more of: ${KNOWN_PLATFORMS.join(', ')})`);
    process.exit(2);
  }

  installCore();
  for (const p of platforms) installAdapter(p);

  console.log('');
  doctor();
  console.log('');
  console.log('Done. Restart the tool, then run:  /pr-review 842 paxiai-event-processor');
}

main();
