"""Read a course directory in display order, then call download_official.main."""
import argparse
import asyncio
import json
import re
from contextlib import AsyncExitStack
from urllib.parse import urlsplit, parse_qs, quote

from . import download_official
from .common import browser_session, local_cdp_url, course_url, course_key, is_course_page
from .common import BASE
from . import common as settings


def catalog_url(value):
    course_url(value)
    parsed = urlsplit(value)
    route, _, query = parsed.fragment.lstrip('/').partition('?')
    if route != 'list' or not parse_qs(query).get('catalogId'):
        raise argparse.ArgumentTypeError('请使用本站包含 catalogId 的课程目录链接')
    return value


def is_catalog_response(url):
    parsed = urlsplit(url)
    return settings.is_api_url(url) and ('/kng/' in parsed.path or '/knowledge/' in parsed.path)


def course_records(payload):
    """Read actual course IDs/names from responses; generic category IDs are ignored."""
    found = {}
    def walk(value, depth=0):
        if depth > 14:
            return
        if isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, dict):
            # The saved live response from /kng/knowledge/pagelist uses
            # {datas:[{id,title,type,fileType,...}],paging:{limit,offset,...}}.
            if isinstance(value.get('datas'), list) and isinstance(value.get('paging'), dict):
                for entry in value['datas']:
                    if (isinstance(entry, dict) and isinstance(entry.get('id'), str)
                            and isinstance(entry.get('title'), str)):
                        found[entry['id']] = {'id':entry['id'], 'title':entry['title'].strip(),
                                             'type':entry.get('type'), 'file_type':entry.get('fileType')}
            identifier = next((value.get(key) for key in ('kngId', 'knowledgeId', 'courseId')
                               if isinstance(value.get(key), str)), None)
            if not identifier and any(key in value for key in ('kngName', 'knowledgeName', 'courseName', 'kngType', 'knowledgeType')):
                identifier = value.get('id')
            title = next((value.get(key) for key in ('kngName', 'knowledgeName', 'courseName', 'title', 'name')
                          if isinstance(value.get(key), str) and value[key].strip()), None)
            if (isinstance(identifier, str) and re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', identifier)
                    and title):
                found[identifier] = {'id':identifier, 'title':title.strip()}
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)
    walk(payload)
    return list(found.values())


