"""Fixed, local browser assets for the explicitly enabled guest invitation.

No filesystem paths, private session values or dynamic code are accepted from
the guest. All room data still comes through the scoped authenticated protocol.
"""
import re

from afterhours.paths import PACKAGE_ROOT

ROOT = PACKAGE_ROOT / 'crowd_web'
ASSETS={'crowd.css':'text/css; charset=utf-8','core.mjs':'text/javascript; charset=utf-8',
        'app.mjs':'text/javascript; charset=utf-8'}
CSP="default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src https: http:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"


def page(root):
    if not re.fullmatch(r'/r/[A-Za-z0-9_-]{16,128}',root):raise ValueError('Invalid invitation path.')
    return (ROOT/'index.html').read_text().replace('__CROWD_ROOT__',root).encode()


def asset(name):
    if name not in ASSETS:raise ValueError('Unknown guest asset.')
    return (ROOT/name).read_bytes(),ASSETS[name]
