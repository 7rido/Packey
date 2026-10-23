"""Formatting, keyboards and message helpers for the tracker bot.

Extracted from main.py (session order was swapped: ui.py landed BEFORE
miniapp.py, so the Mini App views can import these helpers directly instead
of using injected hooks). main.py re-imports every name here, so handlers
and tests keep working.

ui.py imports config / storage / carriers only. The live Bot (needed by
is_admin) comes from botstate (main() calls botstate.set_bot() once).
"""
import asyncio
import hashlib
import logging
import re
from html import escape
from typing import Dict, List, Optional, Tuple

from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

import carriers
import config
import storage
import botstate

logger = logging.getLogger("tracker")

# ── Privacy policy — ONE source for both surfaces ────────────────────────
# The bot's /privacy command and the Mini App's privacy sheet must say the same
# thing, so the text lives here and both render it (the Mini App over
# GET /api/privacy). Telegram-HTML subset only: <b>, <i>, <code>, <a>, and
# &amp; for a literal ampersand. Blank lines separate paragraphs — the app
# splits on them. Claims users can check are pinned by tests/test_privacy.py.
# The share-links promise carries the configured TTL, so the text cannot drift
# from the code that enforces it (same discipline as the retention line).
_share_ttl_sentence = (
    f"Links stop working after {config.SHARE_LINK_TTL_DAYS} days"
    if config.SHARE_LINK_TTL_DAYS > 0 else "A link does not expire on its own")
PRIVACY_TEXT = (
    "🔒 <b>Your data, in plain words</b>\n\n"

    "<b>1. What's stored</b> — your chat ID, the tracking numbers you add, "
    "the names on your packages (the ones you set), their status history, "
    "and your Pro payment records — the amount, token and signature of each, "
    "plus the payment address reserved for you. Nothing else. Delivered "
    f"packages are forgotten automatically after {config.RETENTION_DAYS} days — "
    "the bot keeps a tracking number only while it's still doing work. "
    "Three things outlive the package. The payment receipts above and the "
    "payment address reserved for you (it has to stay stable, or a payment "
    "already sent would go to an address we no longer watch) hold no tracking "
    "number. The third does: a record of the international packages you've "
    "registered this Pro period — the numbers themselves — kept so a new one "
    "spends a slot of the shared quota, while re-adding one you already "
    "registered is free instead of locked out. It resets when your period "
    "does. "
    "A fourth file — the delivery estimates' training data — records only "
    "carrier, transit time and dates, with no chat ID and no number in it. "
    "Removing or forgetting a package also sends a stop-tracking request to "
    "our partner (17TRACK) — best effort, and a failed stop never blocks the "
    "local delete. Their side has no delete call, so a dormant record can "
    "remain with them (measured 2026-09-16).\n\n"

    "<b>2. What a notification never contains</b> — a status change sends "
    "one line: how many of your packages changed, plus a link to the app. "
    "No tracking number, name, status or carrier — so nothing sensitive "
    "lands in a chat that other people can read. Two messages are "
    "deliberately not that shape, and both delete themselves right after: "
    "the lost-package notice (it has to name the package to mean anything) "
    "and the Porch Guard delivery ping (it names the parcel so you know "
    "which door to run to).\n\n"

    "<b>3. In the chat</b> — the replies this bot sends delete themselves a "
    "moment after they're sent, so tracking numbers never pile up in chat "
    "history — group members included: the cards, <code>/list</code>, "
    "<code>/info</code>, and also the usage lines, errors and short notices. "
    "What stays is what still has a job: the prompts that wait for a tap (the "
    "removal and confirm ones) and the reference texts (<code>/privacy</code> "
    "itself, <code>/help</code>, the greeting). And your own side is cleared "
    "too: a command, a pasted number or a photo is deleted as soon as the bot "
    "has acted on it (in a group this needs the bot's own delete permission — "
    "if that's missing, the message simply stays).\n\n"

    "<b>4. Who sees what</b> — Free chats: USPS, UPS, FedEx, DHL and OnTrac "
    "numbers are read from the carriers' own sites. Pro chats: tracking also "
    "runs through 17TRACK (our partner), which sees every number you track, and "
    "a number no source can read may go to Ship24 as a paid last-resort lookup. "
    "Deleting a "
    "package asks 17TRACK to stop tracking it (unless another chat still tracks "
    "the same number) — best effort. Pro payments are public on the Solana "
    "blockchain: the amount and the receiving address are part of that record; "
    "the memo never carries your chat ID. That payment address is stable, so "
    "payments made to it are linked to one another on-chain — the QR / "
    "payment-link route is the unlinked one. No "
    "advertisers, no analytics, no resale.\n\n"

    "<b>5. What the bot does on its own</b> — updates are automatic and "
    "paced per package; delivered packages are never checked again. There "
    "are no refresh buttons, and nothing can add lookups beyond the normal "
    "cadence. One outside signal exists: a carrier's own delivery webhook "
    "can ask for a number it names to be checked at the next cycle tick "
    "(capped at one nudge per few minutes) — it can shift timing, never "
    "volume. (Manual refresh was removed so a shared carrier quota can't "
    "be burned by a busy chat, and so checking a package never means "
    "retyping its number).\n\n"

    "<b>6. Where it lives</b> — on the bot owner's machine, encrypted at "
    "rest (AES + HMAC); the owner's log masks tracking numbers and chat IDs. "
    "Plainly: the operator runs that machine and holds the encryption key, so "
    "they could technically read the encrypted store \u2014 that is one access "
    "this architecture cannot remove. What the bot itself never does is hand "
    "them another user's data: no command, alert, notice or log line carries "
    "one user's tracking number or chat ID, and nothing is ever sent anywhere "
    "else. "
    "Browser-based USPS checks use a disposable cookie-only profile copy, "
    "deleted on every shutdown. A photo or screenshot you send is read on that "
    "same machine \u2014 its barcode is decoded there and nothing is uploaded \u2014 and "
    "the image is deleted immediately \u2014 only what is read "
    "out of it is kept: the tracking numbers encoded in its barcodes.\n\n"

    "<b>7. Honest limits</b> — Telegram (the platform) sees your chats, "
    "and a notification reveals only that and how many packages changed.\n\n"

    "<b>8. Your controls</b> — <code>/forget</code> (delete one without "
    "typing its number) · <code>/remove</code> (one) · "
    "<code>/purge</code> (delivered) · <code>/clear</code> (everything, "
    "instantly). Deletion is immediate and irreversible.\n\n"

    "<b>9. Share links</b> — the Share button on a card creates a link anyone "
    "with the URL can open. It shows the package's name, status and latest "
    "update — never the tracking number. "
    f"{_share_ttl_sentence}, and you can revoke one with the button on the "
    "message that created it: that message deletes itself a moment later, so "
    "pressing Share again any time replaces the link (and kills the old "
    "one).\n\n"

    "Questions? Contact the bot owner."
)

