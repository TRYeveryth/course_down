"""Shared user configuration, URL validation, browser sessions and run locking."""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs

BASE = Path(__file__).resolve().parent


def load_config(path):
    spec = importlib.util.spec_from_file_location('_course_tools_config', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_local = BASE / 'config.py'
_config = load_config(_local if _local.is_file() else BASE / 'config.example.py')
CATALOG_URL = getattr(_config, 'CATALOG_URL', '').strip()
COURSE_URL = getattr(_config, 'COURSE_URL', '').strip()
SITE_ORIGIN = getattr(_config, 'SITE_ORIGIN', '').strip().rstrip('/')
CDP_URL = getattr(_config, 'CDP_URL', '').strip()
API_HOST_SUFFIXES = tuple(getattr(_config, 'API_HOST_SUFFIXES', ()))
FOLDER_SUFFIX = getattr(_config, 'FOLDER_SUFFIX', '').strip() or datetime.now().strftime('%y-%m')


def https_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in {None, 443}):
        raise ValueError('课程网址必须是无用户名/密码的 HTTPS 链接')
    return urlunsplit(('https', parsed.hostname, '', '', ''))


def site_origin():
    value = SITE_ORIGIN or CATALOG_URL or COURSE_URL
    return https_origin(value) if value else ''


def validate_target(value):
    actual = https_origin(value)
    expected = site_origin()
    if expected and actual != expected:
        raise ValueError('课程网址与配置中的站点不一致；请检查 config.py 的 SITE_ORIGIN 和课程链接')
    return value


def configure_site(value=None):
    """Bind explicit CLI links to this run; retain the configured origin boundary."""
    global SITE_ORIGIN
    value = value or COURSE_URL or CATALOG_URL
    if value:
        validate_target(value)
        SITE_ORIGIN = https_origin(value)
    elif not site_origin():
        raise ValueError('请复制 config.example.py 为 config.py，并填写 CATALOG_URL / COURSE_URL；也可用命令行传入链接')
    return site_origin()


def is_site_url(value):
    try:
        return bool(site_origin()) and https_origin(value) == site_origin()
    except ValueError:
        return False


def is_api_url(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and (is_site_url(value) or any(host == suffix.lstrip('.') or host.endswith('.' + suffix.lstrip('.'))
                 for suffix in API_HOST_SUFFIXES if suffix)))


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:100]
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value or '课程'


def local_cdp_url(value):
    parsed = urlsplit(value)
    try:
        valid_port = parsed.port
    except ValueError:
        valid_port = None
    if (parsed.scheme not in {'http', 'https', 'ws', 'wss'}
            or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or not valid_port):
        raise argparse.ArgumentTypeError('CDP 地址必须为带端口的本机地址；请检查 config.py 的 CDP_URL')
    return value


