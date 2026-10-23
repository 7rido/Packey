"""M10: the copied Chrome cookie jar is stripped to carrier-only rows.

The slim profile copy clones the user's WHOLE `Cookies` database so the
browser tier keeps its Akamai trust cookies — which also put every other
site's session cookies on disk inside ~/Library/Caches for up to two hours.
_prune_profile_cookies() must keep only the hosts the tier can actually need
(carrier first-party domains + Akamai's bot-defense hosts) and leave none of
the deleted values behind in the file's free pages.
"""
import os
import sqlite3

import pytest

import usps_browser


def _make_jar(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    con.executemany("INSERT INTO cookies VALUES (?,?,?)", rows)
    con.commit()
    con.close()


def test_prune_keeps_carrier_rows_and_deletes_everything_else(tmp_path):
    jar = tmp_path / "Cookies"
    _make_jar(str(jar), [
        ("tools.usps.com", "_abck", "usps-trust"),
        (".usps.com", "bm_sz", "usps-trust-2"),
        ("usps.com", "legacy", "root"),
        ("webapis.ups.com", "ups", "v"),
        ("www.fedex.com", "fx", "v"),
        ("www.dhl.com", "dhl", "v"),
        ("akstat.io", "ak", "v"),
        ("mail.google.com", "SSID", "secret-session"),
        ("accounts.youtube.com", "SAPISID", "secret-session-2"),
        # Near-miss hosts must NOT ride the allowlist: no dot before
        # "usps.com", and "usps.com" is not a suffix of either.
        ("fakusps.com", "spoof", "v"),
        ("x.usps.com.evil.io", "spoof", "v"),
    ])
    usps_browser._prune_profile_cookies(str(tmp_path))
    con = sqlite3.connect(str(jar))
    kept = sorted(r[0] for r in con.execute("SELECT host_key FROM cookies"))
    con.close()
    assert kept == [".usps.com", "akstat.io", "tools.usps.com", "usps.com",
                    "webapis.ups.com", "www.dhl.com", "www.fedex.com"]
    # secure_delete + VACUUM: the dropped hosts and their values must not
    # linger in the file's free pages either.
    raw = jar.read_bytes()
    assert b"mail.google.com" not in raw
    assert b"accounts.youtube.com" not in raw
    assert b"secret-session" not in raw
    assert b"fakusps.com" not in raw
    # Owner-only, so the pruned jar stays as private as the copy dir (0700).
    assert os.stat(str(jar)).st_mode & 0o777 == 0o600


def test_a_cookies_file_without_a_cookie_table_is_left_alone(tmp_path):
    junk = tmp_path / "Cookies"
    con = sqlite3.connect(str(junk))
    con.execute("CREATE TABLE something_else (x)")
    con.commit()
    con.close()
    # Schema drift (or a non-jar file that happens to be named Cookies) must
    # not fail the copy — there is nothing recognizable to strip.
    usps_browser._prune_profile_cookies(str(tmp_path))


def test_a_corrupt_cookie_jar_fails_closed(tmp_path):
    junk = tmp_path / "Cookies"
    junk.write_bytes(b"not a database at all")
    # The caller drops the copy and skips the cycle rather than browse with
    # an unverifiable jar — a raised error IS the contract here.
    with pytest.raises(sqlite3.DatabaseError):
        usps_browser._prune_profile_cookies(str(tmp_path))


def test_full_profile_mode_never_clones_the_passwords_store():
    # The legacy full-mode ignore list must cover the saved-passwords store
    # and its journal — a clone of those under ~/Library/Caches would be a
    # credential snapshot no matter how the dir is chmodded.
    assert "Login Data" in usps_browser._PROFILE_FULL_IGNORE
    assert "Login Data-journal" in usps_browser._PROFILE_FULL_IGNORE
