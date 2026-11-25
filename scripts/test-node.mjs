// Run tests against disposable state.
import {existsSync, readdirSync, mkdtempSync, rmSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const tests = join(root, 'tests/js');
const files = readdirSync(tests).filter(name => /^test[-_].*\.mjs$/.test(name)).sort();
const requested = process.argv.slice(2);
if (requested.some(name => !files.includes(name))) {
  console.error('Choose a test module from tests/js.');
  process.exit(2);
}
const selected = requested.length ? requested : files;
const dataRoot = mkdtempSync(join(tmpdir(), 'afterhours-node-tests-'));
const env = {...process.env, AFTERHOURS_DATA_ROOT: dataRoot,
  AFTERHOURS_ANALYSIS_REPORTS_DIR: join(dataRoot, '.runtime/audio-analysis'), PYTHONDONTWRITEBYTECODE: '1'};
for (const key of Object.keys(env)) if (/(_KEY|_TOKEN|_SECRET|_PASSWORD|_PROXY)$/i.test(key) || key === 'AFTERHOURS_RECAP_FIXTURE') delete env[key];
const localPython = join(root, '.venv/bin/python');
if (!env.AFTERHOURS_TEST_PYTHON && existsSync(localPython)) env.AFTERHOURS_TEST_PYTHON = localPython;
if (!env.DJ_PYTHON && env.AFTERHOURS_TEST_PYTHON) env.DJ_PYTHON = env.AFTERHOURS_TEST_PYTHON;
let status = 1;
try {
  const result = spawnSync(process.execPath, ['--test', ...selected.map(name => join(tests, name))], {cwd: root, env, stdio: 'inherit'});
  if (result.error) console.error(result.error.message);
  status = result.status ?? 1;
} finally { rmSync(dataRoot, {recursive: true, force: true}); }
process.exit(status);
