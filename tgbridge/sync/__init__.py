"""Telegram synchronization layer.

This is the only package allowed to import Telethon.
"""

from tgbridge.sync.engine import SyncEngine

__all__ = ["SyncEngine"]
