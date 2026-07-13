"""User mode management — private client vs lawyer."""
from __future__ import annotations
from datetime import datetime
from core.i18n import t

def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

USER_MODE_PRIVATE = "private"
USER_MODE_LAWYER = "lawyer"

def configure_user_mode(settings: dict) -> str:
    """Show the welcome screen and ask user to select mode.

    Returns:
        "ok"       — mode chosen (private or lawyer), ready to proceed.
        "settings" — user pressed 's', caller should open settings submenu.
        "quit"     — user pressed 'q', caller should exit.
    """
    print("\n" + "=" * 60)
    print(t("welcome_title"))
    print("=" * 60)
    print(t("welcome_private"))
    print(t("welcome_lawyer"))
    print(t("welcome_settings"))
    print(t("welcome_quit"))
    print("=" * 60)
    while True:
        choice = input(t("welcome_prompt")).strip().lower()
        if choice in ("", "1"):
            settings["user_mode"] = USER_MODE_PRIVATE
            print(f"{_ts()} [Mode] {t('mode_private')}")
            return "ok"
        elif choice == "2":
            settings["user_mode"] = USER_MODE_LAWYER
            print(f"{_ts()} [Mode] {t('mode_lawyer')}")
            return "ok"
        elif choice == "s":
            return "settings"
        elif choice == "q":
            return "quit"
        else:
            print("Invalid choice.")

def get_user_mode(settings: dict) -> str:
    return settings.get("user_mode", USER_MODE_PRIVATE)

def is_lawyer_mode(settings: dict) -> bool:
    return get_user_mode(settings) == USER_MODE_LAWYER

def select_representative(representatives: list[str], settings: dict) -> list[str]:
    """Show list of representative names found in a case, ask user to identify themselves.
    Returns selected representative name(s), or empty list if skipped.
    Stores result in settings['representative'].
    """
    if not representatives:
        return []

    # If already set from previous case, return it
    existing = settings.get("representative", [])
    if existing:
        return existing

    print(f"\n{_ts()} [Mode] Representatives found in this case:")
    for i, rep in enumerate(representatives, 1):
        print(f"  {i}. {rep}")
    print(f"  0. Skip / Not listed")

    selected = []
    while True:
        raw = input("Which representative are you? (enter number(s) separated by comma, or 0 to skip): ").strip()
        if not raw or raw == "0":
            break
        parts = [p.strip() for p in raw.split(",")]
        valid = True
        for p in parts:
            if p.isdigit() and 1 <= int(p) <= len(representatives):
                selected.append(representatives[int(p) - 1])
            else:
                print(f"Invalid: '{p}'")
                valid = False
                break
        if valid:
            break

    if selected:
        settings["representative"] = selected
        print(f"{_ts()} [Mode] Representative(s) set: {', '.join(selected)}")
    return selected
