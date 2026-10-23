"""Shared state, semantic discovery and bounded recovery, independent of platform forms."""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import Decision
from .locator import LocatorMemory, SemanticLocator, safe_candidate, signature
from .page_state import PageBlocked, classify_page


class BrowserRuntime:
    allowed_hosts = frozenset()
    intents = {}

    def __init__(self, root: Path, *, expected_account='', recovery_steps=2):
        self.expected_account = expected_account
        self.recovery_steps = recovery_steps
        self.recovery_used = 0
        self.recovery_attempts = set()
        self.recover = None
        self.use_vision = False
        self.intent_grants = {}
        self.pending_intents = []
        self.events = []
        self.event_sink = None
        self.locator = SemanticLocator(LocatorMemory(root / 'locator-memory.json'))

    async def emit(self, kind, **payload):
        entry = {'kind': kind, **payload}
        self.events.append(entry)
        self.events = self.events[-100:]
        if self.event_sink:
            await self.event_sink(entry)

    def validate_url(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in self.allowed_hosts:
            raise PageBlocked('wrong_domain', '当前页面不是已授权的平台，请在浏览器中完成登录')

    async def observe_state(self, snap, *, allow_login=False):
        self.validate_url(snap['url'])
        state = classify_page(snap)
        snap['pageState'] = state.name
        await self.emit('observation', state=state.name, evidence=state.evidence, url=snap['url'],
                        controlCount=len(snap['controls']), truncated=snap.get('truncated', False))
        messages = {'challenge': '遇到验证码或安全验证，停止自动操作，请手动处理',
                    'agreement': '遇到法律协议或平台授权确认，需要用户自行处理',
                    'permission_denied': '平台明确报告账号权限不足',
                    'page_error': '平台页面加载或服务异常，保留现场后重试',
                    'locked': '审核中-锁定，平台拒绝修改资料'}
        if state.name in messages:
            raise PageBlocked(state.name, messages[state.name])
        if state.name == 'login' and not allow_login:
            raise PageBlocked('login_required', '等待扫码登录')
        # The platform adapter extracts account evidence from the account area,
        # never from arbitrary body text or the model's opinion.
        observed = self.account_evidence(snap)
        account_tokens = {line.strip() for line in observed.splitlines() if line.strip()}
        account_tokens.update(re.findall(r'(?<!\d)\d{5,}(?!\d)', observed))
        if self.expected_account and self.expected_account not in account_tokens:
            raise PageBlocked('account_mismatch', '无法在账号区域核对配置的目标账号，停止以免上传到错误账号')
        return snap

    def account_evidence(self, snap):
        return snap.get('accountText', '')

    async def resolve_intent(self, snap, intent, *, recover=True):
        if intent not in self.intents:
            raise ValueError('平台未声明此动作意图')
        aliases = self.intents[intent]
        ambiguity = None
        try:
            node = self.locator.resolve(snap, intent, aliases)
        except ValueError as exc:
            node, ambiguity = None, exc
        if ambiguity and self.recover and not recover:
            return None
        request_key = (snap['url'], snap.get('pageState'), intent)
        if node is None and recover and self.recover and self.recovery_used < self.recovery_steps and request_key not in self.recovery_attempts:
            if snap.get('pageState') in {'loading', 'login', 'challenge', 'agreement', 'page_error', 'permission_denied'}:
                return None
            self.recovery_used += 1
            self.recovery_attempts.add(request_key)
            screenshot = await self.page.screenshot() if self.use_vision else None
            node = await asyncio.wait_for(self.locator.recover(snap, intent, aliases, self.recover, screenshot=screenshot), timeout=90)
            await self.emit('locator_recovery', intent=intent, selected=signature(node) if node else None)
        if node is None and ambiguity:
            raise ambiguity
        if node and intent != 'search_field':
            href = node.get('href')
            if href:
                from urllib.parse import urljoin
                self.validate_url(urljoin(snap['url'], href))
            self.intent_grants[(node['ref'], intent)] = (snap['url'], signature(node))
        return node

    async def click_intent(self, node, intent, phase, snap):
        if not node:
            raise ValueError(f'没有可验证的 {intent} 控件')
        await self.act(Decision(action='click', ref=node['ref'], intent=intent), phase, snap)
        self.pending_intents.append((intent, snap, node))

    def consume_intent_grant(self, decision, node, snap):
        grant = self.intent_grants.pop((node['ref'], decision.intent), None)
        return bool(decision.intent and grant == (snap['url'], signature(node)) and safe_candidate(node, decision.intent))

    async def confirm_intents(self, snap):
        # Called only after the workflow observes its expected postcondition.
        for intent, before, node in self.pending_intents:
            self.locator.memory.remember(intent, before, node)
            await self.emit('intent_verified', intent=intent, resultState=snap.get('pageState'))
        self.pending_intents.clear()

    async def wait_for_states(self, expected, *, polls=60):
        for _ in range(polls):
            snap = await self.snapshot()
            if snap.get('pageState') in expected:
                return snap
            await asyncio.sleep(0.5)
        raise PageBlocked('transition_timeout', f'动作后未取得目标状态{sorted(expected)}；当前状态={snap.get("pageState")}')
