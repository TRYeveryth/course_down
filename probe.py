"""Interactive resource probe. Site-specific automatic traversal is not configured yet."""
import asyncio
import hashlib
import json
import re
import argparse
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, unquote

import common as settings
from common import BASE, safe_name


def resource_kind(url, content_type):
    path = unquote(urlsplit(url).path).lower()
    mime = content_type.split(';', 1)[0].strip().lower()
    if path.endswith('.pdf') or mime == 'application/pdf':
        return 'pdf'
    if path.endswith('.m3u8') or mime in {
        'application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl'
    }:
        return 'hls'
    if path.endswith('.mpd') or mime == 'application/dash+xml':
        return 'dash'
    # Ignore HLS/DASH fragments; saving a fragment would not save the full video.
    if path.endswith('.mp4') or mime == 'video/mp4':
        return 'mp4'
    return None


def public_url(url):
    """Remove query, fragment and userinfo from the report; originals stay in memory."""
    parts = urlsplit(url)
    host = parts.hostname or ''
    if parts.port:
        host += ':' + str(parts.port)
    return urlunsplit((parts.scheme, host, parts.path, '', ''))


def download_file(resource, cookies, target):
    import requests

    # Captured credentials are sent only to the exact observed resource endpoint.
    # Refuse redirects rather than forwarding proprietary token headers elsewhere.
    headers = {k: v for k, v in resource['headers'].items()
               if k.lower() not in {'range', 'host', 'content-length', 'accept-encoding'}
               and not k.startswith(':')}
    with requests.Session() as session:
        for cookie in cookies:
            session.cookies.set(cookie['name'], cookie['value'],
                                domain=cookie['domain'], path=cookie['path'],
                                secure=cookie['secure'])
        with session.get(resource['url'], headers=headers, stream=True,
                         timeout=(20, 90), allow_redirects=False) as response:
            if response.status_code != 200:
                raise RuntimeError(f'HTTP {response.status_code}：需重新打开资源或适配下载接口')
            kind = resource['kind']
            mime = response.headers.get('content-type', '').lower()
            if 'text/html' in mime or 'application/json' in mime:
                raise RuntimeError('返回了网页/JSON，可能登录已失效')
            temporary = target.with_suffix(target.suffix + '.part')
            size = 0
            prefix = b''
            with temporary.open('wb') as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        if len(prefix) < 4096:
                            prefix += chunk[:4096 - len(prefix)]
                        size += len(chunk)
                        output.write(chunk)
            expected = response.headers.get('content-length')
            if size == 0 or (expected and size != int(expected)):
                raise RuntimeError('文件为空或长度不完整，未标记成功')
            if kind == 'pdf' and b'%PDF-' not in prefix[:1024]:
                raise RuntimeError('下载内容不是 PDF')
            if kind == 'mp4' and b'ftyp' not in prefix[:4096]:
                raise RuntimeError('下载内容未识别为完整 MP4 容器')
            temporary.replace(target)
            return size


