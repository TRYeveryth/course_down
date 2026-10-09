from tests.fixtures import SITE_URL, API_URL, CDN_URL
import tempfile
import unittest
from pathlib import Path
from scripts.common import download_run_lock


class DownloadRunLockTests(unittest.TestCase):
    def test_blocks_concurrent_writer_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            with self.assertRaisesRegex(ValueError,'fixture'):
                with download_run_lock(output):
                    with self.assertRaisesRegex(RuntimeError,'正在运行'):
                        with download_run_lock(output):
                            self.fail('duplicate writer was admitted')
                    raise ValueError('fixture')
            with download_run_lock(output):
                self.assertTrue((output/'download-run.lock').is_file())
