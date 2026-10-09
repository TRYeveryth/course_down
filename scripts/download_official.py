"""Download course chapters using the platform's visible download control.

First run: login and open one course manually, then press Enter in the terminal.
Optional later runs: pass --courses courses.txt (one verified course URL per line).
The active outline marker is calibrated from the saved live DOM diagnostic.
"""
import argparse
import asyncio
import hashlib
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path

from .common import BASE, safe_name
from .common import browser_session, local_cdp_url, course_url, course_key
from .transfer import click_official_download
from . import common as settings


FOLDER_SUFFIX = settings.FOLDER_SUFFIX


def clean_course_name(title):
    return re.sub(r'\s*[-–—|]\s*云学堂\s*$', '', title).strip()


def chapter_filename(ordinal, title, suggested_filename, kind):
    """Use the Chinese outline title; preserve only the server filename's suffix."""
    suffix = Path(safe_name(suggested_filename)).suffix.lower()
    if not (suffix and suffix[1:].isascii() and suffix[1:].isalnum() and len(suffix) <= 12):
        suffix = '.mp4' if kind == '视频' else '.pdf'
    title = safe_name(title)
    if title.lower().endswith(suffix):
        title = title[:-len(suffix)]
    return f'{ordinal:02d}-{title}{suffix}'


async def accept_course_update(page):
    """Enter an updated course only when the site's explicit update wall appears."""
    warning = page.get_by_text(re.compile(r'^课程内容已更新.*查看最新课程$', re.S)).filter(visible=True)
    if await warning.count() == 0:
        return False
    control = page.get_by_text('继续学习', exact=True).filter(visible=True)
    if await warning.count() != 1 or await control.count() != 1:
        raise RuntimeError('课程更新提示或继续学习入口不唯一，无法确认应进入的课程')
    print('课程内容已更新，正在进入最新课程。', flush=True)
    await control.click(timeout=15000)
    await warning.wait_for(state='hidden', timeout=15000)
    return True


