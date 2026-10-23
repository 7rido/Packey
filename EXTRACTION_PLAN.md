# main.py Extraction Plan — one module per fresh Cline session

Generated 2026-09-14 from a structural scan of main.py (3,012 lines, 126 defs).
UPDATED 2026-09-15 after Session 8 (main.py 1,136 lines). REFRESHED 2026-09-28:
main.py is ~2,100 lines (photo/barcode reading, /ping, the operator alerts and
the share flow grew after the extraction ended — the obvious next extractions
are photo_read, status_report and ping), the session numbers below ran out of
sequence, and "81 tests must pass" is stale — the suite is 960+ and growing.
RULES for every session: pure code
motion only (same names, same behavior), line-range reads only,
`python3 -m py_compile main.py` + `pytest tests/ -q` must pass after each
step, and `main.py` re-imports from the new module so nothing else changes.
Never do more than one numbered step per session.

## Sessions 1–7a status — DONE: config.py, storage.py, carriers.py, pro.py, ui.py, miniapp.py, refresh.py (+ botstate.py)
(Session 6 ran before Session 5 per the option-C call, so the Mini App
views import the ui helpers directly — no format/parse hooks.)
- `config.py` (~110 lines): all env-driven constants + the .env load,
  which runs before any constant is evaluated. main.py does
  `import config` + `from config import (...)` re-exports; `main()` keeps
  `config._bot_token` in sync with its own `_bot_token` rebind.
- `storage.py` (~250 lines): Fernet encryption helpers
  (`_get_fernet` reads `config._bot_token`), `_read/_write_protected`,
  the cooldown store (`_register_blocked_until`, `save/load_cooldown`
  — now take the target file path as an argument), `track_sessions`
  (mutated in place by `load_data`, never rebound — so
  `main.track_sessions IS storage.track_sessions`), the confirmation
  store, and the time/format helpers (`now_iso`, `format_ts`,
  `_trim_sentence`, `time_since`).
- `main.py` keeps thin wrappers `load_data()/save_data()/load_cooldown()/
  save_cooldown()` that pass `main.DATA_FILE` / `main.REG_COOLDOWN_FILE`
  at call time — this preserves the tests' `main.DATA_FILE = tmp` patching.
- `carriers.py` (~600 lines): `TRACK17_BASE_URL`, `USER_AGENTS`, carrier
  detection (`CARRIER_NAMES`/`CARRIER_URL_TEMPLATES`/`detect_carrier`),
  shared `http_client`, `RateLimiter` + both limiters, `LOOKUP_OK/FAIL`
  (owned here), `_usps_browser_sem` (owned here — the batch path in main
  touches `carriers._usps_browser_sem`), `_usps_auth_failed_until` (owned
  here now, re-exported by main), all status helpers + lookup backends +
  `fetch_track_status` dispatcher. Defaults to option (B): production code
  reads `config.*` single-copy values; rebind-style tests now patch
  `carriers.config.*` / `carriers.*` (~8 lines in tests/test_tracker.py).
  carriers.py calls main's `save_cooldown()` wrapper (defined before the
  `import carriers` line); the `__main__` CLI syncs `carriers.track17_api_key`.
