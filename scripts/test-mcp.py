#!/usr/bin/env python3
"""Required SDK transport suite; missing/wrong SDK is a failure, never a skip."""
from importlib.metadata import version
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0, str(ROOT / 'tests/python'))
sys.path.insert(0, str(ROOT / 'tests/mcp'))
from test_isolation import test_environment


def main():
    if version('mcp')!='2.3.0':raise RuntimeError('Install the optional hash-locked MCP requirements before testing.')
    with test_environment():
        suite=unittest.defaultTestLoader.loadTestsFromName('test_stdio')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        server=sys.modules.get('afterhours.server')
        if server:
            for item in (server.ROOM_REQUESTS,server.GUEST_COORDINATOR,server.TRACK_ARTWORK,
                         server.RUN_ARCHIVE,server.DJ_SESSION.crate,server.GUEST_LEDGER):
                if item:item.close()
    print(json.dumps({'sdk':version('mcp'),'python':sys.version.split()[0],'testsRun':result.testsRun,
                      'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
                      'scope':'Real SDK stdio pipes; disposable fake engine/provider; network confined to test-owned loopback'}))
    return 0 if result.wasSuccessful() and not result.skipped and result.testsRun>=5 else 1


if __name__=='__main__':raise SystemExit(main())
