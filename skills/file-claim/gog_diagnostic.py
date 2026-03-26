#!/usr/bin/env python3
"""
GOG CLI Diagnostic Script — run once on Railway to discover exact command syntax.
Output everything to stdout so FerdyBot can relay it.
"""
import subprocess
import os
import json
import sys

SHEET_ID = "1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk"
TAB = "2026"

def run(cmd, label):
    """Run a command and print full output."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"CMD:  {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"EXIT CODE: {result.returncode}")
        if result.stdout:
            # Truncate very long output
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

def main():
    print("GOG CLI DIAGNOSTIC — Railway Environment")
    print(f"XDG_CONFIG_HOME = {os.environ.get('XDG_CONFIG_HOME', '(not set)')}")
    print(f"GOG_ACCOUNT = {os.environ.get('GOG_ACCOUNT', '(not set)')}")
    print(f"GOG_KEYRING_PASSWORD = {'(set)' if os.environ.get('GOG_KEYRING_PASSWORD') else '(not set)'}")
    print(f"HOME = {os.environ.get('HOME', '(not set)')}")
    print(f"USER = {os.environ.get('USER', '(not set)')}")

    # 1. Version
    run(["gog", "--version"], "gog version")

    # 2. Auth — what accounts are configured?
    run(["gog", "auth", "list"], "gog auth list")

    # 3. Sheets help
    run(["gog", "sheets", "--help"], "gog sheets help")
    run(["gog", "sheets", "get", "--help"], "gog sheets get help")
    run(["gog", "sheets", "update", "--help"], "gog sheets update help")

    # 4. Gmail help
    run(["gog", "gmail", "--help"], "gog gmail help")
    run(["gog", "gmail", "search", "--help"], "gog gmail search help")

    # 5. Drive help
    run(["gog", "drive", "--help"], "gog drive help")
    run(["gog", "drive", "download", "--help"], "gog drive download help")

    # 6. ACTUAL sheets get — try reading 2 rows from the real spreadsheet
    run(
        ["gog", "sheets", "get", SHEET_ID, f"'{TAB}'!A1:M3", "--json"],
        "sheets get (no --account)"
    )

    # 7. Try with --account flag using the email from auth list
    # We'll try the known email from SKILL.md env context
    run(
        ["gog", "--account", "fernanda.mdcruz@gmail.com", "sheets", "get", SHEET_ID, f"'{TAB}'!A1:M3", "--json"],
        "sheets get (with --account before subcommand)"
    )

    # 8. Try account flag after subcommand
    run(
        ["gog", "sheets", "get", SHEET_ID, f"'{TAB}'!A1:M3", "--json", "--account", "fernanda.mdcruz@gmail.com"],
        "sheets get (with --account after args)"
    )

    # 9. Gmail search test
    run(
        ["gog", "gmail", "search", "from:noreply@bcbsglobalsolutions.com", "--max", "1", "--json"],
        "gmail search (no --account)"
    )

    run(
        ["gog", "--account", "fernanda.mdcruz@gmail.com", "gmail", "search", "from:noreply@bcbsglobalsolutions.com", "--max", "1", "--json"],
        "gmail search (with --account)"
    )

    print(f"\n{'='*60}")
    print("DIAGNOSTIC COMPLETE")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
