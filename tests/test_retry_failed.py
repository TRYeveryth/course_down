from tests.fixtures import SITE_URL, API_URL, CDN_URL
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import retry_failed
from retry_failed import failed_courses


class FailedSelectionTests(unittest.TestCase):
    def test_subset_preserves_catalog_order_and_metadata(self):
        courses=[{'order':i,'id':str(i),'title':'同名课程','url':f'https://example.com/{i}',
                  'standalone':i==16,'kind':'文档'} for i in (1,16,24)]
        prefix=hashlib.sha256(b'kngId=24').hexdigest()[:10]+':'
        report={prefix+'bad':{'status':'failed','chapter':'第九章','reason':'empty'},
                'course-16-error':{'status':'failed','course_url':courses[1]['url'],'reason':'timeout'},
                'old-resolved':{'status':'resolved','course_url':courses[0]['url']}}
        selected,rows=failed_courses(courses,report)
        self.assertEqual([c['order'] for c in selected],[16,24])
        self.assertTrue(selected[0]['standalone'])
        self.assertEqual(rows[1]['chapter'],'第九章')

    def test_unknown_failure_cannot_be_silently_omitted(self):
        with self.assertRaisesRegex(RuntimeError,'无法匹配'):
            failed_courses([],{'unknown':{'status':'failed'}})


class RetryPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_course_stays_in_scope_after_first_failure_is_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'output'
            output.mkdir()
            course={'order':25,'id':'25','title':'案板','url':'https://example.com/25','standalone':False}
            (output/'catalog-courses.json').write_text(json.dumps({'courses':[course]}),encoding='utf-8')
            (output/'official-downloads.json').write_text('{}',encoding='utf-8')
            (output/'retry-pending-courses.json').write_text(json.dumps([course]),encoding='utf-8')
            delegate=AsyncMock(return_value={'downloaded':2,'skipped':1,'failed':0})
            session=object()
            with patch('retry_failed.BASE',root),patch('retry_failed.download_official.main',delegate):
                self.assertEqual(await retry_failed.main([],existing_session=session),0)
            self.assertEqual(delegate.await_args.kwargs['course_entries'],[course])
            self.assertIs(delegate.await_args.kwargs['existing_session'],session)
            self.assertEqual(json.loads((output/'retry-pending-courses.json').read_text(encoding='utf-8')),[])