# Telegram caps one message at 4096 chars; the full text outgrew that (5,131)
# and /privacy silently failed for every user (found 2026-09-28) — the send was
# rejected outright, so a user tapping /privacy saw NOTHING. The policy is ONE
# document, so it stays one constant — the chat sends it as several messages
# (see privacy_parts) while the Mini App keeps rendering the joined text in its
# scrollable sheet.
TELEGRAM_MSG_CAP = 4096

# The preferred break is the section-4 boundary, which splits the text where a
# reader expects a new topic rather than mid-sentence.
_split = PRIVACY_TEXT.find("<b>4. Who sees what")
PRIVACY_PARTS = (PRIVACY_TEXT[:_split], PRIVACY_TEXT[_split:])


def privacy_parts() -> Tuple[str, ...]:
    """PRIVACY_TEXT as the messages /privacy actually sends, in document order.

    Splitting on the section boundary alone is not enough of a guarantee: a later
    copy edit can push either half past the cap again, and the failure is silent
    (the whole command renders nothing). So any part that still does not fit is
    re-split on paragraph breaks, and a part with no room for even one paragraph
    is cut at the cap. A too-long policy then shows up as a few extra messages
    instead of a blank reply. Callers send every part; none of them expire.

    The break is carried on the head of each piece rather than stripped off, so
    joining the parts reproduces PRIVACY_TEXT byte for byte — a privacy policy
    must not lose a clause to a transport detail.
    """
    parts: List[str] = []
    for part in PRIVACY_PARTS:
        while len(part) > TELEGRAM_MSG_CAP:
            cut = part.rfind("\n\n", 0, TELEGRAM_MSG_CAP - 1)
            if cut > 0:
                cut += 2          # keep the blank line with the head
            else:
                cut = TELEGRAM_MSG_CAP
            parts.append(part[:cut])
            part = part[cut:]
        if part:
            parts.append(part)
    return tuple(parts)


def _format_duration(secs: float) -> str:
    secs = int(max(0, secs))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# ──────────────────────────────────────────────
# Parsing / sorting helpers
# ──────────────────────────────────────────────
def _is_tracking_like(token: str) -> bool:
    """Best-effort 'does this token look like a tracking number?' — used to
    disambiguate multi-number /track commands from names. Letters-only words
    (names) never match."""
    slug = carriers.detect_carrier(token)["code"]
    if slug != "unknown":
        return True
    return token.isdigit() and len(token) >= 8


# Two number-shaped things a shipping label prints that are NOT tracking numbers,
# and that the all-digits rule above would otherwise take for one: the
# destination ZIP+4 (9 bare digits) and a phone number (10 bare digits — even a
# plausible "DHL"). Shape is the only signal left, and it survives only BEFORE
# the separators are stripped: _clean_candidate turns "11550-4703" into
# 115504703, so a real label photo stored the destination ZIP as the package.
# Both forms are checked joined AND space-joined, because OCR hands a ZIP+4 over
# as one token ("11550-4703") or as two ("11550 4703").
_JUNK_SHAPES = (
    re.compile(r"\d{5}[\s-]\d{4}"),                      # ZIP+4
    re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}"),   # (555) 867-5309
)


def _junk_number_shape(tokens: List[str]) -> bool:
    """True when these raw tokens spell a ZIP+4 or a phone number.

    A separator is REQUIRED, so a bare 9- or 10-digit run is left to the gate
    exactly as before: this only refuses what a label prints as postal/contact
    data, never a number that merely has that length.
    """
    raw = "".join(tokens)
    spaced = " ".join(tokens)
    return any(p.fullmatch(raw) or p.fullmatch(spaced) for p in _JUNK_SHAPES)


def _contains_junk_shape(tokens: List[str]) -> bool:
    """True if these tokens, or any adjacent pair of them, spell a ZIP+4/phone.

    A join window that swallows postal or contact data can only FUSE junk: on a
    real label the window "NY" + "11550-4703" + "US" cleaned to two letters, nine
    digits and two letters — a valid-looking UPU/S10 number that was never
    printed anywhere on the page. A real number's tokens are printed next to each
    other, so refusing a window that spans a ZIP or a phone costs nothing.
    """
    return (_junk_number_shape(tokens)
            or any(_junk_number_shape(tokens[k:k + 1])
                   for k in range(len(tokens)))
            or any(_junk_number_shape(tokens[k:k + 2])
                   for k in range(len(tokens) - 1)))


