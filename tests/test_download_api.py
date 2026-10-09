from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Parser and redirect credential-scope regressions."""
import unittest
import urllib.request
from transfer import extract_download_urls, FileRedirects, file_identity, is_download_api_response


class DownloadAPITests(unittest.TestCase):
    def test_only_official_download_api_is_interpreted(self):
        self.assertTrue(is_download_api_response(f'{API_URL}/kng/knowledge/downloadurl'))
        self.assertFalse(is_download_api_response('https://example.com/kng/knowledge/downloadurl'))
        self.assertFalse(is_download_api_response(f'{API_URL}/studylog/logs'))

    def test_nested_and_string_responses_keep_only_file_addresses(self):
        base = f'{API_URL}/kng/knowledge/downloadurl'
        self.assertEqual(extract_download_urls({'data':f'{CDN_URL}/file.pdf?Signature=secret'}, base),
                         [f'{CDN_URL}/file.pdf?Signature=secret'])
        payload = {'data':{'sourceUrl':f'{CDN_URL}/file.pdf',
                           'previewUrl':f'{CDN_URL}/preview.pdf',
                           'thumbnailUrl':f'{CDN_URL}/page.jpg',
                           'fileUrl':'http://127.0.0.1/private.pdf'}}
        self.assertEqual(extract_download_urls(payload, base), [f'{CDN_URL}/file.pdf'])

    def test_identity_ignores_storage_signature_but_preserves_file_id(self):
        self.assertEqual(file_identity(f'{CDN_URL}/file?Signature=a&Expires=1&id=5'),
                         file_identity(f'{CDN_URL}/file?id=5&Signature=b&Expires=2'))
        self.assertNotEqual(file_identity(f'{CDN_URL}/file?id=5'),
                            file_identity(f'{CDN_URL}/file?id=6'))

    def test_explicit_image_download_is_kept_and_preview_is_excluded(self):
        payload={'downloadUrl':f'{CDN_URL}/steps.png',
                 'thumbnailUrl':f'{CDN_URL}/thumb.png',
                 'sourceUrl':f'{CDN_URL}/preview.png'}
        self.assertEqual(extract_download_urls(payload,f'{API_URL}/download'),
                         [f'{CDN_URL}/steps.png'])

    def test_redirect_strips_authorization_across_origins(self):
        request = urllib.request.Request(f'{API_URL}/file',
            headers={'Authorization':'Bearer memory-secret', 'Token':'memory-secret',
                     'User-Agent':'test-agent', 'Referer':f'{SITE_URL}/kng/'})
        redirected = FileRedirects().redirect_request(request, None, 302, 'Found', {}, f'{CDN_URL}/file')
        self.assertFalse(any(key.lower() in {'authorization', 'token'} for key in redirected.headers))
        self.assertEqual(redirected.get_header('User-agent'), 'test-agent')
        same_origin = FileRedirects().redirect_request(request, None, 302, 'Found', {}, f'{API_URL}/file2')
        self.assertEqual(same_origin.get_header('Authorization'), 'Bearer memory-secret')
        with self.assertRaisesRegex(RuntimeError, '不支持的地址'):
            FileRedirects().redirect_request(request, None, 302, 'Found', {}, 'http://cdn.yxt.com/file')


if __name__ == '__main__':
    unittest.main()