async def course_outline(page, timeout_seconds=60):
    """Find the left course outline, excluding the overview and recommendations."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    acknowledged_update = False
    while asyncio.get_running_loop().time() < deadline:
        if not acknowledged_update and await accept_course_update(page):
            acknowledged_update = True
            deadline = asyncio.get_running_loop().time() + timeout_seconds
        headings = page.get_by_text('课程大纲', exact=True)
        visible = []
        for index in range(await headings.count()):
            heading = headings.nth(index)
            box = await heading.bounding_box()
            if box and await heading.is_visible():
                visible.append((box['y'], box['x'], heading))
        if visible:
            heading = min(visible, key=lambda item: (item[0], item[1]))[2]
            root = heading
            viewport_width = await page.evaluate('() => window.innerWidth')
            for _ in range(8):
                root = root.locator('xpath=..')
                box = await root.bounding_box()
                if not box:
                    continue
                badges = (await root.get_by_text('文档', exact=True).count()
                          + await root.get_by_text('视频', exact=True).count())
                if (badges and box['x'] < viewport_width * 0.3
                        and box['width'] < viewport_width * 0.6):
                    return root
        await asyncio.sleep(0.2)
    raise RuntimeError('无法确定左侧课程大纲，已停止以避免把推荐课程当作章节')


async def chapters_on(page):
    chapters = []
    outline = await course_outline(page)
    for kind in ('文档', '视频'):
        badges = outline.get_by_text(kind, exact=True)
        for index in range(await badges.count()):
            badge = badges.nth(index)
            if not await badge.is_visible():
                continue
            title = await badge.evaluate('''(element, kind) => {
                let node = element;
                for (let level = 0; node && level < 6; level++, node = node.parentElement) {
                    const text = (node.innerText || '').trim();
                    if (text.length > kind.length && text.length < 180) {
                        return text.replace(kind, '').replace(/\\s+/g, ' ').trim();
                    }
                }
                return '';
            }''', kind)
            box = await badge.bounding_box()
            chapters.append({'type': kind, 'index': index,
                             'title': title or f'{kind}{index + 1}',
                             'y': box['y'] if box else index})
    return sorted(chapters, key=lambda c: c['y'])


async def wait_selected(badge, chapter):
    """Verify active attributes, the outline's unique highlighted row, or its content heading."""
    for _ in range(50):
        selected = await badge.evaluate('''(element, chapter) => {
            const normalized = value => (value || '').replace(/[\\s\\[\\]【】]/g, '');
            const wanted = normalized(chapter.type + chapter.title);
            // The saved live DOM uses .linozj for each clickable chapter and
            // .liactive for the selected chapter inside its <li> wrapper.
            const siteRow = element.closest('.linozj');
            if (siteRow) {
                const catalog = siteRow.closest('.yxtulcdsdk-catalog');
                const unique = !catalog || catalog.querySelectorAll('.linozj.liactive').length === 1;
                return unique && siteRow.classList.contains('liactive') && normalized(siteRow.innerText) === wanted;
            }
            let node = element;
            for (let level = 0; node && level < 6; level++, node = node.parentElement) {
                const text = normalized(node.innerText);
                if (text.length > 180) break;
                if (text !== wanted) continue;
                if (node.getAttribute('aria-selected') === 'true' ||
                    ['true', 'page', 'step'].includes(node.getAttribute('aria-current')) ||
                    /(^|[\\s_-])(active|selected|current|cur|on)([\\s_-]|$)/i.test(node.className || '')) return true;
                // The live PDF viewer removes the cover heading. Its selected
                // outline row instead has a unique opaque background; completion
                // check marks alone are not a selection signal.
                const isChapterRow = peer => {
                    const value = normalized(peer.innerText);
                    return /^(文档|视频).+/.test(value) && value.length < 180;
                };
                const peers = node.parentElement ? Array.from(node.parentElement.children).filter(isChapterRow) : [];
                const color = getComputedStyle(node).backgroundColor;
                const opaque = color !== 'transparent' && !/rgba\\([^)]*,\\s*0(?:\\.0+)?\\s*\\)/.test(color);
                if (peers.length >= 2 && opaque && peers.every(peer =>
                    peer === node || getComputedStyle(peer).backgroundColor !== color)) return true;
            }
            const boundary = element.getBoundingClientRect().right + 80;
            return Array.from(document.querySelectorAll('body *')).some(candidate => {
                if (normalized(candidate.textContent) !== wanted) return false;
                const bounds = candidate.getBoundingClientRect();
                return bounds.width > 0 && bounds.height > 0 && bounds.left > boundary;
            });
        }''', chapter)
        if selected:
            return
        await asyncio.sleep(0.2)
    raise RuntimeError('未能确认章节已切换；需按实际页面校准，已停止下载以避免错配文件')