# ── OCR character confusion ────────────────────────────────────────────────
# A label's printed glyphs come back as their look-alikes: 1→I/l, 0→O, 5→S,
# 8→B, 6→G, 2→Z. Joining fragments fixes spacing; only a swap fixes the rest.
# This is deliberately the narrowest correction that recovers a real number:
# ONE position, letter→digit, and only on a candidate the gate already rejected
# — trying combinations would let two edits turn a name fragment into a
# plausible id, which is worse than missing a number (a wrong package is watched
# silently). Direction matters: digits misread as letters is the common OCR
# failure, the reverse is not, so a legitimate G in a 1Z shipper block is never
# swapped away.
_OCR_CONFUSIONS = {"I": "1", "L": "1", "O": "0", "S": "5", "B": "8",
                   "G": "6", "Z": "2"}
_MAX_TRACKING_LEN = 40      # nothing real is longer; bounds the work per window


def _ocr_variant(candidate: str) -> Optional[str]:
    """`candidate` with ONE confusable letter read as its digit, or None.

    Only the two shapes a single swap can rescue are attempted, which is also
    the false-positive guard: an all-digits number whose sole obscuring letter
    is a confusable (94O0… → 9400…), and a 1Z number whose leading 1 was read as
    I/L (IZ… → 1Z…). A word, a punctuated phone number, or a too-short fragment
    cannot be made valid by one swap, so no variant is generated for it and the
    junk stays junk. Callers pass a candidate only AFTER the gate rejected it, so
    a number that already parses is never rewritten.
    """
    if not 8 <= len(candidate) <= _MAX_TRACKING_LEN:
        return None
    letters = [i for i, ch in enumerate(candidate) if not ch.isdigit()]
    if len(letters) == 1:
        pos = letters[0]
        digit = _OCR_CONFUSIONS.get(candidate[pos])
        if digit is None:
            return None
        variant = candidate[:pos] + digit + candidate[pos + 1:]
        return variant if _is_tracking_like(variant) else None
    # 1Z: the id body is alphanumeric by definition, so only the leading digit
    # can be a confusion — the "letters" here include legit shipper-block ones.
    if len(candidate) == 18 and candidate[1] == "Z":
        digit = _OCR_CONFUSIONS.get(candidate[0])
        if digit is not None:
            variant = digit + candidate[1:]
            return variant if _is_tracking_like(variant) else None
    return _repair_1z_prefix(candidate)


def _repair_1z_prefix(candidate: str) -> Optional[str]:
    """Restore a 1Z prefix whose Z came back as 2 (and whose 1 came back as I/L).

    Allowed to go digit→letter where the general substitution never does, and
    for a specific reason: the prefix is a CONSTANT of the format, not a guess
    about one character. Without it, a UPS label read as "12 1E8 52G …" has no
    readable number at all, the join falls back to a short tail window, and that
    tail — a slice of the real number — passes the all-digits rule and gets
    stored as the package. Guarded so a legitimate all-digit number from another
    courier is never rewritten into a UPS one.
    """
    if len(candidate) != 18 or not candidate.isalnum() or candidate.isdigit():
        return None
    if candidate[1] not in "2Z" or candidate[0] not in "1IL":
        return None
    variant = "1Z" + candidate[2:]
    return variant if _is_tracking_like(variant) else None


def _loose(candidate: str) -> str:
    """The candidate with every confusable letter read as its digit: the form two
    readings of the same printed number agree on. Used only to compare a stored
    fragment against a number we could not finish — the fragment may have been
    corrected (G→6) where the longer candidate was not, so a literal substring
    test would miss it."""
    return "".join(_OCR_CONFUSIONS.get(ch, ch) for ch in candidate)


def _damaged_number(candidate: str) -> bool:
    """True when a candidate has the SHAPE of a tracking number but fails the
    gate: the label had a number there and not all of it could be read.

    Fragments of such a candidate must never be stored on their own. A 13-digit
    slice of a UPS number is not a package — storing it silently watches the
    wrong thing forever, which is exactly what "it didn't scan the whole
    tracking" looked like live. Refusing is the honest answer: the photo path
    then asks for a tighter shot instead.
    """
    if not 15 <= len(candidate) <= 22 or not candidate.isalnum():
        return False
    digits = sum(c.isdigit() for c in candidate)
    return digits >= 12 and len(candidate) - digits <= 6


def _split_track_args(text: str) -> List[Tuple[str, bool]]:
    """Whitespace-split that honors quotes. Accepts straight ("..."/'...')
    and curly (“…”/‘…’) quotes — iOS auto-converts quotes as you type, so
    only accepting ASCII would look broken on iPhone. Matched quotes group;
    an unmatched quote is treated as a literal character. Returns
    (token, was_quoted) pairs so callers can tell literal names apart from
    bare tokens.

    A quote that never closes is a mark, not a quote: its span ends at the first
    line break (or at the end of the text) and its content is re-split as
    ordinary text. OCR text carries stray quote glyphs, and one of them used to
    swallow every following line into a single token — which is how a label's
    whole number line went missing before the gate ever saw it."""
    OPEN = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    # A straight apostrophe inside a word (Mike's) is a letter-part, not a
    # quote — Telegram never sends apostrophes pre-quoted, so only an
    # apostrophe at a token boundary opens a quoted span.
    out: List[Tuple[str, bool]] = []
    buf: List[str] = []
    closer: Optional[str] = None
    quoted = False  # current token came from a matched quoted span
    started = False  # a token is open (so "" or " " still yield a token)

    def flush():
        if started:
            out.append(("".join(buf), quoted))
            buf.clear()

    def flush_literal():
        """Emit an open span's content as ordinary text.

        An unterminated quote is not a quote, it is a stray mark — and OCR text
        is full of them: on a real label photo a noise glyph came back as a
        straight quote, and because a quoted span swallowed every newline after
        it, the whole TRACKING #: line became ONE token and the number was never
        even offered to the gate. Splitting the content keeps the mark literal
        (what the docstring always promised) and lets the whitespace inside it
        separate tokens again.
        """
        content = "".join(buf)
        buf.clear()
        for piece in content.split():
            out.append((piece, False))

    for ch in text:
        if closer is not None:
            if ch == closer:
                closer = None  # keep the quote marks out of the value
            elif ch in "\r\n":
                # A real quoted name is typed on one line; an unterminated span
                # is noise, and letting it cross a line is what hid a whole
                # label's number line.
                flush_literal()
                started = False
                quoted = False
                closer = None
            else:
                buf.append(ch)
        elif ch in OPEN and (ch != "'" or not buf):
            started = True
            quoted = True
            closer = OPEN[ch]
        elif ch.isspace():
            flush()
            started = False
            quoted = False
        else:
            started = True
            buf.append(ch)
    if closer is not None:
        flush_literal()      # unterminated at the end of the text: same reason
    else:
        flush()
    return out


