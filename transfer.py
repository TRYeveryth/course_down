"""Official download responses, native downloads and validated streamed files."""
import asyncio
import hashlib
from dataclasses import dataclass
import http.cookiejar
import http.client
import json
import re
import time
import urllib.error
import urllib.request
import common as settings
from urllib.parse import unquote, urljoin, urlsplit, parse_qsl, urlencode, urlunsplit, quote


def is_download_api_response(url):
    parsed = urlsplit(url)
    return settings.is_api_url(url) and parsed.path.rstrip('/') == '/kng/knowledge/downloadurl'


def file_url(value, base_url, allow_images=False):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.startswith(('https://', 'http://', '//', '/')):
        return None
    candidate = urljoin(base_url, value)
    parsed, base = urlsplit(candidate), urlsplit(base_url)
    local_same_origin = (base.scheme == 'http' and base.hostname in {'127.0.0.1', 'localhost', '::1'}
                         and (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc))
    if (parsed.scheme != 'https' and not local_same_origin) or not parsed.hostname or parsed.username or parsed.password:
        return None
    path = unquote(parsed.path).lower()
    if (path.endswith(('.jpg', '.jpeg', '.png', '.svg', '.webp', '.gif'))
            and not (allow_images and path.endswith(('.jpg','.jpeg','.png')))):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc,
        quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
        quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~"), ''))


def extract_download_urls(payload, base_url):
    """Accept file URLs in data/result or URL fields; never synthesize a CDN path."""
    found = []
    url_keys = {'data', 'result', 'url', 'urls', 'downloadurl', 'downloadurls',
                'fileurl', 'fileurls', 'sourceurl', 'originalurl', 'href', 'link', 'downloadlink'}
    def walk(value, key='', depth=0):
        if depth > 10:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key).replace('_', '').lower(), depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, key, depth + 1)
        elif isinstance(value, str):
            allowed_key = (key in url_keys or key.endswith(('url', 'urls'))
                           or key in {'downloadaddress', 'downloadpath', 'filepath'})
            if allowed_key and not re.search(r'preview|thumbnail|cover|avatar|icon|license', key):
                candidate = file_url(value, base_url, allow_images='download' in key)
                if candidate:
                    found.append(candidate)
                    return
            if value.lstrip().startswith(('{', '[')):
                try:
                    walk(json.loads(value), key, depth + 1)
                except (ValueError, TypeError):
                    pass
    walk(payload, 'data')
    return list(dict.fromkeys(found))


def file_identity(url):
    """Rotating storage signatures do not make the same file a second candidate."""
    parsed = urlsplit(url)
    signed_keys = {'signature', 'expires', 'ossaccesskeyid', 'x-oss-signature',
                   'x-oss-date', 'x-oss-expires', 'x-oss-credential', 'x-oss-security-token',
                   'x-amz-signature', 'x-amz-date', 'x-amz-expires', 'x-amz-credential',
                   'x-amz-security-token', 'x-amz-signedheaders'}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in signed_keys]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(sorted(query)), ''))


def sanitized_api_payload(payload, redact_url, key='', depth=0):
    """Keep structure and messages; signed links and credentials stay in memory."""
    if depth > 8:
        return '<超过诊断深度>'
    if re.search(r'token|cookie|secret|signature|authorization|password|session', key, re.I):
        return '<已隐藏>'
    if isinstance(payload, dict):
        return {str(k):sanitized_api_payload(v, redact_url, str(k), depth + 1)
                for k, v in list(payload.items())[:50]}
    if isinstance(payload, list):
        return [sanitized_api_payload(value, redact_url, key, depth + 1) for value in payload[:20]]
    if isinstance(payload, str):
        if payload.startswith(('https://', 'http://')):
            return redact_url(payload)
        if payload.lstrip().startswith(('{', '[')):
            try:
                return sanitized_api_payload(json.loads(payload), redact_url, key, depth + 1)
            except ValueError:
                pass
        if key.lower() in {'code', 'status', 'state', 'message', 'msg', 'errmsg', 'error', 'filename', 'name'}:
            return re.sub(r'https?://[^\s"<>]+', lambda match:redact_url(match.group()), payload)[:300]
        return f'<字符串，{len(payload)} 字符>'
    return payload