async def wait_download_control(page, timeout_seconds=60):
    """Selection is synchronous; the right toolbar arrives after resource loading."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

    control = page.get_by_text('下载', exact=True).filter(visible=True)
    try:
        await control.wait_for(state='visible', timeout=timeout_seconds * 1000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f'等待 {timeout_seconds} 秒后仍未显示下载入口。资源可能仍在加载，或该章节未提供下载；'
            '可用 --ready-timeout 120 延长等待。') from error
    except PlaywrightError as error:
        count = await control.count()
        if count > 1:
            raise RuntimeError(f'可见下载入口有 {count} 个，需要校准定位') from error
        raise
    count = await control.count()
    if count != 1:
        raise RuntimeError(f'加载后可见下载入口有 {count} 个，需要校准定位')
    return control


async def save_failure_page(page, output):
    """Keep text diagnostics even if a large page cannot be screenshotted."""
    try:
        (output / 'last-failure.txt').write_text(
            await page.locator('body').inner_text(timeout=5000), encoding='utf-8')
    except Exception:
        pass
    try:
        await page.screenshot(path=str(output / 'last-failure.png'), full_page=False, timeout=5000)
    except Exception:
        print('失败页面截图未完成；文字和下载错误仍会记录。', flush=True)


async def main(argv=None, existing_session=None, course_entries=None):
    """CLI entry point, also callable with a caller-owned browser session."""
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description='云学堂官方入口批量下载（首次需验证页面结构）')
    parser.add_argument('--courses', type=Path, help='每行一个已核实的课程详情 URL')
    parser.add_argument('--timeout', type=int, default=45, help='点击后等待下载事件或完整 PDF 的秒数')
    parser.add_argument('--ready-timeout', type=int, default=60, help='章节资源和下载入口加载等待秒数')
    parser.add_argument('--cdp', type=local_cdp_url, nargs='?', const=settings.CDP_URL,
                        help='连接已启动的 Chrome；不填参数时使用 config.py 的 CDP_URL')
    parser.add_argument('--course-url', type=course_url, help='在新标签页打开指定课程详情 URL')
    parser.add_argument('--yes', action='store_true', help='跳过人工核对，自动下载；仅在首次验证章节定位后使用')
    parser.add_argument('--inspect-only', action='store_true', help='新建课程标签页并保存页面结构，不执行下载')
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.ready_timeout < 1:
        parser.error('--timeout 和 --ready-timeout 必须大于 0')
    if args.courses and args.course_url:
        parser.error('--courses 与 --course-url 请选择一种')
    urls = []
    if args.courses:
        urls = [line.strip() for line in args.courses.read_text(encoding='utf-8-sig').splitlines()
                if line.strip() and not line.lstrip().startswith('#')]
        if not urls:
            parser.error('课程文件不能为空，每行填写一个 HTTPS 课程详情链接。')
        urls = list(dict.fromkeys(urls))
    elif args.course_url:
        urls = [args.course_url]
    elif settings.COURSE_URL:
        urls = [settings.COURSE_URL]
    try:
        settings.configure_site(urls[0] if urls else None)
        for url in urls:
            course_url(url)
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))

    output = BASE / 'output'
    output.mkdir(exist_ok=True)
    report_path = output / 'official-downloads.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    stats = {'downloaded': 0, 'skipped': 0, 'failed': 0}

    def save():
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(report_path)

    async with AsyncExitStack() as stack:
        if existing_session is None:
            playwright = await stack.enter_async_context(async_playwright())
            session = await stack.enter_async_context(browser_session(
                playwright, args.cdp, urls[0] if urls else None, args.inspect_only))
        else:
            session = existing_session
        context, page = session.context, session.page
        if session.attached:
            print('已连接现有 Chrome，并新建课程标签页。脚本退出时只断开连接。', flush=True)
        try:
            try:
                await page.goto(session.initial_url, wait_until='domcontentloaded', timeout=60000)
                if session.attached:
                    await page.bring_to_front()
            except Exception:
                print('导航尚未完成，可在脚本打开的标签页手动打开课程页面。')
            if args.inspect_only:
                try:
                    await page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass
                sample = {'title': await page.title(), 'url': page.url,
                          'body_text': await page.locator('body').inner_text(timeout=15000),
                          'links': await page.locator('a').evaluate_all(
                              '(nodes) => nodes.map(n => ({text:n.innerText,href:n.href,className:n.className}))'),
                          'chapter_candidates': await chapters_on(page)}
                (output / 'connected-page.json').write_text(
                    json.dumps(sample, ensure_ascii=False, indent=2), encoding='utf-8')
                await page.screenshot(path=str(output / 'connected-page.png'), full_page=True)
                print('页面标题：', sample['title'])
                print('章节候选：', json.dumps(sample['chapter_candidates'], ensure_ascii=False))
                print('已保存 output/connected-page.json 和 output/connected-page.png。')
                return
            if not args.yes:
                print('请在脚本打开的标签页进入课程详情（左侧能看到“文档/视频”）；若需登录，请先完成登录。')
                print('保留原标签页，直接在新标签页操作。无需播放或点击“继续学习”。')
                await asyncio.to_thread(input, '准备好后，在此终端按 Enter：')
            if not urls:
                urls = [page.url]
                print('本次处理当前课程。全目录自动翻页尚未适配。')
            for course_number, url in enumerate(urls, 1):
                entry = next((item for item in (course_entries or []) if item['url'] == url), {})
                error_key = f"course-{entry.get('order',course_number)}-error"
                try:
                    standalone = bool(entry.get('standalone'))
                    if page.url != url:
                        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    # Wait for the visible chapter label supplied in the screenshot.
                    if standalone:
                        await wait_download_control(page, args.ready_timeout)
                        expected_title = clean_course_name(entry['title'])
                        actual_title = clean_course_name(await page.title())
                        visible_title = page.get_by_text(expected_title, exact=True).filter(visible=True)
                        if (actual_title.removesuffix('.pdf') != expected_title.removesuffix('.pdf')
                                and await visible_title.count() == 0):
                            raise RuntimeError('独立文件页面标题与目录不一致，停止以避免下载错配')
                        chapters = [{'type':entry.get('kind','文档'), 'index':0, 'title':expected_title, 'standalone':True}]
                    else:
                        await course_outline(page, args.ready_timeout)
                        chapters = await chapters_on(page)
                    if not chapters:
                        raise RuntimeError('未识别到文档/视频章节，需要查看实际 DOM')
                    print(f'\n课程 {course_number}/{len(urls)}，识别到 {len(chapters)} 个章节：')
                    for index, chapter in enumerate(chapters, 1):
                        print(index, chapter['type'], chapter['title'])
                    # The heuristic must be reviewed before its first use on a course.
                    answer = 'y' if args.yes else (await asyncio.to_thread(input, '列表是否与左侧章节一致？输入 y 开始，其他输入跳过：')).strip().lower()
                    if answer != 'y':
                        print('已跳过课程，待校准章节定位。')
                        continue
                    course_name = '' if args.yes else (await asyncio.to_thread(input, '保存的课程名（留空使用网页标题）：')).strip()
                    course_name = clean_course_name(course_name or entry.get('title') or (await page.title())) or f'课程{course_number}'
                    if standalone and course_name.lower().endswith('.pdf'):
                        course_name = course_name[:-4]
                    identity = 'kngId=' + entry['id'] if entry.get('id') else course_key(url)
                    course_id = hashlib.sha256(identity.encode()).hexdigest()[:10]
                    folder = output / (safe_name(course_name) + '-' + FOLDER_SUFFIX)
                    conflicts = [record for record_key, record in report.items()
                                 if record.get('status') == 'ok' and record.get('file')
                                 and Path(record['file']).parts[0] == folder.name
                                 and not record_key.startswith(course_id + ':')]
                    if conflicts:
                        raise RuntimeError(f'另一个课程已使用文件夹 {folder.name}，停止当前课程以避免覆盖已有文件')
                    folder.mkdir(exist_ok=True)
                    print('保存目录：', folder.resolve(), flush=True)
                    course_failed = False
                    for index, chapter in enumerate(chapters, 1):
                        key = f"{course_id}:{chapter['type']}:{chapter['index']}:{chapter['title']}"
                        previous = report.get(key, {})
                        if previous.get('status') == 'ok':
                            old_file = output / previous['file']
                            if old_file.exists() and old_file.stat().st_size == previous['bytes']:
                                print('跳过已完成', chapter['title'])
                                stats['skipped'] += 1
                                continue
                        try:
                            step = '选择章节'
                            badge = None
                            if not standalone:
                                outline = await course_outline(page)
                                badge = outline.get_by_text(chapter['type'], exact=True).nth(chapter['index'])
                                await badge.click()
                                await wait_selected(badge, chapter)
                            print('等待章节资源及下载入口加载：', chapter['title'], flush=True)
                            step = '等待下载入口'
                            download_control = await wait_download_control(page, args.ready_timeout)
                            if badge is not None:
                                await wait_selected(badge, chapter)
                            step = '点击下载并等待文件'
                            download = await click_official_download(
                                page, download_control, chapter['type'], args.timeout,
                                output / 'last-download.json')
                            target = folder / chapter_filename(
                                index, chapter['title'], download.suggested_filename, chapter['type'])
                            temporary = target.with_suffix(target.suffix + '.part')
                            step = '保存文件'
                            await download.save_as(temporary)
                            if not temporary.exists() or temporary.stat().st_size == 0:
                                raise RuntimeError('平台下载未完成或文件为空')
                            with temporary.open('rb') as stream:
                                prefix = stream.read(1024).lstrip().lower()
                            if prefix.startswith((b'<!doctype html', b'<html')):
                                raise RuntimeError('下载结果为 HTML 页面，需要检查登录状态')
                            if target.suffix.lower() == '.pdf' and not prefix.startswith(b'%pdf-'):
                                raise RuntimeError('下载结果的文件头不是 PDF，未标记成功')
                            temporary.replace(target)
                            report[key] = {'status': 'ok', 'course': course_name,
                                           'chapter': chapter['title'], 'type': chapter['type'],
                                           'file': str(target.relative_to(output)),
                                           'bytes': target.stat().st_size}
                            # The final file already passed validation; a cleanup
                            # failure must not change its successful status.
                            try:
                                await download.cleanup()
                            except Exception:
                                pass
                            print('已下载', target.name)
                            stats['downloaded'] += 1
                        except Exception as error:
                            message = str(error) if isinstance(error, RuntimeError) else f'{step}失败（{type(error).__name__}）'
                            report[key] = {'status': 'failed', 'course': course_name,
                                           'chapter': chapter['title'], 'step': step, 'reason': message}
                            print('下载失败', chapter['title'], message)
                            stats['failed'] += 1
                            course_failed = True
                            await save_failure_page(page, output)
                            try:
                                debug = await (badge or download_control).evaluate('''element => {
                                    const result = [];
                                    for (let node = element, depth = 0; node && depth < 7; node = node.parentElement, depth++) {
                                        result.push({tag:node.tagName,className:node.className,text:node.innerText,
                                            background:getComputedStyle(node).backgroundColor,
                                            ariaSelected:node.getAttribute('aria-selected')});
                                    }
                                    return result;
                                }''')
                                (output / 'last-selection.json').write_text(
                                    json.dumps(debug, ensure_ascii=False, indent=2), encoding='utf-8')
                            except Exception:
                                print('失败诊断页面未能完整保存；下载错误仍会记录。')
                            print('已保存失败页面及点击诊断。停止当前课程，避免弹窗影响后续章节。')
                            save()
                            break
                        save()
                    old_error = report.get(error_key, {})
                    if (not course_failed and old_error.get('status') == 'failed'
                            and old_error.get('course_url') == url):
                        report[error_key] = {**old_error, 'status':'resolved',
                                             'resolution':'课程已重新处理成功'}
                        save()
                except Exception as error:
                    message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                    report[error_key] = {'status': 'failed', 'reason': message,
                                                              'course_url':url, 'course':entry.get('title')}
                    stats['failed'] += 1
                    print('课程处理失败', message)
                    await save_failure_page(page, output)
                    save()
            ok = sum(item.get('status') == 'ok' for item in report.values())
            failed = sum(item.get('status') == 'failed' for item in report.values())
            print(f"\n本次：新下载 {stats['downloaded']}，跳过已完成 {stats['skipped']}，失败 {stats['failed']}。")
            print(f'历史记录累计：成功 {ok}，失败 {failed}。下载记录：{report_path}')
            print('请打开首个 PDF/视频验证内容、章节对应关系和视频完整性。')
            return stats
        finally:
            save()


if __name__ == '__main__':
    try:
        from .common import download_run_lock
        with download_run_lock(BASE/'output'):
            asyncio.run(main())
    except KeyboardInterrupt:
        print('\n已停止；完成的下载记录会保留。')
    except RuntimeError as error:
        print(str(error))
        raise SystemExit(1)
