"""run.sh — the start command's status block.

Why this file exists: `./run.sh --status` sits BEFORE the stale-instance kill on
purpose. A status command that stops the bot would be the worst possible
regression in a script people run to *check* on things, and nothing else in the
suite would notice — so the ordering and the read-only property are pinned here.
The block itself must stay privacy-safe: counts and up/down only, never a chat
id, tracking number or name (it is meant to be pasted into a chat).
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run.sh"


def _bot_pids():
    out = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True,
                         text=True).stdout.split()
    return sorted(out)


def test_run_sh_is_syntax_valid():
    subprocess.run(["bash", "-n", str(RUN)], check=True)


def test_status_branch_precedes_the_stale_kill():
    """Source order: --status must exit before anything is killed."""
    src = RUN.read_text()
    status_at = src.index('"${1:-}" = "--status"')
    kill_at = src.index("Kill existing bot instances")
    assert status_at < kill_at, "--status must be handled before the kill block"
    assert "exit 0" in src[status_at:kill_at], "and must exit without starting"


def test_status_is_read_only():
    """Running --status must not disturb the running bot (same PIDs after)."""
    before = _bot_pids()
    out = subprocess.run([str(RUN), "--status"], capture_output=True, text=True,
                         cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert _bot_pids() == before, "--status changed the bot's process list"


def test_status_block_reports_the_facts_that_matter():
    out = subprocess.run([str(RUN), "--status"], capture_output=True, text=True,
                         cwd=ROOT)
    text = out.stdout
    assert "Packey status" in text
    for field in ("bot", "console", "store", "pro", "app", "tunnel", "17TRACK"):
        assert re.search(rf"^\s+{field}\s", text, re.M), f"missing {field!r} line"
    # The two facts that decide whether the app is usable at all.
    assert re.search(r"local \d{3}", text), "local backend code missing"
    assert "chats /" in text and "packages" in text, "store counts missing"


def test_status_reads_persisted_sweep_state_not_a_fresh_import():
    """The dashboard subprocess cannot see the running bot's in-memory
    LAST_SWEEP — a fresh `import refresh` reported 'last sweep never' forever,
    even while sweeps were running (2026-09-29). The truth lives in
    sweep_state.json, written by refresh after every sweep."""
    text = RUN.read_text()
    assert 'pathlib.Path("sweep_state.json")' in text
    assert "import refresh" not in text


def test_status_probes_run_storage_read_only():
    """Both store-reading probes (status_counts, ship24) must set
    storage.READ_ONLY before they import/load: a status command that renamed a
    state file to .locked/.corrupt or re-encrypted it would break the
    read-only promise the header makes."""
    src = RUN.read_text()
    assert src.count("storage.READ_ONLY = True") == 2, \
        "both status probes must opt into read-only storage"
    first_flag = src.index("storage.READ_ONLY = True")
    assert first_flag < src.index("storage.load_data(config.DATA_FILE)"), \
        "status_counts must set the flag before load_data"
    second_flag = src.index("storage.READ_ONLY = True", first_flag + 1)
    assert second_flag < src.index("from ship24 import"), \
        "the ship24 probe must set the flag before its import-time counter read"


def test_find_stale_only_kills_python_interpreters(tmp_path):
    """find_stale feeds `kill -9`, so a bare 'main.py' inside a command line
    must never be enough — `vim main.py` is editing the bot's source, not
    being the bot. Only a Python interpreter carrying main.py counts; the
    macOS framework build names its executable `Python`, so basename matching
    must survive a full path in comm."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "pgrep").write_text("#!/bin/sh\necho 424242 424243 424244\n")
    (stubs / "ps").write_text(
        "#!/bin/sh\n"
        "# args: $1=-o $2=comm=|command= $3=-p $4=pid\n"
        'case "$2:$4" in\n'
        "  comm=:424242) echo vim ;;\n"
        "  comm=:424243) echo python3 ;;\n"
        "  comm=:424244) echo /Library/Frameworks/Python.framework/Versions/3.13/"
        "Resources/Python.app/Contents/MacOS/Python ;;\n"
        "  command=:424242) echo 'vim main.py' ;;\n"
        "  command=:424243) echo '/usr/bin/python3 main.py' ;;\n"
        "  command=:424244) echo '/Library/Frameworks/Python.framework/Versions/3.13/"
        "Resources/Python.app/Contents/MacOS/Python main.py' ;;\n"
        "esac\n")
    for stub in (stubs / "pgrep", stubs / "ps"):
        stub.chmod(0o755)
    fn = subprocess.run(["sed", "-n", "/^find_stale()/,/^}/p", str(RUN)],
                        capture_output=True, text=True, check=True).stdout
    out = subprocess.run(
        ["bash", "-c", f'PATH="{stubs}:$PATH"\n{fn}\nfind_stale'],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pids = out.stdout.split()
    assert "424242" not in pids, "vim main.py must not be killed as a bot"
    assert "424243" in pids and "424244" in pids, \
        "real interpreters (python3, framework Python) must still match"


def test_status_block_is_privacy_safe():
    """No chat id, tracking number or other long digit run may appear."""
    out = subprocess.run([str(RUN), "--status"], capture_output=True, text=True,
                         cwd=ROOT)
    leaks = [ln for ln in out.stdout.splitlines()
             if re.search(r"\d{8,}", ln) and "http" not in ln]
    assert not leaks, f"status output carries long digit runs: {leaks}"


def test_status_wait_is_documented_and_overridable():
    src = RUN.read_text()
    assert "STATUS_WAIT" in src, "the settle time must be tunable"
    assert "--status" in src.split("set -uo pipefail")[0], "usage header must list it"


def test_dormant_bot_plist_keeps_the_anti_loop_rules():
    """com.packey.bot.plist is the bot-as-a-service option, shipped DORMANT (in the
    repo root, never copied into ~/Library/LaunchAgents by any code). Its two
    safety rules are what stop launchd from kill/restart-looping the bot, so they
    are pinned here rather than trusted to the comment header:

      * ProgramArguments supervises `main.py` DIRECTLY — never run.sh, which exits
        right after nohup-ing the bot, so KeepAlive on run.sh would respawn it
        forever, each respawn killing the bot the last one started;
      * KeepAlive is SuccessfulExit=false — the bot exits 0 on SIGINT (graceful
        shutdown saves state), so a manual ./run.sh restart must not look like a
        crash to launchd.

    The plist also stays OUT of the launchd domains: dormant means dormant, and
    this test cannot check the live state — only that the shipped file still
    encodes the rules.
    """
    import xml.etree.ElementTree as ET

    def _plist_dict(elem):
        """{key: value-element} for a <dict>: stdlib ET has no sibling access, so
        walk the key/value pairs in order instead."""
        pairs, children = {}, list(elem)
        for i, child in enumerate(children):
            if child.tag == "key" and i + 1 < len(children):
                pairs[child.text] = children[i + 1]
        return pairs

    plist = ROOT / "com.packey.bot.plist"
    assert plist.exists(), "the dormant bot-supervisor option was deleted"
    top = _plist_dict(ET.fromstring(plist.read_text()).find("dict"))

    assert top["Label"].text == "com.packey.bot"

    args = [s.text for s in top["ProgramArguments"].findall("string")]
    assert "main.py" in args, "must supervise the bot process directly"
    assert not any("run.sh" in (a or "") for a in args), (
        "supervising run.sh would restart-loop: run.sh exits right after "
        "nohup-ing the bot, so KeepAlive would kill/restart it forever")

    keep = _plist_dict(top["KeepAlive"])          # KeepAlive is a nested <dict>
    assert set(keep) == {"SuccessfulExit"}, (
        "plain <true/> would respawn over the operator's clean SIGINT restart")
    assert keep["SuccessfulExit"].tag == "false", \
        "a clean exit (0) must never count as a crash"

    assert top["WorkingDirectory"].text == str(ROOT), \
        "the bot must run from the project dir"


def test_plist_logs_live_in_the_private_log_dir():
    """M16: launchd jobs (bot supervisor, tunnel watchdog, backup) must write
    their console logs under ~/Library/Logs/packey — world-writable /tmp lets
    any local user swap the bot's console output — and must create them
    private (UMask 077), since tracebacks can carry chat ids."""
    import re
    import xml.etree.ElementTree as ET
    for name in ("com.packey.bot.plist", "com.packey.backup.plist",
                 "com.packey.tunnel.plist"):
        raw = (ROOT / name).read_text()
        # Prose in plist comments may contain '--' (e.g. the --watch flag),
        # which strict XML forbids inside comments — strip them for parsing.
        top = ET.fromstring(re.sub(r"<!--.*?-->", "", raw, flags=re.S)).find("dict")
        pairs = {top[i].text: top[i + 1].text
                 for i in range(0, len(top) - 1) if top[i].tag == "key"}
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = pairs.get(key) or ""
            assert "/tmp/" not in path, f"{name}:{key} still writes /tmp"
            assert path.startswith("/Users/fredp/Library/Logs/packey/"), \
                f"{name}:{key} = {path!r} is not in the private log dir"
        assert pairs.get("UMask") == "63", f"{name}: UMask 077 (63) missing"


def test_run_sh_starts_the_bot_log_in_the_private_dir():
    """The background start must write bot.out under the private log dir
    (created 0700, file 0600) — and the status probes read $BOT_LOG with the
    old /tmp path only as a pre-restart fallback."""
    src = RUN.read_text()
    assert 'LOG_DIR="${HOME}/Library/Logs/packey"' in src
    assert 'nohup "$PY" main.py "$@" > "$LOG_DIR/bot.out" 2>&1 &' in src
    assert "chmod 600" in src and "chmod 700" in src
    probes_at = src.index('grep -cE \'Traceback|TelegramConflictError\' "$BOT_LOG"')
    fallback_at = src.index("BOT_LOG=/tmp/bot.out")
    assert fallback_at < probes_at, "fallback must be decided before the probes"
    # The ONLY /tmp/bot.out mentions live on the fallback-detection line.
    offenders = [ln for ln in src.splitlines()
                 if "/tmp/bot.out" in ln and "BOT_LOG=/tmp/bot.out" not in ln]
    assert not offenders, f"bare /tmp/bot.out references remain: {offenders}"
