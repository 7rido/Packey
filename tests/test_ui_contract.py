"""UI contract: a control that promises an action must keep the promise.

Three drifts this pins, all of the same shape (the first found in production on
2026-09-26, the rest by the audit that followed it):

  * a keyboard emits a callback_data that no handler matches (a dead button), or a
    handler exists for a callback nothing ever emits (dead code — or a typo that
    silently killed a button);
  * a command is registered but absent from /help (undiscoverable), or the commands
    menu lists something with no handler;
  * the Mini App reaches for an element id that is not there (a silent no-op), calls
    an /api endpoint with no route (a dead call), or renders a button no script ever
    touches (UI that looks interactive and does nothing);
  * a handler that answers with "<code>/command</code>" for the user to type instead
    of doing the thing — the ✏️ Rename bug, where tapping the button posted a template
    the user was expected to copy, edit and send.

Static and offline: no bot, no browser, no network.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = ("main.py", "ui.py", "pro.py", "miniapp.py", "phantom_page.py",
           "refresh.py", "carriers.py", "delivery_history.py", "share_links.py")
SOURCES = {name: (ROOT / name).read_text() for name in MODULES}
HTML = (ROOT / "webapp" / "index.html").read_text()


def _emitted_callbacks():
    emitted = {}
    for name, text in SOURCES.items():
        for raw in re.findall(r'callback_data\s*=\s*f?["\']([^"\']+)["\']', text):
            emitted.setdefault(raw.split("{")[0], set()).add(name)
    return emitted


def _handled_callbacks():
    handled = set()
    for text in SOURCES.values():
        handled |= set(re.findall(r'F\.data\.startswith\(\s*["\']([^"\']+)["\']', text))
        handled |= set(re.findall(r'F\.data\s*==\s*["\']([^"\']+)["\']', text))
    return handled


def _registered_commands():
    registered = set()
    for text in SOURCES.values():
        for group in re.findall(r'Command\(([^)]*)\)', text):
            registered |= set(re.findall(r'["\']([a-z_]+)["\']', group))
    return registered


def test_every_callback_a_keyboard_emits_has_a_handler():
    emitted, handled = _emitted_callbacks(), _handled_callbacks()
    assert emitted, "no callback_data found — did the keyboards move?"
    dead = {p: sorted(w) for p, w in emitted.items()
            if not any(h.startswith(p.rstrip("_")) or p.startswith(h) for h in handled)}
    assert not dead, f"buttons nobody answers (dead button): {dead}"


def test_no_handler_waits_for_a_callback_nothing_emits():
    emitted, handled = _emitted_callbacks(), _handled_callbacks()
    orphan = sorted(h for h in handled
                    if not any(p.startswith(h.rstrip("_")) or h.startswith(p)
                               for p in emitted) and h not in HTML)
    assert not orphan, f"handlers for callbacks nothing emits: {orphan}"


def test_every_registered_command_is_discoverable_in_help():
    help_region = SOURCES["main.py"].split("async def help_handler", 1)[-1] \
                                  .split("@router", 1)[0]
    missing = sorted(c for c in _registered_commands()
                     if f"/{c}" not in help_region and f"&lt;{c}" not in help_region)
    assert not missing, (
        f"commands the user cannot discover in /help: {missing} "
        "(document it, or an alias that is documented)")


def test_the_commands_menu_only_lists_registered_commands():
    block = SOURCES["main.py"].split("MENU_ITEMS = (", 1)[1].split("\n)", 1)[0]
    menu = set(re.findall(r'\("([a-z_]+)",', block))
    assert menu, "the commands menu came back empty"
    assert not (menu - _registered_commands()), \
        f"menu entries with no handler: {sorted(menu - _registered_commands())}"


def test_miniapp_never_reaches_for_a_missing_element():
    ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', HTML))
    referenced = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]", HTML))
    missing = sorted(referenced - ids)
    assert not missing, f"script touches ids that do not exist (silent no-op): {missing}"


def test_every_miniapp_button_is_wired_somewhere():
    """A button whose id appears exactly once exists only as markup — it renders and
    does nothing. Computed lookups (getElementById(id) over an array of ids) count:
    the id string then appears at least twice."""
    buttons = set(re.findall(r'<button[^>]*\bid="([A-Za-z0-9_-]+)"', HTML))
    assert buttons, "no buttons found — did the markup move?"
    inert = sorted(b for b in buttons if HTML.count(b) < 2)
    assert not inert, f"buttons nothing references (inert UI): {inert}"


def test_every_miniapp_api_call_has_a_route():
    called = set(re.findall(r"api\(\s*['\"](/[a-z_/]+)", HTML))
    called |= set(re.findall(r"fetch\(\s*['\"](/[a-z_/]+)", HTML))
    routes = set(re.findall(r'add_(?:get|post)\(\s*["\']([^"\']+)["\']',
                            SOURCES["miniapp.py"]))
    dead = sorted(c for c in called
                  if not any(c == r or c.startswith(r.rstrip("/")) for r in routes))
    assert not dead, f"the app calls endpoints with no route: {dead}"


def test_every_card_action_button_is_wired():
    """The Mini App's card buttons are class-based (not ids), so the inert-button
    gate above cannot see them — a template button with no wireCard handler would
    render and do nothing. The Share button was exactly that gap: the bot's cards
    could mint share links, the app's could not. Every `act-*` class the card
    template emits must be queried in wireCard."""
    template = HTML.split("function renderCard", 1)[1].split("function wireCard", 1)[0]
    wired = HTML.split("function wireCard", 1)[1]
    emitted = re.findall(r'class="btn(?: danger)? (act-[a-z]+)"', template)
    assert emitted, "no card action buttons found — did the template move?"
    missing = sorted(c for c in emitted if f".{c}'" not in wired and f'.{c}"' not in wired)
    assert not missing, f"card buttons rendered but never wired: {missing}"


def test_no_handler_answers_with_a_command_for_the_user_to_type():
    """The rename bug's signature, docstring-safe: only real code strings count."""
    offenders = []
    for name, text in SOURCES.items():
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if "callback_query" not in "".join(ast.dump(d) for d in node.decorator_list):
                continue
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]                      # skip the docstring
            for sub in body:
                for inner in ast.walk(sub):
                    if (isinstance(inner, ast.Constant)
                            and isinstance(inner.value, str)
                            and re.search(r"<code>/[a-z]+", inner.value)):
                        offenders.append(f"{name}:{node.name} → {inner.value[:60]}")
    assert not offenders, (
        "a button answers with a command for the user to type instead of acting "
        f"(the rename bug): {offenders}")