def api_message(payload):
    if not isinstance(payload, dict):
        return ''
    for key in ('message', 'msg', 'errmsg', 'errorMessage'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            # Do not print a query string or token included in a server message.
            return re.sub(r'https?://\S+', '<链接已隐藏>', value.strip())[:250]
    return ''


def api_filename(payload, url, kind):
    def find(value, depth=0):
        if depth > 8:
            return ''
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).replace('_', '').lower() in {'filename', 'originalfilename'} and isinstance(child, str):
                    return child
            for child in value.values():
                result = find(child, depth + 1)
                if result:
                    return result
        return ''
    filename = find(payload)
    if not filename:
        tail = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
        if tail.lower().endswith(('.pdf', '.mp4', '.png', '.jpg', '.jpeg')):
            filename = tail
    return filename or ('官方文档.pdf' if kind == '文档' else '官方视频.mp4')


class FileRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # CookieJar re-evaluates domain/path on redirects. Never attach the
        # learning API's authorization headers to a storage/CDN request.
        if not file_url(newurl, request.full_url, allow_images=True):
            raise RuntimeError('官方文件跳转到了不支持的地址，已停止下载')
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        if redirected and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            keep = {'user-agent', 'referer', 'accept-encoding'}
            for key in list(redirected.headers):
                if key.lower() not in keep:
                    redirected.remove_header(key)
        return redirected


def stream_api_file(url, cookies, user_agent, referer, kind, target, resource_headers=None):
    jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(
        strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
    for cookie in cookies:
        domain = cookie['domain']
        expires = cookie.get('expires', -1)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=cookie['name'], value=cookie['value'], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'), domain_initial_dot=domain.startswith('.'),
            path=cookie.get('path', '/'), path_specified=True, secure=cookie.get('secure', False),
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None, rest={}, rfc2109=False))
    opener = urllib.request.build_opener(FileRedirects(), urllib.request.HTTPCookieProcessor(jar))
    request_headers = dict(resource_headers or {})
    request_headers.update({'User-Agent':user_agent, 'Referer':referer, 'Accept-Encoding':'identity'})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with opener.open(request, timeout=90) as response:
            if response.status != 200 or response.headers.get('Content-Range'):
                raise RuntimeError(f'官方文件返回 HTTP {response.status}，未保存分段或错误响应')
            mime = response.headers.get('Content-Type', '').lower()
            if any(value in mime for value in ('text/html', 'application/json', 'mpegurl', 'dash+xml')):
                raise RuntimeError('官方地址返回网页、JSON 或视频播放清单，没有完整 PDF/MP4 文件')
            expected = response.headers.get('Content-Length')
            size = 0
            prefix = b''
            tail = b''
            last_progress = time.monotonic()
            with target.open('wb') as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    prefix = (prefix + chunk)[:4096] if len(prefix) < 4096 else prefix
                    tail = (tail + chunk)[-2048:]
                    output.write(chunk)
                    if time.monotonic() - last_progress >= 10:
                        print(f'正在保存官方文件：{size / (1024 * 1024):.1f} MB', flush=True)
                        last_progress = time.monotonic()
            if not size or (expected and size != int(expected)):
                raise RuntimeError('官方文件为空或下载长度不完整，未标记成功')
            image_suffix = unquote(urlsplit(response.url).path).lower()
            if kind == '文档' and (image_suffix.endswith('.png') or mime.startswith('image/png')):
                if not prefix.startswith(b'\x89PNG\r\n\x1a\n') or tail[-12:] != b'\x00\x00\x00\x00IEND\xaeB`\x82':
                    raise RuntimeError('官方图片不是完整 PNG，未标记成功')
            elif kind == '文档' and (image_suffix.endswith(('.jpg','.jpeg')) or mime.startswith('image/jpeg')):
                if not prefix.startswith(b'\xff\xd8') or not tail.endswith(b'\xff\xd9'):
                    raise RuntimeError('官方图片不是完整 JPEG，未标记成功')
            elif kind == '文档' and (not prefix.lstrip().startswith(b'%PDF-') or b'%%EOF' not in tail):
                raise RuntimeError('官方地址返回的文档不是完整 PDF，未标记成功')
            if kind == '视频' and (len(prefix) < 12 or prefix[4:8] != b'ftyp'):
                raise RuntimeError('官方地址返回的文件不是 MP4，未标记成功')
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'官方文件返回 HTTP {error.code}，需要检查接口结果或链接是否过期') from error
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        raise RuntimeError(f'保存官方文件时网络或文件操作失败（{type(error).__name__}）') from error


