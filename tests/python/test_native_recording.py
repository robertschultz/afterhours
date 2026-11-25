"""Private native metadata lookup and literal OSC parameters; no real engine."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from afterhours.dj_bridge import BridgeError, VirtualDJ, decode_osc
from afterhours.native_recording import ExactRecording, NativeRecordings
from afterhours.vdj_catalog import identity


def database(path, locators):
    root = ET.Element('VirtualDJ_Database')
    for locator in locators:
        song = ET.SubElement(root, 'Song', FilePath=locator)
        ET.SubElement(song, 'Tags', Author='Same artist', Title='Same title')
    ET.ElementTree(root).write(path, encoding='utf-8')


class NativeRecordingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'database.xml'
        self.index = NativeRecordings(self.path)

    def test_exact_hash_uses_saved_namespace_not_same_title_or_provider_guess(self):
        locators = ['netsearch://bp1234', 'netsearch://va347920']
        database(self.path, locators)
        saved = self.index.find(identity(locators[1]))
        self.assertEqual(saved.value_for(identity(locators[1])), locators[1])
        self.assertNotIn(locators[1], repr(saved))
        with self.assertRaises(TypeError):
            json.dumps(saved)
        with self.assertRaises(BridgeError):
            saved.value_for(identity(locators[0]))
        self.assertIsNone(self.index.find(identity('missing-exact-recording')))

    def test_observed_local_filename_preserves_quotes_spaces_and_unicode(self):
        locator = '/Users/fixture/Music/Beyoncé "Live" & Mix/Record.flac'
        database(self.path, [locator])
        self.assertEqual(self.index.find(identity(locator)).value_for(identity(locator)), locator)

    def test_cache_is_bounded_and_invalidated_when_native_database_changes(self):
        database(self.path, ['netsearch://bp1'])
        self.index.cache_size = 2
        first = self.index.find(identity('netsearch://bp1'))
        with patch('afterhours.native_recording.ET.iterparse', side_effect=AssertionError('Unexpected database reparse')):
            self.assertIs(self.index.find(identity('netsearch://bp1')), first)
        for value in ('missing-a', 'missing-b', 'missing-c'):
            self.assertIsNone(self.index.find(identity(value)))
        self.assertEqual(len(self.index.cache), 2)
        database(self.path, ['netsearch://bp1', 'netsearch://va2'])
        self.assertEqual(self.index.find(identity('netsearch://va2')).selection_id, identity('netsearch://va2'))

    def test_duplicate_exact_entries_are_ambiguous_and_not_cached(self):
        database(self.path, ['netsearch://va2', 'netsearch://va2'])
        with self.assertRaisesRegex(BridgeError, 'ambiguous'):
            self.index.find(identity('netsearch://va2'))
        self.assertFalse(self.index.cache)

    def test_unsupported_or_control_bearing_locator_never_becomes_load_data(self):
        for locator in ('https://example.com/audio', 'file:///private/audio.mp3',
                        'netsearch://va2?token=private', 'netsearch://va2\nplay', '//remote/share/audio.mp3'):
            with self.subTest(locator=locator):
                database(self.path, [locator])
                with self.assertRaises(BridgeError) as caught:
                    self.index.find(identity(locator))
                self.assertNotIn(locator, str(caught.exception))

    def test_missing_or_partial_database_is_not_availability_proof(self):
        self.assertIsNone(self.index.find(identity('anything')))
        self.path.write_text('<VirtualDJ_Database><Song FilePath="netsearch://va1"/>')
        with self.assertRaisesRegex(BridgeError, 'unconfirmed'):
            self.index.find(identity('netsearch://va1'))
        self.assertFalse(self.index.cache)

    def test_database_size_and_concurrent_rewrite_are_bounded(self):
        database(self.path, ['netsearch://va1'])
        with patch('afterhours.native_recording.MAX_DATABASE_BYTES', 20), self.assertRaisesRegex(BridgeError, 'bounded'):
            self.index.find(identity('netsearch://va1'))
        with patch('afterhours.native_recording._signature', side_effect=[('before',), ('after',)]), \
                self.assertRaisesRegex(BridgeError, 'changed during lookup'):
            self.index.find(identity('netsearch://va1'))
        self.assertFalse(self.index.cache)

    def test_private_locator_revalidates_hash_and_unsupported_values_before_use(self):
        for locator in ('netsearch://va2', '/Users/fixture/song.mp3', 'https://example.com/audio'):
            with self.subTest(locator=locator), self.assertRaises(BridgeError):
                ExactRecording(identity('other'), locator).value_for(identity('other'))

    def test_osc_text_is_a_separate_data_argument_never_script_interpolation(self):
        script = "set_var '$afterhours_restore_2'"
        for value in ('netsearch://va347920', '/Users/fixture/Music/Quoted "Live".mp3',
                      '/Users/fixture/`play` & pause/song with spaces.flac'):
            with self.subTest(value=value), patch('afterhours.dj_bridge.socket.socket') as factory:
                with VirtualDJ() as client:
                    address = client.send_text(script, value)
                wire, arguments = decode_osc(factory.return_value.sendto.call_args.args[0])
                self.assertEqual(wire, "/vdj/set_var/'$afterhours_restore_2'")
                self.assertEqual(arguments, [value])
                self.assertEqual(address, wire)
                self.assertNotIn(value, wire)


if __name__ == '__main__':
    unittest.main()
