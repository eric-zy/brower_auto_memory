"""Resolve intent into current controls. Persist semantic signatures, never refs/coordinates."""
import hashlib
import json
import re
from pathlib import Path

from .state import atomic_json

HAZARD = re.compile(r'提交|发布|删除|撤回|下架|同意|免责声明|授权确认|支付|购买|编辑|解绑|绑定账号|退出登录|切换账号')


def signature(node):
    return {k: str(node.get(k, '')) for k in ('tag', 'type', 'accessibleName', 'text', 'label', 'region')}


def safe_candidate(node, intent):
    text = ' '.join(str(node.get(k, '')) for k in ('text', 'accessibleName'))
    context = ' '.join(str(node.get(k, '')) for k in ('label', 'context'))
    if node.get('disabled') or node.get('visible') is False or HAZARD.search(text) or re.search(r'服务协议|用户协议|免责声明|我已阅读|授权确认', context):
        return False
    if intent == 'search_field':
        return node.get('tag') == 'input' and node.get('type') in {'text', 'search'} and not node.get('readOnly')
    return node.get('tag') in {'button', 'a', 'li'} or node.get('type') in {'button', 'menuitem'}


class LocatorMemory:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.entries = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(self.entries, dict):
                self.entries = {}
        except (OSError, ValueError):
            self.entries = {}

    @staticmethod
    def key(intent, snap):
        from urllib.parse import urlsplit
        url = urlsplit(snap['url'])
        return f'{url.hostname}{url.path}:{intent}'

    def find(self, intent, snap, candidates):
        expected = self.entries.get(self.key(intent, snap))
        return [n for n in candidates if signature(n) == expected] if expected else []

    def remember(self, intent, snap, node):
        self.entries[self.key(intent, snap)] = signature(node)
        atomic_json(self.path, self.entries)


class SemanticLocator:
    def __init__(self, memory):
        self.memory = memory

    def resolve(self, snap, intent, aliases):
        candidates = [n for n in snap['controls'] if safe_candidate(n, intent)]
        matches = self.memory.find(intent, snap, candidates)
        if len(matches) == 1:
            return matches[0]
        names = lambda n: {str(n.get(k, '')).strip() for k in ('accessibleName', 'text')}
        matches = [n for n in candidates if names(n).intersection(aliases)]
        if not matches and intent == 'search_field':
            matches = [n for n in candidates if any(a in str(n.get('label', '')) + str(n.get('placeholder', '')) for a in aliases)]
        if len(matches) > 1:
            # Prefer the actionable leaf over an enclosing menu/list container.
            leaves = [n for n in matches if n.get('tag') in {'button', 'input'} or n.get('type') == 'button']
            if len(leaves) == 1:
                return leaves[0]
            raise ValueError(f'{intent} 匹配到 {len(matches)} 个可用控件，需重新观察或模型消歧')
        return matches[0] if matches else None

    async def recover(self, snap, intent, aliases, callback, *, screenshot=None):
        candidates = [n for n in snap['controls'] if safe_candidate(n, intent)]
        if not callback or not candidates:
            return None
        request = {'intent': intent, 'aliases': list(aliases), 'url': snap['url'],
                   'page_state': snap.get('pageState'), 'text': snap.get('text', '')[:6000],
                   'candidates': [{**signature(n), 'ref': n['ref'], 'placeholder': n.get('placeholder', '')} for n in candidates],
                   'screenshot': screenshot}
        result = await callback(request)
        ref = result.get('ref') if isinstance(result, dict) else None
        if not ref:
            return None
        matches = [n for n in candidates if n['ref'] == ref]
        if len(matches) != 1:
            raise ValueError('模型恢复引用不在当前允许候选中')
        return matches[0]
