from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Temporary copy cleanup must keep originals and unmatched artifacts."""
import tempfile
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_downloads import cleanup_temporary_copies, inspect_file, main


class TemporaryCleanupTests(unittest.TestCase):
    def test_png_is_readable_and_truncation_is_rejected(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'步骤.png'
            Image.new('RGB',(10,20),'white').save(path)
            result=inspect_file(path)
            self.assertEqual((result['width'],result['height']),(10,20))
            path.write_bytes(path.read_bytes()[:-15])
            with self.assertRaises(Exception):
                inspect_file(path)

    def test_missing_courses_and_partial_downloads_fail_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            courses=[{'id':str(i),'url':f'https://example.com/{i}','title':f'课程{i}','order':i} for i in (1,2)]
            prefix=hashlib.sha256(b'kngId=1').hexdigest()[:10]+':'
            report={prefix+'ok':{'status':'ok','file':'章节.pdf','bytes':7,'type':'文档','chapter':'第一章'},
                    prefix+'failed':{'status':'failed','chapter':'第二章'}}
            (output/'章节.pdf').write_bytes(b'fixture')
            (output/'official-downloads.json').write_text(json.dumps(report),encoding='utf-8')
            (output/'catalog-courses.json').write_text(json.dumps({'courses':courses}),encoding='utf-8')
            with patch('sys.argv',['verify_downloads','--output',str(output)]),\
                 patch('scripts.verify_downloads.inspect_file',return_value={'bytes':7}):
                self.assertEqual(main(),2)
            summary=json.loads((output/'download-verification.json').read_text(encoding='utf-8'))
            self.assertEqual(len(summary['files']),1)
            self.assertEqual(len(summary['errors']),2)

    def test_pending_retry_cannot_be_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            course={'id':'1','url':'https://example.com/1','title':'课程1','order':1}
            prefix=hashlib.sha256(b'kngId=1').hexdigest()[:10]+':'
            report={prefix+'ok':{'status':'ok','file':'章节.pdf','bytes':7,'type':'文档','chapter':'第一章'}}
            (output/'章节.pdf').write_bytes(b'fixture')
            (output/'official-downloads.json').write_text(json.dumps(report),encoding='utf-8')
            (output/'catalog-courses.json').write_text(json.dumps({'courses':[course]}),encoding='utf-8')
            (output/'retry-pending-courses.json').write_text(json.dumps([course]),encoding='utf-8')
            with patch('sys.argv',['verify_downloads','--output',str(output)]),\
                 patch('scripts.verify_downloads.inspect_file',return_value={'bytes':7}):
                self.assertEqual(main(),2)
            summary=json.loads((output/'download-verification.json').read_text(encoding='utf-8'))
            self.assertIn('批次尚未结束',summary['errors'][0]['error'])

    def test_only_identical_verified_copies_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            target = output / '正式文件.pdf'
            target.write_bytes(b'verified-content')
            root = output / 'cdp-artifacts'
            root.mkdir()
            duplicate = root / 'temporary-copy'
            duplicate.write_bytes(target.read_bytes())
            unknown = root / 'same-size-different-content'
            unknown.write_bytes(b'unknown-content!')
            other = root / 'other-download'
            other.write_bytes(b'another')
            result = cleanup_temporary_copies(output,[{'file':str(target),'bytes':target.stat().st_size}])
            self.assertEqual(result,{'removed':1,'bytes':len(b'verified-content')})
            self.assertEqual(target.read_bytes(),b'verified-content')
            self.assertFalse(duplicate.exists())
            self.assertTrue(unknown.is_file())
            self.assertTrue(other.is_file())


if __name__ == '__main__':
    unittest.main()