def parse_track_args(text: str, has_command: bool = True) -> Tuple[List[str], str]:
    """Split '/track ...' into (tracking numbers, optional name). Every token
    that looks like a number becomes a package; anything else is name text
    (applied to the first package when exactly one is given). Quoted text
    ("/track \\"9400...\\" \\"Mom's gift\\"") is taken literally — so names may
    contain spaces and digits, and numbers may contain spaces.

    has_command=False parses text with no command word in front of it: a photo
    caption, or a screenshot's OCR dump. The first token is dropped only because
    /track carries the command there — with no command, dropping it would eat the
    first real token. Same parser either way: one implementation, no fork.

    OCR text needs more than spacing repair, so a fragment (or a whole token)
    that fails the gate gets ONE confusable-letter swap and is accepted only if
    that clears the same gate — the number as read always gets first chance. A
    candidate that still has a number's shape after that is treated as a number
    we could not finish, and its fragments are refused rather than stored.
    """
    parts = _split_track_args(text)
    if has_command:
        parts = parts[1:]
    ids: List[str] = []
    words: List[str] = []
    consumed: set = set()
    damaged: List[str] = []       # near-miss candidates: their fragments are refused
    # A number printed with spaces ("1Z 1E8 52G 03 2501 5793") arrives as several
    # fragments, none of them tracking-like on its own — OCR text and typed text
    # must parse identically. Join windows of adjacent UNQUOTED tokens, longest
    # first, and take the ones that clear the same gate a single token does.
    # The window reaches 12 because a label printed in 2-char groups is nine
    # pieces, and a cap of 8 silently shortened the join to a 10-digit tail.
    # Consumed tokens are dropped from the name too: those digits are part of
    # the number, not of what the user called the package.
    for size in range(12, 1, -1):
        for start in range(len(parts) - size + 1):
            window = list(range(start, start + size))
            if any(i in consumed or parts[i][1] for i in window):
                continue                      # consumed, or quoted literal text
            tokens = [parts[i][0] for i in window]
            if _contains_junk_shape(tokens):
                continue                      # a ZIP+4 / phone, not a number
            joined = _clean_candidate("".join(tokens))
            if not _is_tracking_like(joined):
                fixed = _ocr_variant(joined)          # the OCR second reading
                if fixed is None and _damaged_number(joined):
                    damaged.append(joined)            # a number we could not finish
                joined = fixed or ""
            if joined and joined not in ids:
                ids.append(joined)
                consumed.update(window)
    for idx, (tok, was_quoted) in enumerate(parts):
        if idx in consumed:
            continue
        if not was_quoted and _junk_number_shape([tok]):
            words.append(tok)     # a ZIP+4 / phone on the label, not a package
            continue
        cleaned = _clean_candidate(tok)
        # Quoted tokens are literals: the FIRST quoted-or-bare tracking
        # number wins as the package id; a quoted token that follows an
        # already-found id is a NAME — that's the whole point of quoting
        # ("/track 9400… "12345"" names the package "12345" instead of
        # tracking a second number). Quoting is also the user saying "exactly
        # this text", so no OCR correction is ever applied to a quoted token.
        candidate = cleaned
        if not was_quoted and not _is_tracking_like(cleaned):
            candidate = _ocr_variant(cleaned) or cleaned
        if candidate and _is_tracking_like(candidate) and not (was_quoted and ids):
            if candidate not in ids:
                ids.append(candidate)
        else:
            words.append(tok)
    # A number we could only half read must not become a package: drop any
    # fragment of a candidate that had a number's shape and failed the gate, so
    # the caller reports "no number" (and asks for a tighter photo) instead of
    # silently watching a slice of the real number forever.
    if damaged:
        # But neither may a number that WAS read in full be thrown away, and a
        # plain substring test cannot tell the two apart. On a real label the
        # full "1Z…4183" is a substring of the longer junk window "1Z…4183J"
        # (the number plus the next word's first letter), exactly like the
        # 12-digit slice this filter exists to refuse. They differ in DIGITS: a
        # fragment is missing digits, while the junk window only added letters,
        # so a reading is dropped exactly when the longer candidate held more
        # digits than it did. Substring on the correction-normalised form (the id
        # may carry a swap the longer candidate does not), digit counts on the
        # RAW form: a stray letter is not a digit that was printed, and _loose
        # would count it as one (J→3), making a complete read look like it was
        # missing a digit.
        loose_damaged = [(_loose(d), sum(c.isdigit() for c in d))
                         for d in damaged]
        ids = [i for i in ids
               if not any(_loose(i) in d
                          and sum(c.isdigit() for c in i) < n
                          for d, n in loose_damaged)]
    return ids, " ".join(words).strip()[:64]