async def main(argv=None):
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description='交互式资源探测：人工打开课程后识别完整 PDF/MP4')
    parser.add_argument('--url', default=settings.COURSE_URL or settings.CATALOG_URL,
                        help='要打开的课程或目录网址；默认从 config.py 读取')
    args = parser.parse_args(argv)
    try:
        if not args.url:
            raise ValueError('请填写 config.py 的 COURSE_URL / CATALOG_URL，或使用 --url 传入链接')
        settings.configure_site(args.url)
    except ValueError as error:
        parser.error(str(error))

    output = BASE / 'output'
    output.mkdir(exist_ok=True)
    resources = {}
    pending = set()
    current_course = '样本课程'
    results = []
    context = None

    def write_report():
        data = {'mode': 'probe_only', 'automatic_traversal_verified': False,
                'resources': [{'id': key, 'course': item['course'],
                               'type': item['kind'], 'url_without_query': public_url(item['url'])}
                              for key, item in resources.items()], 'downloads': results}
        (output / 'report.json').write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding='utf-8')

    async def inspect_response(response, course):
        try:
            kind = resource_kind(response.url, response.headers.get('content-type', ''))
            if kind and response.status in {200, 206}:
                identity = hashlib.sha256((course + '\n' + response.url).encode()).hexdigest()[:12]
                resources[identity] = {'url': response.url, 'kind': kind, 'course': course,
                                       'headers': await response.request.all_headers()}
                write_report()
                print(f'\n发现 {kind.upper()}：{identity}（{course}）', flush=True)
        except Exception:
            print('\n一条资源请求未能读取，请重新打开该资源。', flush=True)

    def on_response(response):
        task = asyncio.create_task(inspect_response(response, current_course))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(BASE / 'profile'), channel='msedge', headless=False, accept_downloads=True)
        context.on('response', on_response)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
        except Exception:
            print('页面导航超时。可在打开的 Edge 中手动访问课程网址。')
        print('\n请在新 Edge 窗口登录。先输入 n 设置课程名，再打开该课程播放视频、预览 PDF。')
        print('命令：n=设置课程名；s=保存当前页面结构；l=列出资源；d=下载已发现 MP4/PDF；q=退出')
        try:
            while True:
                command = (await asyncio.to_thread(input, '\n命令> ')).strip().lower()
                if command == 'q':
                    break
                if command == 'n':
                    current_course = (await asyncio.to_thread(input, '课程名> ')).strip() or '样本课程'
                elif command == 'l':
                    for key, item in resources.items():
                        print(key, item['kind'], item['course'])
                    print('共', len(resources), '条候选资源；不代表目录中的全部课程。')
                elif command == 's':
                    pages = [p for p in context.pages if not p.is_closed()]
                    print('打开的页面：')
                    for number, candidate in enumerate(pages):
                        print(number, await candidate.title())
                    choice = (await asyncio.to_thread(input, '要保存的页面编号（默认 0）> ')).strip() or '0'
                    if not choice.isdigit() or int(choice) >= len(pages):
                        print('页面编号无效。')
                        continue
                    selected = pages[int(choice)]
                    snapshots = []
                    for frame in selected.frames:
                        try:
                            snapshots.append({'url_without_query': public_url(frame.url),
                                              'body_text': await frame.locator('body').inner_text(timeout=10000),
                                              'links': await frame.locator('a').evaluate_all(
                                                  '(nodes) => nodes.map(n => ({text:n.innerText, href:n.href}))')})
                        except Exception:
                            snapshots.append({'error': '无法读取该框架'})
                    (output / 'page-structure.json').write_text(
                        json.dumps(snapshots, ensure_ascii=False, indent=2), encoding='utf-8')
                    await selected.screenshot(path=str(output / 'page.png'), full_page=True)
                    print('已保存 output/page-structure.json 和 output/page.png（仅本地）。')
                elif command == 'd':
                    if pending:
                        await asyncio.gather(*list(pending))
                    for identity, resource in list(resources.items()):
                        if resource['kind'] not in {'mp4', 'pdf'}:
                            print(identity, '是分段视频，需要进一步适配，不会保存为假 MP4。')
                            continue
                        folder = output / safe_name(resource['course'])
                        folder.mkdir(exist_ok=True)
                        target = folder / (identity + '.' + resource['kind'])
                        completed = next((r for r in results if r['id'] == identity and r['status'] == 'ok'), None)
                        if completed and target.exists() and target.stat().st_size == completed['bytes']:
                            print('跳过本次会话已完成资源', identity)
                            continue
                        try:
                            cookies = await context.cookies([resource['url']])
                            size = await asyncio.to_thread(download_file, resource, cookies, target)
                            results.append({'id': identity, 'status': 'ok', 'bytes': size,
                                            'file': str(target.relative_to(output))})
                            print('已下载', target.name, size, '字节')
                        except Exception as error:
                            # Do not print exceptions that might contain signed URLs or tokens.
                            message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                            results.append({'id': identity, 'status': 'failed', 'reason': message})
                            print('下载失败', identity, message)
                        write_report()
                else:
                    print('请输入 n / s / l / d / q。')
        finally:
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            write_report()
            await context.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n已停止。下载报告保存在 output 文件夹。')
