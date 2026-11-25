from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from afterhours.paths import PACKAGE_ROOT, analysis_reports, data_root


class PathsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_default_data_is_outside_the_package(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(data_root(), self.root / '.local/share/afterhours')
            self.assertFalse(data_root().is_relative_to(PACKAGE_ROOT))
            self.assertEqual(analysis_reports(), data_root() / '.runtime/audio-analysis')

    def test_data_and_analysis_overrides(self):
        xdg = self.root / 'xdg'
        with patch.dict('os.environ', {'XDG_DATA_HOME': str(xdg)}, clear=True):
            self.assertEqual(data_root(), xdg / 'afterhours')
            with patch.dict('os.environ', {'AFTERHOURS_DATA_ROOT': str(self.root / 'legacy')}):
                self.assertEqual(analysis_reports(), self.root / 'legacy/.runtime/audio-analysis')
                with patch.dict('os.environ', {'AFTERHOURS_ANALYSIS_REPORTS_DIR': str(self.root / 'reports')}):
                    self.assertEqual(analysis_reports(), self.root / 'reports')