# ── Recipient name from a label ──────────────────────────────────────────────
# The photo path keeps what a label prints for the receiver, so a package is
# named for the person it is going to instead of defaulting to "<your name>'s
# package". A ship-to block is one of these two shapes, and both end with the
# street line:
#
#     SHIP TO:             |  JANE DOE
#     JANE DOE             |  477 MARINE CENTER DR
#     477 MARINE CENTER DR |  HEMPSTEAD NY 11550-4703
#     HEMPSTEAD NY ...
#
# So the name is the lettered line nearest ABOVE the street line, and every
# rule below exists to stop something else from being read as a person: any
# digit disqualifies a line (that is the street, an apartment, a ZIP or a
# barcode), a lone street word ("DRIVE") or a city/state pair is refused, and
# carrier, printer and invoice words ("UPS GROUND", "BILLING: 3RD PARTY") are
# outright. When nothing qualifies the answer is "" and the card falls back to
# the old default name — a package with no name is better than a wrong one.
_STREET_SUFFIXES = frozenset({
    "ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
    "BLVD", "BOULEVARD", "CT", "COURT", "PL", "PLACE", "WAY", "HWY", "HIGHWAY",
    "PKWY", "PARKWAY", "CIR", "CIRCLE", "TER", "TERRACE", "TRL", "TRAIL",
    "LOOP", "SQ", "SQUARE", "TPKE", "TURNPIKE", "RTE", "EXPY", "FWY",
    "FREEWAY", "STE", "SUITE", "APT", "UNIT", "FL", "FLOOR", "BLDG", "RM",
    "PO", "BOX",
})
# Only used to refuse a city/state line whose ZIP landed on the next line — a
# "CITY ST" pair is an address, never a name ("HEMPSTEAD NY").
_US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU", "AS", "MP", "AA", "AE", "AP",
})
_NAME_BLOCKLIST = frozenset({
    "UPS", "USPS", "FEDEX", "DHL", "GROUND", "AIR", "EXPRESS", "PRIORITY",
    "TRACKING", "TRACK", "BILLING", "THIRD", "PARTY", "SHIP", "SHIPPER",
    "SHIPPING", "TO", "FROM", "DELIVER", "DELIVERY", "ADDRESS", "ADDR",
    "PHONE", "TEL", "ORDER", "INVOICE", "RECEIPT", "REF", "REFERENCE",
    "DATE", "TIME", "WEIGHT", "PKG", "PACKAGE", "PARCEL", "POSTAGE",
    "PREPAID", "RETURN", "SIGNATURE", "REQUIRED", "ADULT", "CUSTOMER",
    "ACCOUNT", "PAGE", "TOTAL", "AMOUNT", "HAZMAT", "SERVICE", "SERVICES",
}) | _STREET_SUFFIXES
# "JOHN SMITH III" must not come back as "John Smith Iii".
_ROMAN_SUFFIX = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")
# A suite/apartment line sits INSIDE the block: "JANE DOE / STE 200 / 477 MAIN
# ST" must still name the block, so this line is stepped over while walking up.
_UNIT_WORDS = frozenset({"APT", "STE", "SUITE", "UNIT", "FL", "FLOOR", "BLDG",
                         "BUILDING", "RM", "ROOM", "DEPT", "#"})
# The block is introduced either on its own line or on the name's own line.
_NAME_ANCHOR_RE = re.compile(
    r"^(?:ship\s*to|ship-?to|deliver(?:y)?\s*to|send\s*to|attn|attention"
    r"|c\s*/\s*o|to)\s*[:\-]?\s*", re.I)


def _lettered(token: str) -> str:
    """`token` without the punctuation a label or OCR wraps around a word."""
    return token.strip(".,:;!?\"'“”‘’()[]-–—/\\|*")


def _is_street_line(tokens: List[str]) -> bool:
    """A line that carries a street address: a number plus a street word."""
    if not any(ch.isdigit() for ch in "".join(tokens)):
        return False
    first = _lettered(tokens[0]) if tokens else ""
    return (first[:1].isdigit()
            or any(_lettered(t).upper() in _STREET_SUFFIXES for t in tokens))


def _is_unit_line(tokens: List[str]) -> bool:
    """A suite/apartment line: inside the block, not the line above it."""
    return bool(tokens) and _lettered(tokens[0]).upper() in _UNIT_WORDS


def _is_name_line(tokens: List[str]) -> bool:
    """True when a whole line could be somebody's (or some company's) name."""
    if not 1 <= len(tokens) <= 4:
        return False
    letters = 0
    for tok in tokens:
        word = _lettered(tok)
        if not word or not word.replace("-", "").replace("'", "").isalpha():
            return False
        if len(word) > 1 and word.upper() in _NAME_BLOCKLIST:
            return False
        letters += sum(c.isalpha() for c in word)
    if letters < 3:
        return False
    last = _lettered(tokens[-1]).upper()
    return not (len(tokens) > 1 and len(last) == 2 and last in _US_STATES)


def _tidy_name(line: str) -> str:
    """The printed name as a card should show it: "JANE DOE" → "Jane Doe"."""
    out = []
    for tok in line.split():
        word = _lettered(tok)
        if not word:
            continue
        if word.upper() in _ROMAN_SUFFIX:
            out.append(word.upper())            # John Smith III, not Iii
        elif word.isupper() and len(word) <= 2:
            out.append(word)                    # initials stay: J Doe
        else:
            out.append(word.title())            # O'BRIEN → O'Brien
    return " ".join(out)[:64]


