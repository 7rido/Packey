from pathlib import Path

from usps_browser import _classify, _clean_detail, _result_zone, _result_from_body
import time

import usps_browser as up


class TestBatchUSPS:
    def test_batch_empty_when_throttled(self):
        up._throttled_until = {"usps": time.time() + 600}
        assert up.get_usps_statuses(["T1", "T2"]) == {}
        up._throttled_until = {}

    def test_batch_empty_without_ids(self):
        assert up.get_usps_statuses([]) == {}

    def test_batch_empty_no_profile(self):
        original = up.CHROME_PROFILE
        up.CHROME_PROFILE = "/nonexistent/profile"
        try:
            assert up.get_usps_statuses(["9400000000000000000000"]) == {}
        finally:
            up.CHROME_PROFILE = original

    def test_chunks_respect_batch_max(self):
        n = 80
        chunks = up._batch_chunks(list(range(n)))
        assert all(len(c) <= up.USPS_BATCH_MAX for c in chunks)
        assert sum(len(c) for c in chunks) == n
        assert len(chunks) == -(-n // up.USPS_BATCH_MAX)

    def test_ua_has_no_headless_brand(self):
        assert "HeadlessChrome" not in up.UA

    def test_stealth_scrubs_headless_brand(self):
        assert "HeadlessChrome" in up._STEALTH  # scrub pattern present
        assert "navigator.userAgent" in up._STEALTH or "userAgent" in up._STEALTH


class TestProfileModes:
    def test_slim_keeps_only_cookie_files(self):
        up.USPS_PROFILE_MODE = "slim"
        ignored = up._profile_ignore("x", ["Cookies", "Local State", "Cache", "History",
                                           "Extensions", "Web Data"])
        assert "Cookies" not in ignored
        assert "Local State" not in ignored
        assert "Cache" in ignored and "History" in ignored and "Web Data" in ignored
        up.USPS_PROFILE_MODE = "slim"

    def test_full_mode_preserves_legacy_ignore(self):
        up.USPS_PROFILE_MODE = "full"
        ignored = up._profile_ignore("x", ["Cookies", "Local State", "Cache", "Web Data"])
        assert "Cookies" not in ignored
        assert "Cache" in ignored and "Web Data" in ignored
        assert "Local State" not in ignored
        up.USPS_PROFILE_MODE = "slim"


class TestTerminalEvidence:
    """A Delivered claim may only come from the newest-event text or the rail's
    real event — never the zone-wide fallback: the milestone rail renders
    FUTURE chips, and 2026-09-29 one future "Delivered" chip on a page whose
    real chain was Label Created → In Possession → Departed froze a false
    Delivered forever (downgrade guard + terminal cadence)."""

    def test_future_chip_cannot_claim_terminal(self):
        zone = ("Departed Post Office CINCINNATI, OH 45202 "
                "September 28, 2:00 PM "
                "Delivered Out for Delivery Preparing for Delivery "
                "On the Way Accepted")
        assert _classify(zone, allow_terminal=False) != "Delivered"

    def test_zone_fallback_without_head_refuses_terminal(self):
        # The rail rendered but the real events hadn't (mid-load): bare chips,
        # no dates. Fail closed — anything but Delivered, never a terminal claim.
        zone = ("Delivered Out for Delivery Preparing for Delivery "
                "On the Way Accepted USPS in possession of item "
                "Shipping Label Created")
        status = _classify(zone, no_latest_update=True, allow_terminal=False)
        assert status != "Delivered"

    def test_head_delivered_still_classifies(self):
        # The genuine path: "Latest Update" present, head IS the event text —
        # the terminal claim stands.
        assert _classify("Delivered WASHINGTON, DC 20001 10:00 AM") == "Delivered"

    def test_prediction_banner_is_in_transit_never_delivered(self):
        # Measured 2026-09-29 (operator's own in-transit parcel): USPS's page
        # LEADS with "Delivered by the expected delivery date… currently in
        # transit to the next — Expected: <future date>". The banner word
        # Delivered is a delivery deadline, not an event; it must classify
        # In Transit in both classifier modes.
        banner = ("Delivered by the expected delivery date. As of September 29, "
                  "2026 at 3:49 am, it is currently in transit to the next "
                  "facility — Expected: Thursday 1 October 2026 by 9:00")
        assert _classify(banner) == "In Transit"
        assert _classify(banner, allow_terminal=False) == "In Transit"

    def test_genuine_delivered_with_past_timestamp_still_classifies(self):
        # A real delivery event carries a location and a past time and NO
        # prediction phrasing — the terminal claim must survive.
        assert _classify("Delivered, Front Desk TAMPA, FL 33502 "
                         "September 25, 11:32 am") == "Delivered"


class TestUspsBrowserZone:
    def test_zone_starts_after_latest_update(self):
        text = "Previous junk Latest Update Shipping Label Created zone here"
        assert _result_zone(text).startswith("Shipping Label Created")

    def test_zone_includes_status_row_after_promo(self):
        # Real widget layout: Latest Update → promo block → status row → explainer
        text = ("Latest Update A shipping label has been prepared for your item. "
                "Get More Out of USPS Tracking: USPS Tracking Plus® "
                "USPS Awaiting Item Shipping Label Created TUCSON, AZ 85706 "
                "What Do USPS Tracking Statuses Mean? footer")
        zone = _result_zone(text)
        assert "Shipping Label Created" in zone
        assert "Latest Update" not in zone
        assert "What Do USPS" not in zone
        assert _classify(zone) == "Info Received"

    def test_zone_fallback_anchors_on_status_heading(self):
        # Out-for-Delivery layout has no "Latest Update" marker — must anchor
        # on the status heading and skip the nav text.
        text = ("Skip to Main Content Current language English Inform all "
                "Out for Delivery OZONE PARK, NY 11417 Your item is out for "
                "delivery. What Do USPS Tracking Statuses Mean? footer")
        zone = _result_zone(text)
        assert zone.startswith("Out for Delivery OZONE PARK")
        assert "Skip to Main Content" not in zone
        assert _classify(zone) == "Out for Delivery"

    def test_zone_fails_closed_when_nothing_recognizable(self):
        # Regression for the "Skip to main content" bug: a half-loaded page
        # has "Tracking Number:" chrome (passing _result_from_body's gate)
        # but no "Latest Update" and none of the status anchors — the OLD
        # code fell back to start=0 (the whole page, i.e. the nav text) and
        # _classify then fabricated "Delivery Attempted" from "attempted"
        # appearing somewhere in page furniture. Must now return "" so the
        # caller retries instead of trusting unrendered chrome.
        text = ("Skip to Main Content Current language English Inform all "
                "Tracking Number: 9300111043900026403077 loading please wait "
                "footer links privacy terms")
        assert _result_zone(text) == ""

    def test_zone_anchors_on_prescan_wording(self):
        # A pre-scan/label-created layout without "Latest Update" must still
        # be recognized (previously only "usps awaiting item" and "shipping
        # label created" were in the fallback anchor list).
        text = ("Skip to Main Content Pre-Shipment Your label has been "
                "created but the item has not yet been scanned by USPS.")
        zone = _result_zone(text)
        assert "Pre-Shipment" in zone
        assert "Skip to Main Content" not in zone
        assert _classify(zone) == "Info Received"


class TestCleanDetail:
    def test_strips_promo_and_chips(self):
        raw = ("in transit to the destination. Get More Out of USPS Tracking: "
               "USPS Tracking Plus® Delivered Out for Delivery Preparing for Delivery")
        assert _clean_detail(raw) == "in transit to the destination."

    def test_collapses_whitespace(self):
        assert _clean_detail("a   b\n\n c  ") == "a b c"

    def test_empty(self):
        assert _clean_detail("") == ""

    def test_strips_leading_nav_chrome(self):
        # Regression: a half-loaded page's nav text ("Skip to Main Content")
        # must never survive into a "Latest update" line even if it somehow
        # reaches _clean_detail (belt-and-suspenders on top of the
        # _result_zone fail-closed fix below).
        raw = "Skip to Main Content Current language English Inform all In Transit to the destination."
        cleaned = _clean_detail(raw)
        assert "skip to main content" not in cleaned.lower()
        assert "current language" not in cleaned.lower()
        assert cleaned == "In Transit to the destination."


class TestUspsBrowserClassify:
    def test_label_created(self):
        zone = "A shipping label has been prepared for your item. " \
               "USPS Awaiting Item Shipping Label Created TUCSON, AZ 85706"
        assert _classify(zone) == "Info Received"

    def test_delivered(self):
        assert _classify("Delivered, Front Door PARCEL_ROCK") == "Delivered"

    def test_out_for_delivery(self):
        assert _classify("Your item is out for delivery") == "Out for Delivery"

    def test_in_transit(self):
        assert _classify("Arrived at USPS Regional Facility") == "In Transit"
        assert _classify("Your item is on the way to its destination") == "In Transit"

    def test_notice_left(self):
        assert _classify("Notice Left, No Authorized Recipient Available") == "Delivery Attempted"

    def test_prescan_not_yet_scanned(self):
        assert _classify("Your item has not yet been scanned by USPS") == "Info Received"

    def test_pre_shipment(self):
        assert _classify("USPS Awaiting Item Pre-Shipment TUCSON, AZ 85706") == "Info Received"

    def test_movement_wins_over_scanned_wording(self):
        # A parcel that has moved must never read as pre-scan just because
        # "processed" text also mentions a prior scan event.
        assert _classify("Processed through USPS facility after being scanned") == "In Transit"

    def test_unknown(self):
        assert _classify("Completely unrecognizable text") is None

class TestResultFromBody:
    def test_shell_only_page_returns_none_not_fabricated_status(self):
        """The exact failure that produced 'Delivery Attempted / Skip to main
        content' cards: a page whose shell rendered ("Tracking Number:")
        before the actual result did. Must return None so the batch loop's
        retry (fresh page.goto) fires instead of accepting nav text."""
        body = ("Skip to Main Content Current language English Inform all "
                "Tracking Number: 9300111043900026403077 loading please wait")
        assert _result_from_body(body) is None

    def test_prescan_layout_parses_once_rendered(self):
        body = ("Skip to Main Content Tracking Number: 9400... Pre-Shipment "
                "Your label has been created but the item has not yet been "
                "scanned by USPS. What Do USPS Tracking Statuses Mean? footer")
        result = _result_from_body(body)
        assert result is not None
        status, detail = result
        assert status == "Info Received"
        assert "skip to main content" not in detail.lower()


def test_progress_chips_do_not_beat_the_latest_update():
    """The widget chips list every stage (Delivered / Out for Delivery /
    Preparing for Delivery), so matching them made an in-transit package read
    as out-for-delivery. The newest-event text must win."""
    raw = ("in transit to the destination. Get More Out of USPS Tracking: "
           "USPS Tracking Plus Delivered Out for Delivery Preparing for Delivery")
    assert _classify(raw) == "In Transit"


class TestExpectedDeliveryBlock:
    """The 'Expected Delivery by MONDAY 21 September 2026 by 9:00pm' block is
    page-header furniture OUTSIDE the fail-closed result zone (measured on a
    live pre-scan label, 2026-09-17). It must be surfaced, but only when the
    block actually carries a date/time."""

    BODY = (
        "Tracking Number:\n\n"
        "9300111043900375946621\n"
        "Copy Add to Informed Delivery\n"
        "Expected Delivery by\nMONDAY\n21 \nSeptember\n2026 \t\nby\n9:00pm \n\n"
        "USPS electronic data indicates receipt of your package at our "
        "Self-Service Kiosk (SSK) in TAMPA, FL 33630 on September 16, 2026 "
        "at 10:49 pm.\n\n"
        "USPS Awaiting Item\n\n"
        "USPS electronic receipt of item for mailing\n\n"
        "TAMPA, FL 33630\n\nSeptember 16, 2026 10:49 PM\n\n"
        "Shipping Label Created\n\nTAMPA, FL 33602\n\nSeptember 14, 2026 5:54 PM"
    )

    def test_eta_from_the_product_header_is_surfaced(self):
        status, detail = _result_from_body(self.BODY)
        assert status == "Info Received"
        assert detail.endswith("Expected: Monday 21 September 2026 by 9:00pm")
        # The real event rows survive alongside the ETA.
        assert "Shipping Label Created" in detail

    def test_detail_never_exceeds_the_cap_even_with_eta(self):
        _, detail = _result_from_body(self.BODY)
        assert len(detail) <= 180
        assert detail.endswith("Expected: Monday 21 September 2026 by 9:00pm")

    def test_eta_without_a_date_or_time_fails_closed(self):
        body = self.BODY.replace(
            "MONDAY\n21 \nSeptember\n2026 \t\nby\n9:00pm",
            "SOON")
        _, detail = _result_from_body(body)
        assert "Expected:" not in detail

    def test_no_marker_means_no_suffix(self):
        body = self.BODY.replace(
            "Expected Delivery by\nMONDAY\n21 \nSeptember\n2026 \t\nby\n9:00pm \n\n",
            "")
        _, detail = _result_from_body(body)
        assert "Expected:" not in detail

    def test_colon_header_variant_is_clean(self):
        """The page HTML renders the header as 'Expected Delivery by:' (colon
        span) — the suffix must not inherit the 'by:' prefix."""
        body = self.BODY.replace(
            "Expected Delivery by\nMONDAY\n21 \nSeptember\n2026 \t\nby\n9:00pm",
            "Expected Delivery by:\nMonday\n21 \nSeptember\n2026\nby\n9:00pm")
        _, detail = _result_from_body(body)
        assert detail.endswith("Expected: Monday 21 September 2026 by 9:00pm")
        assert "by:" not in detail

    def test_tooltip_prose_never_rides_along(self):
        """The tooltip sentence is nested inside the same DOM block; if a text
        capture ever includes it, it must not land in the card detail."""
        body = self.BODY.replace(
            "by\n9:00pm \n\n",
            "by\n9:00pm \nExpected delivery on the date provided is the latest "
            "information on when the Postal Service\u2122 expects to deliver "
            "your package.\n\n")
        _, detail = _result_from_body(body)
        assert detail.endswith("Expected: Monday 21 September 2026 by 9:00pm")
        assert "latest information" not in detail


_UPS_FIXTURE = """Skip to Main Content
Find Closest UPS Location
Service Alerts
United States - English
Support
Shipping
Tracking
Log In
Tracking Details
arrow_circle_right
Tracking No. or Delivery Notice
Delivered check_circle
1Z1234567890123456
content_copy
Copy Tracking Number
check
close
Tracking number copied to clipboard.
Saturday, September 05
at
11:47 A.M.
Delivered To
SPRINGFIELD, IL US
View Package Locationchevron_right
Delivery Photo
Show Details keyboard_arrow_down
Proof of Deliverychevron_right
File a Claimchevron_right
lock
Stay Safe - Avoid Fraud and Scams
Received a text, call, or email that seems suspicious? Don't respond to it.
Shipment Details
Delivered To
SPRINGFIELD, IL US
Service
UPS Ground
Shipment Category
Package
Shipped / Billed On
09/04/2026
Support
Help and Support Center
Tracking Support
Where's My Package
Copyright 2026 United Parcel Service
"""


class TestUpsParser:
    """UPS /track?tracknum= parser — from a live captured page (2026-09-18,
    delivered parcel; number and city redacted).

    The rule under test: the status comes from the line right after
    "Tracking No. or Delivery Notice", NOT from scanning the page — the
    progress rail lists every milestone (a delivered page measured both
    'delivered' and 'shipped'), so page-wide word matching would misread
    in-transit parcels as delivered.
    """

    TID = "1Z1234567890123456"

    def _in_transit(self):
        # Same page, but the widget's CURRENT status line is In Transit: the
        # rail still advertises the delivered milestone further down.
        return _UPS_FIXTURE.replace("Delivered check_circle\n", "In Transit\n", 1) \
            .replace("Saturday, September 05\nat\n11:47 A.M.\nDelivered To\n"
                     "SPRINGFIELD, IL US",
                     "Expected Delivery: Tuesday, September 29")

    def test_delivered_parcel_parses(self):
        status, detail = up._parse_ups_body(_UPS_FIXTURE, self.TID)
        assert status == "Delivered"
        assert "September 05" in detail and "11:47 A.M." in detail
        assert "SPRINGFIELD, IL US" in detail
        assert "Shipped / Billed On 09/04/2026" in detail
        assert len(detail) <= 180
        for noise in ("content_copy", "Tracking number copied", "check_circle"):
            assert noise not in detail

    def test_in_transit_beats_the_rail_words(self):
        status, _ = up._parse_ups_body(self._in_transit(), self.TID)
        assert status == "In Transit"

    def test_fail_closed_on_page_not_found(self):
        assert up._parse_ups_body(
            "Page Not Found | UPS - United States\nnav text only", self.TID) is None

    def test_fail_closed_when_the_number_is_absent(self):
        """A render without our number could be a stale or unrelated page."""
        assert up._parse_ups_body(_UPS_FIXTURE, "1Z9999999999999999") is None

    def test_fail_closed_on_empty_body(self):
        assert up._parse_ups_body("", self.TID) is None

    def test_unknown_status_word_is_never_guessed(self):
        body = _UPS_FIXTURE.replace("Delivered check_circle",
                                    "Quantum Tunnelling\n", 1)
        assert up._parse_ups_body(body, self.TID) is None

    def test_browser_slugs_and_labels(self):
        assert "usps" in up.browser_slugs() and "ups" in up.browser_slugs()
        assert up.page_label("ups") == "UPS"
        assert up.page_label("usps") == "USPS"
        assert up.page_label("nope") == "NOPE"

    def test_generic_entrypoints_route_by_slug(self, monkeypatch):
        """No Chrome in tests: routing is proven by recording which slug the
        batch engine receives — never by launching a real session."""
        seen = []

        def _recorder(chunk, budget, slug="usps", headful=None):
            seen.append((slug, tuple(chunk)))
            return {}

        monkeypatch.setattr(up, "_check_batch_chunk", _recorder)
        assert up.get_browser_statuses("ups", ["A1"]) == {}
        assert up.get_browser_statuses("dhl", ["D1"]) == {}
        assert up.get_browser_statuses("usps", ["B2"]) == {}
        assert up.get_usps_statuses(["C3"]) == {}
        assert seen == [("ups", ("A1",)), ("dhl", ("D1",)),
                        ("usps", ("B2",)), ("usps", ("C3",))], seen
        # An unknown slug never reaches the engine at all.
        assert up.get_browser_statuses("nope", ["x"]) == {}
        assert up.get_browser_status("nope", "x") is None
        assert len(seen) == 4


_DHL_FIXTURE = """Navigation and Content
Skip to main content
You are in
United States of America
Track & Trace
Enter your tracking number(s)
Track
Tracking Code: 9 9 9 9 9 9 9 9 9 9 , Shipment Delivered ;
Tracking Results
Tracking Code: 9999999999
This shipment is handled by: DHL Global Forwarding
Customer Service
Print
Shipment Delivered
, Tracking Code: 9 9 9 9 9 9 9 9 9 9
Last Update: Wednesday, January 14, 2026 at 1:01 PM (UTC -05:00), CAHAM

Origin: Bremen, Germany

Destination: Springfield, IL US

Shipment Details
Service
Less than Container Load
Track & Trace FAQs
Still have questions? Contact us!
Footer
Quick Links
"""


class TestDhlParser:
    """DHL Track & Trace parser — from a live captured page (2026-09-18, the
    DHL Global Forwarding sample shipment, form-submitted). The SPA ignores
    URL parameters, so the engine fills the search box and submits; this parser
    reads the rendered results panel and fails closed on anything else."""

    TID = "9999999999"

    def test_delivered_sample_parses(self):
        status, detail = up._parse_dhl_body(_DHL_FIXTURE, self.TID)
        assert status == "Delivered"
        assert "Last Update: Wednesday, January 14, 2026 at 1:01 PM (UTC -05:00)" in detail
        assert "Origin: Bremen, Germany" in detail
        assert "Destination: Springfield, IL US" in detail
        assert len(detail) <= 180

    def test_in_transit_variant(self):
        body = _DHL_FIXTURE.replace("Shipment Delivered", "Shipment in transit")
        status, _detail = up._parse_dhl_body(body, self.TID)
        assert status == "In Transit"

    def test_fail_closed_without_the_results_panel(self):
        assert up._parse_dhl_body(
            "Enter your tracking number(s)\nTrack\n", self.TID) is None

    def test_fail_closed_when_the_number_is_absent(self):
        assert up._parse_dhl_body(_DHL_FIXTURE, "8888888888") is None

    def test_fail_closed_on_empty_body(self):
        assert up._parse_dhl_body("", self.TID) is None

    def test_unknown_status_phrase_is_never_guessed(self):
        body = _DHL_FIXTURE.replace("Shipment Delivered", "Shipment beamed aboard")
        assert up._parse_dhl_body(body, self.TID) is None

    def test_dhl_is_a_form_kind_carrier(self):
        cfg = up._CARRIER_PAGES["dhl"]
        assert cfg["kind"] == "form"
        assert "dhl.com" in cfg["warmup"]
        assert any("tracking-id" in s for s in cfg["inputs"])
        assert up.page_label("dhl") == "DHL"

    def test_dhl_is_no_longer_pro_gated(self):
        import carriers
        assert "dhl" not in carriers._PRO_GATED_SLUGS
        # Browser-capable BY CODE lives in the carrier table; whether it is in
        # the live slug tuple is the operator's choice via
        # USPS_BROWSER_DISABLED_SLUGS (2026-10-04: DHL's widget cannot render,
        # so this deployment disables it). The captured tuple must simply
        # agree with the live filter.
        assert "dhl" in up._CARRIER_PAGES
        assert carriers._browser_slugs == up.browser_slugs()


def _fedex_pkg(status="Delivered", code="DL",
               description="Delivered - Left at front door. Signed for by: J. SAMPLE",
               scans=None):
    """Shape ground-truthed on a live capture (2026-09-20): output.packages[0]
    with statusDetail + scanEvents. Values are the tester's placeholders —
    only the FIELD structure is copied from the real payload."""
    return {"transactionId": "t",
            "output": {"packages": [{"statusDetail": {
                "statusCode": code, "status": status,
                "description": description,
            }, "scanEvents": scans if scans is not None else [
                {"date": "2026-06-11T14:00:00+00:00", "eventCode": "PU",
                 "eventDescription": "Picked up",
                 "scanLocation": {"city": "MEMPHIS",
                                  "stateOrProvinceCode": "TN",
                                  "countryCode": "US"}},
                {"date": "2026-06-12T14:32:00+00:00", "eventCode": "DL",
                 "eventDescription": description,
                 "scanLocation": {"city": "SPRINGFIELD",
                                  "stateOrProvinceCode": "IL",
                                  "countryCode": "US"}},
            ], "serviceDetail": {"serviceName": "FedEx Ground"}}]}}


def _ups_pkg(status="Delivered", code="011",
             milestone=("Delivered", "cms.stapp.delivered"),
             number="1Z999AA10123456784", error=None, future_delivered=False):
    """UPS GetStatus shape as captured live 2026-09-24 (a delivered shipment),
    with the values swapped for the case under test. Synthetic number: a real
    one is user data and has no place in a fixture."""
    ms = [
        {"isCurrent": False, "isCompleted": True, "date": "09/21/2026",
         "time": "10:02 AM", "location": "LOUISVILLE, KY, US",
         "name": "Label Created", "nameKey": "cms.stapp.orderReceived"},
        {"isCurrent": True, "isCompleted": True, "date": "09/23/2026",
         "time": "11:14 AM", "location": "PHOENIX, AZ, US",
         "name": milestone[0], "nameKey": milestone[1]},
    ]
    if future_delivered:
        ms.append({"isCurrent": False, "isCompleted": False, "isFuture": True,
                   "date": "", "name": "Delivered",
                   "nameKey": "cms.stapp.delivered"})
    return {"statusCode": "200", "statusText": "Successful",
            "isLoggedInUser": False, "trackedDateTime": "09/24/2026 08:00:00",
            "trackDetails": [{
                "errorCode": error, "errorText": None,
                "requestedTrackingNumber": number, "trackingNumber": number,
                "packageStatus": status, "packageStatusCode": code,
                "packageStatusType": "D", "progressBarType": status,
                "receivedBy": "J SAMPLE",
                "milestones": ms,
                "shipToAddress": {"city": "PHOENIX", "state": "AZ",
                                  "country": "US"},
            }]}


class TestUpsJsonParser:
    """_parse_ups_json reads UPS's own guest API response (the opt-in spy,
    UPS_BROWSER_JSON). Shape ground-truthed on a live capture 2026-09-24; these
    tests pin the mapping, the number-echo guard and the fail-closed paths."""

    TID = "1Z999AA10123456784"

    def test_delivered_uses_the_current_milestone_and_drops_received_by(self):
        res = up._parse_ups_json(_ups_pkg(), self.TID)
        assert res is not None
        status, detail = res
        assert status == "Delivered"
        assert "Delivered" in detail and "PHOENIX, AZ, US" in detail
        assert "09/23/2026" in detail
        # receivedBy carries the recipient's signature — never card text.
        assert "J SAMPLE" not in detail

    def test_vocabulary_maps_from_name_key(self):
        for label, key, expected in (
                ("On the Way", "cms.stapp.inTransit", "In Transit"),
                ("Out for Delivery", "cms.stapp.outForDelivery", "Out for Delivery"),
                ("Label Created", "cms.stapp.orderReceived", "Info Received"),
                ("We have your Package", "cms.stapp.weHaveYourPkg", "In Transit"),
                ("Ready for Customer Pickup", "cms.stapp.deliveredToUAP",
                 "Available for Pickup")):
            res = up._parse_ups_json(
                _ups_pkg(status=label, milestone=(label, key)), self.TID)
            assert res is not None, label
            assert res[0] == expected, f"{label} → {res[0]}"

    def test_a_future_delivered_milestone_never_reads_as_delivered(self):
        """The milestone array carries future steps: an in-transit parcel whose
        next step is 'Delivered' must stay In Transit."""
        res = up._parse_ups_json(_ups_pkg(
            status="On the Way", code="003",
            milestone=("On the Way", "cms.stapp.inTransit"),
            future_delivered=True), self.TID)
        assert res is not None and res[0] == "In Transit"

    def test_a_payload_for_another_number_is_refused(self):
        """The JSON echoes the number, so a mismatch is detectable — reporting
        another shipment's status would be worse than reporting nothing."""
        res = up._parse_ups_json(_ups_pkg(number="1Z999AA10123456799"), self.TID)
        assert res is None

    def test_a_payload_that_does_not_identify_the_shipment_is_refused(self):
        """No echo at all is not proof either: only a payload naming THIS number
        may be trusted (a warmup page can fire the call for another shipment)."""
        assert up._parse_ups_json(_ups_pkg(number=""), self.TID) is None
        assert up._parse_ups_json(_ups_pkg(number=None), self.TID) is None

    def test_error_envelope_fails_closed(self):
        assert up._parse_ups_json(_ups_pkg(error="1500"), self.TID) is None
        assert up._parse_ups_json({}, self.TID) is None
        assert up._parse_ups_json({"trackDetails": []}, self.TID) is None

    def test_unknown_vocabulary_fails_closed_and_names_it(self, caplog):
        import logging as _logging
        with caplog.at_level(_logging.INFO, logger="tracker"):
            res = up._parse_ups_json(_ups_pkg(
                status="Something New", code="099",
                milestone=("Something New", "cms.stapp.somethingNew")), self.TID)
        assert res is None
        lines = "\n".join(r.getMessage() for r in caplog.records)
        assert "unmapped status vocabulary" in lines
        assert "'099'" in lines and "cms.stapp.somethingNew" in lines
        # Enum-only: the recipient-bearing fields stay out of the log.
        assert "J SAMPLE" not in lines and "PHOENIX" not in lines

    def test_the_spy_is_off_by_default_but_wired(self):
        """Shipping default: the rendered-page parse stays the primary path
        (UPS's API call arrives ~95-110s late), and the opt-in entry is present
        and matches only the carrier's own POST."""
        # The CODE default — the module loads .env at import, so the live
        # UPS_BROWSER_JSON reflects whoever runs the tests, not the default.
        import os
        saved = os.environ.pop("UPS_BROWSER_JSON", None)
        try:
            assert up._env_flag("UPS_BROWSER_JSON") is False
        finally:
            if saved is not None:
                os.environ["UPS_BROWSER_JSON"] = saved
        cfg = up._CARRIER_PAGES["ups"]
        assert cfg["kind"] == "url" and cfg["parse"] is up._parse_ups_body
        match = cfg["json_watch"]["api_match"]

        class _Req:
            def __init__(self, method):
                self.method = method

        class _Resp:
            def __init__(self, url, method="POST"):
                self.url = url
                self.request = _Req(method)

        api = "https://webapis.ups.com/track/api/Track/GetStatus?loc=en_US"
        assert match(_Resp(api))
        assert not match(_Resp(api, "GET"))
        assert not match(_Resp("https://www.ups.com/track/api/WemsData/GetLookupData"))
        # The window is a measured value, not a guess: the call fired in 6 of 6
        # windows at 9-12s on the engine's own path (2026-09-24), so 30s is 2.5x
        # headroom and a miss costs 30s rather than the 115s a cold reading had
        # suggested (which paid 116s for one number the carrier had no data for).
        assert cfg["json_watch"]["api_wait_ms"] == 30000


class TestSpyAsFallback:
    """prefer="page": read the page first, and open the spy window only when the
    page came up empty.

    This is the shape UPS ships (and the shape FedEx takes once its page anchors
    are ground-truthed). The property that matters: the fast path never pays for
    a window, so a spy that fires only sometimes cannot make latency
    unpredictable — it can only rescue a number whose page rendered nothing.
    """

    TID = "1Z999AA10123456784"

    def _install(self, monkeypatch, page, page_result):
        monkeypatch.setattr(up, "UPS_BROWSER_JSON", True)
        monkeypatch.setattr(up, "_PROFILE_COPY", "/tmp")
        monkeypatch.setattr(up, "_ensure_profile_copy", lambda copy_dir=None: "/tmp")
        monkeypatch.setattr(up, "_human_pause", lambda page, moves=8: None)
        monkeypatch.setattr(up.time, "sleep", lambda seconds: None)
        # The page poll has its own tests; here the ORDER is what is under test,
        # so its outcome is supplied directly.
        monkeypatch.setattr(
            up, "_poll_page", lambda page, cfg, tid, slug, deadline: page_result)
        _fake_playwright(monkeypatch, page)

    def test_the_fast_path_opens_no_window(self, monkeypatch):
        calls = {"expect": 0}

        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            def expect_response(self, pred, timeout=None):
                calls["expect"] += 1
                raise AssertionError("the page answered — no window expected")

        page = Page()
        self._install(monkeypatch, page, ("Delivered", "from the page"))
        res = up._check_batch_chunk([self.TID], 120, "ups")
        assert res == {self.TID: ("Delivered", "from the page")}
        assert calls["expect"] == 0, "the page path must not consult the spy"
        navs = [g for g in page.gotos if "tracknum" in g]
        assert len(navs) == 1, "exactly the page navigation"

    def test_a_page_that_answers_nothing_falls_back_to_the_window(self,
                                                                  monkeypatch):
        class Resp:
            @staticmethod
            def json():
                return {"trackDetails": [{
                    "trackingNumber": "1Z999AA10123456784",
                    "packageStatus": "Delivered", "packageStatusCode": "011",
                    "progressBarType": "Delivered",
                    "milestones": [{"isCurrent": True, "isCompleted": True,
                                    "name": "Delivered",
                                    "nameKey": "cms.stapp.delivered",
                                    "location": "PHOENIX, AZ, US",
                                    "date": "09/23/2026"}]}]}

        class CM:
            value = Resp()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            def expect_response(self, pred, timeout=None):
                return CM()

        page = Page()
        self._install(monkeypatch, page, None)      # the page never answers
        res = up._check_batch_chunk([self.TID], 120, "ups")
        # The spy answered, in the JSON parser's own detail shape.
        assert res[self.TID][0] == "Delivered"
        assert "PHOENIX, AZ, US" in res[self.TID][1]
        navs = [g for g in page.gotos if "tracknum" in g]
        assert len(navs) == 2, "page navigation + the fallback window's"

    def test_the_shipping_order_is_page_first(self):
        """Pin the shipped order and the no-data cost, so changing either is a
        deliberate edit — FedEx keeps the spy as its only signal for now."""
        cfg = up._CARRIER_PAGES["ups"]
        assert cfg["prefer"] == "page"
        assert cfg["empty_retries"] == 0
        assert up._CARRIER_PAGES["fedex"].get("prefer", "spy") == "spy"
        assert up._CARRIER_PAGES["fedex"].get("parse") is None


class TestFedexParser:
    """_parse_fedex_json reads the wtrk SPA's own guest API response — the
    rendered UI is unusable (it shows 'can't find' on live data), so the
    parser reads JSON and fails closed on anything it cannot map."""

    TID = "522093987024"

    def test_delivered_parses_with_newest_scan_and_signature_stripped(self):
        res = up._parse_fedex_json(_fedex_pkg(), self.TID)
        assert res is not None
        status, detail = res
        assert status == "Delivered"
        # The newest scan by ISO date, never trusting array order.
        assert "SPRINGFIELD, IL, US" in detail
        assert "2026-06-12 14:32" in detail
        # Recipient signatures never reach card text.
        assert "SAMPLE" not in detail
        assert "Signed for by" not in detail

    def test_spy_log_keeps_the_vocabulary_but_never_the_description(self, caplog):
        """The spy log is what confirms the parser against live payloads, but the
        statusDetail description can carry the recipient's signature ('Signed for
        by: J. SAMPLE') — so it keeps the code/status pair and the
        payload/parse-outcome signal, and drops the string."""
        import logging as _logging
        with caplog.at_level(_logging.INFO, logger="tracker"):
            res = up._parse_fedex_json(_fedex_pkg(), self.TID)
        assert res is not None
        lines = "\n".join(r.getMessage() for r in caplog.records)
        assert "payload shape" in lines and "parse outcome" in lines
        assert "'DL'" in lines and "'Delivered'" in lines
        assert "SAMPLE" not in lines and "Signed for by" not in lines
        assert "Left at front door" not in lines

    def test_in_transit(self):
        res = up._parse_fedex_json(_fedex_pkg(
            status="In transit", code="IT",
            description="In transit"), self.TID)
        assert res and res[0] == "In Transit"

    def test_out_for_delivery(self):
        res = up._parse_fedex_json(_fedex_pkg(
            status="On FedEx vehicle for delivery", code="OD",
            description="On FedEx vehicle for delivery"), self.TID)
        assert res and res[0] == "Out for Delivery"

    def test_exception(self):
        res = up._parse_fedex_json(_fedex_pkg(
            status="Delivery exception", code="DE",
            description="Delivery exception - Recipient not available"),
            self.TID)
        assert res and res[0] == "Exception"

    def test_info_received(self):
        res = up._parse_fedex_json(_fedex_pkg(
            status="Shipment information sent to FedEx", code="GI",
            description="Shipment information sent to FedEx"),
            self.TID)
        assert res and res[0] == "Info Received"

    def test_fail_closed_on_unknown_vocabulary(self):
        assert up._parse_fedex_json(_fedex_pkg(
            status="Beamed aboard", code="XX",
            description="Beamed aboard"), self.TID) is None

    def test_fail_closed_on_no_packages(self):
        assert up._parse_fedex_json(
            {"output": {"packages": []}}, self.TID) is None
        assert up._parse_fedex_json({}, self.TID) is None

    def test_fail_closed_on_missing_status_detail(self):
        body = {"output": {"packages": [{"scanEvents": []}]}}
        assert up._parse_fedex_json(body, self.TID) is None

    def test_no_scans_still_describes(self):
        res = up._parse_fedex_json(_fedex_pkg(scans=[]), self.TID)
        assert res and res[0] == "Delivered"
        assert res[1]

    def test_fedex_config_is_json_watch(self):
        cfg = up._CARRIER_PAGES["fedex"]
        assert cfg["kind"] == "json-watch"
        # /fedextrack/, not /wtrk/track/ (2026-10-22): see the cfg comment.
        assert "fedex.com/fedextrack/?trknbr=" in cfg["url"](self.TID)
        # Measured, not guessed (2026-09-24): the guest API call fired in 6.0-7.8s
        # in 6 of 6 runs, and again in every retry window — so the window is 3x
        # that and a miss costs 25s, where the old 55s reading cost more than
        # twice that and squeezed the per-session budget.
        assert cfg["api_wait_ms"] == 25000

        class _FakeResp:
            url = "https://api.fedex.com/track/v2/shipments"

            class request:
                method = "POST"
        assert cfg["api_match"](_FakeResp()) is True
        _FakeResp.request.method = "OPTIONS"
        assert cfg["api_match"](_FakeResp()) is False, \
            "a CORS preflight must never be parsed as a result"

    def test_fedex_is_no_longer_pro_gated(self):
        import carriers
        assert "fedex" not in carriers._PRO_GATED_SLUGS
        assert "fedex" in carriers._browser_slugs

    def test_fedex_is_sweep_owned_for_free_chats(self, monkeypatch):
        """Free chats: the browser is FedEx's primary tier, so the number
        belongs to the sweep — the background loop must never pay Ship24
        for it (the DHL lesson)."""
        import refresh
        monkeypatch.setattr(refresh.config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(refresh.pro, "is_pro", lambda chat_id: False)
        assert refresh._browser_only(self.TID, {}, chat_id=1) is True
    def test_fedex_left_17track_only_slugs(self):
        """Phase 3 routing: with a live browser tier, FedEx is no longer a
        17TRACK-only slug — under quota pressure it is shed (the browser
        covers it) exactly like UPS/DHL."""
        import carriers
        assert "fedex" not in carriers._17TRACK_ONLY_SLUGS

    def test_fedex_20_digit_numbers_are_detected(self):
        import carriers
        # 12 (Ground/Express), 15 (SmartPost-era) and 20 (GSN) digits.
        for tid in ("522093987024", "522093987024526", "52209398702452677099"):
            assert carriers.detect_carrier(tid)["code"] == "fedex", tid
        # Other lengths keep their existing classification.
        assert carriers.detect_carrier("9999999999")["code"] == "dhl"
        assert carriers.detect_carrier("1Z999AA10123456784")["code"] == "ups"


class TestPerCarrierThrottle:
    """The throttle is keyed per carrier: a FedEx edge refusal pauses FedEx
    only — USPS sweeps (the pre-scan lifeline) keep running."""

    def test_refusal_throttles_only_that_carrier(self, monkeypatch):
        monkeypatch.setattr(up, "_throttled_until", {})
        up._note_edge_refusal("fedex", 403)
        assert up.is_throttled("fedex") is True
        assert up.is_throttled("usps") is False

    def test_only_hard_refusals_count(self, monkeypatch):
        monkeypatch.setattr(up, "_throttled_until", {})
        up._note_edge_refusal("fedex", None)      # no response captured
        up._note_edge_refusal("fedex", 200)       # page loaded fine
        up._note_edge_refusal("fedex", 500)       # origin error, not the edge
        assert up.is_throttled("fedex") is False
        up._note_edge_refusal("fedex", 429)       # rate limit — pause it
        assert up.is_throttled("fedex") is True

    def test_no_arg_callers_mean_usps(self):
        saved = dict(up._throttled_until)
        try:
            up._throttled_until.clear()
            up._throttled_until["usps"] = time.time() + 600
            assert up.is_throttled() is True
            assert up.is_throttled("fedex") is False
        finally:
            up._throttled_until.clear()
            up._throttled_until.update(saved)

    def test_404_arming_matrix(self, monkeypatch):
        """Warmup-404 is the edge lying (that page exists unconditionally) —
        as hard as a 403. A per-number-URL 404 stays soft: ambiguous."""
        monkeypatch.setattr(up, "_throttled_until", {})
        up._note_edge_refusal("usps", 404, warmup=True)
        assert up.is_throttled("usps") is True
        monkeypatch.setattr(up, "_throttled_until", {})
        up._note_edge_refusal("usps", 404)
        assert up.is_throttled("usps") is False
        up._note_edge_refusal("usps", 403)
        assert up.is_throttled("usps") is True

    def test_warmup_404_isolation(self, monkeypatch):
        monkeypatch.setattr(up, "_throttled_until", {})
        up._note_edge_refusal("dhl", 404, warmup=True)
        assert up.is_throttled("dhl") is True
        assert up.is_throttled("usps") is False


def _fake_playwright(monkeypatch, page):
    """Drive _check_batch_chunk without a browser: one fake context holding one
    fake page. Shared by the budget tests and the spy-as-fallback tests."""
    import playwright.sync_api

    class FakeCtx:
        pages = [page]

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, *a, **kw):
            return FakeCtx()

    class FakePw:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: FakePw())


