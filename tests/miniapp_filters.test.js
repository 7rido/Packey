#!/usr/bin/env node
// Session 8 regression: Mini App dashboard filter/grouping logic.
//
// Mirrors the pure grouping rules in webapp/index.html (group() + filter
// wiring) so a future chip change is caught by pytest instead of eyeballs.
// Run: node tests/miniapp_filters.test.js  (also executed by pytest — see
// tests/test_miniapp_filters.py, which falls back to macOS JavaScriptCore via
// `osascript -l JavaScript` because node is not installed on this machine).
//
// The shim below keeps this file runnable by both engines: node's built-in
// assert when it exists, otherwise just the two comparisons used here.
const assert = (typeof require === 'function')
  ? require('node:assert')
  : {
      strictEqual(a, b) {
        if (a !== b) throw new Error('strictEqual failed: ' + a + ' !== ' + b);
      },
      deepStrictEqual(a, b) {
        const sa = JSON.stringify(a), sb = JSON.stringify(b);
        if (sa !== sb) throw new Error('deepStrictEqual failed: ' + sa + ' !== ' + sb);
      },
    };

// ── copied verbatim from webapp/index.html ──
function group(p) {
  if (p.status === 'Delivered') return 'delivered';
  if (p.status === 'Exception' || p.status === 'Delivery Attempted') return 'attention';
  return 'transit';
}

function needsAttention(p) {
  return group(p) === 'attention';
}
// ── end verbatim copy ──

// ── international lane (keep byte-identical to webapp/index.html) ──
const INTL_SLUGS = ['chinapost', 'hkpost', 'intl'];
const INTL_COUNTRIES = ['CN', 'HK'];
function isIntl(p) {
  if (INTL_SLUGS.includes(p.carrier)) return true;
  return INTL_COUNTRIES.includes((p.carrier_country || '').toUpperCase());
}
// ── end international copy ──

// 🌍 International: national posts only 17TRACK can check, and origin-country
// verification (a number our regex called 'unknown' but 17TRACK found in CN).
assert.strictEqual(isIntl({ carrier: 'chinapost' }), true);
assert.strictEqual(isIntl({ carrier: 'hkpost' }), true);
assert.strictEqual(isIntl({ carrier: 'intl' }), true);
assert.strictEqual(isIntl({ carrier: 'unknown', carrier_country: 'CN' }), true);
assert.strictEqual(isIntl({ carrier: 'unknown', carrier_country: 'hk' }), true);
assert.strictEqual(isIntl({ carrier: 'usps', carrier_country: 'US' }), false);
assert.strictEqual(isIntl({ carrier: 'ups', carrier_country: 'US' }), false);
assert.strictEqual(isIntl({ carrier: 'unknown' }), false);

const pkgs = [
  { tracking_id: 'A', status: 'Delivered' },
  { tracking_id: 'B', status: 'In Transit' },
  { tracking_id: 'C', status: 'Exception', direction: 'outgoing' },
  { tracking_id: 'D', status: 'Delivery Attempted' },
  { tracking_id: 'E', status: 'Pending', register_blocked: true },
  { tracking_id: 'F', status: 'Info Received' },
];

const counts = { attention: 0, transit: 0, delivered: 0 };
pkgs.forEach(p => counts[group(p)]++);
assert.deepStrictEqual(counts, { attention: 2, transit: 3, delivered: 1 });

const visible = (filter) => pkgs.filter(p => filter === 'all' ? true
  // Outgoing is a direction, not a status group: it reads the payload field
  // (the chip itself is Pro-only, but the rule is the same for everyone).
  : filter === 'outgoing' ? p.direction === 'outgoing'
  : group(p) === filter);
assert.strictEqual(visible('all').length, 6);
assert.deepStrictEqual(visible('attention').map(p => p.tracking_id), ['C', 'D']);
assert.deepStrictEqual(visible('transit').map(p => p.tracking_id), ['B', 'E', 'F']);
assert.deepStrictEqual(visible('delivered').map(p => p.tracking_id), ['A']);
assert.deepStrictEqual(visible('outgoing').map(p => p.tracking_id), ['C']);
assert.strictEqual(visible('outgoing').length, pkgs.filter(p => p.direction === 'outgoing').length);
// An outgoing Delivered parcel is labelled differently, and only for outgoing.
const label = (p) => (p.status === 'Delivered' && p.direction === 'outgoing')
  ? 'Delivered — arrived at its destination' : p.status;
assert.strictEqual(label({ status: 'Delivered', direction: 'outgoing' }),
                   'Delivered — arrived at its destination');
assert.strictEqual(label({ status: 'Delivered', direction: 'incoming' }), 'Delivered');
assert.strictEqual(label({ status: 'Delivered' }), 'Delivered');   // older records

assert.strictEqual(needsAttention({ status: 'Exception' }), true);
assert.strictEqual(needsAttention({ status: 'Pending' }), false);

// Blocked (pre-first-scan) numbers render the explanatory note, not bare Pending.
const blocked = pkgs.find(p => p.register_blocked);
assert.strictEqual(blocked.status, 'Pending');
assert.strictEqual(group(blocked), 'transit');

console.log('miniapp filter rules ok:', JSON.stringify(counts));
