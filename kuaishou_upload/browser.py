"""Compatibility imports; platform implementation lives in platforms/."""
import asyncio
from .contracts import Decision
from .observation import SNAPSHOT, compact_snapshot
from .platforms.kuaishou_browser import PlatformBrowser, content_id, field_candidates, safe_control