class TestJsonWatchBudget:
    """The sweep budget is a contract: a json-watch attempt only starts when
    a full API window (api_wait_ms) fits inside the remaining budget."""

    TID = "522093987024"

    @staticmethod
    def _fake_playwright(monkeypatch, page):
        _fake_playwright(monkeypatch, page)

    def _run_chunk(self, monkeypatch, page, budget, slug="fedex"):
        # These pin the GENERIC json-watch loop (budget floor, gates, retry
        # rules), which defaults to 3 attempts. FedEx's own cfg caps itself at
        # one attempt per fresh session — see TestFedexFreshSession.
        monkeypatch.setitem(up._CARRIER_PAGES[slug], "max_attempts", 3)
        monkeypatch.setattr(up, "_PROFILE_COPY", "/tmp")
        monkeypatch.setattr(up, "_ensure_profile_copy", lambda copy_dir=None: "/tmp")
        monkeypatch.setattr(up, "_human_pause", lambda page, moves=8: None)
        monkeypatch.setattr(up.time, "sleep", lambda seconds: None)
        self._fake_playwright(monkeypatch, page)
        return up._check_batch_chunk([self.TID], budget, slug)

    def test_small_budget_is_floored_to_a_full_window(self, monkeypatch):
        """A sub-window caller budget is meaningless for json-watch — the
        floor raises it to warmup + one window (70 s), so attempts run."""
        calls = {"expect": 0}

        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            class _NullCM:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                value = None

            def expect_response(self, pred, timeout=None):
                calls["expect"] += 1
                return self._NullCM()

        page = Page()
        res = self._run_chunk(monkeypatch, page, 5)
        assert res == {}
        per_number = [g for g in page.gotos if "trknbr" in g]
        assert len(per_number) == 3, "floored budget covers 3 windows"
        assert calls["expect"] == 3

    def test_gate_breaks_when_the_window_no_longer_fits(self, monkeypatch):
        """Floored budget 70 s + a fake clock advancing 60 s per window:
        attempt 2's gate (60 elapsed + 55 > 70) must break the loop."""
        real_mono = time.monotonic
        state = {"now": None}

        def fake_mono():
            if state["now"] is None:
                state["now"] = real_mono()
            return state["now"]

        class _WindowCM:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                state["now"] += 60.0   # the 55 s window elapsing
                return False

            value = None

        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            def expect_response(self, pred, timeout=None):
                return _WindowCM()

        page = Page()
        saved_mono = up.time.monotonic
        up.time.monotonic = fake_mono
        try:
            res = self._run_chunk(monkeypatch, page, 5)   # floored to 70
        finally:
            up.time.monotonic = saved_mono
        assert res == {}
        per_number = [g for g in page.gotos if "trknbr" in g]
        assert len(per_number) == 1, "attempt 2 must be gated out"

    def test_attempts_run_when_the_budget_fits(self, monkeypatch):
        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            class _NullCM:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                value = None

            def expect_response(self, pred, timeout=None):
                # Enter the window (so the in-with goto runs) but never
                # produce a response — the attempt fails on rinfo.value,
                # exactly like a real 55 s timeout.
                return self._NullCM()

        page = Page()
        res = self._run_chunk(monkeypatch, page, 130)
        assert res == {}
        # 130 s covers three 25 s windows on the fake clock (elapsed stays
        # ~0), so all three attempts navigate.
        per_number = [g for g in page.gotos if "trknbr" in g]
        assert len(per_number) == 3

    def test_a_fired_empty_payload_costs_one_retry_not_three(self, monkeypatch,
                                                             caplog):
        """A window that FIRED and carried no data is the carrier's answer for
        that number, so the attempt loop stops after ONE retry.

        Contrast with the test above: a missed window (no response) keeps its
        three attempts — only a fired-and-empty payload is terminal. One retry
        is deliberately still allowed, so a single transient empty payload can
        never cost the answer.
        """
        class Resp:
            @staticmethod
            def json():
                return {"output": {"packages": []}}      # fired, no shipment

        class CM:
            value = Resp()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Page:
            gotos = []

            def add_init_script(self, script):
                pass

            def goto(self, url, **kw):
                self.gotos.append(url)
                return None

            def expect_response(self, pred, timeout=None):
                return CM()

        page = Page()
        import logging as _logging
        with caplog.at_level(_logging.INFO, logger="tracker"):
            res = self._run_chunk(monkeypatch, page, 130)
        assert res == {}
        per_number = [g for g in page.gotos if "trknbr" in g]
        assert len(per_number) == 2, "one retry, then stop — not three windows"
        lines = "\n".join(r.getMessage() for r in caplog.records)
        assert "no data" in lines and "not retrying" in lines

