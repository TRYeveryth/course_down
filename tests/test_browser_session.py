from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Ownership regressions: attaching must not close user contexts or fall back to Edge."""
import argparse
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts.common import browser_session, local_cdp_url


def fixture():
    original = SimpleNamespace(
        url=f'{SITE_URL}/kng/#/course/play?kngId=example',
        is_closed=lambda: False, evaluate=AsyncMock(return_value={'token': 'memory-only'}))
    new_page = SimpleNamespace(add_init_script=AsyncMock())
    context = SimpleNamespace(pages=[original], new_page=AsyncMock(return_value=new_page),
                              close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    chromium = SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser),
                               launch_persistent_context=AsyncMock(return_value=context))
    return SimpleNamespace(chromium=chromium), browser, context, original, new_page


class BrowserOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base_patcher = patch('scripts.common.BASE', Path(temporary.name))
        base_patcher.start()
        self.addCleanup(base_patcher.stop)
        patcher = patch('scripts.common.resolve_cdp_endpoint', side_effect=lambda endpoint: endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_attach_creates_new_tab_and_preserves_original_context(self):
        playwright, browser, context, original, new_page = fixture()
        original_url = original.url
        async with browser_session(playwright, 'http://127.0.0.1:9222') as session:
            self.assertIs(session.context, context)
            self.assertIs(session.page, new_page)
            self.assertEqual(session.initial_url, original_url)
            self.assertTrue(session.attached)
        context.close.assert_not_awaited()
        browser.close.assert_awaited_once()
        context.new_page.assert_awaited_once()
        playwright.chromium.launch_persistent_context.assert_not_awaited()
        self.assertEqual(original.url, original_url)
        script = new_page.add_init_script.await_args.args[0]
        self.assertIn('location.origin === origin', script)
        self.assertIn('sessionStorage.getItem(key) === null', script)

    async def test_exception_in_downloader_only_disconnects(self):
        playwright, browser, context, _, _ = fixture()
        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            async with browser_session(playwright, 'http://127.0.0.1:9222'):
                raise RuntimeError('download failed')
        context.close.assert_not_awaited()
        browser.close.assert_awaited_once()

    async def test_connection_failure_does_not_launch_edge(self):
        playwright, _, context, _, _ = fixture()
        playwright.chromium.connect_over_cdp.side_effect = OSError('unavailable')
        with self.assertRaisesRegex(RuntimeError, '未能连接 Chrome'):
            async with browser_session(playwright, 'http://127.0.0.1:9222'):
                self.fail('must not enter download flow')
        context.new_page.assert_not_awaited()
        playwright.chromium.launch_persistent_context.assert_not_awaited()

    async def test_owned_edge_is_closed(self):
        playwright, browser, context, _, _ = fixture()
        async with browser_session(playwright) as session:
            self.assertFalse(session.attached)
        context.close.assert_awaited_once()
        browser.close.assert_not_awaited()

    async def test_directory_start_copies_session_from_same_site_course(self):
        playwright, browser, context, original, new_page = fixture()
        directory = f'{SITE_URL}/kng/#/list?catalogId=example'
        async with browser_session(playwright, 'http://127.0.0.1:9222', directory) as session:
            self.assertEqual(session.initial_url, directory)
        original.evaluate.assert_awaited_once()
        new_page.add_init_script.assert_awaited_once()
        context.close.assert_not_awaited()

    def test_only_loopback_debug_endpoint_is_accepted(self):
        self.assertEqual(local_cdp_url('http://127.0.0.1:9222'), 'http://127.0.0.1:9222')
        for url in ('http://example.com:9222', 'http://0.0.0.0:9222',
                    'http://user:pass@127.0.0.1:9222', 'http://localhost'):
            with self.assertRaises(argparse.ArgumentTypeError):
                local_cdp_url(url)


if __name__ == '__main__':
    unittest.main()
