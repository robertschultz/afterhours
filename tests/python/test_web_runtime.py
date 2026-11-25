"""Archive routing and media remain separate from native/provider operations."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from afterhours import server
from afterhours import performance_server
from afterhours.web_runtime import SHARED_WEB, web_file


class RuntimeWebFilesTests(unittest.TestCase):
    def test_only_bundled_page_is_available_at_root(self):
        self.assertEqual(web_file('/', SHARED_WEB), SHARED_WEB / 'index.html')
        for route in ('live.html', 'display.html', 'console.html', 'site.js', 'live.css'):
            self.assertIsNone(web_file('/' + route, SHARED_WEB))

    def test_existing_data_root_cannot_reactivate_retired_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'live.html').write_text('old operator')
            (root / 'index.html').write_text('old homepage')
            self.assertIsNone(web_file('/live.html', root))
            self.assertEqual(web_file('/', root), SHARED_WEB / 'index.html')

    def test_saved_native_media_and_shared_module_aliases_remain_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agent-media').mkdir()
            saved = root / 'agent-media' / 'fixture.png'
            saved.write_bytes(b'fixture image')
            self.assertEqual(web_file('/agent-media/fixture.png', root), saved.resolve())
            self.assertEqual(web_file('/assets/performer.png', root), SHARED_WEB / 'assets/performer.png')
            self.assertEqual(web_file('/engine/web/planner.js', root), SHARED_WEB / 'planner.js')

    def test_directories_private_files_and_encoded_traversal_are_unavailable(self):
        for route in ('/media/', '/fonts/', '/.env.local', '/%2e%2e/server.py',
                      '/engine/web/%2e%2e/server.py', '/build_site.py', '/README.md'):
            self.assertIsNone(web_file(route, SHARED_WEB))

    def test_live_stage_preview_exposes_only_content_addressed_jpegs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'director-stage';stage.mkdir()
            digest='a'*64;image=stage/(digest+'.jpg');image.write_bytes(b'synthetic image')
            self.assertEqual(web_file('/director-stage/'+image.name,root),image.resolve())
            for name in ('manifest.json','private.jpg',digest+'.png',digest+'.js'):
                (stage/name).write_bytes(b'not an approved stage route')
                self.assertIsNone(web_file('/director-stage/'+name,root))
            self.assertIsNone(web_file('/director-stage/',root))

    def test_symlink_outside_data_media_root_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / 'web'
            (web / 'agent-media').mkdir(parents=True)
            (root / 'private.png').write_bytes(b'private fixture')
            (web / 'agent-media/leak.png').symlink_to(root / 'private.png')
            self.assertIsNone(web_file('/agent-media/leak.png', web))

    def test_live_stage_file_symlink_cannot_expose_otherwise_private_web_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);web=root/'web';stage=web/'director-stage';stage.mkdir(parents=True)
            private=web/'private.json';private.write_text('{"synthetic":"private"}')
            image=stage/('b'*64+'.jpg');image.symlink_to(private)
            self.assertIsNone(web_file('/private.json',web))
            self.assertIsNone(web_file('/director-stage/'+image.name,web))
            self.assertIsNone(web_file('/engine/web/director-stage/'+image.name,web))

    def test_live_stage_directory_symlink_cannot_escape_its_own_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);web=root/'web';web.mkdir()
            for target in (web/'private',root/'outside-web'):
                target.mkdir();image=target/('c'*64+'.jpg');image.write_bytes(b'private synthetic image')
                stage=web/'director-stage';stage.symlink_to(target,target_is_directory=True)
                self.assertIsNone(web_file('/director-stage/'+image.name,web))
                stage.unlink()


class RuntimeWebHTTPTests(unittest.TestCase):
    def test_saved_alpha_quicktime_is_served_with_ranges_and_existing_media_guards(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(performance_server, 'WEB', Path(directory)):
            root = Path(directory)
            media = root / 'media' / 'character-loops'
            media.mkdir(parents=True)
            payload = b'local synthetic alpha movie byte fixture'
            clip = media / 'fixture-white-123.alpha.mov'
            clip.write_bytes(payload)
            (root / 'private.mov').write_bytes(b'not in an allowed media directory')
            self.assertEqual(web_file('/media/character-loops/' + clip.name, root), clip.resolve())
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), performance_server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{httpd.server_port}'
            route = base + '/media/character-loops/' + clip.name
            try:
                for header, expected, content_range in [(None, payload, None), ('bytes=0-7', payload[:8], f'bytes 0-7/{len(payload)}'), ('bytes=-5', payload[-5:], f'bytes {len(payload)-5}-{len(payload)-1}/{len(payload)}')]:
                    with urlopen(Request(route, headers={'Range': header} if header else {}), timeout=2) as response:
                        self.assertEqual(response.status, 206 if header else 200)
                        self.assertEqual(response.headers['Content-Type'], 'video/quicktime')
                        self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
                        self.assertEqual(response.headers['Content-Range'], content_range)
                        self.assertEqual(response.read(), expected)
                for path, code in [('/private.mov', 404), ('/media/%2e%2e/private.mov', 404)]:
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(base + path, timeout=2)
                    self.assertEqual(caught.exception.code, code)
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(route, headers={'Range': 'bytes=999-'}), timeout=2)
                self.assertEqual(invalid.exception.code, 416)
            finally:
                httpd.shutdown();httpd.server_close();thread.join(timeout=2)

    def test_both_services_serve_status_and_shared_media(self):
        for httpd in (ThreadingHTTPServer(('127.0.0.1', 0), server.Handler), performance_server.make_server(0)):
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{httpd.server_port}'
            try:
                with urlopen(base + '/', timeout=2) as response:
                    page = response.read()
                    self.assertIn(b'<title>Afterhours</title>', page)
                    self.assertIn(b'Afterhours is running', page)
                with self.assertRaises(HTTPError) as missing:
                    urlopen(base + '/live.html', timeout=2)
                self.assertEqual(missing.exception.code, 404)
                with urlopen(base + '/assets/performer.png', timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
                with urlopen(base + '/engine/web/director-controller.js', timeout=2) as response:
                    self.assertIn(b'DirectorController', response.read())
                if hasattr(httpd, 'performance'):
                    with urlopen(Request(base + '/media/rivet-demo.mp4', headers={'Range': 'bytes=0-31'}), timeout=2) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(len(response.read()), 32)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