# ── USPS pre-shipment progress rail (measured live 2026-09-28) ──────────────
# The operator's own label (9434650106151147493531, created 11:59 AM) rendered
# the progress rail — Delivered / Out for Delivery / Preparing for Delivery /
# On the Way / Accepted — with NO Latest Update block, and first-match over
# that rail classified it "Out for Delivery", which the bot and Mini App then
# showed. The only real event on such a page is the rail's last entry:
# Shipping Label Created = Info Received here.

PRESHIPMENT_BODY = (
    "USPS.com® — Tracking Results\n"
    "Tracking Number: 9434650106151147493531\n"
    "Expected delivery: Pending\n"
    "Delivered Out for Delivery Preparing for Delivery On the Way Accepted\n"
    "USPS in possession of item\n"
    "OZONE PARK, NY 11417\n"
    "September 28, 2026 11:59 AM\n"
    "Shipping Label Created\n"
    "NEW YORK, NY 11417\n"
    "What Do USPS Tracking Statuses Mean?"
)


def test_a_preshipment_rail_is_not_a_delivery_status():
    """Five milestone chips and no Latest Update block: the rail is widget
    furniture. First-match over it said 'Out for Delivery' — measured on the
    operator's own package. The page's real newest event is the acceptance
    (USPS in possession of item, with a location and date), so In Transit."""
    result = _result_from_body(PRESHIPMENT_BODY)
    assert result is not None, "a label-created page must still parse"
    status, detail = result
    assert status == "In Transit", (status, detail)
    assert "in possession of item" in detail.lower(), detail
    assert "OZONE PARK" in detail, "the acceptance event's location"
    assert "Out for Delivery" not in detail, "the rail must not leak into the detail"
    assert "Delivered" not in detail, "the rail must not leak into the detail"