def extract_recipient_name(text: str) -> str:
    """The ship-to name on a label, or "" when no line clearly is one.

    Read from the OCR text of ONE segmentation mode, and called only for a mode
    that also produced a tracking number: a reading too poor to find the number
    is not trusted to name the package. When OCR caught the anchor line
    ("SHIP TO:") it bounds the search, which keeps a sender's block (SHIP FROM)
    from being read as the receiver.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    anchor = next((i for i, ln in enumerate(lines)
                   if _NAME_ANCHOR_RE.match(ln)), None)
    start = anchor if anchor is not None else 0
    found = []
    for i in range(start, len(lines)):
        if not _is_street_line(lines[i].split()):
            continue
        # Nearest lettered line above the street line names that block. A unit
        # line is stepped over (it is part of the block); any other line with a
        # digit ends the search, because past it sits another block.
        for back in (1, 2, 3):
            j = i - back
            if j < start:
                break
            above = lines[j]
            if _is_unit_line(above.split()):
                continue
            if any(ch.isdigit() for ch in above):
                break
            candidate = _NAME_ANCHOR_RE.sub("", above)
            if _is_name_line(candidate.split()):
                found.append(_tidy_name(candidate))
                break
    # The LAST block wins when there is no anchor: a label prints the sender's
    # return address above the ship-to block, so the lower address is the one
    # the parcel is going to. (A return address printed *below* the ship-to
    # would win here instead — the limit of reading this from layout alone.)
    return found[-1] if found else ""


def sort_packages(packages: Dict[str, dict]) -> List[Tuple[str, dict]]:
    """Delivered packages sink to the bottom; everything else sorts by most
    recently updated first."""
    def _key(item):
        _tid, info = item
        is_delivered = 0 if info.get("status") == "Delivered" else 1
        return (is_delivered, info.get("last_updated") or "")
    return sorted(packages.items(), key=_key, reverse=True)


def _clean_tracking_id(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


# OCR turns a printed space into whatever it likes: a hyphen, a dot, a middle
# dot, a barcode bar. Those are separators *around* a number, not part of it, so
# a candidate is normalised before the gate — "1Z-1E8-52G-03-2501-5793" must
# parse exactly like "1Z 1E8 52G 03 2501 5793", which it did not before (the 1Z
# pattern needs exactly 18 alphanumerics, so every separator made the whole
# number invisible and the add path answered "couldn't find a tracking number").
# This does NOT loosen the gate: the normalised candidate still has to clear the
# same patterns, and _clean_tracking_id (used for /remove, /name and the Mini App
# ids) is deliberately left alone.
_OCR_SEPARATORS = str.maketrans({c: None for c in "-._·|–—/\\"})


def _clean_candidate(raw: str) -> str:
    """A candidate as the gate should see it: no spaces, no separators."""
    return raw.strip().upper().translate(_OCR_SEPARATORS).replace(" ", "")


def get_chat_id(message: Message) -> int:
    return message.chat.id


def status_badge(status: str) -> str:
    """Colored badge matching the status. Telegram HTML has no text-coloring
    support, so color-coded emoji circles are used to tint cards & lists."""
    return {
        "Delivered": "\U0001f7e2",             # 🟢 green
        "Out for Delivery": "\U0001f7e0",      # 🟠 orange
        "In Transit": "\U0001f535",            # 🔵 blue
        "Info Received": "\U0001f7e3",         # 🟣 purple
        "Available for Pickup": "\U0001f7e1",  # 🟡 yellow
        "Delivery Attempted": "\U0001f536",    # 🔶 orange diamond
        "Exception": "\U0001f534",             # 🔴 red
    }.get(status, "\u26aa")                     # ⚪ gray (pending/unknown)


# ── Package-as-character copy layer ────────────────────────────────────────
# Each package gets a first-person voice: one line per card, keyed by status.
# This is pure copy — no detection, no new state, no network, and nothing the
# card does not already show (it interpolates the package's own name, which the
# card prints anyway). The facts above it — status, carrier, dates, tracking
# number — are never reworded: the line is additive, so a card still reads the
# same to anyone who does not care about the voice.
_CHARACTER_LINES: Dict[str, List[str]] = {
    "In Transit": [
        "It's me — your {name}. Currently rattling around in a truck somewhere "
        "between the warehouse and your door.",
        "Still me! I'm in transit. Making great time, if I do say so myself.",
    ],
    "Out for Delivery": [
        "TODAY'S THE DAY. I'm on the truck — I've rehearsed my arrival. Be ready.",
        "Out for delivery! I'm so close I can practically hear your doorbell.",
    ],
    "Delivered": [
        "Made it! I'm at your door. (Or your neighbor's — check.) It's been an honor.",
        "Delivered! I traveled all this way just for you. Open me already.",
    ],
    "Info Received": [
        "The label's printed — I exist! Officially. Waiting for the carrier to "
        "pick me up.",
        "I'm in the system and labeled up. The truck hasn't found me yet.",
    ],
    "Available for Pickup": [
        "I'm at the pickup point, waiting. Bring ID. (Snacks optional. ID is not.)",
        "Ready for pickup! Come get me — I'm getting bored on this shelf.",
    ],
    "Delivery Attempted": [
        "I came by and nobody answered. Rude. I'll try again — or come get me.",
        "Missed connection! The driver tried, you weren't there. We go again.",
    ],
    "Exception": [
        "Something went sideways — not gonna lie. Check the details; I'm as "
        "confused as you are.",
        "We've hit a snag. Don't panic yet — read the latest update below.",
    ],
    "Pending": [
        "I'm in the system, warming up. Give me a minute.",
        "Just got here. Stretching before the journey.",
    ],
    # Not a carrier event: the last lookup failed, so say that and nothing more.
    "Unavailable": [
        "Having a moment — the carrier isn't answering right now. I'll be back.",
    ],
}
# Anything we cannot place gets a line rather than a hole in the card.
_CHARACTER_FALLBACK = "I'm here. The details are fuzzy, but I'm here."

# A package we have declared lost stops talking as if it were moving: the
# status table would read like a live reading. {date} is its last carrier event.
_MEMORIAL_LINE = "\U0001faa6 Your {name}, lost since {date}. Gone, but not forgotten."
# The one-time notices of the lost lifecycle, picked by the same md5 scheme as
# the character lines ("funeral"/"resurrect" stand in for the status).
# NOTE: the "30 days" in the funeral copy is the user's wording and matches
# LOST_AFTER_DAYS's default — an operator who raises the env knob is changing
# the threshold this notice quotes.
_LOST_NOTICES: Dict[str, List[str]] = {
    "funeral": [
        "\U0001faa6 It's me — your {name}. 30 days, no scans, no trucks, "
        "nothing. I'm declaring myself lost. It's been an honor.",
        "\U0001faa6 Your {name} here. I survived warehouses, customs, and "
        "conveyor belts — and then… silence. Pour one out.",
    ],
    "resurrect": [
        "\U0001f389 Plot twist — it's your {name}. I'm ALIVE. New scan just "
        "came in. The funeral was premature; please delete the eulogy.",
        "\U0001f389 Your {name}, back from the dead! They found me. I have "
        "stories.",
    ],
    "retraction": [
        "\u270b Hold on — your {name} was reported delivered, but the carrier "
        "pulled that back. Not delivered after all. The bot is watching it "
        "again.",
    ],
}


def lost_notice(tracking_id: str, name: str, kind: str) -> str:
    """The one-time funeral or resurrection notice for a package.

    Plain text (the sender escapes it) — it names the package, so the send must
    be ephemeral: this is the only loop notice that carries package data, and
    chat history is a store we don't control. Same md5-variant scheme as
    character_line, so a package keeps one voice through death and rebirth.
    """
    variants = _LOST_NOTICES.get(kind) or _LOST_NOTICES["funeral"]
    digest = hashlib.md5(f"{tracking_id}|{kind}".encode()).hexdigest()
    return variants[int(digest, 16) % len(variants)].format(name=name or "package")


def character_line(tracking_id: str, name: str, status: str,
                   lost: bool = False,
                   lost_since: Optional[str] = None) -> str:
    """One first-person line for this package in this status. Never empty.

    `lost=True` returns the memorial line instead of the status table: a
    package with no carrier event for LOST_AFTER_DAYS must not keep talking in
    the present tense. `lost_since` is the last-seen ISO stamp, rendered in the
    card's own date format.

    The variant is picked from an md5 of (tracking_id, status) so it is stable:
    the same card shows the same line on every render, in the bot and in the
    app alike, and a new line appears when the status moves on. md5 is a
    *stable* hash here, not a security choice — Python's hash() is salted per
    process, which would make the bot and the app disagree on the same package.
    """
    if lost:
        seen = storage.format_ts(lost_since) if lost_since else ""
        return _MEMORIAL_LINE.format(
            name=name or "package",
            date=seen if seen and seen != "-" else "an unknown date")
    variants = _CHARACTER_LINES.get(status)
    if variants is None and status.startswith("Unavailable"):
        # Unavailable arrives with a reason suffix in some paths.
        variants = _CHARACTER_LINES["Unavailable"]
    if not variants:
        return _CHARACTER_FALLBACK
    digest = hashlib.md5(f"{tracking_id}|{status}".encode()).hexdigest()
    return variants[int(digest, 16) % len(variants)].format(
        name=name or "package")


def package_card(tracking_id: str, info: dict) -> str:
    """Renders the card with a color-coded status badge. All user-controlled
    fields are escaped — an unescaped '<' in a name would make Telegram reject
    the whole message."""
    carrier_name = info.get("carrier_name", "Unknown")
    status = info.get("status", "Pending")
    # An outgoing parcel's "Delivered" means it arrived where it was sent, not
    # that something arrived here — same event, different sentence.
    status_label = status
    if status == "Delivered" and info.get("direction") == "outgoing":
        status_label = "Delivered — arrived at its destination"

    lines = [
        f"\U0001f4e6 <b>{escape(info.get('name', 'Package'))}</b>",
        f"<code>{escape(tracking_id)}</code>",
        "\u2500" * 18,
        f"{status_badge(status)} <b>{escape(status_label)}</b>",
    ]
    carrier_line = f"Carrier: {escape(carrier_name)}"
    if info.get("source"):
        carrier_line += f" \u00b7 Source: {escape(info['source'])}"
    lines.append(carrier_line)
    # The package's own voice: additive copy, right after the latest scan when
    # there is one, so the factual block above it stays a factual block. A lost
    # package gets the memorial line instead, dated from the last carrier event.
    line = character_line(
        tracking_id, info.get("name", "Package"), status,
        lost=info.get("lost") is True,
        lost_since=info.get("last_event_at") or info.get("last_updated"))
    voice = f"<i>{escape(line)}</i>"
    if info.get("detail"):
        lines.append(f"Latest: <i>{escape(storage._trim_sentence(info['detail']))}</i>")
        lines.append(voice)
    else:
        lines.append(voice)

    lines.append(f"Last updated: {escape(storage.format_ts(info.get('last_updated')))}")
    if info.get("last_checked"):
        lines.append(f"Checked: {escape(storage.format_ts(info['last_checked']))}")

    card = "\n".join(lines)

    if info.get("needs_pro"):
        # The card must explain a frozen status: 17TRACK-only carrier on a
        # free plan — the status above is the last known one, not a live read.
        card += (
            "\n\n\U0001f30d <b>Updates paused — Pro required.</b> This "
            f"{escape(carrier_name)} number can only be checked through "
            "17TRACK, which is part of Pro. It resumes automatically on Pro."
        )
    if info.get("register_blocked"):
        card += (
            f"\n\nNote: 17TRACK cannot register this {escape(carrier_name)} package "
            f"under the carrier's current API policy; the bot could not read the "
            f"official {escape(carrier_name)} tracking page automatically. "
            "Use the Official tracking button below."
        )
    return card


def build_package_keyboard(tracking_id: str, info: Optional[dict] = None,
                           is_pro_tier: bool = False) -> InlineKeyboardMarkup:
    # No Update button: updating is the global loop's job (state-adaptive
    # cadence), never a user action — the details live in the Mini App.
    rows = [[
        InlineKeyboardButton(text="\u270f\ufe0f Rename", callback_data=f"rename:{tracking_id}"),
        InlineKeyboardButton(text="\U0001f5d1 Remove", callback_data=f"remove:{tracking_id}"),
    ]]
    if (info or {}).get("muted"):
        rows.append([InlineKeyboardButton(text="\U0001f50a Unmute", callback_data=f"mute:{tracking_id}")])
    else:
        rows.append([InlineKeyboardButton(text="\U0001f514 Mute", callback_data=f"mute:{tracking_id}")])
    # Porch Guard: deliberately NOT Pro-gated, unlike the direction toggle — a
    # free chat gets it on the slow cadence and a Pro chat gets it webhook-fast,
    # and that speed difference is the honest upsell rather than a locked switch.
    # Action-oriented label, like the mute row above.
    porch_on = (info or {}).get("porch_guard", True)
    rows.append([InlineKeyboardButton(
        text="\U0001f515 Porch guard off" if porch_on else "\U0001f514 Porch guard on",
        callback_data=f"pg:{tracking_id}")])
    if is_pro_tier:
        # Incoming/outgoing is display-only metadata, and only Pro may set it:
        # a free chat sees no toggle at all (the handler refuses anyway).
        outgoing = (info or {}).get("direction") == "outgoing"
        rows.append([InlineKeyboardButton(
            text="\U0001f4e5 Mark incoming" if outgoing else "\U0001f4e4 Mark outgoing",
            callback_data=f"direction:{tracking_id}")])
    # A public status link for this one package: the recipient (a gift's
    # recipient, say) watches it arrive without Telegram — and without ever
    # seeing the tracking number.
    rows.append([InlineKeyboardButton(text="\U0001f517 Share",
                                      callback_data=f"share:{tracking_id}")])
    url = (info or {}).get("carrier_url") or ""
    if url:
        rows.append([InlineKeyboardButton(text="\U0001f310 Official tracking", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_confirm_keyboard(action: str, tracking_id: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="\u2705 Confirm",
            callback_data=f"confirm_{action}:{tracking_id}" if tracking_id else f"confirm_{action}",
        ),
        InlineKeyboardButton(text="\u274c Cancel", callback_data="cancel_action"),
    ]])
async def is_admin(message: Message) -> bool:
    """True in private chats (the owner is the only user anyway); for groups,
    true only when the sender is an admin or the group creator."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return True
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return False
    try:
        member = await botstate.bot.get_chat_member(chat_id=message.chat.id, user_id=uid)
    except Exception as e:
        logger.warning("⭐️ Admin check failed (chat %s, user %s): %s",
                       storage.mask_chat(message.chat.id), uid, e)
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def can_manage_package(message: Message, tracking_id: str) -> bool:
    """Admins can manage anything; other users can only manage the packages
    they originally added (the 'added_by' field)."""
    if await is_admin(message):
        return True
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return False
    info = storage.track_sessions.get(message.chat.id, {}).get(tracking_id)
    return bool(info and info.get("added_by") == uid)