def response_shape(value, depth=0):
    """Diagnostic field names/types only, with no response credentials."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key):response_shape(child, depth + 1) for key, child in list(value.items())[:35]}
    if isinstance(value, list):
        return [response_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def item_key(item):
    return 'kngId=' + item['id'] if item.get('id') else course_key(item['url'])


def merge_display_order(existing, snapshot):
    """Use the newest rendered order, including earlier cards loaded late."""
    result = list(existing)
    snapshot_keys = {item_key(item) for item in snapshot}
    if all(item_key(item) in snapshot_keys for item in existing):
        return list({item_key(item):item for item in snapshot}.values())
    for index, item in enumerate(snapshot):
        key = item_key(item)
        if any(item_key(old) == key for old in result):
            continue
        following = {item_key(later) for later in snapshot[index + 1:]}
        anchor = next((i for i, old in enumerate(result) if item_key(old) in following), len(result))
        if anchor == len(result) and index:
            previous_key = item_key(snapshot[index - 1])
            previous = next((i for i, old in enumerate(result) if item_key(old) == previous_key), None)
            if previous is not None:
                anchor = previous + 1
        result.insert(anchor, item)
    return result


def display_order(items):
    """Keep a grid row together when wrapped titles have slightly different y."""
    rows = []
    for item in sorted(items,key=lambda value:(value['y'],value['x'])):
        if not rows or item['y'] - rows[-1]['y'] > 48:
            rows.append({'y':item['y'],'items':[]})
        rows[-1]['items'].append(item)
    return [item for row in rows for item in sorted(row['items'],key=lambda value:(value['x'],value['y']))]


async def visible_courses(page, records, resolved_urls=None):
    """Prefer real player links; match API records to titles rendered in the directory."""
    links = await page.locator('a[href]').evaluate_all('''nodes => nodes.map(node => {
        const box=node.getBoundingClientRect();
        const title=node.querySelector('[class*="title"], [class*="name"], h3, h4');
        return {url:node.href, title:(title?.innerText || node.getAttribute('title') || node.innerText || '').trim(),
            visible:box.width>0 && box.height>0 && getComputedStyle(node).visibility!=='hidden'
                && !node.closest('[class*="recommend"], [class*="related-course"], nav, footer'),
            x:box.x,y:box.y};
    })''')
    candidates = {}
    for item in links:
        if item['visible'] and is_course_page(item['url']):
            key = course_key(item['url'])
            if key not in candidates or (item['y'], item['x']) < (candidates[key]['y'], candidates[key]['x']):
                candidates[key] = item
    for record in records.values():
        matches = page.get_by_text(record['title'], exact=True).filter(visible=True)
        boxes = []
        for index in range(await matches.count()):
            match = matches.nth(index)
            # Exclude unrelated recommendations if a directory also has a sidebar.
            excluded = await match.evaluate('''node => !!node.closest(
                '[class*="recommend"], [class*="related-course"], nav, footer')''')
            box = await match.bounding_box()
            if box and not excluded:
                boxes.append(box)
        if boxes:
            box = min(boxes, key=lambda item:(item['y'], item['x']))
            key = 'kngId=' + record['id']
            if key not in candidates:
                # This is the verified player route used by the existing downloader,
                # populated only with an actual ID returned for a visible course.
                candidates[key] = {'url':settings.site_origin() + '/kng/#/course/play?kngId=' + quote(record['id']),
                                   'title':record['title'], 'x':box['x'], 'y':box['y']}
            candidates[key]['file_type'] = record.get('file_type')
            candidates[key]['type'] = record.get('type')
            candidates[key]['id'] = record['id']
            file_type = str(record.get('file_type') or '').lower().lstrip('.')
            video = file_type in {'video','mp4'} or record.get('type') == 2
            pdf = record['title'].lower().endswith('.pdf') or file_type == 'pdf'
            candidates[key]['standalone'] = pdf or video
            candidates[key]['kind'] = '视频' if video else '文档'
            if pdf:
                # The real PDF card was followed in Chrome and verified to use
                # #/scan?kngId=..., rather than the course player. Use that route
                # so both inspection manifests and downloads follow list order.
                candidates[key]['url'] = settings.site_origin() + '/kng/#/scan?kngId=' + quote(record['id'])
            if resolved_urls and record['id'] in resolved_urls:
                candidates[key]['url'] = resolved_urls[record['id']]
    return display_order(candidates.values())


async def next_directory_page(page):
    """Only click an unambiguous, enabled pagination next control."""
    control = await next_directory_control(page)
    if control is None:
        return False
    await control.click()
    return True


async def next_directory_control(page):
    """Locate the same next-page arrow used in the actual course directory."""
    control = page.get_by_role('button', name=re.compile(r'^(下一页|Next page|Go to next page)$', re.I)).filter(visible=True)
    if await control.count() == 0:
        control = page.locator('.el-pagination .btn-next, .yxtf-pagination .btn-next, '
                               '[class*="pagination"] [aria-label="下一页"], '
                               '[class*="pagination"] [title="下一页"], '
                               '.ant-pagination-next').filter(visible=True)
    count = await control.count()
    if count == 0:
        return None
    if count != 1:
        raise RuntimeError('发现多个“下一页”入口，无法确定课程分页；已停止')
    if await control.is_disabled() or await control.evaluate('''node =>
        node.getAttribute('aria-disabled')==='true' || /disabled/.test(node.className || '')'''):
        return None
    return control


def catalog_entries(ordered):
    return [{'order':index, 'title':item['title'], 'url':item['url'],
             'id':item.get('id') or course_key(item['url']).removeprefix('kngId='),
             'catalog_page':item.get('catalog_page',1),
             'standalone':item.get('standalone', item['title'].lower().endswith('.pdf')),
             'file_type':item.get('file_type'), 'type':item.get('type'), 'kind':item.get('kind','文档')}
            for index,item in enumerate(ordered,1)]


async def save_directory_diagnostic(page, output, api_shapes, records, error=None):
    sample = {'url':page.url, 'api_shapes':api_shapes, 'course_records':list(records.values()),
              'original_error':str(error) if isinstance(error, RuntimeError) else type(error).__name__ if error else None}
    try:
        sample.update({'title':await page.title(),
            'body_text':(await page.locator('body').inner_text(timeout=5000))[:100000],
            'links':await page.locator('a[href]').evaluate_all(
                'nodes => nodes.map(node => ({text:node.innerText,href:node.href,className:node.className}))')})
    except Exception as diagnostic_error:
        sample['page_read_error'] = type(diagnostic_error).__name__
    (output / 'catalog-page.json').write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        await page.screenshot(path=str(output / 'catalog-page.png'), full_page=False, timeout=5000)
    except Exception as screenshot_error:
        sample['screenshot_error'] = type(screenshot_error).__name__
        (output / 'catalog-page.json').write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding='utf-8')
        print('目录截图未完成；原始错误及文字诊断已保留。', flush=True)


async def resolve_video_card(page, item, url, page_number, records, resolved_urls):
    """Follow a video card in an owned tab, leaving directory pagination intact."""
    probe = await page.context.new_page()
    popups = []
    def on_popup(popup):
        popups.append(popup)
    probe.on('popup',on_popup)
    try:
        # Cookies are shared by the context; sessionStorage belongs to each tab.
        # Copy it in memory only, just as browser_session does when attaching.
        storage = await page.evaluate('() => Object.entries(sessionStorage)')
        await probe.add_init_script("if (location.origin===" + json.dumps(settings.site_origin()) + ") {"
            "for (const [key,value] of " + json.dumps(storage) + ") sessionStorage.setItem(key,value)}")
        await probe.goto(url,wait_until='domcontentloaded',timeout=60000)
        target=probe.get_by_text(item['title'],exact=True).filter(visible=True)
        if page_number > 1:
            pages=probe.locator('[class*="pagination"]').get_by_text(str(page_number),exact=True).filter(visible=True)
            await pages.first.wait_for(state='visible',timeout=30000)
            if await pages.count()!=1:
                raise RuntimeError('独立视频目录页码入口不唯一')
            await pages.click(timeout=15000)
        await target.first.wait_for(state='visible',timeout=30000)
        if await target.count()!=1:
            raise RuntimeError('独立视频卡片定位不唯一，无法确认详情地址')
        source_url=probe.url
        print('读取独立视频详情地址：',item['title'],flush=True)
        await target.click(timeout=15000)
        deadline = asyncio.get_running_loop().time()+30
        while asyncio.get_running_loop().time()<deadline:
            for candidate in [probe]+popups:
                if not candidate.is_closed() and candidate.url != source_url and item['id'].lower() in candidate.url.lower():
                    return course_url(candidate.url)
            await asyncio.sleep(.2)
        raise RuntimeError('点击独立视频后未能确认其详情地址')
    finally:
        probe.remove_listener('popup',on_popup)
        for popup in popups:
            if not popup.is_closed():
                await popup.close()
        if not probe.is_closed():
            await probe.close()


async def collect_catalog(page, url, expected_count=None, timeout_seconds=120, output=None, paginate=True):
    output = output or BASE / 'output'
    output.mkdir(exist_ok=True)
    records = {}
    api_shapes = []
    tasks = set()
    completed_pages = []
    current_page = []
    page_number = 1
    target_count = expected_count
    catalog_error = None
    resolved_urls = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    async def observe(response):
        nonlocal target_count, catalog_error
        try:
            if response.request.frame.page is not page or not is_catalog_response(response.url):
                return
            if response.status != 200 or 'json' not in response.headers.get('content-type', '').lower():
                return
            payload = await response.json()
            if (paginate and urlsplit(response.url).path.endswith('/knowledge/pagelist')
                    and isinstance(payload,dict) and isinstance(payload.get('paging'),dict)):
                total = payload['paging'].get('count')
                if isinstance(total,int) and not isinstance(total,bool) and total > 0:
                    if target_count is not None and target_count != total:
                        catalog_error = RuntimeError(f'目录接口总数为 {total}，与预期 {target_count} 不一致；已停止')
                    target_count = total
            entries = course_records(payload)
            records.update((entry['id'], entry) for entry in entries)
            if len(api_shapes) < 60:
                api_shapes.append({'path':urlsplit(response.url).path,
                    'keys':list(payload)[:40] if isinstance(payload, dict) else [type(payload).__name__],
                    'course_record_count':len(entries), 'shape':response_shape(payload)})
        except Exception:
            pass

    def response_handler(response):
        task = asyncio.create_task(observe(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on('response', response_handler)
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        if '/login' in urlsplit(page.url).path:
            raise RuntimeError('目录页面需要登录，请先在 Chrome 完成登录再运行')
        page_fingerprint = None
        quiet_since = loop.time()
        previous_length = 0
        pagination_fingerprints = set()
        while loop.time() < deadline:
            if catalog_error:
                raise catalog_error
            visible = await visible_courses(page, records, resolved_urls)
            for item in visible:
                item['catalog_page'] = page_number
            fingerprint = tuple(item_key(item) for item in visible)
            if fingerprint != page_fingerprint:
                page_fingerprint = fingerprint
                quiet_since = loop.time()
            current_page = merge_display_order(current_page, visible)
            ordered = list(completed_pages)
            known = {item_key(item) for item in ordered}
            for item in current_page:
                key = item_key(item)
                if key not in known:
                    ordered.append(item)
                    known.add(key)
            if target_count is not None and len(ordered) > target_count:
                raise RuntimeError(f'识别到 {len(ordered)} 项，超过预期 {target_count} 项；请检查目录或 --expected-count')
            if len(ordered) != previous_length:
                previous_length = len(ordered)
                quiet_since = loop.time()
                print(f'第 {page_number} 页，目录累计识别 {len(ordered)}/{target_count or "待确认"} 项。', flush=True)
            if visible and not tasks and loop.time()-quiet_since >= 1:
                for item in current_page:
                    if item.get('standalone') and item.get('kind')=='视频' and item['id'] not in resolved_urls:
                        resolved_urls[item['id']] = await resolve_video_card(page,item,url,page_number,records,resolved_urls)
                        item['url'] = resolved_urls[item['id']]
            # Wait for both lazy rendering and outstanding list-response parsing.
            if target_count is not None and len(ordered) == target_count and not tasks and loop.time() - quiet_since >= 1:
                if not paginate or await next_directory_control(page) is None:
                    return catalog_entries(ordered)
            if visible and loop.time() - quiet_since >= 2:
                # Scroll content containers as well as the window for lazy lists.
                await page.evaluate('''() => {
                    window.scrollTo(0, document.documentElement.scrollHeight);
                    for (const node of document.querySelectorAll('main,[class*="list"],[class*="scroll"]')) {
                        if (node.scrollHeight>node.clientHeight+10 && /auto|scroll/.test(getComputedStyle(node).overflowY))
                            node.scrollTop=node.scrollHeight;
                    }
                }''')
                await asyncio.sleep(0.5)
                refreshed = await visible_courses(page, records, resolved_urls)
                refreshed_fingerprint = tuple(item_key(item) for item in refreshed)
                if refreshed_fingerprint == fingerprint and fingerprint not in pagination_fingerprints:
                    pagination_fingerprints.add(fingerprint)
                    if paginate and await next_directory_page(page):
                        completed_pages = ordered
                        current_page = []
                        page_number += 1
                        print(f'正在翻到第 {page_number} 页。',flush=True)
                        quiet_since = loop.time()
                        # Pagination can render the old cards while its request is pending.
                        for _ in range(40):
                            await asyncio.sleep(0.2)
                            changed = await visible_courses(page, records, resolved_urls)
                            if tuple(item_key(item) for item in changed) != fingerprint:
                                break
                        else:
                            raise RuntimeError('点击下一页后课程列表没有变化，已停止以避免重复或漏采集')
                    else:
                        if target_count is None or len(ordered) == target_count:
                            return catalog_entries(ordered)
                        raise RuntimeError(f'目录只识别到 {len(ordered)}/{target_count} 项，且没有可用下一页')
            await asyncio.sleep(0.2)
        raise RuntimeError(f'目录加载超时：识别到 {len(ordered)}/{target_count or "未知"} 项')
    except Exception as error:
        try:
            await save_directory_diagnostic(page, output, api_shapes, records, error)
            print('目录诊断已保存：output/catalog-page.json', flush=True)
        except Exception as diagnostic_error:
            print(f'目录诊断保存失败（{type(diagnostic_error).__name__}）；原始错误仍会显示。', flush=True)
        raise
    finally:
        page.remove_listener('response', response_handler)
        pending = list(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def main(argv=None, existing_session=None):
    from playwright.async_api import async_playwright
    parser = argparse.ArgumentParser(description='按目录顺序下载课程，复用 download_official.py')
    parser.add_argument('--cdp', type=local_cdp_url, default=settings.CDP_URL)
    parser.add_argument('--catalog-url', type=catalog_url, default=settings.CATALOG_URL or None)
    parser.add_argument('--expected-count', type=int, help='可选：核对目录总数；默认读取列表接口的总数')
    parser.add_argument('--catalog-timeout', type=int, default=240)
    parser.add_argument('--ready-timeout', type=int, default=60)
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--yes', action='store_true', help='识别满课程后直接按顺序下载')
    parser.add_argument('--inspect-only', action='store_true', help='只保存课程清单，不下载章节')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--all-pages', action='store_true', help='遍历全部分页（默认行为，兼容旧命令）')
    scope.add_argument('--first-page', action='store_true', help='只处理第一页')
    args = parser.parse_args(argv)
    try:
        if not args.catalog_url:
            raise ValueError('请填写 config.py 的 CATALOG_URL，或使用 --catalog-url 传入目录链接')
        settings.configure_site(args.catalog_url)
    except ValueError as error:
        parser.error(str(error))
    if min(args.catalog_timeout, args.ready_timeout, args.timeout) < 1 or (args.expected_count is not None and args.expected_count < 1):
        parser.error('课程数量和各项超时必须大于 0')
    output = BASE / 'output'
    output.mkdir(exist_ok=True)
    async with AsyncExitStack() as stack:
        if existing_session is None:
            pw = await stack.enter_async_context(async_playwright())
            session = await stack.enter_async_context(browser_session(pw,args.cdp,args.catalog_url,args.inspect_only))
        else:
            session = existing_session
        print('已连接 Chrome，在新标签页读取课程目录。', flush=True)
        await session.page.bring_to_front()
        courses = await collect_catalog(session.page, args.catalog_url, args.expected_count, args.catalog_timeout, output,
                                        paginate=not args.first_page)
        page_counts = {}
        for item in courses:
            number = item['catalog_page']
            page_counts[number] = page_counts.get(number,0) + 1
        manifest = {'catalog_url':args.catalog_url, 'course_count':len(courses),
                    'page_count':len(page_counts), 'pages':[{'page':number,'count':count} for number,count in page_counts.items()],
                    'courses':courses}
        (output / 'catalog-courses.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        urls_path = output / 'catalog-course-urls.txt'
        urls_path.write_text('\n'.join(item['url'] for item in courses) + '\n', encoding='utf-8')
        print('\n本次课程顺序：', flush=True)
        for course in courses:
            print(f"{course['order']:02d} {course['title'] or course_key(course['url'])}", flush=True)
        if args.inspect_only:
            print('课程清单已保存到 output/catalog-courses.json；本次未下载章节。', flush=True)
            return 0
        if not args.yes:
            answer = await asyncio.to_thread(input, '按以上顺序下载全部课程？输入 y 开始：')
            if answer.strip().lower() != 'y':
                print('课程清单已保存，未开始下载。', flush=True)
                return 0
        # Reuse the existing module and the SAME CDP session; no reconnect for
        # each course and no duplicated chapter/download/naming implementation.
        stats = await download_official.main([
            '--courses', str(urls_path), '--yes', '--timeout', str(args.timeout),
            '--ready-timeout', str(args.ready_timeout)], existing_session=session, course_entries=courses)
        return 2 if stats['failed'] else 0


if __name__ == '__main__':
    try:
        from .common import download_run_lock
        with download_run_lock(BASE/'output'):
            raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n已停止；已完成文件保留，再次运行会跳过。')
        raise SystemExit(130)
    except RuntimeError as error:
        print(str(error), flush=True)
        raise SystemExit(1)