def test_the_accepted_chip_description_alone_is_not_an_acceptance():
    """"USPS in possession of item" doubles as the Accepted chip's static
    description — without a date in that stretch it is furniture, and the page's
    real event is the label creation: Info Received."""
    body = (
        "USPS.com® — Tracking Results\n"
        "Tracking Number: 9434650106151147493531\n"
        "Expected delivery: Pending\n"
        "Delivered Out for Delivery Preparing for Delivery On the Way Accepted\n"
        "USPS in possession of item\n"
        "Shipping Label Created\n"
        "NEW YORK, NY 10002\n"
        "September 26, 2026 6:00 PM\n"
        "What Do USPS Tracking Statuses Mean?"
    )
    result = _result_from_body(body)
    assert result is not None
    status, detail = result
    assert status == "Info Received", (status, detail)
    assert "Shipping Label Created" in detail, "the label event is the detail"
    assert "Out for Delivery" not in detail


def test_the_batch_path_uses_the_warm_check():
    """Static pin: the batch path must consult the warm-copy check, not delete
    unconditionally — re-copying ~50 MB per batch dropped the warm cookies."""
    source = (Path(__file__).resolve().parent.parent / "usps_browser.py").read_text()
    assert ('if cfg.get("fresh_profile") or not _profile_copy_is_warm(copy_path):'
            in source)