def redacted_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'}:
        return parsed.scheme + ':'
    return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))


@dataclass
class OfficialTransfer:
    download: object = None
    pdf: bytes = None
    suggested_filename: str = ''
    resource_url: str = ''
    cookies: object = None
    user_agent: str = ''
    referer: str = ''
    kind: str = ''
    resource_headers: object = None
    native_timeout_seconds: float = 90

    async def save_as(self, path):
        if self.download is not None:
            timed_out=False
            try:
                await asyncio.wait_for(self.download.save_as(path),self.native_timeout_seconds)
            except asyncio.TimeoutError:
                timed_out=True
                await self.download.cancel()
            failed=timed_out or await self.download.failure()
            if not failed and path.is_file() and path.stat().st_size > 0:
                return
            if not self.resource_url:
                raise RuntimeError('浏览器下载超时、失败或返回空文件，且本次下载没有可用的官方文件地址')
            print('浏览器文件未完整保存，正在通过本次官方下载地址重新保存。',flush=True)
        if self.resource_url:
            await asyncio.to_thread(stream_api_file, self.resource_url, self.cookies, self.user_agent,
                                    self.referer, self.kind, path, self.resource_headers)
        elif self.download is None:
            await asyncio.to_thread(path.write_bytes, self.pdf)

    async def cleanup(self):
        """Delete only this transfer's browser temporary copy after saving."""
        if self.download is not None:
            await self.download.delete()


