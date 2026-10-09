from tests.fixtures import SITE_URL, API_URL, CDN_URL
"""Browser fixtures for directory ordering/pagination and shared downloader reuse."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from scripts import download_catalog
from scripts import download_official
from scripts.common import course_key
from tests.fixtures import CATALOG_URL as START_URL


def identifier(index):
    return f'00000000-0000-4000-8000-{index:012d}'


def player_url(index):
    return f'{SITE_URL}/kng/#/course/play?kngId=' + identifier(index)


def card(index):
    return f'<a href="{player_url(index)}"><h3>课程{index:02d}</h3></a>'


def directory_html(count=16):
    return ('<style>main{display:grid;grid-template-columns:repeat(4,180px)}a{display:block;height:90px}</style>'
            '<main>' + ''.join(card(i) for i in range(1, count + 1)) + '</main>')


class BrowserFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.context = await self.browser.new_context(viewport={'width':1100, 'height':800})
        self.page = await self.context.new_page()
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name) / 'output'
        self.output.mkdir()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temporary.cleanup()

    async def serve(self, html, payload=None):
        async def route_handler(route):
            if '/kng/knowledge/list' in route.request.url or '/kng/knowledge/pagelist' in route.request.url:
                await route.fulfill(json=payload)
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=html)
        await self.context.route('**/*', route_handler)

    async def collect(self, count=16):
        return await download_catalog.collect_catalog(self.page, START_URL, count, 12, self.output)


class DirectoryTests(BrowserFixture):
    async def test_video_navigation_keeps_original_directory_on_page_two(self):
        destination=f'{SITE_URL}/kng/#/video/play?kngId='+identifier(20)
        html=('<main></main><div class="yxtf-pagination"><button>1</button><button>2</button>'
              '<button>3</button></div><script>let index=1;function render(){'
              'document.querySelector("main").innerHTML=index===2?'+json.dumps(
                  '<article onclick=\'location.href='+json.dumps(destination)+'\'>独立视频</article>')+
              ':"其他课程"}document.querySelectorAll("button").forEach((b,i)=>'
              'b.onclick=()=>{index=i+1;render()});render()</script>')
        await self.serve(html)
        await self.page.goto(START_URL)
        await self.page.get_by_role('button',name='2',exact=True).click()
        result=await download_catalog.resolve_video_card(self.page,
            {'title':'独立视频','id':identifier(20)},START_URL,2,{}, {})
        self.assertEqual(result,destination)
        self.assertEqual(self.page.url,START_URL)
        self.assertEqual(await self.page.locator('main').inner_text(),'独立视频')
        self.assertEqual(len(self.context.pages),1)

    async def test_standalone_video_card_resolves_popup_route_and_keeps_catalog(self):
        destination=f'{SITE_URL}/kng/#/video?kngId='+identifier(1)
        html=('<article onclick=\'window.open('+json.dumps(destination)+')\'>独立视频</article>'
              f'<script>fetch("{API_URL}/kng/knowledge/pagelist")</script>')
        await self.serve(html,{'datas':[{'id':identifier(1),'title':'独立视频','type':2,'fileType':'video'}],
                               'paging':{'count':1}})
        result=await self.collect(1)
        self.assertTrue(result[0]['standalone'])
        self.assertEqual(result[0]['kind'],'视频')
        self.assertEqual(result[0]['url'],destination)
        self.assertEqual(self.page.url,START_URL)
        self.assertEqual(len(self.context.pages),1)

    async def test_three_pages_infer_total_and_preserve_page_order(self):
        page_cards = [''.join(f'<article><h3>课程{i:02d}</h3></article>' for i in indices)
                      for indices in [range(1,17),range(17,33),range(33,35)]]
        html = ('<style>main{display:grid;grid-template-columns:repeat(4,180px)}article{height:90px}</style>'
                '<main></main><div class="yxtf-pagination"><button class="btn-next">→</button></div><script>'
                'let index=0;const pages='+json.dumps(page_cards)+';async function render(){'
                f'await fetch("{API_URL}/kng/knowledge/pagelist?page="+index);'
                'document.querySelector("main").innerHTML=pages[index];'
                'document.querySelector("button").disabled=index===2} '
                'document.querySelector("button").onclick=()=>{index++;render()};render()</script>')
        async def route_handler(route):
            if '/knowledge/pagelist' in route.request.url:
                index=int(route.request.url.rsplit('=',1)[-1])
                indices=[range(1,17),range(17,33),range(33,35)][index]
                await route.fulfill(json={'datas':[{'id':identifier(i),'title':f'课程{i:02d}',
                                                     'fileType':'course','type':0} for i in indices],
                                         'paging':{'count':34,'pages':3,'offset':index*16,'limit':16}})
            else:
                await route.fulfill(content_type='text/html; charset=utf-8',body=html)
        await self.context.route('**/*',route_handler)
        result = await download_catalog.collect_catalog(self.page,START_URL,None,45,self.output)
        self.assertEqual([item['url'] for item in result],[player_url(i) for i in range(1,35)])
        self.assertEqual([item['catalog_page'] for item in result],[1]*16+[2]*16+[3]*2)
        self.assertTrue(await self.page.locator('.btn-next').is_disabled())

    async def test_wrapped_grid_titles_stay_in_left_to_right_order(self):
        payload={'datas':[{'id':identifier(i),'title':f'课程{i:02d}','type':0,'fileType':'course'} for i in range(1,5)],
                 'paging':{'count':4}}
        html=('<style>main{display:grid;grid-template-columns:repeat(2,180px)}article{height:140px}'
              'article:nth-child(odd) h3{margin-top:35px}article:nth-child(even) h3{margin-top:15px}</style>'
              '<main>'+''.join(f'<article><h3>课程{i:02d}</h3></article>' for i in range(1,5))+'</main>'
              f'<script>fetch("{API_URL}/kng/knowledge/pagelist")</script>')
        await self.serve(html,payload)
        result=await self.collect(4)
        self.assertEqual([item['url'] for item in result],[player_url(i) for i in range(1,5)])

    async def test_actual_pagelist_schema_preserves_types_and_standalone_pdf(self):
        title = '岗位指引.pdf'
        payload = {'datas':[{'id':identifier(1), 'title':'课程01', 'type':0, 'fileType':'course'},
                            {'id':identifier(2), 'title':title, 'type':1, 'fileType':'pdf'}],
                   'paging':{'count':2, 'limit':16, 'offset':0}}
        html = ('<main><article>课程01</article><article onclick="location.hash=\'/document/play?kngId='
                + identifier(2) + '\'">' + title + '</article></main>'
                f'<script>fetch("{API_URL}/kng/knowledge/pagelist")</script>')
        await self.serve(html, payload)
        result = await self.collect(2)
        self.assertFalse(result[0]['standalone'])
        self.assertTrue(result[1]['standalone'])
        self.assertEqual(result[1]['file_type'], 'pdf')
        self.assertIn('/scan?', result[1]['url'])

    async def test_diagnostic_screenshot_failure_preserves_original_directory_error(self):
        await self.serve(directory_html(1))
        with patch.object(self.page, 'screenshot', side_effect=TimeoutError('fixture')):
            with self.assertRaisesRegex(RuntimeError, '1/2'):
                await self.collect(2)
        sample = json.loads((self.output / 'catalog-page.json').read_text(encoding='utf-8'))
        self.assertIn('1/2', sample['original_error'])
        self.assertEqual(sample['screenshot_error'], 'TimeoutError')

    async def test_sixteen_links_in_visual_order_deduplicate_and_ignore_recommendations(self):
        await self.serve(directory_html() + card(1) + '<aside class="recommend">' + card(17) + '</aside>')
        result = await self.collect()
        self.assertEqual([item['title'] for item in result], [f'课程{i:02d}' for i in range(1,17)])
        self.assertEqual([item['order'] for item in result], list(range(1,17)))

    async def test_cards_without_hrefs_use_api_ids_matched_to_visible_titles(self):
        records = [{'kngId':identifier(i), 'kngName':f'课程{i:02d}'} for i in range(16,0,-1)]
        html = ('<style>main{display:grid;grid-template-columns:repeat(4,180px)}article{height:90px}</style><main>'
                + ''.join(f'<article><h3>课程{i:02d}</h3></article>' for i in range(1,17)) + '</main>'
                + f'<script>fetch("{API_URL}/kng/knowledge/list")</script>')
        await self.serve(html, {'data':{'items':records}})
        result = await self.collect()
        self.assertEqual([item['url'] for item in result], [player_url(i) for i in range(1,17)])

    async def test_staggered_cards_loaded_above_initial_cards_keep_display_order(self):
        html = ('<main>' + ''.join(card(i) for i in range(9,17)) + '</main><script>'
                'setTimeout(()=>document.querySelector("main").insertAdjacentHTML("afterbegin",'
                + json.dumps(''.join(card(i) for i in range(1,9))) + '),400)</script>')
        await self.serve(html)
        result = await self.collect()
        self.assertEqual([course_key(item['url']) for item in result], ['kngId='+identifier(i) for i in range(1,17)])

    async def test_two_pages_keep_page_order(self):
        html = ('<main></main><button aria-label="下一页">下一页</button><script>'
                'let index=0; const pages=' + json.dumps([''.join(card(i) for i in range(1,9)), ''.join(card(i) for i in range(9,17))])
                + ';function render(){document.querySelector("main").innerHTML=pages[index];'
                'document.querySelector("button").disabled=index===1} '
                'document.querySelector("button").onclick=()=>{index++;render()};render()</script>')
        await self.serve(html)
        result = await self.collect()
        self.assertEqual([item['url'] for item in result], [player_url(i) for i in range(1,17)])

    async def test_incomplete_directory_stops_and_saves_diagnostic(self):
        await self.serve(directory_html(15))
        with self.assertRaisesRegex(RuntimeError, '15/16'):
            await self.collect()
        self.assertTrue((self.output / 'catalog-page.json').is_file())
        self.assertTrue((self.output / 'catalog-page.png').is_file())

    async def test_batch_calls_original_module_once_with_same_session(self):
        await self.serve(directory_html())
        session = SimpleNamespace(page=self.page, context=self.context, initial_url=START_URL, attached=True)
        @asynccontextmanager
        async def borrow(*args):
            yield session
        delegate = AsyncMock(return_value={'downloaded':0, 'skipped':6, 'failed':0})
        with patch('scripts.download_catalog.BASE', Path(self.temporary.name)),\
                patch('scripts.download_catalog.browser_session', side_effect=borrow) as sessions,\
                patch('scripts.download_catalog.download_official.main', delegate):
            code = await download_catalog.main(['--yes'])
        self.assertEqual(code, 0)
        sessions.assert_called_once()
        delegate.assert_awaited_once()
        self.assertIs(delegate.await_args.kwargs['existing_session'], session)
        args = delegate.await_args.args[0]
        urls_path = Path(args[args.index('--courses') + 1])
        self.assertEqual(urls_path.read_text(encoding='utf-8').splitlines(), [player_url(i) for i in range(1,17)])
        self.assertTrue(self.browser.is_connected())


class SharedDownloaderTests(BrowserFixture):
    async def test_standalone_video_uses_video_download_and_mp4_filename(self):
        title='视频操作指引'
        url=f'{SITE_URL}/kng/#/video?kngId='+identifier(1)
        await self.serve(f'<title>{title} - 云学堂</title><h1>{title}</h1><button>下载</button>')
        session=SimpleNamespace(page=self.page,context=self.context,initial_url=url,attached=False)
        class Transfer:
            suggested_filename='opaque.mp4'
            async def save_as(self,path):
                path.write_bytes(b'\x00\x00\x00\x18ftypisomfixture')
        delegate=AsyncMock(return_value=Transfer())
        entry={'url':url,'id':identifier(1),'title':title,'standalone':True,'kind':'视频'}
        with patch('scripts.download_official.BASE',Path(self.temporary.name)),\
                patch('scripts.download_official.click_official_download',delegate):
            stats=await download_official.main(['--course-url',url,'--yes'],existing_session=session,course_entries=[entry])
        self.assertEqual(stats,{'downloaded':1,'skipped':0,'failed':0})
        self.assertEqual(delegate.await_args.args[2],'视频')
        self.assertTrue((self.output/'视频操作指引-26-10'/'01-视频操作指引.mp4').is_file())

    async def test_explicit_course_update_wall_is_acknowledged_before_outline(self):
        next_html = '<aside style="width:250px"><h3>课程大纲</h3><div><span>文档</span><span>最新章节</span></div></aside>'
        html = ('<p>课程内容已更新，请点击“继续学习”，查看最新课程</p>'
                '<button onclick=\'document.body.innerHTML=' + json.dumps(next_html) + '\'>继续学习</button>')
        await self.page.set_content(html)
        outline = await download_official.course_outline(self.page, timeout_seconds=3)
        self.assertIn('最新章节', await outline.inner_text())

    async def test_regular_continue_learning_cover_is_not_clicked(self):
        await self.page.set_content('<button onclick="window.coverClicked=true">继续学习</button>')
        self.assertFalse(await download_official.accept_course_update(self.page))
        self.assertIsNone(await self.page.evaluate('() => window.coverClicked'))

    async def test_standalone_pdf_uses_download_without_chapter_outline(self):
        title = '岗位指引.pdf'
        url = f'{SITE_URL}/kng/#/scan?kngId=' + identifier(1)
        await self.serve(f'<title>{title} - 云学堂</title><h1>{title}</h1><button>下载</button>')
        session = SimpleNamespace(page=self.page, context=self.context, initial_url=url, attached=False)
        class Transfer:
            suggested_filename = 'opaque.pdf'
            async def save_as(self, path):
                path.write_bytes(b'%PDF-1.4\nfixture\n%%EOF\n')
        entry = {'url':url, 'id':identifier(1), 'title':title, 'standalone':True,'order':16}
        (self.output/'official-downloads.json').write_text(json.dumps({
            'course-16-error':{'status':'failed','course_url':url,'reason':'old timeout'},
            'course-1-error':{'status':'failed','course_url':player_url(2),'reason':'unrelated'}
        }),encoding='utf-8')
        with patch('scripts.download_official.BASE', Path(self.temporary.name)),\
                patch('scripts.download_official.click_official_download', AsyncMock(return_value=Transfer())):
            stats = await download_official.main(['--course-url',url,'--yes'],
                                                  existing_session=session, course_entries=[entry])
        self.assertEqual(stats, {'downloaded':1,'skipped':0,'failed':0})
        self.assertTrue((self.output / '岗位指引-26-10' / '01-岗位指引.pdf').is_file())
        report=json.loads((self.output/'official-downloads.json').read_text(encoding='utf-8'))
        self.assertEqual(report['course-16-error']['status'],'resolved')
        self.assertEqual(report['course-1-error']['reason'],'unrelated')

    async def test_delayed_course_outline_waits_beyond_previous_six_second_limit(self):
        html = ('<script>setTimeout(()=>document.body.innerHTML='
                + json.dumps('<aside style="width:250px"><h3>课程大纲</h3>'
                             '<div><span>文档</span><span>延迟章节</span></div></aside>')
                + ',6200)</script>')
        await self.page.set_content(html)
        outline = await download_official.course_outline(self.page, timeout_seconds=10)
        self.assertIn('延迟章节', await outline.inner_text())

    async def test_original_downloader_is_sequential_resumable_and_does_not_reconnect(self):
        def player_html(index):
            return (f'<title>课程{index:02d} - 云学堂</title><aside style="width:250px"><h3>课程大纲</h3>'
                    '<div class="yxtulcdsdk-catalog"><ul><li><div class="linozj liactive">'
                    f'<span>文档</span><span>章节{index:02d}</span></div></li></ul></div></aside><button>下载</button>')
        opened = []
        async def route_handler(route):
            # Hash routes are client-side and absent from the HTTP request URL.
            await route.fulfill(content_type='text/html', body='<html></html>')
        await self.context.route('**/*', route_handler)
        real_goto = self.page.goto
        async def goto(url, **kwargs):
            opened.append(url)
            await real_goto(url, **kwargs)
            index = 1 if identifier(1) in url else 2
            await self.page.set_content(player_html(index))
        transferred = []
        class Transfer:
            suggested_filename = 'opaque.pdf'
            async def save_as(self, path):
                path.write_bytes(b'%PDF-1.4\nfixture\n%%EOF\n')
        async def transfer(page, control, kind, timeout, diagnostic):
            transferred.append(await page.title())
            return Transfer()
        urls_file = Path(self.temporary.name) / 'courses.txt'
        urls_file.write_text(player_url(1)+'\n'+player_url(2)+'\n', encoding='utf-8')
        session = SimpleNamespace(page=self.page, context=self.context, initial_url=player_url(1), attached=False)
        argv = ['--courses', str(urls_file), '--yes']
        with patch('scripts.download_official.BASE', Path(self.temporary.name)),\
                patch.object(self.page, 'goto', side_effect=goto),\
                patch('scripts.download_official.click_official_download', side_effect=transfer),\
                patch('playwright.async_api.async_playwright', side_effect=AssertionError('must reuse session')):
            first = await download_official.main(argv, existing_session=session)
            second = await download_official.main(argv, existing_session=session)
        self.assertEqual(first, {'downloaded':2, 'skipped':0, 'failed':0})
        self.assertEqual(second, {'downloaded':0, 'skipped':2, 'failed':0})
        self.assertEqual(transferred, ['课程01 - 云学堂', '课程02 - 云学堂'])
        self.assertEqual(opened, [player_url(1), player_url(2), player_url(1), player_url(2)])
        self.assertTrue((self.output / '课程01-26-10' / '01-章节01.pdf').is_file())
        self.assertTrue((self.output / '课程02-26-10' / '01-章节02.pdf').is_file())
        self.assertTrue(self.browser.is_connected())


if __name__ == '__main__':
    unittest.main()