def test_a_warm_profile_copy_survives_the_batch(tmp_path, monkeypatch):
    import os
    copy = tmp_path / "profile"
    copy.mkdir()
    (copy / "Cookies").write_bytes(b"x")
    os.utime(copy, (time.time() - 60,) * 2)          # 1 minute old: warm
    monkeypatch.setattr(up, "_PROFILE_COPY", str(copy))
    assert up._profile_copy_is_warm() is True


def test_a_stale_profile_copy_is_flagged_for_rebuild(tmp_path, monkeypatch):
    import os
    copy = tmp_path / "profile"
    copy.mkdir()
    os.utime(copy, (time.time() - up._PROFILE_MAX_AGE - 120,) * 2)
    monkeypatch.setattr(up, "_PROFILE_COPY", str(copy))
    assert up._profile_copy_is_warm() is False


def test_a_real_delivery_page_still_classifies_normally():
    """Positive control: the rail guard is gated on the page having no Latest
    Update block — a real event page must classify exactly as before."""
    body = (
        "USPS.com® — Tracking Results\n"
        "Tracking Number: 9400111206200074888350\n"
        "Latest Update\n"
        "Delivered, Front Desk, OZONE PARK, NY 11417\n"
        "September 26, 2026, 2:31 pm\n"
    )
    result = _result_from_body(body)
    assert result is not None
    status, detail = result
    assert status == "Delivered"
    assert "Front Desk" in detail