def course_url(value):
    try:
        validate_target(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def is_course_page(url):
    parsed = urlsplit(url)
    return (is_site_url(url)
            and parsed.path.startswith('/kng/')
            and parsed.fragment.lstrip('/').startswith('course/play?'))


def course_key(url):
    parsed = urlsplit(url)
    _, _, query = parsed.fragment.partition('?')
    identifier = parse_qs(query).get('kngId', [None])[0]
    return 'kngId=' + identifier if identifier else url


@dataclass
class BrowserSession:
    context: object
    page: object
    initial_url: str
    attached: bool


def resolve_cdp_endpoint(endpoint):
    """Support legacy JSON discovery and Chrome's consent-based WebSocket server."""
    parsed = urlsplit(endpoint)
    if parsed.scheme in {'ws', 'wss'}:
        return endpoint
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint.rstrip('/') + '/json/version', timeout=5) as response:
            metadata = json.load(response)
            return local_cdp_url(metadata['webSocketDebuggerUrl'])
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        directories = [BASE / 'chrome-profile']
        if os.environ.get('LOCALAPPDATA'):
            directories.append(Path(os.environ['LOCALAPPDATA']) / 'Google' / 'Chrome' / 'User Data')
        for directory in directories:
            try:
                lines = (directory / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
                if (len(lines) >= 2 and int(lines[0]) == parsed.port
                        and lines[1].startswith('/devtools/browser/')
                        and not any(char in lines[1] for char in ('?', '#', ' ', '\\'))):
                    return local_cdp_url(f'{scheme}://{parsed.netloc}{lines[1]}')
            except (OSError, ValueError):
                continue
        return f'{scheme}://{parsed.netloc}/devtools/browser'


@asynccontextmanager
async def browser_session(playwright, endpoint=None, target_url=None, inspect_only=False):
    browser = None
    context = None
    try:
        if endpoint:
            try:
                print('正在连接 Chrome；如出现调试授权提示，请在 Chrome 中点击“允许”。', flush=True)
                endpoint = await asyncio.to_thread(resolve_cdp_endpoint, endpoint)
                browser = await playwright.chromium.connect_over_cdp(
                    endpoint, timeout=180000, is_local=True, no_defaults=inspect_only,
                    artifacts_dir=str(BASE / 'output' / 'cdp-artifacts'))
            except Exception as error:
                try:
                    diagnostic_dir = BASE / 'output'
                    diagnostic_dir.mkdir(exist_ok=True)
                    (diagnostic_dir / 'cdp-connect-error.txt').write_text(str(error)[:12000], encoding='utf-8')
                except OSError:
                    pass
                raise RuntimeError(
                    '未能连接 Chrome。请确认调试服务已开启，并在 Chrome 的调试授权提示中点“允许”。'
                    '也可运行 scripts/start_chrome_cdp.cmd。脚本不会自动关闭或重启 Chrome。'
                    f'（连接错误类型：{type(error).__name__}；详细日志：output/cdp-connect-error.txt）') from error
            if len(browser.contexts) != 1:
                raise RuntimeError('Chrome 暴露的浏览器配置不唯一，无法确定应使用哪个登录会话。')
            context = browser.contexts[0]
            original_pages = [p for p in context.pages if not p.is_closed()]
            if not target_url:
                candidates = [p for p in original_pages if is_course_page(p.url)]
                if len({course_key(candidate.url) for candidate in candidates}) == 1:
                    target_url = candidates[0].url
            initial_url = target_url or COURSE_URL or CATALOG_URL or 'about:blank'
            # Keep original tabs untouched. Cookies/localStorage share this context.
            page = await context.new_page()
            matching = [p for p in original_pages if p.url == initial_url]
            if not matching:
                # A batch starts at the directory while the logged-in source tab
                # may be a course. Both belong to this same enterprise origin.
                matching = [p for p in original_pages if is_site_url(p.url)
                            and urlsplit(p.url).path.startswith('/kng/')]
            if matching:
                # sessionStorage is per-tab. Copy only from the selected same-site tab,
                # hold it in memory, and never overwrite newer state during navigation.
                source = matching[0]
                storage = await source.evaluate('() => Object.fromEntries(Object.entries(sessionStorage))')
                origin = site_origin()
                script = ('(() => { const origin = ' + json.dumps(origin) + '; '
                          'const data = ' + json.dumps(storage) + '; '
                          'if (location.origin === origin) { for (const [key, value] of Object.entries(data)) '
                          '{ if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value); } } })();')
                await page.add_init_script(script)
        else:
            context = await playwright.chromium.launch_persistent_context(
                str(BASE / 'profile'), channel='msedge', headless=False, accept_downloads=True)
            page = context.pages[0] if context.pages else await context.new_page()
            initial_url = target_url or COURSE_URL or CATALOG_URL or 'about:blank'
        yield BrowserSession(context, page, initial_url, bool(endpoint))
    finally:
        if endpoint:
            if browser is not None:
                # Playwright CDP Browser.close disconnects the transport, not Chrome.
                # Never close the externally owned default browser context or its tabs.
                await browser.close()
        elif context is not None:
            await context.close()


@contextmanager
def download_run_lock(output):
    output.mkdir(parents=True,exist_ok=True)
    stream=(output/'download-run.lock').open('a+b')
    if stream.tell()==0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    acquired=False
    try:
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
        except OSError as error:
            raise RuntimeError('已有下载脚本正在运行，请等待它结束后再运行，避免同时操作 Chrome 和下载记录。') from error
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name=='nt':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()
