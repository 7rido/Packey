"""Config-surface guards: the docs and the dependencies must match the code.

Two drifts this pins down (both found in the 2026-09-15 audit):
  * an env knob the code reads but .env.example never mentions — an operator
    cannot discover it, and `SOL_POLL_SECONDS` was documented while the code
    ignored it, so the doc lied;
  * a third-party module imported at runtime but missing from requirements.txt
    (aiohttp was only arriving transitively through aiogram).

These are cheap static checks — no bot, no network — and they fail loudly the
moment either surface drifts again.
"""
import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Same module set the hygiene gate lints: the app itself, not scripts/ probes.
MODULES = (
    "botstate.py", "carriers.py", "config.py", "main.py", "miniapp.py",
    "phantom_link.py", "phantom_page.py", "pro.py", "refresh.py", "ship24.py",
    "storage.py", "ui.py", "usps_api.py", "usps_browser.py",
)
# Import name -> distribution name on PyPI (only where they differ).
DISTRIBUTION = {"dotenv": "python-dotenv", "nacl": "pynacl"}
# Deliberately optional extras: imported lazily inside a try/except so the app
# runs without them (they must NOT be runtime requirements).
OPTIONAL = {"playwright"}
# Documented but read through a wrapper (usps_api._cred("...")) rather than
# getenv, so the literal scan below can't see the read.
READ_VIA_HELPER = {"USPS_CONSUMER_KEY", "USPS_CONSUMER_SECRET"}


def _sources():
    return {name: (ROOT / name).read_text() for name in MODULES}


def test_every_env_key_the_code_reads_is_documented():
    """.env.example is the only config surface an operator sees."""
    documented = set(re.findall(
        r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    read = set()
    for text in _sources().values():
        read |= set(re.findall(r'(?:getenv|environ\.get)\(\s*["\']([A-Z0-9_]+)["\']', text))
    missing = sorted(read - documented)
    assert not missing, (
        "these env vars are read by the code but absent from .env.example: "
        + ", ".join(missing))


def test_env_example_documents_nothing_the_code_ignores():
    """A documented knob that nothing reads is a lie to the operator."""
    documented = set(re.findall(
        r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    sources = _sources()
    # A key counts as read when its literal appears anywhere in the modules
    # (covers the helper-based reads) — a name that never appears is dead doc.
    stale = sorted(k for k in documented
                   if k not in READ_VIA_HELPER
                   and not any(re.search(rf'["\']{k}["\']', t) for t in sources.values())
                   and not any(re.search(rf'^{k}\s*=', t, re.M) for t in sources.values()))
    assert not stale, (
        "documented in .env.example but never read by any module: "
        + ", ".join(stale))


def test_every_third_party_import_is_declared_in_requirements():
    """Importing a module we never declared is how an app breaks on a fresh
    install (or silently depends on a transitive extra)."""
    declared = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            declared.add(re.split(r"[<>=!~\[; ]", line)[0].strip().lower())
    imported = set()
    for name, text in _sources().items():
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = set()
    for mod in imported:
        if mod in OPTIONAL:
            continue
        spec = importlib.util.find_spec(mod) if mod not in _sources() else None
        if spec is None:
            continue
        origin = spec.origin or ""
        if "site-packages" in origin or "dist-packages" in origin:
            third_party.add(DISTRIBUTION.get(mod, mod).lower())
    undeclared = sorted(third_party - declared)
    assert not undeclared, (
        "imported at runtime but not in requirements.txt: " + ", ".join(undeclared))


def test_optional_extras_are_never_runtime_requirements():
    """playwright must stay optional: it is imported lazily behind a guard."""
    declared = (ROOT / "requirements.txt").read_text().lower()
    assert "playwright" not in declared, \
        "playwright is optional (lazy import) — it must not become a hard dep"


def _knobs_under(env_overrides):
    """Import config in a fresh process with these env vars and report the knobs.

    A subprocess is the honest way to test this: `load_dotenv()` deliberately does
    not clobber a variable already in the environment (so the override wins), and
    the shared `config` module is never mutated for the rest of the suite.
    """
    code = "import config; print(config.PRO_PRICE_USD, config.SUBSCRIPTION_DAYS)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          env={**os.environ, **env_overrides},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def test_the_price_and_the_term_come_from_the_environment():
    """Repricing Pro is a .env edit plus a restart — never a code edit.

    This is the operator's whole workflow, so it gets a test: if either knob ever
    goes back to a literal in config.py, the value stops moving here.
    """
    assert _knobs_under({"PRO_PRICE_USD": "2.50", "SUBSCRIPTION_DAYS": "30"}) == \
        ["2.5", "30"]


def test_a_zero_day_term_is_floored_at_one_day():
    """SUBSCRIPTION_DAYS=0 would credit a buyer and expire them in the same breath."""
    assert _knobs_under({"SUBSCRIPTION_DAYS": "0"})[1] == "1"
