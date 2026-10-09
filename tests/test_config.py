from tests.fixtures import SITE_URL, CATALOG_URL
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import common


class ConfigurationTests(unittest.TestCase):
    def test_blank_configuration_requires_user_link(self):
        with patch.multiple(common,SITE_ORIGIN='',CATALOG_URL='',COURSE_URL=''):
            with self.assertRaisesRegex(ValueError,'config.py'):
                common.configure_site()
            self.assertEqual(common.configure_site(CATALOG_URL),SITE_URL)

    def test_configured_origin_rejects_other_sites_and_plain_http(self):
        with self.assertRaises(ValueError):
            common.validate_target('https://unrelated.example.test/kng/')
        with self.assertRaises(ValueError):
            common.validate_target('http://learning.example.test/kng/')

    def test_api_suffix_boundary_cannot_match_lookalike_host(self):
        with patch('scripts.common.API_HOST_SUFFIXES',('example.test',)):
            self.assertTrue(common.is_api_url('https://api.example.test/kng/'))
            self.assertFalse(common.is_api_url('https://evilexample.test/kng/'))

    def test_user_variables_load_from_a_separate_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.py'
            path.write_text('CATALOG_URL = '+repr(CATALOG_URL)+'\nFOLDER_SUFFIX = "archive"\n',encoding='utf-8')
            loaded=common.load_config(path)
            self.assertEqual(loaded.CATALOG_URL,CATALOG_URL)
            self.assertEqual(loaded.FOLDER_SUFFIX,'archive')
