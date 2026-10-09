"""Check public module commands and root configuration after code relocation."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProjectLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = Path(__file__).resolve().parent.parent
        (self.root / 'scripts').mkdir()
        for path in (source / 'scripts').glob('*.py'):
            shutil.copy2(path, self.root / 'scripts' / path.name)
        shutil.copy2(source / 'config.example.py', self.root / 'config.example.py')

    def run_python(self, *arguments):
        return subprocess.run(
            [sys.executable, *arguments], cwd=self.root, capture_output=True,
            text=True, encoding='utf-8', timeout=30,
        )

    def test_all_public_module_commands_show_help(self):
        for name in ('download_catalog', 'download_official', 'retry_failed',
                     'probe', 'verify_downloads'):
            with self.subTest(command=name):
                result = self.run_python('-m', 'scripts.' + name, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--help', result.stdout)
        self.assertFalse((self.root / 'scripts' / 'output').exists())

    def test_user_configuration_and_storage_paths_stay_at_root(self):
        url = 'https://learning.example.test/kng/#/list?catalogId=example'
        (self.root / 'config.py').write_text(
            'CATALOG_URL = ' + repr(url) + '\nFOLDER_SUFFIX = "archive"\n',
            encoding='utf-8',
        )
        result = self.run_python('-c',
            'import json; from scripts import common; '
            'print(json.dumps({"base":str(common.BASE),'
            '"url":common.CATALOG_URL,"suffix":common.FOLDER_SUFFIX}))')
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout)
        self.assertEqual(Path(loaded['base']), self.root.resolve())
        self.assertEqual(loaded['url'], url)
        self.assertEqual(loaded['suffix'], 'archive')
