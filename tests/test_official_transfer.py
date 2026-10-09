from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Real-browser regressions for official buttons with several download behaviors."""
import asyncio
import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from playwright.async_api import async_playwright
from transfer import click_official_download

PDF = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n'
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42\x00\x00\x00\x08moov\x00\x00\x00\x08mdat'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.records.append((self.path, dict(self.headers)))
        parsed = urlsplit(self.path)
        if parsed.path == '/kng/knowledge/downloadurl':
            mode = parse_qs(parsed.query).get('mode', ['pdf'])[0]
            url = '/inline?Signature=memory-secret'
            filename = 'chapter.pdf'
            if mode == 'protected':
                url = '/protected-pdf'
            elif mode == 'html':
                url = '/empty'
            elif mode == 'video':
                url, filename = '/mp4?Signature=memory-secret', 'chapter.mp4'
            elif mode.startswith('rotate'):
                url = '/inline?Expires=' + mode[-1] + '&Signature=memory-secret' + mode[-1]
            payload = {'code':0, 'data':{'fileDownloadUrl':url, 'fileName':filename,
                                      'accessToken':'memory-secret'}}
            if mode == 'denied':
                payload = {'code':403, 'success':False, 'message':'当前章节不允许下载', 'data':None}
            data = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
        elif self.path.startswith('/protected-pdf'):
            if (self.headers.get('Authorization') != 'Bearer memory-secret'
                    or 'course_cookie=memory-secret' not in self.headers.get('Cookie', '')):
                self.send_response(401)
                data = b'authentication required'
                self.send_header('Content-Type', 'text/plain')
            else:
                data = PDF
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
        elif self.path.startswith('/mp4'):
            data = MP4
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
        elif self.path.startswith('/native-mp4'):
            data=MP4
            self.send_response(200)
            self.send_header('Content-Type','video/mp4')
            self.send_header('Content-Disposition','attachment; filename=chapter.mp4')
        elif self.path.startswith('/native-png'):
            data=PNG
            self.send_response(200)
            self.send_header('Content-Type','image/png')
            self.send_header('Content-Disposition','attachment; filename=steps.png')
        elif self.path.startswith('/native') or self.path.startswith('/inline'):
            data = PDF
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            if self.path.startswith('/native'):
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''chapter.pdf")
        elif self.path.startswith('/other-pdf'):
            data = PDF.replace(b'1.4', b'1.5')
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
        else:
            data = b'<html><body>download permission error</body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class OfficialTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.records = []
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.temporary = tempfile.TemporaryDirectory()
        self.diagnostic = Path(self.temporary.name) / 'click.json'
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.context = await self.browser.new_context(accept_downloads=True)
        self.page = await self.context.new_page()
        await self.page.goto(self.origin)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.server_thread.join()
        self.temporary.cleanup()

    async def run_button(self, javascript, timeout=3, kind='文档'):
        await self.page.set_content('<button>下载</button>')
        control = self.page.get_by_text('下载', exact=True)
        await control.evaluate('(button, code) => button.onclick = new Function(code)', javascript)
        return await click_official_download(self.page, control, kind, timeout, self.diagnostic)

    def local_api(self):
        return patch('transfer.is_download_api_response',
                     side_effect=lambda url: urlsplit(url).path == '/kng/knowledge/downloadurl')

    async def assert_saved(self, transfer):
        path = Path(self.temporary.name) / 'saved.pdf'
        await transfer.save_as(path)
        self.assertEqual(path.read_bytes(), PDF)

    async def test_native_attachment(self):
        result = await self.run_button("location.href='/native'")
        await self.assert_saved(result)
        self.assertIsNotNone(result.download)
        temporary_copy = Path(await result.download.path())
        self.assertTrue(temporary_copy.is_file())
        await result.cleanup()
        self.assertFalse(temporary_copy.exists())
        self.assertEqual((Path(self.temporary.name) / 'saved.pdf').read_bytes(), PDF)

    async def test_empty_native_file_recovers_from_its_official_download_address(self):
        result=await self.run_button("location.href='/native'")
        async def empty(path):
            path.write_bytes(b'')
        with patch.object(result.download,'save_as',side_effect=empty):
            await self.assert_saved(result)
        self.assertEqual(result.resource_url,self.origin+'/native')

    async def test_native_video_event_streams_complete_file_without_waiting_for_chrome(self):
        result=await self.run_button("location.href='/native-mp4'",kind='视频')
        self.assertIsNone(result.download)
        target=Path(self.temporary.name)/'chapter.mp4.part'
        await result.save_as(target)
        self.assertEqual(target.read_bytes(),MP4)

    async def test_stalled_native_save_has_a_timeout_and_recovers(self):
        result=await self.run_button("location.href='/native'")
        result.native_timeout_seconds=.02
        async def stalled(path):
            await asyncio.sleep(10)
        with patch.object(result.download,'save_as',side_effect=stalled):
            await self.assert_saved(result)

    async def test_empty_native_image_keeps_complete_png(self):
        result=await self.run_button("location.href='/native-png'")
        target=Path(self.temporary.name)/'steps.png.part'
        async def empty(path):
            path.write_bytes(b'')
        with patch.object(result.download,'save_as',side_effect=empty):
            await result.save_as(target)
        self.assertEqual(target.read_bytes(),PNG)

    async def test_empty_native_without_an_address_is_not_successful(self):
        result=await self.run_button("location.href='/native'")
        result.resource_url=''
        async def empty(path):
            path.write_bytes(b'')
        with patch.object(result.download,'save_as',side_effect=empty):
            with self.assertRaisesRegex(RuntimeError,'空文件'):
                await result.save_as(Path(self.temporary.name)/'empty.pdf')

    async def test_download_in_initialized_child_page(self):
        result = await self.run_button("const child=window.open('/empty'); setTimeout(()=>child.location.href='/native', 350)")
        await self.assert_saved(result)
        self.assertIsNotNone(result.download)

    async def test_fetch_pdf_without_download_event(self):
        result = await self.run_button("fetch('/inline?signature=secret')")
        await self.assert_saved(result)
        self.assertIsNone(result.download)
        diagnostic = self.diagnostic.read_text(encoding='utf-8')
        self.assertNotIn('secret', diagnostic)
        self.assertEqual(json.loads(diagnostic)['stage'], 'pdf_response')

    async def test_preview_in_child_page(self):
        result = await self.run_button("window.open('/inline')")
        await self.assert_saved(result)

    async def test_html_page_and_unrelated_pdf_are_not_success(self):
        other = await self.context.new_page()
        await other.goto(self.origin)
        async def unrelated_pdf():
            await asyncio.sleep(0.2)
            await other.evaluate("fetch('/inline')")
        unrelated = asyncio.create_task(unrelated_pdf())
        with self.assertRaisesRegex(RuntimeError, '已点击下载.*未收到'):
            await self.run_button("window.open('/empty?signature=secret')", timeout=1)
        await unrelated
        diagnostic = json.loads(self.diagnostic.read_text(encoding='utf-8'))
        self.assertTrue(diagnostic['click_completed'])
        self.assertEqual(diagnostic['stage'], 'wait_file')
        self.assertTrue(diagnostic['popups'])
        self.assertNotIn('secret', json.dumps(diagnostic))
        self.assertFalse(any(item['mime'] == 'application/pdf' for item in diagnostic['responses']))

    async def test_two_distinct_pdfs_stop_as_ambiguous(self):
        with self.assertRaisesRegex(RuntimeError, '多个不同 PDF'):
            await self.run_button("fetch('/inline'); fetch('/other-pdf')")

    async def test_existing_child_page_does_not_supply_next_chapters_pdf(self):
        popup_event = self.page.wait_for_event('popup')
        popup_wait = asyncio.create_task(popup_event)
        await self.page.evaluate("window.open('/empty')")
        previous_popup = await popup_wait
        await previous_popup.wait_for_load_state()
        async def old_pdf():
            await asyncio.sleep(0.2)
            await previous_popup.evaluate("fetch('/inline')")
        unrelated = asyncio.create_task(old_pdf())
        with self.assertRaisesRegex(RuntimeError, '已点击下载.*未收到'):
            await self.run_button("fetch('/empty')", timeout=1)
        await unrelated

    async def test_official_json_address_streams_pdf_and_redacts_secrets(self):
        with self.local_api():
            result = await self.run_button("fetch('/kng/knowledge/downloadurl')")
        self.assertTrue(result.resource_url)
        await self.assert_saved(result)
        diagnostic = self.diagnostic.read_text(encoding='utf-8')
        self.assertNotIn('memory-secret', diagnostic)
        self.assertEqual(json.loads(diagnostic)['stage'], 'api_file_url')

    async def test_same_origin_file_uses_scoped_cookies_and_authorization(self):
        await self.context.add_cookies([{'name':'course_cookie', 'value':'memory-secret', 'url':self.origin}])
        with self.local_api():
            result = await self.run_button("fetch('/kng/knowledge/downloadurl?mode=protected',"
                                           "{headers:{Authorization:'Bearer memory-secret'}})")
        await self.assert_saved(result)
        self.assertNotIn('memory-secret', self.diagnostic.read_text(encoding='utf-8'))

    async def test_two_responses_with_rotating_signatures_are_one_file(self):
        with self.local_api():
            result = await self.run_button("fetch('/kng/knowledge/downloadurl?mode=rotate1');"
                                           "fetch('/kng/knowledge/downloadurl?mode=rotate2')")
        await self.assert_saved(result)
        self.assertEqual(len(json.loads(self.diagnostic.read_text(encoding='utf-8'))['download_api']), 2)

    async def test_official_json_address_streams_mp4(self):
        with self.local_api():
            result = await self.run_button("fetch('/kng/knowledge/downloadurl?mode=video')", kind='视频')
        target = Path(self.temporary.name) / 'video.mp4'
        await result.save_as(target)
        self.assertEqual(target.read_bytes(), MP4)

    async def test_api_denial_reports_server_message(self):
        with self.local_api(), self.assertRaisesRegex(RuntimeError, '当前章节不允许下载'):
            await self.run_button("fetch('/kng/knowledge/downloadurl?mode=denied')", timeout=1.5)
        self.assertEqual(json.loads(self.diagnostic.read_text(encoding='utf-8'))['download_api'][0]['file_url_count'], 0)

    async def test_api_link_returning_html_is_not_saved(self):
        with self.local_api():
            result = await self.run_button("fetch('/kng/knowledge/downloadurl?mode=html')")
        target = Path(self.temporary.name) / 'invalid.pdf'
        with self.assertRaisesRegex(RuntimeError, '返回网页'):
            await result.save_as(target)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
