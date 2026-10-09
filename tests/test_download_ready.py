from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Download readiness regressions for an asynchronously rendered right toolbar."""
import asyncio
import unittest
from playwright.async_api import async_playwright

from scripts.download_official import wait_download_control


class DownloadReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_toolbar_and_ignores_hidden_template(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div style="display:none">下载</div><main>资源加载中</main>')
                async def finish_loading():
                    await asyncio.sleep(0.5)
                    await page.locator('main').evaluate('''main => {
                        const button = document.createElement('button');
                        button.textContent = '下载'; main.replaceChildren(button);
                    }''')
                loading = asyncio.create_task(finish_loading())
                control = await wait_download_control(page, 3)
                await loading
                self.assertEqual(await control.count(), 1)
                self.assertTrue(await control.is_visible())
                await control.click()
            finally:
                await browser.close()

    async def test_missing_or_ambiguous_download_does_not_proceed(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<main>加载中</main>')
                with self.assertRaisesRegex(RuntimeError, '仍未显示下载入口'):
                    await wait_download_control(page, 0.3)
                await page.set_content('<button>下载</button><button>下载</button>')
                with self.assertRaisesRegex(RuntimeError, '可见下载入口有 2 个'):
                    await wait_download_control(page, 1)
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
