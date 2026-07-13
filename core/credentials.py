"""Secure credential storage using the OS keychain (macOS Keychain via keyring)."""
from __future__ import annotations

import getpass
import keyring
from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

SERVICE_NAME = "gov-il-connect"


def _migrate_legacy_id_key() -> None:
    """One-time migration: an older UI build saved the ID under the key "id".
    Move it to the canonical "id_number" key so both engine and UI agree."""
    if keyring.get_password(SERVICE_NAME, "id_number"):
        return
    legacy = keyring.get_password(SERVICE_NAME, "id")
    if legacy:
        keyring.set_password(SERVICE_NAME, "id_number", legacy)
        try:
            keyring.delete_password(SERVICE_NAME, "id")
        except keyring.errors.PasswordDeleteError:
            pass
        print(f"{_ts()} [Auth] Migrated legacy keychain key 'id' -> 'id_number'.")


def credentials_exist() -> bool:
    """Return True if credentials are already saved."""
    _migrate_legacy_id_key()
    id_num = keyring.get_password(SERVICE_NAME, "id_number")
    pwd = keyring.get_password(SERVICE_NAME, "password")
    return bool(id_num and pwd)


def save_credentials(id_number: str, password: str) -> None:
    """Save credentials to keychain."""
    keyring.set_password(SERVICE_NAME, "id_number", id_number)
    keyring.set_password(SERVICE_NAME, "password", password)


def clear_credentials() -> None:
    """Remove saved credentials from keychain."""
    try:
        keyring.delete_password(SERVICE_NAME, "id_number")
    except keyring.errors.PasswordDeleteError:
        pass
    try:
        keyring.delete_password(SERVICE_NAME, "password")
    except keyring.errors.PasswordDeleteError:
        pass


def prompt_and_save() -> tuple[str, str]:
    """Prompt user for credentials, validate, and save to keychain."""
    print("\n[Auth] Enter your Israeli government login credentials.")
    while True:
        id_number = input("  ID number (9 digits): ").strip()
        if len(id_number) == 9 and id_number.isdigit():
            break
        print("  [Auth] ID must be exactly 9 digits. Please try again.")

    password = getpass.getpass("  Password: ").strip()

    save_credentials(id_number, password)
    print(f"{_ts()} [Auth] Credentials saved to keychain. Will not ask again.")
    return id_number, password


def get_credentials() -> tuple[str, str]:
    """Return (id_number, password). Prompts user on first run, loads from keychain after."""
    _migrate_legacy_id_key()
    id_number = keyring.get_password(SERVICE_NAME, "id_number")
    password = keyring.get_password(SERVICE_NAME, "password")

    if id_number and password:
        print(f"{_ts()} [Auth] Loaded credentials from keychain.")
        return id_number, password

    return prompt_and_save()


def reset_credentials() -> tuple[str, str]:
    """Clear saved credentials and re-prompt the user."""
    clear_credentials()
    print("[Auth] Existing credentials cleared.")
    return prompt_and_save()