async def click_official_download(page, control, kind, timeout_seconds, diagnostic_path):
    """Only accept downloads/responses from this page or its child pages, after click.

    PDF responses are saved from the browser's original request. The official
    download-url API can instead provide a signed file URL for a streamed GET.
    """
    context = page.context
    initial_pages = set(context.pages)
    diagnostic = {'stage': 'prepare', 'click_completed': False,
                  'responses': [], 'popups': [], 'requests_failed': [], 'native_downloads': []}
    requests = set()
    tasks = set()
    attached = {}
    downloads = []
    pdfs = {}
    api_urls = {}
    api_replies = []
    api_requests = set()
    wake = asyncio.Event()
    armed = False
    last_pdf_at = 0
    pending_bodies = 0
    pending_api = 0
    last_api_at = 0
    loop = asyncio.get_running_loop()

    async def belongs(source):
        # An unrelated existing tab must never supply a file for this chapter.
        seen = set()
        while source is not None and source not in seen:
            if source is page:
                return True
            if source in initial_pages:
                return False
            seen.add(source)
            source = await source.opener()
        return False

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        def finished(completed):
            tasks.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error:
                    diagnostic.setdefault('observer_errors', []).append(type(error).__name__)
        task.add_done_callback(finished)

    async def receive_download(source, download):
        if armed and await belongs(source):
            downloads.append(download)
            diagnostic['native_downloads'].append({'name':download.suggested_filename,
                                                  'url':redacted_url(download.url)})
            wake.set()

    def attach(source):
        handler = lambda download: spawn(receive_download(source, download))
        attached[source] = handler
        source.on('download', handler)

    def request_started(request):
        if armed:
            requests.add(request)
            try:
                if is_download_api_response(request.url) and request.frame.page is page:
                    api_requests.add(request)
            except Exception:
                pass

    async def response_received(response):
        nonlocal last_pdf_at, pending_bodies, pending_api, last_api_at
        if response.request not in requests:
            return
        try:
            source = response.request.frame.page
            if not await belongs(source):
                return
            headers = response.headers
            mime = headers.get('content-type', '').split(';')[0].lower()
            disposition = headers.get('content-disposition', '')
            item = {'url':redacted_url(response.url), 'status':response.status,
                    'mime':mime, 'resource_type':response.request.resource_type,
                    'attachment':disposition.lower().startswith('attachment')}
            if len(diagnostic['responses']) < 100:
                diagnostic['responses'].append(item)
            if is_download_api_response(response.url):
                pending_api += 1
                try:
                    payload = await response.json()
                    urls = extract_download_urls(payload, response.url)
                    api_replies.append(payload)
                    diagnostic.setdefault('download_api', []).append({
                        'status':response.status, 'result':sanitized_api_payload(payload, redacted_url),
                        'file_url_count':len(urls)})
                    explicitly_denied = isinstance(payload, dict) and payload.get('success') is False
                    if response.status == 200 and not explicitly_denied:
                        for url in urls:
                            api_urls[file_identity(url)] = (url, payload, response)
                    last_api_at = loop.time()
                    wake.set()
                finally:
                    pending_api -= 1
                    api_requests.discard(response.request)
                return
            # Partial video segments and PDF preview images are not complete files.
            is_pdf = mime == 'application/pdf' or (
                mime in {'application/octet-stream', 'binary/octet-stream'}
                and re.search(r'\.pdf(?:[";\s]|$)', unquote(disposition), re.I))
            if kind != '文档' or response.status != 200 or not is_pdf:
                return
            if int(headers.get('content-length', '0')) > 64 * 1024 * 1024:
                item['body_result'] = 'PDF 超过 64 MB，需浏览器下载事件'
                return
            pending_bodies += 1
            try:
                payload = await response.body()
                if (len(payload) > 64 * 1024 * 1024 or
                        not payload.lstrip().startswith(b'%PDF-') or
                        b'%%EOF' not in payload[-2048:]):
                    item['body_result'] = '不是完整 PDF，未保存'
                    return
                expected = headers.get('content-length')
                if expected and not headers.get('content-encoding') and len(payload) != int(expected):
                    item['body_result'] = 'PDF 长度不匹配，未保存'
                    return
                filename = '官方文档.pdf'
                match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"|filename=([^;]+)", disposition, re.I)
                if match:
                    filename = unquote(next(value for value in match.groups() if value)).strip()
                elif urlsplit(response.url).path.lower().endswith('.pdf'):
                    filename = unquote(urlsplit(response.url).path.rsplit('/', 1)[-1])
                pdfs[hashlib.sha256(payload).hexdigest()] = OfficialTransfer(pdf=payload, suggested_filename=filename)
                item['body_result'] = '完整 PDF'
                last_pdf_at = loop.time()
                wake.set()
            finally:
                pending_bodies -= 1
        except Exception as error:
            # Native attachment response bodies may be unavailable; the download
            # listener remains active and is preferred to the response fallback.
            diagnostic.setdefault('response_errors', []).append(type(error).__name__)

    def request_failed(request):
        api_requests.discard(request)
        if request in requests:
            # Filter ownership asynchronously, without printing headers or tokens.
            async def record():
                try:
                    if await belongs(request.frame.page):
                        diagnostic['requests_failed'].append({'url':redacted_url(request.url),
                                                              'failure':request.failure})
                except Exception:
                    pass
            spawn(record())

    response_handler = lambda response: spawn(response_received(response))
    attach(page)
    context.on('page', attach)
    context.on('request', request_started)
    context.on('response', response_handler)
    context.on('requestfailed', request_failed)
    try:
        diagnostic['control'] = await control.evaluate('''element => {
            const result = [];
            for (let node=element, depth=0; node && depth<5; node=node.parentElement, depth++) {
                const text=(node.innerText || '').slice(0,500);
                result.push({tag:node.tagName,className:node.className,text,
                    role:node.getAttribute('role'),disabled:node.hasAttribute('disabled'),
                    cursor:getComputedStyle(node).cursor,hasInlineClick:!!node.onclick});
            } return result;
        }''')
        diagnostic['stage'] = 'click'
        armed = True
        print('已找到下载入口，正在点击并等待文件。', flush=True)
        try:
            await control.click(timeout=min(15, timeout_seconds) * 1000)
        except Exception as error:
            raise RuntimeError(f'点击下载入口未完成（{type(error).__name__}）；见 output/last-download.json') from error
        diagnostic['click_completed'] = True
        diagnostic['stage'] = 'wait_file'
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if downloads:
                if len(downloads) != 1:
                    raise RuntimeError('本次点击触发多个下载，已停止以避免文件错配')
                diagnostic['stage'] = 'native_download'
                address=file_url(downloads[0].url,page.url,allow_images=True)
                transfer=OfficialTransfer(download=downloads[0], suggested_filename=downloads[0].suggested_filename,
                    resource_url=address or '',cookies=await context.cookies(address) if address else [],
                    user_agent=await page.evaluate('() => navigator.userAgent'),
                    referer=redacted_url(page.url),kind=kind,native_timeout_seconds=max(90,timeout_seconds))
                if kind=='视频' and address:
                    await downloads[0].cancel()
                    transfer.download=None
                    diagnostic['stage']='native_video_file_url'
                    print('已取得官方下载事件中的视频地址，正在保存完整 MP4。',flush=True)
                return transfer
            if len(pdfs) > 1:
                raise RuntimeError('本次点击返回多个不同 PDF，已停止以避免文件错配')
            if pdfs and not pending_bodies and loop.time() - last_pdf_at >= 0.75:
                diagnostic['stage'] = 'pdf_response'
                print('官方入口返回完整 PDF，正在保存。', flush=True)
                return next(iter(pdfs.values()))
            if api_replies and not api_requests and not pending_api and loop.time() - last_api_at >= 1:
                latest = api_replies[-1]
                if not api_urls and isinstance(latest, dict) and latest.get('success') is False:
                    raise RuntimeError('官方下载接口拒绝下载：' + (api_message(latest) or '平台返回 success=false')
                                       + '；见 output/last-download.json')
                if len(api_urls) > 1:
                    raise RuntimeError('官方下载接口返回多个不同地址，已停止以避免文件错配；见 output/last-download.json')
                if api_urls:
                    url, payload, response = next(iter(api_urls.values()))
                    if unquote(urlsplit(url).path).lower().endswith(('.m3u8', '.mpd')):
                        raise RuntimeError('官方下载接口返回视频播放清单，当前仅支持完整 PDF/MP4 文件')
                    diagnostic['stage'] = 'api_file_url'
                    print('官方下载接口已返回文件地址，正在保存文件。', flush=True)
                    resource_headers = {}
                    api_origin, file_origin = urlsplit(response.url), urlsplit(url)
                    if (api_origin.scheme, api_origin.netloc) == (file_origin.scheme, file_origin.netloc):
                        observed = await response.request.all_headers()
                        resource_headers = {key:value for key, value in observed.items()
                            if key.lower() in {'authorization', 'token', 'x-token', 'x-access-token', 'access_token'}}
                    return OfficialTransfer(resource_url=url, cookies=await context.cookies(url),
                        user_agent=await page.evaluate('() => navigator.userAgent'),
                        referer=redacted_url(page.url), kind=kind,
                        suggested_filename=api_filename(payload, url, kind), resource_headers=resource_headers)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), min(0.25, max(0.001, deadline - loop.time())))
            except asyncio.TimeoutError:
                pass
        if api_replies:
            message = next((api_message(payload) for payload in reversed(api_replies) if api_message(payload)), '')
            raise RuntimeError('官方下载接口已响应，但没有可识别的文件地址。'
                               + (f'平台消息：{message}。' if message else '')
                               + '接口结果已去除签名后保存到 output/last-download.json')
        raise RuntimeError(f'已点击下载，但 {timeout_seconds} 秒内未收到浏览器下载事件或完整 PDF。'
                           '点击后的响应和新标签页信息已保存到 output/last-download.json')
    except Exception as error:
        diagnostic['error_type'] = type(error).__name__
        if not isinstance(error, RuntimeError):
            raise RuntimeError(f"下载流程在 {diagnostic['stage']} 阶段失败（{type(error).__name__}）；"
                               '见 output/last-download.json') from error
        raise
    finally:
        armed = False
        context.remove_listener('page', attach)
        context.remove_listener('request', request_started)
        context.remove_listener('response', response_handler)
        context.remove_listener('requestfailed', request_failed)
        for source, handler in attached.items():
            source.remove_listener('download', handler)
        if tasks:
            pending = list(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for source in attached:
            if source is not page:
                try:
                    if await belongs(source):
                        diagnostic['popups'].append({'url':redacted_url(source.url), 'closed':source.is_closed()})
                except Exception:
                    pass
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
