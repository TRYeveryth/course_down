from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Regression fixture reproducing six chapters, a duplicate heading and recommendations."""
import unittest
from playwright.async_api import async_playwright

from scripts.download_official import chapters_on, course_outline, wait_selected

HTML = '''<!doctype html><html><head><style>
body {margin:0} #outline {width:320px; height:600px; padding:12px; box-sizing:border-box}
.row {padding:12px} #content {position:absolute;left:400px;top:80px}
#details {margin-top:50px} #recommend {position:absolute;left:1200px;top:800px;width:250px}
</style></head><body>
<aside id="outline"><header><span>课程大纲</span></header><div>
<div class="row"><span>文档</span><span>莲子百合红豆沙</span></div>
<div class="row"><span>文档</span><span>翡翠马蹄爽</span></div>
<div class="row"><span>视频</span><span>翡翠马蹄爽</span></div>
<div class="row"><span>文档</span><span>云顶豆花</span></div>
<div class="row"><span>文档</span><span>山楂糕</span></div>
<div class="row"><span>文档</span><span>甜品拼盘</span></div>
</div></aside><main id="content"><h2>【文档】莲子百合红豆沙</h2></main>
<section id="details"><h2>课程大纲</h2><p>莲子百合红豆沙</p></section>
<section id="recommend"><h2>大家都在学</h2>
<div><span>视频</span><span>金汤碗仔翅.mp4</span></div>
<div><span>文档</span><span>服务指引卡.pdf</span></div>
<div><span>文档</span><span>新品上市公告.pdf</span></div></section>
<script>document.querySelectorAll('#outline .row').forEach(row => row.onclick=() => {
document.querySelectorAll('#outline .row').forEach(other=>other.classList.remove('active'));
row.classList.add('active');
});</script></body></html>'''


class ChapterScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_nested_li_with_liactive_and_pdf_canvas(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1600, 'height': 1200})
                await page.set_content(HTML)
                await page.locator('#content').evaluate("node => node.innerHTML = '<canvas></canvas>'")
                await page.locator('#outline').evaluate('''outline => {
                    const original = Array.from(outline.querySelectorAll('.row'));
                    const catalog = document.createElement('div');
                    catalog.className = 'yxtulcdsdk-catalog mb20';
                    const ul = document.createElement('ul');
                    original.forEach((row, index) => {
                        const text = row.textContent;
                        const kind = text.startsWith('视频') ? '视频' : '文档';
                        const li = document.createElement('li');
                        const clickable = document.createElement('div');
                        clickable.className = 'hand font-w400 linozj mb4 font-size-14 textcolorop6 lh22 ml16 mr16' +
                            (index === 0 ? ' liactive' : '');
                        const content = document.createElement('div');
                        content.className = 'ml20 flex-center flex-space-between';
                        const tooltip = document.createElement('span');
                        tooltip.className = 'yxtf-tooltip ulcdsdk-ellipsis-2 ulcdsdk-break item';
                        const badge = document.createElement('span');
                        badge.className = 'tag-type mr6'; badge.textContent = kind;
                        tooltip.append(badge, document.createTextNode(text.slice(kind.length)));
                        content.append(tooltip); clickable.append(content); li.append(clickable); ul.append(li);
                    });
                    original.forEach(row => row.remove());
                    catalog.append(ul); outline.append(catalog);
                }''')
                chapters = await chapters_on(page)
                self.assertEqual(len(chapters), 6)
                outline = await course_outline(page)
                await wait_selected(outline.get_by_text('文档', exact=True).nth(0), chapters[0])
                # Another active chapter cannot validate this one, even if the
                # cover/overview contains the expected title elsewhere.
                await page.locator('.linozj').nth(0).evaluate("node => node.classList.remove('liactive')")
                await page.locator('.linozj').nth(1).evaluate("node => node.classList.add('liactive')")
                with self.assertRaisesRegex(RuntimeError, '未能确认章节已切换'):
                    await wait_selected(outline.get_by_text('文档', exact=True).nth(0), chapters[0])
            finally:
                await browser.close()

    async def test_pdf_viewer_without_cover_title_uses_outline_highlight(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1600, 'height': 1200})
                await page.set_content(HTML)
                # Reproduce the saved failure: a PDF replaces the title and the
                # selected row uses a compiled style class outside the known markers.
                await page.locator('#content').evaluate("node => node.innerHTML = '<canvas></canvas>'")
                await page.add_style_tag(content='.row.viewer-highlight {background:rgb(70,77,89)}')
                await page.locator('#outline .row').nth(0).evaluate(
                    "node => node.classList.add('viewer-highlight')")
                chapters = await chapters_on(page)
                outline = await course_outline(page)
                await wait_selected(outline.get_by_text('文档', exact=True).nth(0), chapters[0])
                # A completion check icon on another chapter must not count as selection.
                await page.locator('#outline .row').nth(1).evaluate(
                    "node => {const icon=document.createElement('i');icon.style.backgroundColor='white';node.append(icon)}")
                with self.assertRaisesRegex(RuntimeError, '未能确认章节已切换'):
                    await wait_selected(outline.get_by_text('文档', exact=True).nth(1), chapters[1])
            finally:
                await browser.close()

    async def test_recommendations_excluded_and_same_title_video_selects_correctly(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1600, 'height': 1200})
                await page.set_content(HTML)
                chapters = await chapters_on(page)
                self.assertEqual(len(chapters), 6)
                self.assertEqual([c['type'] for c in chapters], ['文档','文档','视频','文档','文档','文档'])
                self.assertEqual(chapters[-1]['title'], '甜品拼盘')
                outline = await course_outline(page)
                video = outline.get_by_text('视频', exact=True).nth(chapters[2]['index'])
                await video.click()
                await wait_selected(video, chapters[2])
                self.assertEqual(await page.locator('#outline .active').inner_text(), '视频翡翠马蹄爽')
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