def test_a_rail_without_any_real_event_fails_closed():
    """Rail present, no label-created event: never guess a delivery state."""
    body = (
        "Tracking Number: 9434650106151147493531\n"
        "Delivered Out for Delivery Preparing for Delivery On the Way Accepted\n"
        "USPS expects the item\n"
    )
    assert _result_from_body(body) is None


class TestFedexFreshSession:
    """FedEx's tracking app only calls track/v2/shipments on the FIRST load in
    an unused profile (scripts/fedex_spy.py, 2026-10-22): any warm-up, retry
    or reused profile never fires it. Pin the cfg that encodes that."""

    def test_fedex_session_policy(self):
        cfg = up._CARRIER_PAGES["fedex"]
        assert cfg["skip_warmup"] is True
        assert cfg["fresh_profile"] is True
        assert cfg["batch_max"] == 1
        assert cfg["max_attempts"] == 1

    def test_other_carriers_keep_warmup_and_warm_profile(self):
        for slug, cfg in up._CARRIER_PAGES.items():
            if slug == "fedex":
                continue
            assert not cfg.get("skip_warmup"), slug
            assert not cfg.get("fresh_profile"), slug

    def test_batch_max_only_shrinks_batches(self):
        ids = [str(i) for i in range(5)]
        assert up._batch_chunks(ids, 1) == [[i] for i in ids]
        assert up._batch_chunks(ids) == [ids]
        many = [str(i) for i in range(up.USPS_BATCH_MAX + 10)]
        assert max(len(c) for c in up._batch_chunks(many, 10_000)) == up.USPS_BATCH_MAX


