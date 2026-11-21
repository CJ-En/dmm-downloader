"""The DMM login (email + password): from the environment, the system's password store, or asked.

Both the login to DMM and jav-it (which decrypts with your account) need it. The password is never
written to a plain file: it is kept in the system's password store (Windows Credential Manager,
macOS Keychain, Linux Secret Service) only when you choose to save it.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

SERVICE = "dmm-downloader"
USER_KEY = "dmm-login"  # the store keeps the email under this name, the password under the email


@dataclass(frozen=True)
class Login:
    username: str
    password: str
    source: str  # "environment", "password store" or "typed"


def _keyring():
    try:
        import keyring  # optional

        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def store_available() -> bool:
    store = _keyring()
    if not store:
        return False
    try:
        from keyring.backends import fail

        return not isinstance(store.get_keyring(), fail.Keyring)
    except Exception:
        return True


def from_environment() -> Login | None:
    username = os.environ.get("DMM_USERNAME", "").strip()
    password = os.environ.get("DMM_PASSWORD", "")
    return Login(username, password, "environment") if username and password else None


def from_store() -> Login | None:
    store = _keyring()
    if not store:
        return None
    try:
        username = store.get_password(SERVICE, USER_KEY)
        password = store.get_password(SERVICE, username) if username else None
    except Exception as exc:
        log.warning("password store not readable: %s", exc)
        return None
    return Login(username, password, "password store") if username and password else None


def ask() -> Login | None:
    if not sys.stdin.isatty():
        return None
    print("DMM login (the email and password of your DMM account).")
    username = input("Email: ").strip()
    if not username:
        return None
    password = getpass.getpass("Password (not shown while typing): ")
    return Login(username, password, "typed") if password else None


def find(interactive: bool = True) -> Login | None:
    return from_environment() or from_store() or (ask() if interactive else None)


def save(login: Login) -> bool:
    store = _keyring()
    if not store or not store_available():
        return False
    try:
        old = store.get_password(SERVICE, USER_KEY)
        store.set_password(SERVICE, USER_KEY, login.username)
        store.set_password(SERVICE, login.username, login.password)
        if old and old != login.username:
            _delete(store, old)
        return True
    except Exception as exc:
        log.warning("could not save the login: %s", exc)
        return False


def _delete(store, name: str) -> None:
    try:
        store.delete_password(SERVICE, name)
    except Exception:
        pass


def forget() -> bool:
    store = _keyring()
    if not store:
        return False
    try:
        username = store.get_password(SERVICE, USER_KEY)
    except Exception:
        return False
    if username:
        _delete(store, username)
    _delete(store, USER_KEY)
    return bool(username)
