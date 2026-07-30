#!/usr/bin/env python3
"""重建 UI 骨架像素字体(Fusion Pixel)子集。

用途:缝合像素字体只子集化了界面实际用到的字符,新增文案(按钮、看板、
提示语等)后,未收录的字会回退系统黑体、不再像素化。重跑本脚本即可:

    python3 scripts/build_font_subset.py

流程:
  1. 扫描 traffic_analyzer/web/static/ 下全部 .js/.html/.css 文本,
     收集唯一字符 + 可打印 ASCII + 常用中文标点,写入 output/font_subset_chars.txt;
  2. 用 pyftsubset 从原始字体子集化,覆盖输出
     traffic_analyzer/web/static/fonts/fusion-pixel-12px.woff2。

原始字体(未子集化)默认取 /tmp/fontpix/fp_woff2/fusion-pixel-12px-proportional-zh_hans.ttf.woff2,
不存在时自动从 fusion-pixel-font GitHub release 下载(经 ghfast.top 镜像);
也可用 --source 指定本地路径。字体: SIL OFL 1.1, 许可见 static/fonts/OFL.txt。
"""

import argparse
import glob
import os
import string
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, 'traffic_analyzer', 'web', 'static')
OUT_FONT = os.path.join(STATIC_DIR, 'fonts', 'fusion-pixel-12px.woff2')
CHARS_FILE = os.path.join(ROOT, 'output', 'font_subset_chars.txt')

FONT_NAME = 'fusion-pixel-12px-proportional-zh_hans.ttf.woff2'
DEFAULT_SOURCE = os.path.join('/tmp', 'fontpix', 'fp_woff2', FONT_NAME)
DOWNLOAD_URL = (
    'https://ghfast.top/https://github.com/TakWolf/fusion-pixel-font/releases/'
    'download/2026.07.20/'
    'fusion-pixel-font-12px-proportional-ttf.woff2-v2026.07.20.zip'
)

# 常用中文标点(静态文本里不一定都出现,界面动态拼接可能用到)
EXTRA_PUNCT = '。「」『』、,…—·()【】?!:;~'


def collect_chars():
    """扫描静态前端文本,收集唯一字符集。"""
    chars = set(string.printable) | set(EXTRA_PUNCT)
    for path in glob.glob(os.path.join(STATIC_DIR, '**', '*'), recursive=True):
        if path.endswith(('.js', '.html', '.css')):
            with open(path, encoding='utf-8', errors='ignore') as f:
                chars |= set(f.read())
    return sorted(c for c in chars if not c.isspace() or c == ' ')


def ensure_source_font(path):
    """原始字体不存在时下载 release zip 并解出 zh_hans 版本。"""
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    zip_path = path + '.zip'
    print(f'下载原始字体: {DOWNLOAD_URL}')
    urllib.request.urlretrieve(DOWNLOAD_URL, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(FONT_NAME)
    with open(path, 'wb') as f:
        f.write(data)
    os.remove(zip_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default=DEFAULT_SOURCE,
                    help='未子集化的原始字体路径(默认 %(default)s,缺失时自动下载)')
    ap.add_argument('--output', default=OUT_FONT, help='输出 woff2 路径(默认覆盖 static/fonts)')
    args = ap.parse_args()

    ensure_source_font(args.source)

    chars = collect_chars()
    os.makedirs(os.path.dirname(CHARS_FILE), exist_ok=True)
    with open(CHARS_FILE, 'w', encoding='utf-8') as f:
        f.write(''.join(chars))
    print(f'字符表: {len(chars)} 个字符 → {CHARS_FILE}')

    cmd = [
        'pyftsubset', args.source,
        '--text-file=' + CHARS_FILE,
        '--output-file=' + args.output,
        '--flavor=woff2',
        '--layout-features=*',
        '--no-hinting',
        '--desubroutinize',
    ]
    subprocess.run(cmd, check=True)
    size = os.path.getsize(args.output)
    print(f'子集字体: {args.output} ({size / 1024:.1f} KiB)')


if __name__ == '__main__':
    sys.exit(main())
