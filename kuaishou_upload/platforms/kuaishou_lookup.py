"""Read identity evidence from rendered list rows; never infer an ID from a title."""
import re
from urllib.parse import parse_qs, urlsplit

ID = re.compile(r'(?:短剧|作品)\s*ID\s*[:：]?\s*([A-Za-z0-9_-]+)', re.I)
EMPTY = re.compile(r'暂无数据|暂无内容|无搜索结果|没有找到|暂无短剧')
DECORATIONS = {'预览', '播放', '查看封面'}


def normalize_title(value):
    return re.sub(r'\s+', '', value)


def cell_title(cell):
    # Preview actions are separate rendered lines in the same title cell.
    # Remove only known standalone leading actions, retaining every title character.
    prefix = ID.split(cell, maxsplit=1)[0]
    lines = [line.strip() for line in prefix.splitlines() if line.strip()]
    while len(lines) > 1 and lines[0] in DECORATIONS:
        lines.pop(0)
    return '\n'.join(lines)


def lookup_evidence(snap, title, *, search_ref):
    candidates, matches = [], set()
    for row in snap.get('rows', []):
        ids = set(ID.findall(row.get('text', '')))
        for link in row.get('links', []):
            url = urlsplit(link)
            if url.scheme == 'https' and url.hostname == 'kdj.kuaishou.com':
                ids.update(parse_qs(url.query).get('miniSeriesId', []))
        if not ids:
            continue
        # Only the identity cell can establish the title; a matching status/creator
        # cell elsewhere in the row is never title evidence.
        titles = {cell_title(cell) for cell in row.get('cells', []) if ID.search(cell) and cell_title(cell)}
        candidates.append({'titles': sorted(titles), 'ids': sorted(ids)})
        if normalize_title(title) in {normalize_title(value) for value in titles}:
            if len(ids) != 1:
                raise ValueError('同一作品行存在互相冲突的平台ID，停止查重')
            matches.update(ids)
    # A result must belong to the query we just entered, rather than a stale list.
    query_verified = any(parse_qs(urlsplit(snap.get(key, '')).query).get('miniSeriesTitle') == [title]
                         for key in ('url', 'frameUrl')) or any(
        n.get('ref') == search_ref and n.get('value') == title for n in snap.get('controls', []))
    ready = query_verified and not snap.get('loading') and not snap.get('truncated')
    empty = bool(EMPTY.search(snap.get('text', ''))) and not candidates
    return {'ids': sorted(matches) if ready else [], 'empty': bool(empty and ready),
            'queryVerified': query_verified, 'ready': ready, 'candidates': candidates}
