"""Check saved PDFs/videos against the download record, without network access."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from common import BASE


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(2 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_temporary_copies(output, files):
    """Remove only browser artifacts identical to verified final files."""
    root = (output / 'cdp-artifacts').resolve()
    if not root.is_relative_to(output) or not root.is_dir():
        return {'removed':0, 'bytes':0}
    by_size = {}
    for item in files:
        by_size.setdefault(item['bytes'],[]).append(Path(item['file']))
    hashes = {}
    result = {'removed':0, 'bytes':0}
    for artifact in root.rglob('*'):
        path = artifact.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        size = path.stat().st_size
        candidates = by_size.get(size,[])
        if not candidates:
            continue
        digest = file_digest(path)
        for target in candidates:
            if not target.resolve().is_relative_to(output) or not target.is_file():
                continue
            if target not in hashes:
                hashes[target] = file_digest(target)
            if digest == hashes[target]:
                path.unlink()
                result['removed'] += 1
                result['bytes'] += size
                print('已清理与正式文件完全相同的浏览器临时副本：',target.name,flush=True)
                break
    return result


def inspect_file(path):
    result = {'file':str(path), 'bytes':path.stat().st_size}
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        result['pages'] = len(reader.pages)
        if not result['pages']:
            raise ValueError('PDF 没有页面')
        result['first_page_text'] = (reader.pages[0].extract_text() or '')[:700]
    elif path.suffix.lower() == '.mp4':
        executable = shutil.which('ffprobe')
        if not executable:
            raise RuntimeError('缺少 ffprobe，无法核验视频时长')
        completed = subprocess.run([executable, '-v','error','-show_entries',
            'format=duration,size:stream=codec_type,width,height,duration','-of','json',str(path)],
            check=True, capture_output=True, text=True, encoding='utf-8', timeout=60)
        metadata = json.loads(completed.stdout)
        result['duration_seconds'] = float(metadata.get('format',{}).get('duration',0))
        result['streams'] = metadata.get('streams',[])
        if result['duration_seconds'] <= 0 or not any(s.get('codec_type') == 'video' for s in result['streams']):
            raise ValueError('视频没有可读取的视频轨或有效时长')
    elif path.suffix.lower() in {'.png','.jpg','.jpeg'}:
        from PIL import Image
        with Image.open(path) as picture:
            result['image_format'] = picture.format
            result['width'], result['height'] = picture.size
            picture.verify()
        if result['width'] <= 0 or result['height'] <= 0:
            raise ValueError('图片尺寸无效')
    else:
        raise ValueError('无法核验的文件类型')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'output')
    parser.add_argument('--cleanup-temp', action='store_true', help='检查通过后清理内容相同的浏览器临时副本')
    args = parser.parse_args()
    output = args.output.resolve()
    report = json.loads((output / 'official-downloads.json').read_text(encoding='utf-8'))
    manifest = json.loads((output / 'catalog-courses.json').read_text(encoding='utf-8'))
    pending_path=output/'retry-pending-courses.json'
    pending_urls={c['url'] for c in json.loads(pending_path.read_text(encoding='utf-8'))} if pending_path.exists() else set()
    summary = {'courses':[], 'files':[], 'errors':[]}
    for course in manifest['courses']:
        identity = 'kngId=' + course['id']
        key_prefix = hashlib.sha256(identity.encode()).hexdigest()[:10] + ':'
        records = [r for key,r in report.items() if key.startswith(key_prefix) and r.get('status') == 'ok']
        failed = [r for key,r in report.items() if r.get('status')=='failed' and
                  (key.startswith(key_prefix) or r.get('course_url')==course['url'])]
        if not records or failed or course['url'] in pending_urls:
            summary['errors'].append({'course':course['title'],
                'error':('课程尚未完成下载' if not records else
                         '课程仍有失败记录' if failed else '该课程所属重试批次尚未结束'),
                'failed_chapters':[r.get('chapter') or r.get('reason') for r in failed]})
        summary['courses'].append({'order':course['order'],'title':course['title'], 'files':len(records),
                                  'pdfs':sum(Path(r['file']).suffix.lower()=='.pdf' for r in records),
                                  'videos':sum(Path(r['file']).suffix.lower()=='.mp4' for r in records),
                                  'images':sum(Path(r['file']).suffix.lower() in {'.png','.jpg','.jpeg'} for r in records)})
        for record in records:
            path = (output / record['file']).resolve()
            try:
                if not path.is_relative_to(output):
                    raise ValueError('记录指向下载目录以外')
                if path.stat().st_size != record['bytes']:
                    raise ValueError('文件大小与成功记录不一致')
                metadata = inspect_file(path)
                metadata.update({'course':course['title'], 'chapter':record['chapter']})
                summary['files'].append(metadata)
            except Exception as error:
                summary['errors'].append({'file':record['file'], 'error':str(error)})
    if args.cleanup_temp and not summary['errors']:
        summary['temporary_cleanup'] = cleanup_temporary_copies(output,summary['files'])
    target = output / 'download-verification.json'
    target.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    for course in summary['courses']:
        print(f"{course['order']:02d} {course['title']}：PDF {course['pdfs']}，视频 {course['videos']}，图片 {course['images']}",flush=True)
    print(f"检查完成：{len(summary['files'])} 个文件通过，{len(summary['errors'])} 个异常。报告：{target}",flush=True)
    return 2 if summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
