"""Resolve shared engine assets and approved local media."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit

from afterhours.paths import SHARED_WEB
MEDIA_DIRECTORIES = {'assets', 'media', 'audio', 'agent-media'}
MEDIA_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.svg', '.mp4', '.mov', '.webm', '.wav', '.json'}


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # The numeric loopback address is already known. HTTPServer normally
        # resolves a hostname here, which can delay the listener's startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def web_file(url, data_web):
    """Resolve one file without directory listings or old data-root UI fallbacks.

    The /engine/web alias gives browser imports the same shared asset paths.
    """
    path = unquote(urlsplit(url).path)
    parts = path.lstrip('/').split('/')
    if '\\' in path or '\0' in path or any(part.startswith('.') for part in parts if part):
        return None
    relative = path.lstrip('/') or 'index.html'
    if relative.startswith('engine/web/'):
        relative = relative.removeprefix('engine/web/')
    bundled = (SHARED_WEB / relative).resolve()
    if not bundled.is_relative_to(SHARED_WEB):
        return None
    # Only media may be supplied by an existing data root. Old HTML/JS there
    # cannot reactivate a retired interface when the service starts normally.
    stage = re.fullmatch(r'director-stage/[a-f0-9]{64}\.jpg',relative) is not None
    if stage:
        root=Path(data_web).resolve();stage_root=root/'director-stage';saved=root/relative
        # Stage previews have their own boundary, not the whole data web root.
        # Neither a file alias nor a redirected stage directory may expose an
        # otherwise private file under an apparently content-addressed URL.
        if stage_root.resolve()!=stage_root or saved.is_symlink():return None
        saved=saved.resolve()
        return saved if saved.is_relative_to(stage_root) and saved.is_file() else None
    media = relative.split('/')[0] in MEDIA_DIRECTORIES and bundled.suffix.lower() in MEDIA_SUFFIXES
    if media:
        root = Path(data_web).resolve()
        saved = (root / relative).resolve()
        if saved.is_relative_to(root) and saved.is_file():
            return saved
    if bundled.is_file() and (media or bundled.suffix in {'.js', '.css', '.html'}):
        return bundled
    return None


class RuntimeStaticHandler(SimpleHTTPRequestHandler):
    def static_file(self, path=None):
        return web_file(path or self.path, self.directory)

    def send_head(self):
        if self.static_file() is None:
            self.send_error(404, 'Not found')
            return None
        return super().send_head()

    def translate_path(self, path):
        return str(self.static_file(path) or SHARED_WEB / '__not_available__')
