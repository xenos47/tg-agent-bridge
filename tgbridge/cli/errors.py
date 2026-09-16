"""Stable CLI errors and exit codes."""


class TgqError(Exception):
    exit_code = 2


class DatabaseError(TgqError):
    exit_code = 3


class TelegramError(TgqError):
    exit_code = 4


class PolicyError(TgqError):
    exit_code = 5
