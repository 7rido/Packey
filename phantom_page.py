"""The Phantom deep-link page: connect once, sign once, come back here each time.

Why a module of its own: this is a whole page (its styles, its two-tap state machine and
the fetch calls that drive it) rather than a snippet of another one, and the pay page's
template already showed what a 200-line Template does to the module around it.

Two things this page must never do:
  * navigate to a "solana:" URI — that scheme is claimed by every Solana wallet, so the
    operating system decides who answers, which is the misroute this whole route exists
    to avoid;
  * keep a secret on screen. The session token and the payer's key travel inside the
    encrypted payload, and the redirect carries only "step".

No "$" may appear outside a placeholder: this is a string.Template, and a stray dollar
sign anywhere in the JS raises at render time (learnt the hard way on the pay page).
"""
from string import Template
from typing import Mapping

_PAGE = Template("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Pay for Packey Pro</title>
<style>
  /* Same design language as the /pay page and the Mini App sheet: one accent, the
     system font stack, the same radii. Phantom's own purple is deliberately NOT
     used — this page is ours, and three surfaces in three brand colours is how a
     payment flow starts to look like three different products. */
  :root { color-scheme: dark;
    --bg:#06090f; --text:#f2f5f7; --muted:#8b98a5;
    --line:rgba(255,255,255,.08); --field:rgba(255,255,255,.06);
    --accent:#2f6bff; --accent-soft:#4f8cff;
    --font:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
    --r-sm:10px; --r-md:14px; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 var(--font); }
  .wrap { max-width:400px; margin:0 auto; padding:26px 20px 40px; }
  h1 { font-size:14px; color:var(--muted); font-weight:600; margin:0 0 6px; }
  .amt { font-size:38px; font-weight:700; letter-spacing:-0.5px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 18px; }
  .go { display:block; width:100%; text-align:center; padding:15px 0; border:0;
        border-radius:var(--r-md); background:var(--accent); color:#fff; font:inherit;
        font-weight:700; font-size:16px; cursor:pointer; }
  .go[disabled] { opacity:.6; }
  .addr { margin:14px 0; padding:12px; border-radius:var(--r-sm); background:var(--field);
          border:1px solid var(--line); font-family:var(--mono);
          font-size:11.5px; word-break:break-all; }
  .copy { width:100%; padding:12px 0; border-radius:var(--r-md); border:1px solid var(--line);
          background:transparent; color:var(--text); font:inherit; font-weight:600;
          cursor:pointer; margin-bottom:10px; }
  .status { margin-top:14px; color:var(--muted); font-size:13px; min-height:34px; }
  details.manual { margin-top:16px; }
  details.manual summary { cursor:pointer; color:#8b98a5; font-size:13px;
                           font-weight:600; padding:4px 0; }
  details.manual[open] summary { margin-bottom:8px; }
  details.manual .addr { margin-top:4px; }
  .diag { margin-top:12px; font-family:ui-monospace,Menlo,monospace; font-size:11.5px;
          color:#7d8b9a; line-height:1.5; word-break:break-all; }
  .hide { display:none; }
</style></head><body><div class="wrap">
  <h1>Pay for Packey Pro &mdash; through the Phantom app</h1>
  <p class="amt">$amount_str</p>
  <p class="sub">$subtitle</p>
  <button class="go" id="act" data-step="$step">$action</button>
  $who_block
  <div class="status" id="st">$note</div>
  <!-- The manual route must stay (every refusal promises it) but must not lead: most
       payers tap the Phantom button, so the address and its two copy buttons live one
       disclosure below, and an error path opens it for them. -->
  <details class="manual" id="manual">
    <summary>Or pay manually</summary>
    <p class="sub">Send exactly this amount to this address from any wallet.</p>
    <div class="addr" id="addr">$wallet</div>
    <button class="copy" id="copy">Copy address</button>
    <button class="copy" id="copyAmt">Copy amount</button>
  </details>
  <!-- Support aid, not customer copy: hidden until an error branch has something
       worth showing. -->
  <div class="diag hide" id="diag"></div>
</div>
<script>
(function () {
  var st = document.getElementById("st");
  var act = document.getElementById("act");
  var diag = document.getElementById("diag");
  var who = document.getElementById("who");
  var manualBox = document.getElementById("manual");
  var notes = [];
  function note(text) { notes.push(text); diag.textContent = notes.join(" \\u00b7 "); }
  // The trail is a support aid: it appears once there is an error to explain.
  function showDiag() { diag.classList.remove("hide"); }
  // Every refusal we print says the address below still works, so when one fires the
  // disclosure is opened rather than left for the payer to find.
  function openManual() { if (manualBox) manualBox.open = true; }
  // The connected wallet, truncated: a payer who connected an empty or wrong wallet
  // should see it here, not at the sign step.
  function connected(payer) {
    if (!who || !payer) return;
    who.textContent = "Connected: " + payer.slice(0, 4) + "\\u2026" + payer.slice(-4);
    who.classList.remove("hide");
  }
  // Two API failures arrive as bare codes ("too many requests", "unknown payment")
  // while every other refusal carries a sentence — a payer reading a status code
  // learns nothing, so both become one here.
  function friendly(data) {
    var code = String((data && data.error) || "");
    if (code === "too many requests")
      return "Too many tries \\u2014 wait a minute, then tap again.";
    if (code === "unknown payment")
      return "This link expired \\u2014 reopen Packey for a fresh one.";
    return (data && data.message) ? data.message : code;
  }

  function copyText(value, ok) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(function () { st.textContent = ok; },
                                                function () { st.textContent = ok; });
    } else { st.textContent = ok; }
  }
  document.getElementById("copy").onclick = function () {
    copyText(document.getElementById("addr").textContent, "Address copied.");
  };
  document.getElementById("copyAmt").onclick = function () {
    copyText(document.querySelector(".amt").textContent, "Amount copied.");
  };

  // The page's own path, without a trailing-slash regex: a literal dollar sign in this
  // template would be read as a placeholder.
  var base = location.pathname;
  if (base.charAt(base.length - 1) === "/") base = base.slice(0, -1);

  // Inside Telegram, openLink is the sanctioned breakout; in a mobile browser a real
  // navigation is the only thing a custom scheme answers to (a webview swallows it,
  // which is why the universal link is tried second rather than relied on).
  function openExt(url) {
    try {
      if (window.Telegram && Telegram.WebApp &&
          typeof Telegram.WebApp.openLink === "function") {
        Telegram.WebApp.openLink(url);
        return;
      }
    } catch (_) {}
    location.href = url;
  }
  function openPair(links) {
    openExt(links.scheme);
    setTimeout(function () {
      if (!document.hidden && links.universal) openExt(links.universal);
    }, 1500);
  }
  function post(path, body) {
    return fetch(base + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) { return res.json().catch(function () { return {}; }); });
  }

  var params = new URLSearchParams(location.search);
  var step = params.get("step") || "connect";
  var token = (params.get("token") || "USDC").toUpperCase();
  note("step " + step);

  if (params.get("errorCode")) {
    // Phantom refused, in its own words. Nothing is lost: the address and the exact
    // amount are on this page, and the amount is what identifies the payer.
    var why = params.get("errorMessage") || params.get("errorCode");
    note("refused " + params.get("errorCode"));
    showDiag();
    openManual();
    st.textContent = "Phantom refused (" + why + "). Try again, or send to the address "
                   + "below with this exact amount.";
    if (step === "sign") {
      act.textContent = "Pay " + document.querySelector(".amt").textContent
                      + " in Phantom again";
    }
  } else if (params.get("nonce") && params.get("data")) {
    // Phantom answered. Only the backend can read it: it holds the session's key.
    history.replaceState({}, "", base);
    note("answer");
    var isConnect = step !== "sign";
    // Whatever comes back, the button is busy until this page decides what to say: a
    // second tap mid-flight fired a second POST (the payer saw no feedback at all).
    act.disabled = true;
    st.textContent = "Finishing\\u2026";
    post(isConnect ? "/session" : "/submit", {
      phantom_encryption_public_key: params.get("phantom_encryption_public_key") || "",
      nonce: params.get("nonce"), data: params.get("data")
    }).then(function (data) {
      if (!data.ok) throw new Error(friendly(data));
      if (!isConnect) {
        submitted = true;                 // the durable guard must not be undone
        note("submitted");
        act.classList.add("hide");
        st.textContent = "\\u2705 Signed and submitted. Reopen Telegram \\u2014 Packey "
                       + "shows Pro as active within a minute.";
        return;
      }
      // Connected — and everything the sign request needs is already here, so this page
      // becomes the sign page instead of reloading into it: the intermediate tap was
      // ceremony. The page itself stays, because only a real navigation fires a custom
      // scheme reliably (openLink with "phantom://" is the untested mechanism that made
      // the sheet's Solana Pay button a dead end on iOS).
      connected(data.payer);
      act.dataset.step = "sign";
      act.textContent = "Pay " + document.querySelector(".amt").textContent
                      + " in Phantom";
      st.textContent = "Talking to Phantom\\u2026";
      note("auto sign");
      return post("/sign", { token: token }).then(function (links) {
        if (!links.scheme) throw new Error(friendly(links));
        openPair(links);
        // Some browsers refuse a gesture-less custom-scheme navigation. If this page is
        // still on screen after the first attempt, offer the tap the scheme needs — the
        // button is the fallback, never the path.
        setTimeout(function () {
          if (document.hidden || submitted || act.classList.contains("hide")) return;
          act.disabled = false;
          act.textContent = "Continue in Phantom";
          note("continue offered");
        }, 2500);
      });
    }).catch(function (e) {
      note("failed");
      showDiag();
      openManual();
      st.textContent = "Could not finish: " + e.message;
      act.disabled = false;
    });
  }

  // Coming back WITHOUT an answer is normal: the payer may close the confirmation
  // sheet and return here, in which case no errorCode and no nonce/data arrive. The
  // button was disabled for the in-flight moment only, so it must be live again the
  // instant this page is visible — a payer who returns to a dead button stops trusting
  // this screen (found in the UI audit, 2026-09-24). A submitted payment is different:
  // that button stays hidden and must not be re-enabled by a stray return.
  var submitted = false;
  function comeBack() {
    if (act.disabled && !document.hidden && !submitted) {
      act.disabled = false;
      st.textContent = "Nothing came back from Phantom \\u2014 tap to try again, or use "
                     + "the address below with this exact amount.";
      note("returned without an answer");
    }
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) comeBack();
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) comeBack();
  });

  act.onclick = function () {
    act.disabled = true;
    st.textContent = "Opening Phantom\\u2026";
    note("tap " + act.dataset.step);
    // The token rides along on BOTH calls: /connect bakes it into the redirect so the
    // return trip still knows which token the payer chose, and /sign needs it directly.
    post(act.dataset.step === "connect" ? "/connect" : "/sign", { token: token })
      .then(function (data) {
        // The API sends a real sentence alongside its code: a payer reading "unavailable"
        // learns nothing, and that was the reported failure on 2026-09-24. The two bare
        // codes it does send ("too many requests", "unknown payment") get one here.
        if (!data.scheme) {
          throw new Error(friendly(data) || "could not be prepared");
        }
        if (data.switched) {
          note("token " + data.token);
          st.textContent = "Paying in " + data.token + " \\u2014 that is the token this "
                         + "wallet holds.";
        } else {
          st.textContent = "Phantom should be opening. If nothing happened, tap again "
                         + "\\u2014 the address below always works. No Phantom yet? The "
                         + "link takes you to install it first.";
        }
        openPair(data);
      })
      .catch(function (e) {
        note("error");
        showDiag();
        openManual();
        st.textContent = e.message;
        act.disabled = false;
      });
  };
})();
</script></body></html>""")


def _short(payer: str) -> str:
    """first4…last4 — long enough to recognise a wallet, short enough for one line."""
    return f"{payer[:4]}\u2026{payer[-4:]}" if len(payer) > 8 else payer


def render(invoice: Mapping[str, object], wallet: str, connected: bool,
           payer: str = "", minutes: int = 30) -> str:
    """The page in whichever of its two tap states applies to this payer.

    `payer` is the wallet Phantom named in the connect answer (truncated for display;
    empty when not connected). `minutes` is the invoice window the payer must finish
    inside — the session TTL is the same 30 minutes.
    """
    amount_str = f"{float(invoice.get('amount') or 0):.6f}"
    return _PAGE.substitute(
        amount_str=amount_str,
        wallet=wallet,
        step="sign" if connected else "connect",
        action=(f"Pay {amount_str} in Phantom" if connected else "Connect Phantom"),
        # Rendered server-side when the page is loaded (or reloaded) already connected;
        # the auto-advance path fills the same element from the /session answer.
        who_block=(f'<div class="sub" id="who">Connected: {_short(payer)}</div>'
                   if connected and payer else '<div class="sub hide" id="who"></div>'),
        subtitle=("Phantom is connected: the next tap hands it this invoice's payment "
                  "to confirm." if connected else
                  "Two taps: connect Phantom, then approve the exact payment it shows "
                  "you."),
        note=(f"Phantom opens with the amount, the address and the memo already in the "
              f"transaction. Complete within {minutes} minutes — afterwards reopen "
              f"Packey for a fresh link." if connected else
              f"Phantom will ask to connect, then bring you straight back here. "
              f"Complete within {minutes} minutes — afterwards reopen Packey for a "
              f"fresh link."),
    )