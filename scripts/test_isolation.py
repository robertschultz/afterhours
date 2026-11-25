"""Test-only data and transport boundaries. Never imported by production code."""
from contextlib import ExitStack, contextmanager
import errno
import os
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import patch

from afterhours.paths import SHARED_WEB
STATIC_FIXTURES = ('rivet-demo.mp4', 'nova-demo.mp4')
PROTECTED_PORTS = {7474, 7475, 8765, 8768}
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


@contextmanager
def test_environment():
    """Create disposable storage before imports and allow test-owned peers only.

    Ports become eligible only after this process successfully binds them.
    Reserved test ports remain eligible for its synthetic child services.
    Installed native/service ports are always blocked, as is off-host traffic.
    """
    with tempfile.TemporaryDirectory(prefix='afterhours-python-tests-') as directory, ExitStack() as stack:
        root = Path(directory)
        media = root / 'web/media'
        media.mkdir(parents=True)
        for name in STATIC_FIXTURES:
            shutil.copyfile(SHARED_WEB / 'media' / name, media / name)
        home = root / 'home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.upper().endswith(('_KEY', '_TOKEN', '_SECRET', '_PASSWORD', '_PROXY'))
               and key not in {'AFTERHOURS_RECAP_FIXTURE', 'AFTERHOURS_TRACK_SECONDS', 'AFTERHOURS_CURATION_MINUTES'}
               and not key.startswith('AFTERHOURS_PLANNER_')}
        env.update(AFTERHOURS_DATA_ROOT=str(root),
                   AFTERHOURS_ANALYSIS_REPORTS_DIR=str(root / '.runtime/audio-analysis'),
                   PYTHONDONTWRITEBYTECODE='1')
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        # Native metadata defaults are captured during import. They must point
        # into an empty fixture home instead of the operator's VirtualDJ data.
        stack.enter_context(patch.object(Path, 'home', return_value=home))
        real_socket, real_getaddrinfo = socket.socket, socket.getaddrinfo
        owned = set()

        def address(value, require_owned=False):
            if not isinstance(value, tuple) or len(value) < 2 or value[0] not in LOOPBACK:
                raise PermissionError(errno.EPERM, 'Tests only allow disposable loopback peers')
            if value[1] in PROTECTED_PORTS or (require_owned and value[1] not in owned):
                raise PermissionError(errno.EPERM, 'Tests cannot contact an installed or unowned service')

        class TestSocket(real_socket):
            def bind(self, value):
                address(value)
                result = super().bind(value)
                owned.add(self.getsockname()[1])
                return result

            def connect(self, value):
                address(value, require_owned=True)
                return super().connect(value)

            def connect_ex(self, value):
                address(value, require_owned=True)
                return super().connect_ex(value)

            def sendto(self, data, *args):
                address(args[-1], require_owned=True)
                return super().sendto(data, *args)

        def getaddrinfo(host, *args, **kwargs):
            if host not in LOOPBACK:
                raise PermissionError(errno.EPERM, 'Tests cannot resolve external hosts')
            return real_getaddrinfo(host, *args, **kwargs)

        stack.enter_context(patch.object(socket, 'socket', TestSocket))
        stack.enter_context(patch.object(socket, 'getaddrinfo', getaddrinfo))
        # Keep the key lookup in disposable state even if imported earlier.
        from afterhours import ai_mix
        stack.enter_context(patch.object(ai_mix, 'ROOT', root))
        yield root