- `pro.py` (~350 lines): pro state dicts (`_pro_users`, `_refresh_usage`,
  `_pending_payments`, `_expiry_warned` — shared objects), `load/save_*`
  persistence, tier/quota fns (`is_pro`, `pkg_limit`, `daily_refresh_limit`,
  `get_refresh_interval`, `check_package_limit`, `check_refresh_limit`,
  `consume_refresh`), Solana (`_sol_rpc`, `_tx_payment_info`,
  `_activate_pro`, `monitor_solana_payments`, `_pro_expiry_task`).
  Two documented adaptations: `load_pro_state` mutates the dicts in place
  (Session-2 `load_data` pattern — main's re-imports stay the same objects),
  and `bot` is setter-injected (`main()` calls `pro.set_bot(bot)`).
  Stayed in main: `_last_bg_check` (refresh-loop owned) + the three aiohttp
  invoice views and `_build_miniapp_app` (Session 5 owns app assembly).
  **Session 18 addition (unique-amount payments)** — `allocate_amount`,
  `to_micros`, `open_amounts`, `new_memo_token`, `_match_payment` and the two
  new state dicts `_expired_payments` / `_used_amounts` live ONLY in pro.py
  (miniapp reaches them through its `_pro()` handle; main.py deliberately does
  not re-export them, so there is exactly one owner of payment state).
  Tests: `tests/test_payments.py`.
  **Session 19 additions (international allowance + one privacy text)**:
  `PRO_INTL_LIMIT = 3` (config) is enforced by `pro.check_intl_limit` /
  `count_international`, called from the bot's `/track` and
  `miniapp._api_add` (which answers `intl_limit` — never `limit_reached`, so a
  paying user is not shown the upgrade sheet). The classification rule moved to
  `carriers.is_international` / `carriers.is_international_number`; miniapp's
  `_is_international` is now a thin delegate so the 🌍 tab and the allowance
  cannot drift. The privacy policy lives in `ui.PRIVACY_TEXT` (main.py
  re-exports it), rendered by `/privacy` and served to the Mini App sheet by
  `GET /api/privacy`. Tests: `tests/test_intl_limit.py`, `tests/test_privacy.py`.
- `ui.py` (~210 lines, Session 6 — landed before Session 5 per option-C):
  `_format_duration`, parsing/sorting helpers (`_is_tracking_like`,
  `parse_track_args`, `sort_packages`, `_clean_tracking_id`), `get_chat_id`,
  `status_badge`, `package_card`, both keyboards, `is_admin`,
  `can_manage_package`, `safe_edit_text`, `FakeMessage`. Reads `storage.*`
  and `carriers.*`; `bot` setter-injected (`main()` calls `ui.set_bot(bot)`).
  Zero test edits (all pure functions, re-exported by identity).
- `miniapp.py` (~430 lines, Session 5 — ran after Session 6 per option-C):
  `_validate_init_data` (reads `config._bot_token`), `_miniapp_user`,
  `_miniapp_chat_id`, `_package_payload`, all aiohttp package views +
  pro invoice views, `_build_miniapp_app`. Moved ranges: main.py
  1137–1411 (validation through _api_rename) and 1430–1515 (pro views +
  app assembly); the pro re-import block between them stayed in main.
  Deviations (documented in miniapp.py): only stdlib + aiohttp + config +
  storage at module level; carriers/pro/ui enter via lazy `_carriers()` /
  `_pro()` / `_ui()` accessors (module-level imports would execute
  `import main` via carriers before main is ready); `_refresh_one` /
  `_register_and_fetch` / `save_data` wired via `miniapp.configure()`
  (Session 7 territory); tests/test_miniapp.py token fixture retargeted
  to `config._bot_token` (Session-3 precedent).

## Actual section map (line numbers at time of writing)

| Lines   | Section                                              | ~Lines |
|---------|------------------------------------------------------|--------|
| 1–184   | imports, all re-imports, wrappers, logging, state    | 184    |
| 185–952 | Handlers (/start … all command + callback handlers)  | ~768   |
| 953–967 | miniapp re-imports (no configure() anymore)          | ~15    |
| 968–1136| Startup (check_17track_key, main, __main__)          | ~169   |

### Session 10 — copy pass (BotFather text, /start, /help, /privacy, Mini App copy, Packey naming) + /track quoting — DONE
- `/start` rewritten (power-user framing + one-line roadmap; header is now
  📦 **Packey**); `/help` tightened w/ quoted-form example; `/privacy`
  restructured control-first; all per user picks 1-by-1 (items 1–5).
- Name decision: **Packey** (app: h1 + `<title>` + /start header) +
  **Packeybot** (bot display name, BotFather-side); `MODEL_NAME` is now
  `"packeybot"` so startup logs read `Bot started (packeybot)`.
- Mini App: empty state is action-first, outside-Telegram screen fixed
  (the ☰ menu-button reference was wrong — menu button was reset to
  default in startup), footer trimmed, privacy sheet = short variant;
  blocked-note, attention chip + counts landed unchanged alongside.
- `/track "number" "name"`: new quote-aware tokenizer `_split_track_args`
  (straight + curly iOS quotes; mid-word apostrophes stay literal; unmatched
  quotes stay literal). Quoted numbers still clean (spaces stripped); a quoted
  token after an already-found id is a NAME (fixes digit-names being eaten
  as packages). Unquoted behavior byte-identical to before.
- Verified: 90 passed + 1 skip (was 84/83 before: +6 parser tests, incl. the
  curly-quote case). Bot-side text needs a bot restart; app copy served from disk.
- Fail-loud follow-up: api() flags network-level failures as `unreachable`
  (dead tunnel / DNS gone / offline) with the message "Can't reach the
  server — send /start … open the app from the new message."; first paint
  (`load()`) gets an 8s timeout so a stale button fails in seconds, not
  after 20s of shimmer; the load error state names the two causes (dead
  tunnel vs. old button) and prescribes fresh /start. Verified: 91 passed +
  1 skip, new `test_unreachable_fails_loud_and_fast` structural assertions.
- Self-healing announce follow-up: miniapp.py owns `mini_app_url.json`
  (encrypted via storage helpers; load/save + `announce_miniapp_url` +
  `maybe_announce_miniapp_url`). main() calls it once at startup BEFORE
  polling: only when MINI_APP_URL exists AND differs from the recorded one,
  it sends one "📦 Packey dashboard moved" + fresh button to every chat
  with packages (per-chat try/except so one blocked chat can't abort the
  rest), then records. Empty store or same URL = silent no-op / silent
  record. Verified: 92 passed + 1 skip incl. the new announce test.
- Tunnel supervision follow-up (`tunnel.sh --watch` + `com.packey.tunnel.plist`
  LaunchAgent): 60s health loop on `MINI_APP_URL/api/packages` (401 =
  healthy, 000 = dead); 2 consecutive failures → re-mint quick tunnel,
  rewrite .env, restart bot (bot's self-heal announces the fresh button).
  `--watch --funnel` skips URL management for Tailscale; `--check` is a
  read-only status report (exit 0/1). Verified: 93 passed + 1 skip with
  `-W error`, incl. `test_tunnel_watch_mode` (`bash -n`, plist contents,
  --check exit/health output).
- App-syntax hotfix (the real cause of every "blank/frozen" report): editing
  residue from an earlier bad edit left `webapp/index.html` with a terminated
  string followed by stray tokens (`'…</div>';'s ☰ menu button.</p></div>';`),
  a JS **syntax error** that killed the whole `<script>` block — so the page
  rendered its static shell and nothing else. Text-based pytest checks could
  not see it and node (a real JS parser) isn't installed. Fixed: clean
  statement; `bootFailed()` moved ABOVE `if (!T)` because 'use strict' makes
  block-scoped function declarations invisible to the later catch-all call
  (would have been the next ReferenceError). New guards:
  `test_app_script_is_syntactically_plausible` (comment-aware string-state
  lint that flags an unterminated quote per line + brace/paren balance +
  residue string) and `test_boot_failed_is_top_level`. Verified: 95 passed +
  1 skip with `-W error`; served HTML confirmed clean via curl.
- Supervisor post-mortem (the 01:50 silent-death): four compounded bugs, all
  fixed — (1) `log()` printed to stdout, polluting `url=$(start_quick_tunnel)`
  captures; (2) BSD sed aborts on embedded newlines, and with `set -e` that
  killed the watcher mid-rotation; (3) the write path had no validator;
  (4) one monolithic loop meant any failed step aborted the supervisor.
  Fixes: file-only `log()` + console `note()`; `start_quick_tunnel` sets
  globals (no stdout capture); `valid_public_url` gate + atomic awk-rewrite
  in `set_env_url()`; `watch_tick()` structured to always return 0;
  `ROTATE_COOLDOWN` (300s) against churn; `--selftest` exercises the whole
  write path in a scratch dir (10/10 pass) and is wired into pytest.
- Attention cards visually distinct: `.card.attention` (status-colored
  inset edge + 5px accent) driven by `group(p)`; "Needs attention" section
  header tinted via `.section-h.attention-h`. Reuses the existing
  STATUS_COLORS vocabulary — Delivered behavior unchanged.
- `@media (prefers-reduced-motion: reduce)` kills spin/shimmer/transitions
  and all `:active` press-transforms; content and layout identical.
- Removed duplicate `.tier-text` and `.qr-caption` rules (kept the winning
  values — zero visual change).
- Chips: `role="group"`, `aria-pressed` synced in the click handler; fixed
  the stale "Auto" theme comment.
- `aria-live="polite"` on `#summary`, `role="status"` + `aria-live` on toast.
- Verified: 85 passed + 1 skip; brace/paren balance on the edited style +
  script blocks; new `test_attention_ux_session9` structural assertions.
  No backend touch, no restart needed — served from disk per request.

## Session 11 — encryption regression + verification-pipeline repairs (P0→P2)
- **At-rest encryption had been silently OFF.** `storage.py::_get_fernet()`
  lost its final `return _Fernet(key)` at an earlier edit boundary: the key was
  computed and discarded, so `_encrypt_payload` fell back to plaintext while
  `_ENCRYPTION_AVAILABLE` stayed True. Verified live: `_get_fernet()` was
  `None`, `track_data.json` (1586 B) was JSON rather than a Fernet token, and
  the migration branch fired on every startup logging "now encrypted at rest"
  — a false claim (same for pro/refresh/cooldown files).
  `pro_users.json.locked` (renamed by the broken read path) still decrypts with
  the current token: **1 entry, recoverable**. Fixed: the `return`, plus a
  `_is_ciphertext()` read-back so a migration log can only claim encryption
  after inspecting the bytes that landed, plus 5 tests that assert the
  ARTIFACT — `TestAtRestEncryption`: ciphertext on disk, round-trip, plaintext
  migration (+ `.plaintext.bak`), wrong-key still fails loud, and
  `_get_fernet()` is not `None`.
- **A Mini App parse error can no longer ship silently.** node is absent here,
  so the only JS check silently skipped. Now `osascript -l JavaScript`
  (JavaScriptCore — a real engine already installed, no new dependency) parses
  both inline blocks with `new Function(src)` (compiles, never runs); the
  existing `miniapp_filters.test.js` also executes under JXA instead of
  skipping (assert shim instead of `require('node:assert')`). Added an
  independent watchdog script block placed BEFORE the app block — a parse error
  in the app kills its own error handling, so only a separate block can report
  it — with the app setting `window.__packeyReady` once it boots. Proved by
  re-injecting the original residue into a scratch copy: the parser answers
  `PARSE_FAIL: Invalid escape in identifier: '\'`.
- Announce bookkeeping (`miniapp.py`): record `mini_app_url.json` only when a
  send actually landed, so a fully failed announcement retries next start
  instead of looking like success.
- `tunnel.sh --check` now reports a tunnel for OUR port specifically; the old
  `pgrep -f 'cloudflared|tailscaled'` line was satisfied by an unrelated
  cloudflared (Cloudflare's own launchd service) on this machine.
- Residue removed: `miniapp.py` `return app` (undefined name),
  `refresh.py` `return _save_data_fn()` (both callers save themselves; it also
  violated the `-> Tuple[dict, str]` signature), `carriers.py`
  `return random.choice(...)`; `main()` now cancels `_sol_task`/`_expiry_task`
  on shutdown. New gate `tests/test_code_hygiene.py` runs pyflakes and fails on
  anything except the documented re-export / CLI-override exceptions — the
  check that would have caught the missing `return`.
- Verified: **104 passed, 0 skipped** with `-W error` (`--selftest` 10/0,
  `--check` exit 0). Zero skipped matters: the JS rules now actually execute.
- **Verification lessons (this pipeline's two holes):** (1) tests are not a
  parser — after editing `webapp/index.html`, run the JS parse check, not just
  pytest; (2) for persisted state, assert the ARTIFACT (is the file ciphertext
  on disk?) instead of only an in-memory round-trip. And after ANY edit,
  re-read the edited region *and the line immediately after it*: both incidents
  (stray `\'` in HTML, missing `return` in Python) were tails lost at an edit
  boundary and were invisible to the tests of the day.

## Session 12 — international carriers, verified identity, source routing
- **CN/HK + general international detection**: UPU/S10 numbers end with their
  ORIGIN country code, so `detect_carrier` maps `…CN` → `chinapost`
  ("China Post / ePacket"), `…HK` → `hkpost`, any other country → `intl`
  ("International post (FR)"). Every slug now gets a working link: unknown and
  international numbers fall back to 17TRACK's universal tracker page, because
  CN/HK/EU posts mostly aren't deep-linkable.
- **"Unknown Carrier" fixed using 17TRACK's verified identity.** Shape-probed
  the live API (values masked): `track_info.tracking.providers[0].provider`
  carries `{alias, country, homepage, key, name, tel}`. `_extract_carrier()`
  (tolerant, in the same style as `_extract_status`) returns it;
  `_check_17track` now returns `(status, hint)` and the hint's verified
  name/key/country override our format guess. `_extract_detail()` also grabs
  `latest_event` text, so 17TRACK-sourced cards finally carry the
  "what happened" line the browser tier provides.
- **Verified links** (`verify_link()`): purely numeric formats are ambiguous
  (12 digits regexes as FedEx), so when the verified carrier disagrees with
  the guess the card switches to the universal 17TRACK link instead of
  pointing the user at the wrong carrier's site.
- **Source routing — `SOURCE_ROUTING=matrix` (default)**: 17TRACK is now the
  RESERVED lane. CN/HK/international always keep it; a slug whose official API
  is configured (USPS API v3 / Web Tools) stops spending 17TRACK quota. The
  hourly `/getquota` reading is now recorded in carriers, and quota pressure
  sheds only slugs that have a fallback tier — never the ones where 17TRACK is
  the only source. `SOURCE_ROUTING=legacy` restores the old behavior. Live
  check: with no USPS credentials configured, every slug still uses 17TRACK,
  i.e. today's behavior is unchanged until a real alternative exists.
- Privacy: `_check_17track` no longer dumps the raw payload (tracking number,
  addresses, phone fragments) — key names only; `refresh.py`'s Unavailable line
  sanitizes the number.
- Verified: **125 passed** with `-W error`; pyflakes clean on changed files;
  live payload extraction confirmed (`{'name': 'UPS', 'key': 100002,
  'country': 'US'}`); `main.py --help` OK. Restart the bot to pick it up.

## Session 13 — 🌍 International tab (Pro) + quota visibility
- New Pro-only Mini App view: a 🌍 International chip (locked for free users —
  the count still shows as an upsell, and tapping opens the Pro sheet) listing
  the cross-border lane: national posts only 17TRACK can check. A 17TRACK API
  quota meter heads the view.
- `GET /api/international` (miniapp.py): 401 unauthenticated, 403 with
  `pro_only` for free users, else `{pro, source: "17TRACK", packages: […],
  quota: {used, total, percent, checked_at, known}}`. Classification is
  server-side (`_is_international`: chinapost/hkpost/intl slugs, or 17TRACK's
  verified `carrier_country` in CN/HK — so a number our own regex called
  "unknown" still lands in the right view).
- `_package_payload` now exposes `carrier_country` (the client filter needs the
  verified origin country).
- One quota call site: `carriers.fetch_quota()` + `quota_snapshot(max_age=900)`
  — reuse the hourly reading, refresh at most every 15 minutes. The hourly
  watch in refresh.py now calls it instead of posting its own `/getquota`.
- Frontend: the chip, a colour-coded quota card (<70% green · ≥70% amber ·
  ≥85% red + "non-essential checks are paused"), and `isIntl()` kept verbatim
  in three places (server rule, index.html, tests/miniapp_filters.test.js) with
  a byte-check test. The view degrades gracefully: a bot that predates the
  endpoint answers 404 → the list falls back to the local filter and says a
  restart is needed, instead of an error the user can't act on.
- `api()` now tolerates a non-JSON body and exposes `err.status` / `err.proOnly`
  so callers can degrade rather than surface a JSON parse error.
- Verified: **130 passed** (`-W error`); pyflakes clean; offline smoke through
  the real aiohttp app (401 / 403 `pro_only` / 200 returning exactly the CN
  package plus the quota shape); served HTML already carries the view — the
  **endpoint needs a bot restart** (the running process still 404s it, which is
  the degradation path above).

## Session 14 — global adaptive updating + data-free notifications
- **Manual refresh is gone.** Removed: `/update`, `/refresh`, their commands-menu
  entries and `/help` section, the `update:` callback + the bot keyboard's
  "🔄 Update" button, the Mini App's ↻ header button and per-card "↻ Update"
  button, and `POST /api/refresh` (plus `_refresh_denied`). The add-flow's first
  check (`_register_and_fetch`) is the only user-triggered lookup left.
- **State-adaptive cadence (`refresh.CADENCE`)** replaces the per-chat tier gate:
  per-package due map `_due_at[(chat, tid)]` (in-memory; no schema change),
  terminal statuses land on `inf`. Table (pro/free seconds): Out for Delivery
  120/900 · Delivery Attempted 600/1800 · Available for Pickup 900/3600 ·
  In Transit 180/1800 (the advertised tiers) · Exception 1800/7200 ·
  Info Received/Pending 1800/7200 · Unavailable 900/3600 (retry, never freeze).
  Delivered = terminal (never re-checked — this is where the lookups went).
  Under quota pressure, non-critical waits stretch ×2; delivery-day stays sharp.
- **Notifications carry no package data.** `_notify_status_change` (which edited
  the package card — tracking number, name, status — into chat history) is gone.
  `_check_one` now returns its chat_id when a change deserves a ping; the loop
  aggregates per chat and sends ONE `_send_global_ping` per cycle: "n of your
  packages changed" + an "📦 Open the app" button to MINI_APP_URL (no tracking
  id in the URL — inline-button URLs are visible to the whole chat). Mute rules
  unchanged: muted packages stay silent unless the status is an ALERT_STATUS.
- `_check_one` moved out of `background_updater` (module level, semaphore passed
  in) so the cadence/mute/ping logic is directly testable.
- **Dormant by decision:** `pro.check_refresh_limit/consume_refresh/
  daily_refresh_limit/get_refresh_interval`, `_refresh_usage`,
  `refresh_limits.json` and the `FREE/PRO_REFRESH_LIMIT` config values stay in
  the codebase untouched — nothing calls them anymore.
- Copy/docs updated everywhere the removed feature was advertised: /start pitch,
  `/help`, the package-limit upsell, the Pro activation/expiry messages (pro.py),
  the Pro sheet (the refreshes perk became "Delivery-day checks every 2 minutes"),
  the tier row ("… packages · auto-updating"), README, and the smoke script
  (now asserts `/api/refresh` 404s).
- Verified: **150 passed** (`-W error`, +20 new tests), py_compile + pyflakes
  clean, `import main` OK; offline smoke through the real aiohttp app (404 on
  `/api/refresh`, 200 on `/api/packages` without refresh fields) and a fake-bot
  run of `_send_global_ping`. Restart the bot to pick it up.

## Session 15 — chat-surface privacy hardening (ephemeral cards + log masking)
- **Data-bearing chat messages self-destruct.** New `config.EPHEMERAL_TTL`
  (env `EPHEMERAL_TTL`, default 90 s, `0` = keep) + `ui.send_ephemeral()`: sends,
  then deletes via a referenced task (`_pending_deletes`, so the delete can't be
  GC'd mid-sleep). Deletion failures are swallowed — in a group where the bot
  isn't an admin the message simply stays.
- Applied to every send that carries a tracking number or name:
  the `/track` card, the "Already tracking: …" echo, the `/info` card and the
  `/list` roster. Confirmation prompts (remove/purge/clear) stay put on purpose:
  they only name the package and must survive until the user taps Confirm.
- **Log masking gaps closed**: `miniapp.py`'s MiniApp-remove 404 line logged the
  RAW tracking number (`tid=%r`) — now masked; `usps_browser.py` logged raw
  numbers in two batch-failure lines — now via a lazy `_mask()` wrapper around
  `carriers._sanitize_tracking_id` (lazy import keeps the browser module
  standalone). Chat IDs stay in the log by design (documented trade-off: the
  file is chmod 600 and rotates).
- Copy: `/privacy` grew item 5 ("In the chat"), the Mini App privacy sheet says
  the same, README documents `EPHEMERAL_TTL`.
- New `tests/test_privacy.py` (7 tests): deletion fires at the ttl, the default
  comes from config, `ttl=0` keeps the message, a refusing bot doesn't raise,
  every data-bearing handler goes through `send_ephemeral`, and both maskers
  agree. Plus a filter test that the Mini App privacy sheet mentions it.
- Verified: **158 passed** (`-W error`), py_compile + pyflakes clean,
  `import main` OK. Restart the bot to pick it up.
- **Follow-up (copy alignment):** `/privacy` rewritten into 8 numbered items that
  describe today's architecture — data-free pings, self-deleting cards, official
  USPS API instead of 17TRACK for USPS numbers, automatic/adaptive updating with
  no refresh buttons, log masking, and an explicit "honest limits" item (Telegram
  the platform, blockchain payments). The Mini App sheet was aligned to the same
  five lines. `test_privacy.py` now joins the per-line literals back into the
  rendered text and asserts the load-bearing claims survive future copy edits
  (159 passed).

## Session 16 — lifecycle deletion (/forget + retention)
- **`config.RETENTION_DAYS`** (env, default 14, `0` = off): delivered packages
  are forgotten after that window. `refresh._auto_purge_delivered()` runs on the
  hourly tick; `storage.last_updated` doubles as the delivery timestamp (the
  cadence table stops checking delivered numbers, and `_apply_lookup` only
  stamps on change) — **no schema change**. Only exactly `Delivered` qualifies;
  malformed/missing stamps are skipped, never guessed; `_parse_iso_epoch`
  tolerates legacy offset-less stamps.
- **`/forget`** — privacy-first removal: a numbered list of names/statuses (no
  tracking numbers) whose buttons carry the id in `callback_data` (never
  rendered), feeding the *existing* remove-confirmation flow, plus a "Forget all
  delivered" button that feeds the existing purge confirmation. Registered in the
  ☰ menu + `/help`. The list gets a longer TTL (≥300 s) because the user has to
  act on it.
- **Notices are data-free** like everything else: `build_retention_notice()`
  reuses the extracted `_app_button_keyboard()` (no tracking id in the URL).
- Disclosure: `/privacy` item 1 states the retention window, item 8 adds
  `/forget`; the Mini App's privacy sheet gained "Deleted for you", and delivered
  cards render a `retentionNote()` ("Auto-forgets in N days") driven by
  `retention_days` in `/api/packages`.
- Tests (+9 → **168 passed**): purge selection (old delivered removed; In Transit
  /Exception/Info Received kept; `0` disables; malformed and legacy stamps),
  notice carries no identifiers, `/forget` list prints no tracking numbers and
  emits index buttons, menu/markup gates.







## Session 17 — key separation, per-chat envelopes, redaction, masking
- **`config.DATA_ENCRYPTION_KEY`** (env, optional): the master key for the local
  data files. `storage._master_secret()` prefers it over the bot token, so
  rotating the token can't brick the store and a leaked token can't decrypt the
  disk. Files under the other key stay readable and are **re-keyed on first
  read** (`_rekey_file`, logged as `RE-KEYED …`). With no token *and* no key the
  bot now writes plaintext + warns, instead of the old hardcoded
  `glm-tracking-default-key` that only *looked* encrypted.
- **Per-chat envelopes (track_data.json v2)**: `save_data()` writes
  `{"v":2,"chats":{id:{"dek","blob"}}}` — each chat sealed under its own random
  key, wrapped by the master, inside the master-encrypted file. `load_data()`
  accepts v1 (flat mapping) and upgrades on the next save, keeping the pre-upgrade
  ciphertext as `track_data.v1.bak` so older code can still read the data. An
  envelope that won't open raises `_EnvelopeError` → loud `SystemExit` with the
  file preserved as `.corrupt` (a key problem must never look like corrupt data).
  Honest scope: the master still opens everything (the server must decrypt to
  poll), so this is blast-radius control, not zero-knowledge.
- **`/list` redaction**: names + statuses + carriers only, plus a pointer to the
  app; the "Already tracking …" echo no longer reprints numbers either.
- **`storage.mask_chat()`**: logs carry a stable `chat:xxxxxxxx` HMAC tag instead
  of raw chat IDs; all 10 log sites (main/miniapp/pro/ui) swapped, gated by a
  source test. int/str forms of an id map to the same tag.
- **`INIT_DATA_MAX_AGE` 86400 → 3600** + a guarded one-reload self-heal in
  `webapp/index.html` (`sessionStorage.initDataReload`), so a dashboard left open
  past the window refreshes its own signature instead of erroring.
- Tests: +9 → **182 passed** (key preference/rotation/re-key/plaintext honesty,
  envelope shape + per-chat key isolation + shredding + v1 upgrade + unopenable
  envelope, `/list` and `/forget` leak checks, mask stability, source gates,
  401-reload markup).

## Session 18 — command copy pass + a real /start bug found by it
- **Bug (behavioral, found while rendering /start):** the greeting throttle did
  `last = _start_last_response.get(chat_id, 0.0)` and then `now - last < 60`,
  which conflates "never greeted" with "greeted at clock zero". On any host whose
  `time.monotonic()` starts near zero (fresh boot — and this dev sandbox reports
  ~0.009s), **every first /start of a chat looked like a duplicate and was
  deleted as spam**: the bot greeted nobody. Fixed with an explicit
  `last is not None` check; `tests/test_tracker.py::TestStartGreeting` pins both
  halves (fresh chat is greeted with a 2.0s clock; a genuine 3s-later repeat is
  still throttled).
- **Copy pass across every command** (`main.py` only):
  - `/start`: schedule described as it now works (per-package pacing, delivery-day
    every 2 min, delivered never re-checked), privacy facts added (data-free pings,
    self-deleting chat messages, 14-day retention), roadmap corrected to
    "official carrier APIs **beyond** USPS" (USPS API v3 shipped), group block no
    longer promises "& update" (manual update is gone). Numbers come from
    `config` + `refresh.CADENCE` so copy can't drift from the schedule.
  - Icons: mute result 🔊→🔔 for "enabled" (🔔 is the on-icon everywhere else);
    `/cancel` success ❌→✅ ("Cancelled — nothing was removed"); 🗑 added to the
    remove/purge/clear confirmations alongside `/forget`'s 🧹.
  - Wording: `/clear` empty state → "📦 No packages to clear." (matches `/list`
    and `/forget`); `/help` cancel line "pending confirmation" → "pending action";
    `/help` legend "⚪ Pending" → "⚪ Pending/Unavailable"; `/help` header
    "Help &amp; Examples" → "Help &amp; examples"; `/privacy` item 3 no longer
    enumerates remove/purge/clear ("Confirmation prompts stay…"); blocked-package
    prefix points at the app, not "the card" (cards are ephemeral).
  - Commands menu: "Add packages" · "Mute or unmute a package" · "Remove a package
    without its number" · "Bot health & stats" · "Cancel a pending action".
- Verified: **184 passed**, py_compile + pyflakes clean, and `/start` (private +
  group), `/help`, `/privacy`, `/forget` rendered offline through the real
  handlers (tag balance, 4096-char limit, no tracking numbers).
- **`/start` v2 (cadence-driven pitch):** the message now leads with the
  product, adds a **PRIVACY BY DEFAULT** block (data-free pings, self-deleting
  cards, retention, `/forget`, encryption + masked logs + Telegram-signed app
  access), and splits the tiers with real numbers: FREE = in-transit 30 min ·
  delivery day 15 min · pre-scan 2 h; PRO = 15 packages · in-transit 3 min ·
  delivery day 2 min · pickup 15 min · pre-scan 30 min, plus the 🌍 International
  tab and quota view. **Every cadence number is computed from
  `refresh.CADENCE`** (pro/free seconds ÷ 60), so the pitch follows the table.
  Roadmap now lists "official carrier APIs beyond USPS", "17TRACK deregistration
  (pending API support)" and "per-chat retention controls" (group-buy tooling
  dropped).
  - Feature claims are conditional: with `EPHEMERAL_TTL=0` the self-delete line
    disappears, with `RETENTION_DAYS=0` it reads "Delivered packages stay until
    you delete them" — no promise the bot doesn't keep. Both branches verified
    by rendering with the values flipped.
  - Consistency fix found by that check: the block read `config.EPHEMERAL_TTL`
    (live) but bare `RETENTION_DAYS` (main's import-time copy, which a runtime
    rebind wouldn't reach). Both now read through `config.`.
  - **Tier blocks carry the privacy values** (same-day follow-up): FREE says
    "Privacy: data-free pings · cards gone after 90 s · auto-forget after 14 days"
    and PRO closes with "Privacy is never paywalled — <same three>". The phrase is
    assembled from live config (`privacy_bits` → `privacy_short`), so a disabled
    feature disappears from the pricing instead of being advertised: with both
    switches off the lines read "Privacy: data-free pings" / "…never paywalled —
    data-free pings" (verified by rendering with the values flipped). Layout also
    fixed: tiers are bullet lists now instead of one long wrapping line.
  - **The "why" is now in the copy**: `/start` gained "No update buttons, by
    design: one global cycle paces every lookup, so no chat can burn the shared
    carrier quota and checking a package never means retyping its number";
    `/help` gained an **UPDATES** section (cadence + same rationale); `/privacy`
    item 5 spells out why manual refresh was removed. Icon/spacing pass: 🔒 on the
    privacy header, 🚧 on the roadmap, 🆓 on FREE (pairing with 🌟 PRO), roadmap
    split over two lines for phone wrapping, and the double spaces in `/help`'s
    ADD examples collapsed.
- **Copy-truthfulness gate — `tests/test_copy_truth.py` (17 checks, +17 → 202
  passed).** Renders `/start` and `/help` through the real handlers and asserts
  each claim against the code that has to back it: the six advertised carriers
  actually auto-detect with links; "paced by each package / delivered never"
  matches `CADENCE` + `CADENCE_TERMINAL`; "no update buttons" (no command, no
  callback, no keyboard button, no menu entry); pings leak no identifier;
  "~90 s" == `EPHEMERAL_TTL` and those handlers use `send_ephemeral`; "14 days"
  == `RETENTION_DAYS` and purge targets only Delivered; `/forget` exists and is
  in the menu; encryption is really Fernet; logs really mask; initData HMAC
  really rejects a wrong-token signature; `/api/international` + `_is_international`
  + a real `/getquota` call; themes/haptics exist **and** are Pro-gated; tier
  numbers/cadences equal config + orderable; none of the privacy paths *call*
  `is_pro(`; roadmap items are still unshipped (no official API for
  ups/fedex/dhl/royalmail/canadapost, no deregister call, retention still one
  global int); and every ☰ menu command has a handler AND a `/help` line.
  - Two claims were **false/imprecise and got fixed**: "one global cycle paces
    every lookup" ignored the add-flow's immediate first check → now "(a new
    package gets one immediate first check)", pinned by the test; and `/help`
    was in the menu but never mentioned in its own text (house rule) → added the
    subtitle "All commands below — `/start` for the overview, `/help` any time".
  - Four failures were the gate's own bugs (not the product's): `hap` is an arrow
    function, the FREE line capitalises "In-transit", `is_pro_tier` is a cadence
    argument rather than a tier gate, and 7200 s is `// 3600` for the hours form.

## Session 19 — paid-payment post-mortem: invoices now survive a restart

A USDT payment arrived and the buyer got nothing: no Pro, no receipt. Three
separate holes, all in the payment path:

1. **The invoice pools were memory-only** (`_pending_payments`,
   `_expired_payments`, `_used_amounts`). A restart discarded every open
   invoice, so a buyer who paid the exact amount they were shown matched an
   empty pool — `_match_payment` returned None and the monitor logged
   "transfer without matching invoice". Two restarts that day (ETA fix, webview
   cache fix) put real money in that window. This is the root cause.
2. **The `first_scan` blanket skip** marked the last 15 signatures as seen
   *without looking at them*, so a payment landing just before a restart was
   invisible forever, restart or not.
3. **Silent drops.** `if not tx: continue` and `if not info: continue` left no
   trace, and the public RPC answers `getTransaction` with
   `429 Too many requests` (measured: 5 of 15 signatures unreadable in one scan)
   — so a rate-limited poll lost the payment with nothing in the log.

Fixes: `config.PENDING_PAYMENTS_FILE` + `pro.save_pending_payments()` /
`load_pending_payments()` (encrypted at rest, 0600, verified — chat IDs live in
it), `pro.register_invoice()` so the endpoint persists before the buyer can pay,
one write per monitor cycle via a dirty flag, the skip removed, `getTransaction`
failures retried 5 cycles before giving up (logged), and a log line when a tx
moves tokens but holds no accepted transfer. `scripts/payment_reconcile.py`
credits a payment that already fell through (`--scan` / dry-run / `--apply`,
refusing `--apply` while the bot runs because it would overwrite the credit on
its next save). Tests: restart round-trip for both pools, consumed-amount
protection across a restart, and a source gate for the skip — mutation-checked
(removing persistence or restoring the skip both fail the suite). 331 passed.

Operational note: `SOLANA_RPC_URL` still points at the public RPC, which 429s
under this poll rate. A free provider key (Helius/QuickNode) in `.env` is the
cheapest reliability win available here.

### Session 19b — the payment that DID arrive, and why it still failed

The buyer's signature proved the money moved: **0.020000 USDT to the bot wallet
at 21:09:38, on Solana, success** — while an invoice for **0.012590** was open
(created 21:08:07). So the three holes above were real but not the whole story:
the amount simply did not match, and the amount IS the identifier.

Cause: a wallet (or an exchange withdrawal form) that shows two decimals renders
every invoice in the 0.010001–0.019999 envelope as **0.01 or 0.02**, and a human
may just send "2 cents". Exact-only matching therefore took the money and gave
nothing back — the worst possible failure for a paid product.

Fix: `pro._match_by_timing()` — when nothing matches exactly, accept a transfer
whose amount sits inside the price band (base … the rounded-up envelope top,
0.020000) **only if exactly ONE invoice was open at the transfer's own
blockTime** (pending window + grace). Two buyers waiting at once → refused, and
logged as unattributable. The payer cannot choose the chat (they can only pay an
invoice that already exists), so the worst case is gifting Pro to the chat that
was already waiting for it. A repeat of an already-consumed amount is never
re-credited. The monitor judges "open" from `blockTime`, never from "now".

`scripts/payment_reconcile.py` gained `--chat-tag` (resolves a masked log tag to
the chat in memory, never printing the ID) and its credit guard is now "inside
the price band" rather than "exactly invoice-shaped", so a legitimate 0.02
overpay credits without `--force`.

The buyer was credited with that signature and the welcome message sent. 6 new
tests (rounding match, ambiguity refusal, out-of-band refusal, pre-invoice
transfer, consumed-amount repeat, monitor wiring), mutation-checked. 337 passed.

### Session 19c — the receipt tells the truth, and can be re-sent

Two support realities, both from the same incident:

1. **The receipt lied about how the payment was matched.** The activation message
   hard-coded "Matched by: the unique amount above — it identified you". For this
   buyer that was false twice over: the amount did not identify them (timing did),
   and an operator credited them by hand. A receipt is a document a customer
   keeps, so `pro_welcome_text()` now takes `matched_by` (`amount` / `timing` /
   `manual`) and states the real method; the monitor passes `timing` when the
   fallback matched, the reconcile script passes `manual`. The message body moved
   into that one helper so every path shares a single copy (the old inline f-string
   in `_activate_pro` is gone).
2. **"I paid but got no message" had no remedy.** `payment_reconcile.py --notify`
   re-sends the receipt to a chat without touching the subscription: it quotes the
   payment from the chain (`--sig`) or from `pro_receipts.log`, refuses when the
   chat has no active Pro, and is safe to run while the bot is up (it sends only —
   no `pro_users.json` write to race with). Verified live on the incident's chat.

5 new tests (three match-method wordings, the quoted payment details, the
labelled copy that still shows the expiry, the automatic path's default wording,
and a source gate that `--notify` never credits or persists), mutation-checked.
348 passed.

### Session 19d — receipts encrypted, backups automated, and a live-API test leak

Three findings from an "improve the existing setup" pass:

1. **`pro_receipts.log` was the last plaintext personal-data file** — raw chat
   IDs, one CSV line per activation. Receipts now live in encrypted
   `pro_receipts.json` (`config.PRO_RECEIPTS_FILE`) via
   `pro.load_receipts()/save_receipts()/append_receipt()`, with
   `_fold_legacy_receipts()` absorbing a legacy log **and deleting it** — it runs
   on every load, not once, because a not-yet-restarted bot could recreate the
   plaintext file. Read-modify-write on purpose: an append-only log cannot be
   encrypted as a whole document, and activations are rare. `--notify` reads
   through the same path.
2. **No automated backups.** `scripts/backup_state.sh` snapshots the state files
   *and* `.env` (without the key the backups are unreadable) into
   `~/Backups/packey/<timestamp>/`, verifies each file **by decrypting it with
   storage's own reader** on temp copies, prunes to `KEEP` (14), refuses a
   destination inside the project, and asks the file list from the code so a new
   state file cannot be missed. Daily at 03:30 via `com.packey.backup.plist`
   (installed, `launchctl` shows exit 0).
3. **A test was hitting the live 17TRACK API.** `TestRetention` ran the real
   purge path, which calls `carriers.stop_17track()` — so the suite made real
   network calls and leaked the httpx socket; `-W error` then blamed whichever
   test happened to run next (it surfaced on an unrelated privacy test and looked
   like a regression from this session's edits). `_setup()` now records the
   deregistration instead, and the test asserts it — the suite is hermetic and
   the assertion is stronger than before.

Tests: 4 receipt-trail tests (six decimals + encrypted at rest, legacy migration
and deletion, quoting from the migrated entries, missing file is fine) and 6
backup tests (syntax, coverage from the code, verification by decrypting, 0600/0700
permissions, refusal inside the project incl. a relative path, pruning) —
mutation-checked: a no-op verification and a dropped state file both fail the
suite. 351 passed.

### Session 20 — the browser tier learns UPS (phase 1 of the tier redesign)

Approved direction: browser + Ship24 serve UPS/DHL/FedEx/USPS for everyone;
17TRACK becomes a Pro-only source. Ordering matters — the browser must be able
to read the domestic carriers BEFORE 17TRACK is gated, or free users fall
straight into paid calls. Phase 1 (this session) gives the browser UPS support;
the 17TRACK gate, tier threading (`is_pro` through `fetch_track_status`) and the
`needs_pro` Mini App state are phase 2.

- `usps_browser.py` is no longer USPS-only: a `_CARRIER_PAGES` table holds
  warm-up URL, per-number URL and parser per carrier; `_check_batch_chunk`
  and the public API (`get_browser_statuses/status`) are slug-parameterized;
  `get_usps_statuses/status` remain as wrappers. USPS behavior is untouched
  (all 35 pre-existing browser tests pass unchanged).
- The UPS parser comes from a live captured page (probe `--save-fixtures`).
  It reads the CURRENT status from the line after "Tracking No. or Delivery
  Notice" — the progress rail lists every milestone, and a delivered page
  measured both 'delivered' and 'shipped', so page-wide word matching would
  have misread in-transit parcels. It fails closed (page-not-found, missing
  number, unknown status word → None → retry).
- Verified live through the production path: `carriers._check_browser(tid,
  "ups")` returned Delivered / "September 05 at 11:47 A.M. Delivered To … ·
  Shipped / Billed On 09/04/2026" — agreeing with Ship24's answer for the
  same number (milestone=delivered, newest event "DELIVERED" 2026-09-05).
- A hermeticity bug in the new tests was caught by the suite itself: passing a
  non-empty id list launched a REAL browser session (91 s test run). The
  routing test now records the slug `_check_batch_chunk` receives instead.
- Still open in later sessions: per-carrier sweep (UPS numbers in the due-driven
  browser sweep), the phase-2 tier flip, DHL/FedEx parsers.

### Session 21 — 17TRACK webhooks live (capture phase) + a key leak found and closed

The user registered `<MINI_APP_URL>/webhook/17track` in 17TRACK's console —
which made their server start POSTing events at our backend **before any route
existed** (three 404s at a ~15 s retry cadence, then their retry window closed).

1. **Key leak found and closed.** httpx logs full request URLs at INFO, and
   Helius carries the API key as `?api-key=` — so every 10 s poll wrote the key
   into the log (585 lines in ~1.6 h, mostly into the rotated tracker.log.1).
   `main.py` now has `_SecretRedactor`, a logging filter on both handlers that
   rewrites `api-key=<secret>` → `api-key=<redacted>` for ANY provider, and the
   leaked lines were redacted in place (`/tmp` script, counts only — the file
   is held open with O_APPEND so it must never be replaced, only rewritten).
   The user still needs to rotate the key in the Helius dashboard: purging the
   disk does not un-leak what already shipped.
2. **Capture-first webhook** (`miniapp.py`): `GET /webhook/17track` answers 200
   for console verification; `POST` answers 200 immediately and logs a masked
   STRUCTURAL fingerprint (`_payload_shape`: field names, types, sizes, list
   truncation — never values). The route is deliberately outside `/api/` (no
   initData exists for a carrier's server) and outside the rate limiter. The
   real handler (payload → number in store → force refresh) lands once the
   measured schema is known; polling remains the backstop.
3. Both fixes verified live through the funnel: GET 200, POST 200, and the log
   shows `17TRACK webhook capture: {'event': 'str(10)', 'data': {'num':
   'str(7)', 'status': 'str(10)'}}` — structure only.
7 tests (routes registered, verify 200, masked capture, malformed payload,
   shape never carries values incl. list truncation, redaction filter). 370
   passed.

Next session: read the captured shapes from the log, build the real handler
(mark due NOW + force refresh through the pipeline; only store-known numbers
are actionable — the anti-spoof posture while the URL carries no token).

### Session 22 — the webhook handler is live

The capture phase ended and the real handler shipped:

- `refresh.mark_due_now(tracking_id)` — pulls the next lookup to the next cycle
  tick (≤60 s) in every chat holding the number. Both consumers read the same
  `_due_at` map, so routing is automatic: loop-refreshed numbers go through the
  loop, browser-only numbers through the sweep, and the cadence is re-derived
  after the lookup (a Delivered push goes straight back to terminal).
- `miniapp._webhook_17track` now walks the ENTIRE payload for strings (the
  schema is unverified — field names cannot be trusted), intersects them with
  the tracked numbers (case/whitespace tolerant — 17TRACK normalizes case),
  schedules each match, and answers `{"ok": true, "refresh_scheduled": n}`.
  Anti-spoof: an unauthenticated POST can only accelerate numbers this bot
  already tracks — unknown numbers are counted and dropped. The masked shape
  capture continues at INFO for schema drift.
- Verified live end-to-end: a real tracked number POSTed through the funnel →
  `refresh_scheduled: 1` → handler log "1 matched a tracked number → due now".
- Tests: 4 new (known number due next tick, every chat holding it, unknown
  numbers never scheduled, malformed payload schedules nothing). 374 passed.

### Session 24 — phase 2: 17TRACK is a Pro perk

The tier flip. `_use_17track(slug, is_pro)` and
`fetch_track_status(tracking_id, allow_browser, is_pro)` now carry the chat's
tier; `is_pro=None` (legacy callers/tests) keeps the pre-gate behavior so the
existing suite stays meaningful.

- **Free chats**: 17TRACK is never spent — for any slug. Self-served carriers
  fall through to browser → Ship24 exactly as designed. 17TRACK-only carriers
  (international posts, Royal Mail, Canada Post, unknown formats) return
  source `"Pro"` with `"Unavailable"`: `_apply_lookup` preserves the last known
  status and sets `info["needs_pro"] = True`; Ship24 is skipped so a paid call
  can never substitute for the perk. `_PRO_GATED_SLUGS = _17TRACK_ONLY_SLUGS -
  {"ups", "fedex", "dhl"}` (those three have browser/Ship24 fallbacks).
- **Pro/owner chats**: unchanged 17TRACK-first routing, quota pressure and all.
- The background loop's free-chat UPS numbers are browser-owned
  (`_browser_only(tid, info, chat_id)` gained the chat's tier) and swept; Pro
  chats keep their numbers in the loop.
- `_register_and_fetch(tracking_id, is_pro)` skips 17TRACK registration for
  free chats entirely (`reg_result = "skipped"` — a free add never spends a
  quota slot) and the add-flow prefix explains the Pro requirement. The webhook
  handler was already tier-blind (it only schedules store-known numbers).
- The Mini App payload exposes `"needs_pro"` so the dashboard can badge why
  updates stopped; `/privacy` and the `/start` tier lines now describe the
  real source split (carriers' own sites + Ship24 for free, 17TRACK for Pro).

Tests: 8 new (Pro-first, free-never-17TRACK, browser-before-Ship24, free-intl
Pro state without Ship24, Pro-intl via 17TRACK, legacy is_pro=None, needs_pro
lifecycle in _apply_lookup) plus fake_register signature updates. 385 passed.

Still open: DHL/FedEx browser parsers; Mini App "Pro required" badge styling;
the ad/README "What you get" wording refresh for the new tiers.

### Session 25 — phase 1c: DHL joins the browser tier (form-flow engine)

The probe re-verified: **DHL GO-BROWSER-STATUS** — but through a FORM, not a
URL: dhl.com's SPA ignores `?tracking-id=` and only answers a number typed into
its search box. FedEx with a REAL delivered number rendered only a shell after
15 s (content loads late via XHR, 78 requests seen) — FedEx stays on Ship24
until a longer-wait capture proves a readable render.

- `_CARRIER_PAGES` entries now carry a `kind`: `"url"` (USPS/UPS — navigate per
  number) or `"form"` (DHL — open the page, dismiss any cookie-consent overlay
  (measured: DHL's OneTrust banner intercepted the click), fill the search
  input from a selector-candidate list, press Enter / click Track), and the
  batch engine branches on it. The form mechanics mirror the probe's proven
  flow (hydrate 2 s, Enter first, page's own Track button as fallback).
- `_parse_dhl_body` reads the results panel ("Tracking Results" → cut at
  "Track & Trace FAQs"), maps DHL GF status phrases ("Shipment Delivered" →
  Delivered, "Shipment in transit" → In Transit, …), fails closed on unknown
  phrases/missing number/absent panel, and builds the detail from "Last
  Update: … (UTC offset)", "Origin: …", "Destination: …".
- `_PRO_GATED_SLUGS` drops `dhl`: free chats' DHL numbers now ride the browser
  tier (17TRACK stays Pro-only). FedEx remains Pro-gated for free chats until
  its parser ships (free DHL/FedEx on unsupported slugs keep last-known status
  in the background — Ship24 is user-invoked only for free chats).
- Verified live through the production engine: `get_browser_status("dhl",
  <sample>)` → Delivered / "Last Update: Wednesday, January 14, 2026 at 1:01
  PM (UTC -05:00) · Origin: Bremen, Germany · Destination: Toronto, Canada".
- Tests: 8 new (delivered parse, in-transit variant, three fail-closed cases,
  unknown-phrase refusal, form-kind config, DHL de-gated). 393 passed.

Known suite-hygiene item (pre-existing, unchanged by this session): an
intermittent unraisable ResourceWarning from a leaked aiohttp transport makes
`-W error` full-suite runs flaky; the standard `pytest tests -q` is clean and
the flagged test passes in isolation.

Still open: FedEx capture with long waits; Mini App "Pro required" badge;
ad/README "What you get" wording refresh.



### Session 23 — phase 1b: failed UPS numbers join the browser sweep

The sweep is no longer USPS-only. Selection is now per-carrier:

- **USPS**: unchanged — browser-only numbers (no official API configured) are
  swept on their due schedule.
- **UPS**: numbers whose last lookup failed (`last_lookup_failed`, set when the
  whole chain returned Unavailable) join the sweep, so the free browser tier
  retries them on the ≥5-min gap instead of leaving them to repeated paid
  calls. A successful browser read clears the flag and hands the number back to
  the loop's 17TRACK cadence.

Mechanics: `_refresh_usps_batch` → `_refresh_browser_batch` (groups ids by
slug, one session per carrier — each carrier site has its own walls), and
`_sweep_usps_browser` → `_sweep_browser`. Healthy UPS numbers stay on their
17TRACK cadence and never touch the sweep. The background loop keeps processing
UPS numbers (17TRACK retry remains valuable); Ship24-in-loop behavior is
unchanged (the user reports their Ship24 account is not billing per call).

Tests: 2 new (failed-UPS selected, read clears the flag and pings;
healthy-UPS never swept) + 10 existing sweep tests renamed to
`_sweep_browser` with the slug-aware recorder. 376 passed.









## Extraction order (least-coupled first)

### Session 1 — config.py — DONE (see status block above)

### Session 2 — storage.py — DONE (see status block above)
- Originally planned as lines 127–187 + 446–602; actual ranges were
  63–121 (encryption) and 334–536 (cooldown + persistence + helpers).
- Note for Session 3: `save_cooldown()`/`load_cooldown()` in main are now
  wrappers that pass `main.REG_COOLDOWN_FILE`; carriers.py should call
  `storage` functions directly (or keep calling main's wrappers).

### Session 2 — storage.py (lines 127–187 + 446–602)
- Encryption helpers + load_data/save_data + cooldown save/load +
  confirmation store + time-formatting helpers (now_iso, format_ts,
  time_since, _trim_sentence).
- One coherent concern: everything that reads/writes disk.
- Highest-value extraction: it isolates the encryption code the rules file
  protects. Import cycle risk is low (storage should import nothing from main).

### Session 3 — carriers.py — DONE (see status block above)
- Moved ranges: main.py 191–201 (URL/UA), 207–273 (detection + http_client),
  279–746 (limiters + status helpers + backends + dispatcher), 756–760
  (_sanitize_tracking_id). Actual moved span wider than planned
  (plan said 603–1046) because detection + http_client + limiter moved too.
- Deviation note (rule: move verbatim, minimal diffs): `config.*` single-copy
  reads, `save_cooldown` from main, semaphore/`_http` qualified in two
  staying spots, CLI sync line — all documented above and in code comments.
- 17TRACK helpers, register/_check, USPS Web Tools, OnTrac direct,
  fetch_track_status dispatcher. Depends on: config, storage (cooldowns),
  usps_api.py, usps_browser.py — all importable, no handlers needed.
- Biggest independent chunk (~440 lines). If too big for one session,
  split: 3a = 17TRACK (603–825), 3b = USPS + dispatcher (826–1046).

### Session 4 — pro.py — DONE (see status block above)
- Moved range: main.py 1581–1899 (pro state through _pro_expiry_task).
  The three aiohttp invoice views + _build_miniapp_app stayed (Session 5).
- Line-count reconciliation: 2,171 − 325 cut + 17 import block = 1,863 ✓
  (pro.py 353 = 325 moved − ~7 banner/blank + 41 header/imports/setter).

### Session 5 — miniapp.py — DONE (see status block above)
- Moved ranges: main.py 1137–1411 + 1430–1515; pro re-import block (1413–1427)
  stayed; `import miniapp` + re-exports + `configure()` wiring at 1153–1169.
- Line-count reconciliation: 1,700 − 362 cut + 18 import block = 1,356 ✓
  (miniapp.py 426 = 362 moved + ~64 header/lazy-accessors/configure).

### Session 6 — ui.py — DONE out of order (see status block above)
- Moved ranges: main.py 140–151 (_format_duration), 152–193 (parsing/sorting
  helpers + banner), 214–334 (get_chat_id through FakeMessage).
- Line-count reconciliation: 1,863 − 177 cut + 14 import block = 1,700 ✓
  (ui.py 213 = 177 moved − banner/blank + ~46 header/imports/setter/logger).
- The stale Session 6 sketch below (which assumed hands-on-miniapp hooks)
  is superseded: miniapp.py now imports ui directly. Session 5 is next.

### Session 7a — refresh.py + botstate.py — DONE (see status block above)
- Moved ranges: main.py 174–365 (refresh plumbing), 251 (ALERT_STATUSES),
  547–575 (_register_and_fetch); START_TS / LAST_CYCLE_AT / _last_bg_check
  now owned by refresh.py (main keeps START_TS copy; /status reads
  refresh.START_TS / refresh.LAST_CYCLE_AT qualified).
- `botstate.py` (~20 lines): single owner of the live Bot handle; pro.py and
  ui.py lost their duplicate `bot`/`set_bot()` (all read `botstate.bot`);
  `main()` calls `botstate.set_bot(bot)` once.
- refresh owns the app's single save injection (`refresh.configure` +
  `save_now()`); `miniapp.configure()` is GONE — views call
  `_refresh()._refresh_one / ._register_and_fetch / .save_now()` (lazy,
  same cycle-safe pattern as the existing _pro()/_ui()/_carriers()).
- Line-count reconciliation: 1,356 − 222 cut + 8 import block = 1,142 ✓
  (refresh.py 270 = 222 moved − banners/blank + ~60 header/state/configure).
- Verified with three functional smokes (refresh shapes + save routing,
  miniapp end-to-end after losing configure(), /status live vs never).
- Session 8 follow-up (hotfix): removed the last `from main import ...`
  edge (`carriers.py:35` → injected `carriers.configure(save_cooldown=…)`
  wired by main.py). Running `python3 main.py` as `__main__` used to import
  main.py a second time under the name `main`, replaying into the
  half-loaded `carriers` module → circular ImportError. The import graph is
  now fully acyclic (grep-verified: no module imports main), and the check
  that would have caught it was missing: verify the launch with
  `python3 main.py --help` (argparse exits before any polling).

### Session 8 — Mini App attention UX + dead-alias cleanup — DONE
- Deleted dead aliases in main.py: the `START_TS` / `LAST_CYCLE_AT` copies
  and the `_last_bg_check` re-export (all live in refresh.py since 7a;
  `/status` reads refresh.* qualified — verified by grep that nothing else
  read the copies).
- webapp/index.html only, same card/button/chip style + status color legend:
  `register_blocked` cards now render an explanatory `.blocked-note`
  (payload field already existed); new ⚠ Attention filter chip for
  `Exception` / `Delivery Attempted` (grouped + sectioned first);
  filter chips carry live counts via `refreshChips()` (`data-label` holds
  the bare label so counts never stack).
- Verified: `py_compile` + `pytest tests/ -q` (82 passed, 1 skipped —
  node missing so the JS-rule subprocess is skipped); the pytest wrapper
  ALSO byte-checks the grouped JS rules against index.html, so filter
  logic can't drift silently. Full driver re-verified in browser afterwards.

## Rules of engagement (every session)
1. Fresh Cline task per extraction; read ONLY the target line range plus
   the imports block and any call sites found via grep.
2. Move code verbatim. No renames, no "improvements", no reformatting.
3. main.py gets `from storage import load_data, save_data, ...` style
   imports (or `import storage` + qualified calls — match whatever keeps
   the diff smallest).
4. Verify: py_compile both files, then `python3 -m pytest tests/ -q`
   (81 tests must pass, possibly more by then).
5. tests/ import from `main` — if tests break on imports, add the new
   module's names as re-exports in main.py rather than editing many tests.
6. Update this file's map line numbers after each extraction (they WILL
   shift).

### Session 26 — status visibility: /status and the Mini App tell the truth

User question of the day: "why is free using 17TRACK?" Audit answer: free
lookups never do (the gate at _use_17track fires first); the confusion came
from the owner-chat bypass, startup re-registration with no tier filter, and
no per-tier metrics anywhere. This session makes the tiers auditable:

- carriers: fetch_track_status is now a thin tier-tallying wrapper around
  _fetch_track_status_impl (public name kept — refresh.py and tests patch it).
  LOOKUP_TIER counts ok/fail per pro/free/unknown; LOOKUP_OK/FAIL unchanged.
- refresh: LAST_SWEEP (at/read/changed) written by _sweep_browser;
  next_due_in()/pending_due()/next_check_in(chat, tid) read _due_at.
- main: /status body extracted to build_status_text() (handler is a shim) —
  health (next-lookups ETA, sweep stats), lookups by source AND by tier,
  cached 17TRACK quota + pressure, Ship24 spend vs cap, package mix including
  "N paused (Pro)". Never mutates state, never spends a lookup.
- miniapp: /api/packages carries a `status` object (same counters) and per-
  package next_check_in; index.html renders a footer health line, per-card
  "next ~Xm" ETAs, and the previously-dead needs_pro flag as a "🌍 Updates
  paused — this carrier needs Pro" note (ui.py got the same note on the bot
  card). Polling is adaptive: 20 s for two ticks after a change, then 60 s,
  or wait out the bot's next scheduled check (capped at 5 min).
- pings deliberately unchanged: ui.PRIVACY_TEXT promises "No tracking number,
  name, status or carrier"; a new guard test (test_tracker.py::
  test_ping_stays_data_free_status_vocabulary) fails if anyone adds status
  vocabulary without rewriting that promise first.
- tests: tier counters, /status report content + no-numbers guarantee, ping
  vocabulary guard, payload status object + next_check_in, app/server wiring.
  401 passed. Bot restart pending (user runs it).

