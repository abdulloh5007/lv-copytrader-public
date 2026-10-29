"""Small accessible terminal UI; no animation, colour or prompts on redirected input."""
from __future__ import annotations
import getpass
import os
import sys
import unicodedata


class Error(Exception):
    pass


def clean(value: object) -> str:
    return ''.join(c for c in str(value) if (unicodedata.category(c) not in ('Cc','Cf')) or c in '\n\t')


def paint(text: str, colour: str) -> str:
    if sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
        if os.name == 'nt':
            import ctypes
            mode = ctypes.c_ulong()
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return text
            if not ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4):
                return text
        return f'\033[{colour}m{text}\033[0m'
    return text


def say(text: object = '', colour: str = '0') -> None:
    print(paint(clean(text), colour))


def banner() -> None:
    from . import __version__
    say('  LV / COPYTRADER', '1;36')
    say(f'  lvct {__version__}  ·  Setup & operations', '2')
    say()


def ask(label: str, default: str = '', *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise Error('Interactive setup needs a terminal. Use init --from-stdin for automation.')
    suffix = f' [{default}]' if default and not secret else ''
    prompt = f'  {label}{suffix}: '
    value = getpass.getpass(prompt) if secret else input(prompt).strip()
    return value or default


def password() -> str:
    first = ask('New admin password (14+ characters)', secret=True)
    if first != ask('Repeat admin password', secret=True):
        raise Error('Passwords do not match.')
    validate_password(first)
    return first


def validate_password(value: str) -> None:
    if not 14 <= len(value) <= 1024 or any(ord(c) < 32 for c in value):
        raise Error('Use 14–1024 password characters without control characters.')


def confirm(message: str, yes: bool = False) -> None:
    if not yes and ask(message + ' [y/N]').lower() not in ('y', 'yes'):
        raise Error('Cancelled; nothing was changed.')
