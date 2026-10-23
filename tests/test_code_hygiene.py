"""Code-hygiene gate: run a real linter over the bot's own modules.

Why this exists: during an edit the at-rest encryption key derivation lost its
`return`, and nothing noticed — every test asserted behavior in memory, while
the file on disk silently became plaintext. pyflakes flags exactly that class
of damage (`local variable 'key' is assigned to but never used`).

Most of pyflakes' remaining output is the intentional re-export surface created
by the module extraction (see the module docstrings and EXTRACTION_PLAN.md), so
those are filtered; anything else is a genuine finding and fails the suite.
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = (
    "botstate.py", "carriers.py", "config.py", "delivery_history.py", "main.py",
    "miniapp.py", "phantom_link.py", "phantom_page.py", "pro.py", "refresh.py",
    "share_links.py", "ship24.py", "solana_tx.py", "storage.py", "ui.py",
    "usps_api.py", "usps_browser.py",
)

# Deliberate, documented exceptions:
#  - "imported but unused": main.py / miniapp.py re-export the extracted
#    modules' names so every existing call site and test keeps addressing
#    main.<name> exactly as before.
#  - the 17TRACK CLI override rebinds main's global on purpose (main.py
#    __main__): the backends read carriers.track17_api_key, while the startup
#    "status sources" log reads main's own copy.
ALLOWED = (
    "imported but unused",
    "redefinition of unused 'track17_api_key'",
)


def test_no_new_pyflakes_findings():
    if shutil.which("python3") is None:  # pragma: no cover
        import pytest
        pytest.skip("python3 missing")
    res = subprocess.run(["python3", "-m", "pyflakes", *MODULES],
                         capture_output=True, text=True, cwd=ROOT)
    if "No module named" in (res.stderr or ""):
        import pytest
        pytest.skip("pyflakes not installed (dev tool, see requirements-dev.txt)")
    findings = [
        line for line in (res.stdout or "").splitlines()
        if line.strip() and not any(allowed in line for allowed in ALLOWED)
    ]
    assert not findings, "new linter findings:\n" + "\n".join(findings)


def test_no_private_helper_is_registered_as_a_handler():
    """A helper that slips under a @router decorator silently REPLACES a command.

    Same family as the encryption `return` above: an edit inserted `_name_forms`
    directly below `@router.message(Command("info"))`, so /info called the helper
    with a Message — every real command raised AttributeError, while the unit tests
    that call `info_handler` directly stayed green. pyflakes cannot see this;
    asking the router can.
    """
    import main

    offenders = []
    for observer_name in ("message", "callback_query", "edited_message", "channel_post"):
        observer = getattr(main.router, observer_name, None)
        for handler in getattr(observer, "handlers", ()):
            name = getattr(handler.callback, "__name__", "")
            if name.startswith("_"):
                offenders.append(f"{observer_name}: {name}")
    assert not offenders, (
        "a private helper is registered as a handler — was it inserted under a "
        "@router decorator? " + ", ".join(offenders)
    )
