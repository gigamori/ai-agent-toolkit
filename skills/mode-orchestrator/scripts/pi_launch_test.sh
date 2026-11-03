#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v node >/dev/null 2>&1 || { echo "node not on PATH" >&2; exit 2; }
if command -v cygpath >/dev/null 2>&1; then
  HERE="$(cygpath -m "$HERE")"
  SHELL_BIN="$(cygpath -m "$(command -v bash)")"
else
  SHELL_BIN="$(command -v bash)"
fi
MSYS_NO_PATHCONV=1 node - "$HERE" "$SHELL_BIN" <<'NODE'
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const here = process.argv[2];
const shell = process.argv[3];
const root = path.resolve(here, '../../..');
const tmp = path.join(root, 'tmp');
fs.mkdirSync(tmp, {recursive: true});
const dir = fs.mkdtempSync(path.join(tmp, 'pi-launch-test-'));
const doc = fs.readFileSync(path.join(here, '../references/harness-pi.md'), 'utf8').replaceAll('\r\n', '\n');
const matched = doc.match(/<!-- pi-cli-launch-line -->\s*```[^\n]*\n([\s\S]*?)\n```/);
assert(matched, 'the documented launch block must exist');
const prompt = ('日本語 "quoted" \'single\' $HOME $(printf SHOULD_NOT_RUN) `printf SHOULD_NOT_RUN` \\backslash\n').repeat(160) + 'PI_PROMPT_END';
let passed = 0;
let failed = 0;
let skipped = 0;
function check(name, body) {
  try { body(); passed++; console.log(`ok ${name}`); }
  catch (error) { failed++; console.error(`FAIL ${name}: ${error.stack}`); }
}
function run(command) {
  const result = spawnSync(shell, ['-c', command], {shell: false, cwd: dir, env: {...process.env, MSYS_NO_PATHCONV: '1'}, encoding: 'utf8', timeout: 45000});
  if (result.error) throw result.error;
  return result;
}
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
function prepare(name, kind, realRecorder = false, stream = 'stream-normal.jsonl', template = matched[1]) {
  const runDir = path.join(dir, name).replaceAll('\\', '/');
  fs.mkdirSync(`${runDir}/raw`, {recursive: true});
  fs.mkdirSync(`${runDir}/sessions`);
  const stem = `${runDir}/01-1`;
  const promptFile = `${stem}-prompt.md`;
  if (kind !== 'missing') fs.writeFileSync(promptFile, kind === 'empty' ? '' : prompt, 'utf8');
  const stub = `${runDir}/cli.js`;
  const fixture = path.join(here, 'fixtures', stream);
  fs.writeFileSync(stub, `const fs=require('node:fs');fs.writeFileSync(${JSON.stringify(`${stem}-argv.json`)},JSON.stringify({argv:process.argv.slice(2),depth:process.env.MODE_ORCH_DEPTH}),'utf8');process.stdout.write(fs.readFileSync(${JSON.stringify(fixture)},'utf8'));${realRecorder ? `const end=Date.now()+12000;const timer=setInterval(()=>{if(fs.existsSync(${JSON.stringify(`${stem}-pids.json`)})){clearInterval(timer);}else if(Date.now()>end){clearInterval(timer);process.exitCode=72;}},50);` : ''}`, 'utf8');
  const scripts = realRecorder ? here.replaceAll('\\', '/') : runDir;
  if (!realRecorder) fs.writeFileSync(`${runDir}/stop_observe.js`, `require('node:fs').writeFileSync(${JSON.stringify(`${stem}-recorder`)},'reached','utf8');`, 'utf8');
  const values = {
    '<run-dir>/<turn-key>-<attempt>-prompt.md': promptFile,
    '<the assembled prompt, verbatim>': kind === 'valid' ? prompt : 'fixture prompt',
    '<pi-cli-entry>': quote(stub),
    '<the turn\'s resolved model>': 'fixture-model',
    '<pi-scripts>': quote(scripts),
    '<pi-reply>': quote(path.join(here, 'pi_reply.js').replaceAll('\\', '/')),
    '<run-dir>': runDir,
    '<turn-key>': '01',
    '<attempt>': '1',
  };
  let command = template;
  for (const [key, value] of Object.entries(values)) command = command.replaceAll(key, value);
  return {stem, runDir, command};
}
try {
  for (const kind of ['missing', 'empty']) {
    check(`${kind} prompt refuses before CLI, recorder and extractor`, () => {
      const test = prepare(kind, kind);
      const result = run(test.command);
      assert.equal(result.status, 64, JSON.stringify(result));
      assert.match(result.stderr, /mode-orchestrator: prompt preflight failed/);
      assert.equal(result.stdout, '');
      assert.equal(fs.existsSync(`${test.stem}-argv.json`), false);
      assert.equal(fs.existsSync(`${test.stem}-recorder`), false);
      assert.equal(fs.existsSync(`${test.runDir}/raw/01-1.jsonl`), false);
    });
  }
  check('long UTF-8 prompt reaches one positional argument without shell evaluation', () => {
    const test = prepare('long', 'valid');
    const result = run(test.command);
    assert.equal(result.status, 0, JSON.stringify(result));
    assert.equal(result.stderr, '');
    const received = JSON.parse(fs.readFileSync(`${test.stem}-argv.json`, 'utf8'));
    assert.deepEqual(received.argv, ['-p', '--mode', 'json', '--model', 'fixture-model', '--no-skills', '--session-dir', `${test.runDir}/sessions`, prompt]);
    assert.equal(received.depth, '1');
    assert.equal(fs.existsSync(`${test.stem}-recorder`), true);
    const terminal = fs.readFileSync(path.join(here, 'fixtures/stream-normal.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line)).filter(event => event.type === 'turn_end').at(-1);
    const expected = terminal.message.content.filter(block => block.type === 'text').map(block => block.text).join('');
    assert(expected.length > 0);
    assert.equal(result.stdout, expected);
    assert.equal(result.stdout.includes('"type":"turn_end"'), false);
  });
  check('a stream without a terminal turn is rejected by the real extractor', () => {
    const test = prepare('invalid-stream', 'valid', false, 'stream-no-turn-end.jsonl');
    const result = run(test.command);
    assert.equal(result.status, 3, JSON.stringify(result));
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /no turn_end/);
  });
  check('removing both guards allows an empty-prompt child to start', () => {
    const template = matched[1].replace(/^PROMPT=.*?\n\s*printf .*?\n\s*exit 64\n}\n\[\[ -n .*?\n\s*printf .*?\n\s*exit 64\n}\n/ms, 'PROMPT=$(< "$PROMPT_FILE")\n');
    assert.notEqual(template, matched[1]);
    const test = prepare('guard-control', 'empty', false, 'stream-normal.jsonl', template);
    const result = run(test.command);
    assert.equal(result.status, 0, JSON.stringify(result));
    assert.equal(JSON.parse(fs.readFileSync(`${test.stem}-argv.json`, 'utf8')).argv.at(-1), '');
  });
  if (process.platform === 'win32') {
    check('the documented launch records real CLI and shell identities', () => {
      const test = prepare('real-pids', 'valid', true);
      const result = run(test.command);
      assert.equal(result.status, 0, JSON.stringify(result));
      assert.equal(result.stderr, '');
      const record = JSON.parse(fs.readFileSync(`${test.stem}-pids.json`, 'utf8'));
      assert.equal(record.processes.length, 2);
      assert(record.processes.some(item => item.image.toLowerCase() === 'node.exe'));
      for (const item of record.processes) { assert(item.pid > 0); assert(item.created); }
      const observed = run(`node ${quote(path.join(here, 'stop_observe.js').replaceAll('\\', '/'))} pi-cli --stem ${quote(test.stem)} --cause ended --deadline 60 --window 1`);
      assert.equal(observed.status, 0, JSON.stringify(observed));
      assert.equal(observed.stdout.trim(), 'CONFIRMED');
    });
    check('missing PID evidence stays unconfirmed and an invalid PID is refused', () => {
      const stem = path.join(dir, 'unrecorded').replaceAll('\\', '/');
      const observer = quote(path.join(here, 'stop_observe.js').replaceAll('\\', '/'));
      const invalid = run(`node ${observer} pids --stem ${quote(stem)} 0`);
      assert.equal(invalid.status, 64, JSON.stringify(invalid));
      assert.equal(fs.existsSync(`${stem}-pids.json`), false);
      const observed = run(`node ${observer} pi-cli --stem ${quote(stem)} --cause ended --deadline 60 --window 1`);
      assert.equal(observed.status, 1, JSON.stringify(observed));
      assert.equal(observed.stdout.trim(), 'UNCONFIRMED');
    });
  } else { skipped += 2; console.log('skip real Windows process identity checks'); }
} finally {
  fs.rmSync(dir, {recursive: true, force: true});
}
console.log(`passed ${passed} / ${passed + failed} (skipped ${skipped})`);
process.exitCode = failed ? 1 : 0;
NODE
