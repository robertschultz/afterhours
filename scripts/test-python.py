"""Run isolated unittest modules, excluding standalone browser/live harnesses."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_TESTS = ROOT / 'tests/python'
sys.path.insert(0, str(PYTHON_TESTS))
sys.dont_write_bytecode = True
from test_isolation import STATIC_FIXTURES, test_environment


def discover():
    modules, excluded = [], []
    for path in sorted(PYTHON_TESTS.glob('test_*.py')):
        tree = ast.parse(path.read_bytes())
        is_unittest = any(isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Attribute) and base.attr == 'TestCase' or
            isinstance(base, ast.Name) and base.id == 'TestCase'
            for base in node.bases) for node in tree.body)
        (modules if is_unittest else excluded).append(path.stem)
    return modules, excluded


def hashes():
    paths = sorted([*(ROOT / 'src').rglob('*.py'), *PYTHON_TESTS.glob('*.py'), *(ROOT / 'scripts').glob('*.py')])
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def close_test_resources():
    server = sys.modules.get('afterhours.server')
    if server is None:
        return
    for resource in (server.ROOM_REQUESTS, server.GUEST_COORDINATOR, server.TRACK_ARTWORK,
                     server.RUN_ARCHIVE, server.DJ_SESSION.crate, server.GUEST_LEDGER):
        if resource is not None:
            resource.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('modules', nargs='*', help='Optional unittest module names from this checkout')
    parser.add_argument('--output-dir', type=Path, help='Write suite manifest and summary here')
    parser.add_argument('--list', action='store_true', help='List selected tests without importing the engine')
    args = parser.parse_args()
    available, excluded = discover()
    modules = args.modules or available
    if any(module not in available for module in modules):
        parser.error('Choose unittest modules listed by --list; standalone harnesses are excluded.')
    if args.list:
        print('\n'.join(modules))
        return 0
    started = time.monotonic()
    before = hashes()
    manifest = {'modules': modules, 'moduleCount': len(modules),
                'excludedNonUnittestHarnesses': excluded, 'python': sys.version.split()[0],
                'sourceFileSHA256': before,
                'staticMediaFixtures': {name: hashlib.sha256((ROOT / 'src/afterhours/web/media' / name).read_bytes()).hexdigest()
                                        for name in STATIC_FIXTURES},
                'scope': 'Disposable data and native metadata roots; only test-owned loopback peers; no live native/provider actions'}
    with test_environment():
        try:
            suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        finally:
            close_test_resources()
    after = hashes()
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    summary = {'moduleCount': len(modules), 'testsRun': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
               'successful': result.wasSuccessful() and not changed, 'changedDuringRun': changed,
               'elapsedSeconds': round(time.monotonic() - started, 3), 'scope': manifest['scope']}
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'backend-suite-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (args.output_dir / 'backend-suite-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
