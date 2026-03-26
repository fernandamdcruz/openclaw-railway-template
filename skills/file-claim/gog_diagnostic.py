#!/usr/bin/env python3
"""
Diagnostic script — tests gog CLI + BCBS login page on Railway.
Run via: python3 /data/workspace/skills/file-claim/gog_diagnostic.py
"""
import subprocess
import os
import json
import sys
import asyncio

SHEET_ID = "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk"
TAB = "2026"
BCBS_URL = "https://members.bcbsglobalsolutions.com"
CDP_URL = "http://127.0.0.1:9222"

GOG_ENV = {**os.environ, "XDG_CONFIG_HOME": "/data/workspace/.config"}

def run(cmd, label):
    """Run a shell command and print full output."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"CMD:  {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=GOG_ENV)
        print(f"EXIT CODE: {result.returncode}")
        if result.stdout:
            out = result.stdout[:3000]
            print(f"STDOUT:\n{out}")
            if len(result.stdout) > 3000:
                print(f"... (truncated, total {len(result.stdout)} chars)")
        if result.stderr:
            print(f"STDERR:\n{result.stderr[:2000]}")
    except FileNotFoundError:
        print("ERROR: command not found")
    except subprocess.TimeoutExpired:
        print("ERROR: timed out after 30s")
    except Exception as e:
        print(f"ERROR: {e}")


async def test_bcbs_login_page():
    """Connect to CDP Chromium, load BCBS, and dump what's on the page."""
    print(f"\n{'='*60}")
    print("TEST: BCBS Login Page — what elements exist?")
    print(f"{'='*60}")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright not installed")
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()

            print(f"[NAV] Going to {BCBS_URL}")
            await page.goto(BCBS_URL, wait_until="networkidle")
            await asyncio.sleep(5)
            print(f"[NAV] Page title: {await page.title()}")
            print(f"[NAV] Page URL: {page.url}")

            # Screenshot
            screenshot_path = "/tmp/bcbs_diagnostic_login.png"
            await page.screenshot(path=screenshot_path)
            print(f"[SCREENSHOT] Saved to {screenshot_path}")

            # Dump ALL elements with roles (buttons, textboxes, links, etc.)
            print("\n--- ACCESSIBILITY TREE (roles + names) ---")
            tree = await page.accessibility.snapshot()
            if tree:
                _print_a11y_tree(tree, depth=0)
            else:
                print("(accessibility tree returned None)")

            # Also try specific Playwright locators the script uses
            print("\n--- LOCATOR TESTS ---")
            for role, pattern in [
                ("textbox", "username"),
                ("textbox", "email"),
                ("textbox", "user"),
                ("textbox", "password"),
                ("button", "login"),
                ("button", "sign in"),
                ("button", "log in"),
                ("link", "login"),
                ("link", "sign in"),
                ("link", "log in"),
            ]:
                import re
                loc = page.get_by_role(role, name=re.compile(pattern, re.IGNORECASE))
                count = await loc.count()
                print(f"  get_by_role('{role}', name=/{pattern}/i) → {count} match(es)")

            await page.close()
            print("\n[DONE] BCBS login page diagnostic complete")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


def _print_a11y_tree(node, depth=0):
    """Recursively print accessibility tree, max 100 nodes."""
    _print_a11y_tree.count = getattr(_print_a11y_tree, 'count', 0) + 1
    if _print_a11y_tree.count > 100:
        if _print_a11y_tree.count == 101:
            print("  ... (truncated at 100 nodes)")
        return

    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    indent = "  " * depth
    parts = [f"{indent}{role}"]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f'[value="{value}"]')
    print(" ".join(parts))

    for child in node.get("children", []):
        _print_a11y_tree(child, depth + 1)


def main():
    print("DIAGNOSTIC v2 — Railway Environment")
    print(f"XDG_CONFIG_HOME = {os.environ.get('XDG_CONFIG_HOME', '(not set)')}")
    print(f"GOG_KEYRING_PASSWORD = {'(set)' if os.environ.get('GOG_KEYRING_PASSWORD') else '(not set)')}")

    # 1. gog sheets get — actual data
    run(
        ["gog", "sheets", "get", SHEET_ID, f"'{TAB}'!A1:M3", "--json"],
        "sheets get — first 3 rows"
    )

    # 2. BCBS login page — what's actually there
    asyncio.run(test_bcbs_login_page())

    print(f"\n{'='*60}")
    print("DIAGNOSTIC v2 COMPLETE")
    print(f"{'='*60}")

if __name__ == "__main__":
    _print_a11y_tree.count = 0
    main()
