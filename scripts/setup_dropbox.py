"""Guided one-time helper to obtain a long-lived Dropbox refresh token.

Run this on your workstation (not inside the container). The script walks you
through every step in the Dropbox Developer Console, opens the relevant pages
in your browser at the right moments and finally prints the three env-var
lines you need to paste into ``.env``.

Usage:

    uv run python scripts/setup_dropbox.py

You can re-run the script as often as you like; nothing is written to disk.
"""

from __future__ import annotations

import sys
import webbrowser

from dropbox import DropboxOAuth2FlowNoRedirect

APPS_URL = "https://www.dropbox.com/developers/apps"

BANNER = "=" * 72
SEPARATOR = "-" * 72


def _hr(title: str) -> None:
    print()
    print(BANNER)
    print(title)
    print(BANNER)


def _step(n: int, total: int, title: str) -> None:
    print()
    print(SEPARATOR)
    print(f"Step {n}/{total}: {title}")
    print(SEPARATOR)


def _prompt_yes(question: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        answer = input(f"{question} {suffix} ").strip().lower()
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer with 'y' or 'n'.")


def _prompt_nonempty(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("This value is required; try again.")


def _open_in_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:
        opened = False
    if not opened:
        print(f"  (could not open browser automatically; open this URL manually: {url})")


def _intro() -> None:
    _hr("Dropbox Refresh Token Setup")
    print(
        "This wizard guides you through creating a Dropbox app, granting it the"
        "\nright permissions and exchanging an authorization code for a long-lived"
        "\nrefresh token. Total time: about 2 minutes.\n"
        "\nYou will need:"
        "\n  - A Dropbox account (free works) signed in in your default browser."
        "\n  - The Dropbox path you want this app to write into.\n"
        "\nNothing is written to disk. At the end the wizard prints three lines"
        "\nthat you paste into your .env file."
    )
    input("\nPress Enter to continue...")


def _step_create_app() -> None:
    _step(1, 5, "Create a Dropbox app")
    print(
        "I will now open the Dropbox 'My apps' page. Once it loads:\n"
        "\n  1. Click 'Create app' (top right)."
        "\n  2. Choose API: 'Scoped access'."
        "\n  3. Choose the type of access:"
        "\n        - 'App folder' if this worker should only write inside"
        "\n          /Apps/<your-app-name>/ . Simplest, safest."
        "\n        - 'Full Dropbox' if you want to write into an arbitrary"
        "\n          path (e.g. inside another existing app's folder)."
        "\n  4. Give the app a unique name, for example 'mailattachments2dropbox'."
        "\n  5. Click 'Create app'."
    )
    if _prompt_yes("\nOpen the Dropbox apps page now?"):
        _open_in_browser(APPS_URL)
    input("\nWhen the app is created (you are on the new app's settings page), press Enter...")


def _step_permissions() -> None:
    _step(2, 5, "Grant the required permissions")
    print(
        "On the app's page, click the 'Permissions' tab and enable EXACTLY these"
        "\nthree scopes:"
        "\n  - files.metadata.write"
        "\n  - files.content.write"
        "\n  - files.content.read"
        "\n\nThen scroll down and click 'Submit'."
        "\n\nIMPORTANT: Always submit permissions BEFORE creating the refresh token."
        "\nA token issued without the right scopes will silently fail at upload time."
    )
    input("\nWhen 'Submit' was successful, press Enter...")


def _step_collect_keys() -> tuple[str, str]:
    _step(3, 5, "Read App key and App secret")
    print(
        "Switch back to the 'Settings' tab of the app."
        "\nNear the top you will see two fields:"
        "\n  - 'App key'    (always visible)"
        "\n  - 'App secret' (click 'Show' to reveal)"
        "\n\nCopy each value when prompted."
    )
    app_key = _prompt_nonempty("\nApp key")
    app_secret = _prompt_nonempty("App secret")
    return app_key, app_secret


def _step_authorize(app_key: str, app_secret: str) -> str:
    _step(4, 5, "Authorize and obtain a refresh token")
    flow = DropboxOAuth2FlowNoRedirect(
        consumer_key=app_key,
        consumer_secret=app_secret,
        token_access_type="offline",
    )
    auth_url = flow.start()
    print(
        "I will open the authorization page. On that page:"
        "\n  1. Confirm the Dropbox account is the right one."
        "\n  2. Click 'Allow'."
        "\n  3. Dropbox will show you a short authorization code; copy it."
    )
    if _prompt_yes("\nOpen the authorization URL now?"):
        _open_in_browser(auth_url)
    print(f"\n(authorization URL: {auth_url})")
    code = _prompt_nonempty("\nPaste the authorization code")
    try:
        result = flow.finish(code)
    except Exception as exc:
        print(f"\nERROR: token exchange failed: {exc}", file=sys.stderr)
        print(
            "Common causes:"
            "\n  - App key or App secret were entered with a leading/trailing space."
            "\n  - The authorization code was used already (codes are single-use)."
            "\n  - The Dropbox account that authorized is different from the app owner."
            "\nRe-run the wizard to try again.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not result.refresh_token:
        print(
            "ERROR: Dropbox did not return a refresh token. Make sure the app is"
            "\na 'Scoped App' (not legacy) and token_access_type=offline was used.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return str(result.refresh_token)


def _step_emit_env(app_key: str, app_secret: str, refresh_token: str) -> None:
    _step(5, 5, "Copy these values into .env")
    print("\nDone. Append (or replace) the following three lines in your .env file:\n")
    print(SEPARATOR)
    print(f"DROPBOX_APP_KEY={app_key}")
    print(f"DROPBOX_APP_SECRET={app_secret}")
    print(f"DROPBOX_REFRESH_TOKEN={refresh_token}")
    print(SEPARATOR)
    print(
        "\nThe refresh token is long-lived. The dropbox SDK exchanges it for a"
        "\nshort-lived access token whenever the current one expires, so you do"
        "\nnot need to re-run this wizard unless you revoke the app or rotate"
        "\nyour credentials."
    )


def main() -> int:
    _intro()
    _step_create_app()
    _step_permissions()
    app_key, app_secret = _step_collect_keys()
    refresh_token = _step_authorize(app_key, app_secret)
    _step_emit_env(app_key, app_secret, refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
