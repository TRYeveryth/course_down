"""List recorded failures and retry their courses using the official downloader."""
import argparse
import asyncio
import hashlib
import json
from .common import BASE
from .common import local_cdp_url
from . import download_official
from . import common as settings


def failed_courses(courses, report):
    selected=[]
    rows=[]
    mapped=set()
    for course in courses:
        prefix=hashlib.sha256(('kngId='+course['id']).encode()).hexdigest()[:10]+':'
        failures=[(key,record) for key,record in report.items()
                  if record.get('status')=='failed' and
                  (key.startswith(prefix) or record.get('course_url')==course['url'])]
        if failures:
            selected.append(course)
            for key,record in failures:
                mapped.add(key)
                rows.append({'key':key,'order':course['order'],'course':course['title'],
                    'chapter':record.get('chapter'),'reason':record.get('reason'),
                    'course_url':course['url']})
    unmapped=[key for key,record in report.items() if record.get('status')=='failed' and key not in mapped]
    if unmapped:
        raise RuntimeError('有失败记录无法匹配课程清单，请先重新读取目录：'+', '.join(unmapped))
    return selected,rows


async def main(argv=None,existing_session=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cdp',type=local_cdp_url,default=settings.CDP_URL)
    parser.add_argument('--ready-timeout',type=int,default=120)
    parser.add_argument('--timeout',type=int,default=90)
    parser.add_argument('--list-only',action='store_true',help='只列出失败，不连接浏览器')
    args=parser.parse_args(argv)
    if min(args.ready_timeout,args.timeout)<1:
        parser.error('超时必须大于 0')
    output=BASE/'output'
    try:
        manifest=json.loads((output/'catalog-courses.json').read_text(encoding='utf-8'))
        report=json.loads((output/'official-downloads.json').read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise RuntimeError('缺少目录清单或下载记录，请先运行 python -m scripts.download_catalog') from error
    courses,rows=failed_courses(manifest['courses'],report)
    pending_path=output/'retry-pending-courses.json'
    pending=json.loads(pending_path.read_text(encoding='utf-8')) if pending_path.exists() else []
    target_urls={c['url'] for c in courses+pending}
    courses=[c for c in manifest['courses'] if c['url'] in target_urls]
    (output/'failed-downloads.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'失败记录 {len(rows)} 条，重试范围（含上次未完成部分）{len(courses)} 项课程：',flush=True)
    for index,row in enumerate(rows,1):
        print(f"{index:02d} {row['course']} / {row['chapter'] or '课程页面加载失败'}",flush=True)
        print('   '+(row['reason'] or '原因未记录'),flush=True)
    if not courses or args.list_only:
        return 0
    pending_path.write_text(json.dumps(courses,ensure_ascii=False,indent=2),encoding='utf-8')
    path=output/'failed-course-urls.txt'
    path.write_text('\n'.join(course['url'] for course in courses)+'\n',encoding='utf-8')
    print('只重试上述课程；已完成章节会跳过，并补齐首次失败后尚未处理的章节。',flush=True)
    stats=await download_official.main(['--cdp',args.cdp,'--courses',str(path),'--yes',
        '--ready-timeout',str(args.ready_timeout),'--timeout',str(args.timeout)],
        existing_session=existing_session,course_entries=courses)
    remaining=json.loads((output/'official-downloads.json').read_text(encoding='utf-8'))
    remaining_courses,failures=failed_courses(manifest['courses'],remaining)
    pending_path.write_text(json.dumps(remaining_courses,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"重试完成：新下载 {stats['downloaded']}，跳过 {stats['skipped']}，剩余失败 {len(failures)}。",flush=True)
    return 2 if stats['failed'] or failures else 0


if __name__=='__main__':
    try:
        from .common import download_run_lock
        with download_run_lock(BASE/'output'):
            raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n已停止，完成的记录保留。')
        raise SystemExit(130)
    except RuntimeError as error:
        print(str(error),flush=True)
        raise SystemExit(1)