async def safe_edit_text(message: Message, text: str, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        pass


# Messages that carry package data (cards, /list, /info) are deleted shortly
# after they're sent: chat history — group history especially — is a permanent
# store we don't control, so tracking numbers and names shouldn't linger there.
# The Mini App is the surface that keeps the details.
_pending_deletes: set = set()


async def _delete_later(chat_id: int, message_id: int, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await botstate.bot.delete_message(chat_id, message_id)
    except Exception as e:
        # Never fatal: in groups the bot may lack delete rights, and a message
        # the user removed themselves is fine too.
        logger.debug("Ephemeral delete skipped (%s/%s): %s",
                     storage.mask_chat(chat_id), message_id, e)


async def send_ephemeral(message: Message, text: str, *, ttl: Optional[int] = None, **kwargs):
    """Send a data-bearing message, then delete it after `ttl` seconds
    (config.EPHEMERAL_TTL by default; <= 0 keeps the message)."""
    sent = await message.answer(text, **kwargs)
    seconds = config.EPHEMERAL_TTL if ttl is None else ttl
    if seconds > 0:
        # Hold a reference: an unreferenced task can be garbage-collected
        # mid-sleep, which would silently skip the deletion.
        task = asyncio.create_task(_delete_later(sent.chat.id, sent.message_id, seconds))
        _pending_deletes.add(task)
        task.add_done_callback(_pending_deletes.discard)
    return sent


async def send_ephemeral_chat(chat_id: int, text: str, *, ttl: Optional[int] = None, **kwargs):
    """send_ephemeral for the background loop, which holds a chat id but no
    Message. Same contract, and it matters most here: the lost-package funeral
    names the package, so it must never become permanent chat history."""
    sent = await botstate.bot.send_message(chat_id, text, **kwargs)
    seconds = config.EPHEMERAL_TTL if ttl is None else ttl
    if seconds > 0:
        # Same GC-safety as send_ephemeral: an unreferenced task can be
        # collected mid-sleep, which would silently skip the deletion.
        task = asyncio.create_task(_delete_later(chat_id, sent.message_id, seconds))
        _pending_deletes.add(task)
        task.add_done_callback(_pending_deletes.discard)
    return sent


class FakeMessage:
    """Wraps a CallbackQuery so it can be passed where a Message is expected.

    from_user is the TAPPER, not whoever sent the message the button sits on: the
    confirmation handlers compare it against the stored requester, and
    _add_packages stamps added_by from it — a package confirmed by a tap has to
    belong to the person who tapped, not to the bot that sent the prompt.
    """

    def __init__(self, callback: CallbackQuery):
        self.chat = callback.message.chat
        self.from_user = callback.from_user
        self._origin = callback.message

    async def answer(self, text: str, **kwargs):
        """Answer where the tapped prompt is, so a confirmed action keeps talking
        in the same chat (send_ephemeral only needs .chat/.message_id back)."""
        return await self._origin.answer(text, **kwargs)