def _wtrk(**kw):
    base = {"notFound": False, "keyStatus": "", "keyStatusCD": "",
            "lastScanDateTime": "", "scanEventList": [], "errorList": []}
    base.update(kw)
    return base


class TestFedexWtrkParser:
    """The shape the fedextrack web app's own API returns (live, 2026-10-22).
    Values are the tester's placeholders."""

    TID = "522093987024"

    def _parse(self, *pkgs):
        return up._parse_fedex_json({"output": {"packages": list(pkgs)}}, self.TID)

    def test_not_found_is_no_data(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="tracker")
        res = self._parse(_wtrk(notFound=True, errorList=[
            {"code": "TRACKING.TRACKINGNUMBER.NOTFOUND"}]))
        assert res is None
        assert "not found at FedEx" in caplog.text

    def test_label_created(self):
        res = self._parse(_wtrk(keyStatus="Label created", keyStatusCD="OC",
                                scanEventList=[{"date": "2026-10-17", "time": "06:25:00",
                                                "status": "Shipment information sent to FedEx",
                                                "scanLocation": ""}]))
        assert res == ("Info Received",
                       "Shipment information sent to FedEx · 2026-10-17 06:25")

    def test_reused_number_picks_the_newest_shipment(self):
        old = _wtrk(keyStatusCD="IT", lastScanDateTime="2026-03-01T18:02:00-06:00",
                    scanEventList=[{"date": "2026-03-01", "time": "18:02:00",
                                    "status": "On the way", "scanLocation": "OLDTOWN, TN"}])
        new = _wtrk(keyStatusCD="IT", lastScanDateTime="2026-10-19T22:25:00-04:00",
                    scanEventList=[{"date": "2026-10-19", "time": "22:25:00",
                                    "status": "On the way", "scanLocation": "NEWTOWN, ON"}])
        res = self._parse(_wtrk(notFound=True), old, new)
        assert res[0] == "In Transit" and "NEWTOWN" in res[1]

    def test_delivered_drops_the_signature(self):
        res = self._parse(_wtrk(delivered=True, keyStatusCD="DL", receivedByNm="J.SAMPLE",
                                scanEventList=[{"date": "2026-04-14", "time": "10:57:00",
                                                "status": "Delivered",
                                                "scanDetails": "Signed for by: J.SAMPLE",
                                                "scanLocation": "SOMEWHERE, NJ"}]))
        assert res[0] == "Delivered"
        assert "J.SAMPLE" not in res[1] and "Signed for" not in res[1]

    def test_status_codes_map(self):
        for code, want in (("OD", "Out for Delivery"), ("DE", "Exception"),
                           ("HL", "Available for Pickup"), ("PU", "In Transit")):
            assert self._parse(_wtrk(keyStatusCD=code))[0] == want, code

    def test_unknown_code_fails_closed(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="tracker")
        assert self._parse(_wtrk(keyStatusCD="ZZ", keyStatus="Something new")) is None
        assert "unmapped wtrk vocabulary" in caplog.text

    def test_old_shape_still_parses(self):
        res = up._parse_fedex_json({"output": {"packages": [{"statusDetail": {
            "statusCode": "DL", "status": "Delivered"}, "scanEvents": []}]}}, self.TID)
        assert res and res[0] == "Delivered"
