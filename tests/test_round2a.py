"""Round 2A: one ticket = one charge for a look (v3 RPC, v2 fallback), every finished try-on
kept for the shop for 24 hours (store, GET /tryon/recent, GET /tryon/result/{id}), the
same-ticket rules on a try-on POST, the 24-hour clean-up and the refund sweep change.
Mocked: Supabase and storage (in memory), Gemini. No network, no real database, no AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import base64
import contextlib
import io
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import attempt_log
import generations_api
import tryon_recent
import worker_housekeeping
from tests.test_tryon_credits_and_results import (
    CUSTOMER_PHOTO,
    FABRIC,
    GARMENT,
    HERO,
    LOOK,
    SHOP,
    ApiTestCase,
    image_size,
)

OTHER_SHOP = "22222222-2222-4222-8222-222222222222"
V3 = "create_generation_with_credit_debit_v3"
V2 = "create_generation_with_credit_debit_v2"


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def capture():
    out = io.StringIO()
    return out, contextlib.redirect_stdout(out)


def fake_sign(bucket, paths, expires_in=3600, **_kwargs):
    assert bucket == tryon_recent.BUCKET
    assert expires_in == 15 * 60
    return {path: f"https://signed.test/{path}" for path in paths}


class Round2ATestCase(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.tables.setdefault("generation_attempts", [])
        tryon_recent._warned.clear()
        sign = mock.patch.object(tryon_recent.image_thumbs, "sign_existing_paths", side_effect=fake_sign)
        sign.start()
        self.addCleanup(sign.stop)

    def rpc_calls(self, name: str) -> list[dict]:
        return [entry[2] for entry in self.db.log if entry[:2] == ("rpc", name)]

    def writes(self, table: str, op: str) -> list:
        return [entry for entry in self.db.log if entry[:2] == (table, op)]

    def recent_files(self) -> dict:
        return {path: data for (bucket, path), data in self.db.files.items() if bucket == tryon_recent.BUCKET}

    def earlier_attempt(self, ticket: str, *, shop: str = SHOP, status: str = "succeeded", minutes: float = 1,
                        stored: bool = False, data: bytes = b"stored-result", **extra) -> dict:
        aid = str(uuid.uuid4())
        row = {
            "id": aid, "request_id": ticket, "client_request_id": ticket, "attempt_type": "tryon",
            "shop_id": shop, "status": status, "started_at": ago(minutes=minutes),
            "created_at": ago(minutes=minutes), "credits_charged": 50, **extra,
        }
        if stored:
            saved = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            path = tryon_recent.result_path(aid, "image/png", saved)
            row.update(shop_result_path=path, shop_result_saved_at=saved.isoformat())
            self.db.files[(tryon_recent.BUCKET, path)] = data
            self.db.files[(tryon_recent.BUCKET, tryon_recent.thumb_path(path))] = b"thumb"
        self.db.tables["generation_attempts"].append(row)
        return row

    def new_attempts(self, *known: dict) -> list[dict]:
        ids = {row["id"] for row in known}
        return [row for row in self.db.attempts() if row["id"] not in ids]


# ---------------------------------------------------------------- PART 2: looks

class LookTicketTests(Round2ATestCase):
    def setUp(self) -> None:
        super().setUp()
        self.generation_id = str(uuid.uuid4())

    def v3_answers(self, *, duplicate=False):
        def handler(params):
            if duplicate:
                return [{"generation_id": self.generation_id, "status": "processing", "credits_used": 50,
                         "balance_before": None, "balance_after": None, "duplicate": True}]
            return [{"generation_id": self.generation_id, "status": "queued", "credits_used": 50,
                     "balance_before": 150, "balance_after": 100, "duplicate": False}]

        self.db.rpc_handlers[V3] = handler

    def post(self, headers=None, body=None):
        return self.client.post(
            "/generations",
            json=body or {"hero_image_id": HERO, "fabric_image_id": FABRIC, "barcode": " B-1 ", "mrp": 999.5},
            headers=headers or {},
        )

    def test_v3_saves_everything_in_one_step(self):
        self.v3_answers()
        ticket = str(uuid.uuid4())
        response = self.post({"X-Client-Request-Id": ticket})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key, value in {"id": self.generation_id, "status": "queued", "credits_used": 50,
                           "balance_before": 150, "balance_after": 100, "prompt_saved": True}.items():
            self.assertEqual(body[key], value)
        self.assertNotIn("duplicate", body)
        self.assertNotIn("warning", body)

        [params] = self.rpc_calls(V3)
        self.assertEqual(params["p_client_request_id"], ticket)
        self.assertEqual(params["p_prompt_used"], "Dress the model in a IndoWestern.")
        self.assertTrue(params["p_model_used"])
        self.assertEqual((params["p_barcode"], params["p_mrp"], params["p_description"]), ("B-1", 999.5, None))
        self.assertIsNone(params["p_default_fabric_slots"])
        self.assertEqual(params["p_fabrics"][0]["fabric_image_id"], FABRIC)
        self.assertEqual(params["p_credits_cost"], 50)
        # v3 wrote it all: no v2, no follow-up update, no fabric metadata update.
        self.assertEqual(self.rpc_calls(V2), [])
        self.assertEqual(self.writes("generations", "update"), [])
        self.assertEqual(self.writes("generation_fabrics", "update"), [])

        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["generation_id"], row["credits_charged"]), ("running", self.generation_id, 50))

    def test_a_header_that_is_not_a_uuid_is_no_ticket(self):
        self.v3_answers()
        self.assertEqual(self.post({"X-Client-Request-Id": "not-a-uuid"}).status_code, 200)
        self.assertIsNone(self.rpc_calls(V3)[0]["p_client_request_id"])

    def test_no_header_is_no_ticket(self):
        self.v3_answers()
        self.assertEqual(self.post().status_code, 200)
        self.assertIsNone(self.rpc_calls(V3)[0]["p_client_request_id"])

    def test_duplicate_returns_the_existing_look_and_charges_nothing(self):
        self.v3_answers(duplicate=True)
        ticket = str(uuid.uuid4())
        response = self.post({"X-Client-Request-Id": ticket})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["id"], body["status"], body["duplicate"]), (self.generation_id, "processing", True))
        self.assertEqual(body["credits_used"], 0)
        for key in ("balance_before", "balance_after", "prompt_saved"):
            self.assertIn(key, body)  # every field of today's response is still there

        self.assertEqual(len(self.rpc_calls(V3)), 1)
        self.assertEqual(self.rpc_calls(V2), [])  # never a second charge
        self.assertEqual(self.writes("generations", "update"), [])
        self.assertEqual(self.db.tables.get("credit_ledger", [])[1:], [])

        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["credits_charged"]), ("succeeded", 0))
        self.assertIn("Repeated an earlier request", row["user_message"])
        self.assertIsNone(row.get("generation_id"))  # the worker keeps finishing the original row

    def test_without_v3_today_v2_path_runs_exactly_as_before(self):
        self.db.rpc_handlers[V2] = lambda params: [
            {"generation_id": self.generation_id, "status": "queued", "credits_used": 50,
             "balance_before": 150, "balance_after": 100}
        ]
        out, redirect = capture()
        with redirect:
            response = self.post({"X-Client-Request-Id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("duplicate", response.json())
        self.assertEqual(len(self.rpc_calls(V3)), 1)
        [params] = self.rpc_calls(V2)
        self.assertEqual(set(params), {"p_shop_id", "p_hero_image_id", "p_fabrics", "p_credits_cost"})
        [update] = self.writes("generations", "update")
        self.assertEqual(update[2]["prompt_used"], "Dress the model in a IndoWestern.")
        self.assertEqual(update[2]["barcode"], "B-1")
        self.assertEqual(len(self.writes("generation_fabrics", "update")), 1)
        self.assertEqual(self.db.only_attempt()["status"], "running")

    def test_same_error_answers_from_v3(self):
        cases = [
            ("P0001: insufficient credits (balance=10 required=50)", 402),
            ("P0001: hero image not found for this shop", 404),
            ("P0001: one or more fabric images not found for this shop", 404),
            ("P0001: Duplicate apply_to values are not allowed", 400),
            ("P0001: p_fabrics count must be between 1 and 6", 400),
            ("something else broke", 500),
        ]
        for message, code in cases:
            def fail(_params, message=message):
                raise fakes.FakeDBError(message)

            self.db.rpc_handlers[V3] = fail
            self.db.rpc_handlers[V2] = lambda params: self.fail("v2 must not run after a v3 error")
            self.assertEqual(self.post().status_code, code, message)
        self.assertEqual(self.rpc_calls(V2), [])


# ---------------------------------------------------------------- PART 3: keep each try-on for the shop

class StoreResultTests(Round2ATestCase):
    def test_every_try_on_route_keeps_the_result_and_a_small_copy(self):
        requests = [
            lambda: self.client.post("/tryon/", json={"generation_id": LOOK, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}),
            lambda: self.client.post("/tryon/quick", json={"fabric_image_id": FABRIC, "folder_id": GARMENT,
                                                           "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}),
            lambda: self.client.post("/tryon/v2", data={"generation_id": LOOK, "consent_confirmed": "true"},
                                     files={"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")}),
            self.quick_v2,
            lambda: self.client.post(
                "/tryon/multi/v2",
                data={"hero_image_id": HERO, "folder_id": GARMENT, "consent_confirmed": "true",
                      "fabric_image_id_1": FABRIC, "apply_to_1": "suit_full_body"},
                files={"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")},
            ),
        ]
        for index, send in enumerate(requests, start=1):
            with capture()[1]:
                self.assertEqual(send().status_code, 200, index)
            self.assertEqual(len(self.recent_files()), index * 2, index)
        for row in self.db.attempts():
            path = row["shop_result_path"]
            self.assertTrue(tryon_recent.is_result_path(path))
            self.assertTrue(path.endswith(f"/{row['id']}.png"))
            self.assertIsNotNone(row["shop_result_saved_at"])
            self.assertEqual(self.recent_files()[path], self.result_png)  # as it is
            thumb = self.recent_files()[tryon_recent.thumb_path(path)]
            self.assertTrue(thumb.startswith(b"\xff\xd8"))
            self.assertEqual(image_size(thumb), (400, 200))
        self.assertNotIn(CUSTOMER_PHOTO, self.db.files.values())

    def test_path_shape(self):
        when = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
        aid = str(uuid.uuid4())
        path = tryon_recent.result_path(aid, "image/png", when)
        self.assertEqual(path, f"2026-11-13T09/{aid}.png")
        self.assertEqual(tryon_recent.thumb_path(path), f"2026-11-13T09/{aid}_thumb.jpg")
        self.assertEqual(tryon_recent.result_path(aid, "image/jpeg", when), f"2026-11-13T09/{aid}.jpg")
        self.assertIsNone(tryon_recent.result_path(aid, "image/gif", when))

    def test_a_storage_failure_never_fails_the_try_on(self):
        self.db.storage_fail[(tryon_recent.BUCKET, "upload")] = "boom"
        out, redirect = capture()
        with redirect:
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.result_png)
        self.assertIn("[tryon_recent] WARNING: try-on result not kept", out.getvalue())
        self.assertNotIn("shop_result_path", self.db.only_attempt())

    def test_missing_bucket_or_columns_warn_once_and_try_ons_work(self):
        self.db.storage_fail[(tryon_recent.BUCKET, "upload")] = "Bucket not found"
        out, redirect = capture()
        with redirect:
            self.assertEqual(self.quick_v2().status_code, 200)
            self.assertEqual(self.quick_v2().status_code, 200)
        self.assertEqual(out.getvalue().count("storage bucket tryon-recent missing"), 1)

        del self.db.storage_fail[(tryon_recent.BUCKET, "upload")]
        self.db.missing_columns = {"generation_attempts": ("shop_result_path",)}
        out, redirect = capture()
        with redirect:
            self.assertEqual(self.quick_v2().status_code, 200)
            self.assertEqual(self.quick_v2().status_code, 200)
        self.assertEqual(out.getvalue().count("generation_attempts.shop_result_path missing"), 1)

    def test_link_waits_for_the_queued_insert(self):
        attempt = mock.Mock(id=str(uuid.uuid4()))
        flushed = []

        def flush(timeout):
            flushed.append(timeout)
            self.db.tables["generation_attempts"].append({"id": attempt.id})

        with mock.patch.object(attempt_log, "flush", side_effect=flush):
            path = tryon_recent.store_result(self.db, attempt, self.result_png, "image/png")
        self.assertEqual(len(flushed), 1)
        self.assertEqual(self.db.attempts()[0]["shop_result_path"], path)


class RecentEndpointTests(Round2ATestCase):
    def test_lists_only_this_shops_last_24_hours_newest_first(self):
        old = self.earlier_attempt(str(uuid.uuid4()), stored=True, minutes=25 * 60)
        older = self.earlier_attempt(str(uuid.uuid4()), stored=True, minutes=90, garment_type_name="Kurta")
        newest = self.earlier_attempt(str(uuid.uuid4()), stored=True, minutes=5, attempt_type="look_tryon",
                                      garment_type_id=GARMENT, garment_type_name="IndoWestern")
        self.earlier_attempt(str(uuid.uuid4()), stored=True, minutes=5, shop=OTHER_SHOP)
        self.earlier_attempt(str(uuid.uuid4()), stored=False, minutes=5)

        response = self.client.get("/tryon/recent")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual([item["request_id"] for item in items], [newest["request_id"], older["request_id"]])
        self.assertNotIn(old["request_id"], [item["request_id"] for item in items])
        first = items[0]
        for key in ("request_id", "attempt_type", "garment_type_id", "garment_type_name", "created_at",
                    "expires_at", "image_url", "thumb_url"):
            self.assertIn(key, first)
        self.assertEqual((first["attempt_type"], first["garment_type_name"]), ("look_tryon", "IndoWestern"))
        self.assertEqual(first["image_url"], f"https://signed.test/{newest['shop_result_path']}")
        self.assertEqual(first["thumb_url"], f"https://signed.test/{tryon_recent.thumb_path(newest['shop_result_path'])}")
        expires = datetime.fromisoformat(first["expires_at"])
        saved = datetime.fromisoformat(newest["shop_result_saved_at"])
        self.assertEqual(expires - saved, timedelta(hours=24))

    def test_at_most_30(self):
        for _ in range(35):
            self.earlier_attempt(str(uuid.uuid4()), stored=True, minutes=5)
        self.assertEqual(len(self.client.get("/tryon/recent").json()), 30)

    def test_before_the_sql_runs_the_list_is_empty(self):
        self.earlier_attempt(str(uuid.uuid4()), stored=True)
        self.db.missing_columns = {"generation_attempts": ("shop_result_path",)}
        with capture()[1]:
            response = self.client.get("/tryon/recent")
        self.assertEqual((response.status_code, response.json()), (200, []))


class ResultEndpointTests(Round2ATestCase):
    def get(self, request_id: str) -> dict:
        response = self.client.get(f"/tryon/result/{request_id}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_ready(self):
        row = self.earlier_attempt(str(uuid.uuid4()), stored=True)
        body = self.get(row["request_id"])
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["image_url"], f"https://signed.test/{row['shop_result_path']}")
        self.assertEqual(body["thumb_url"], f"https://signed.test/{tryon_recent.thumb_path(row['shop_result_path'])}")

    def test_found_by_client_request_id_too(self):
        ticket = str(uuid.uuid4())
        row = self.earlier_attempt(ticket, stored=True)
        row["request_id"] = row["id"]  # the client reused an id: only client_request_id holds it
        self.assertEqual(self.get(ticket)["status"], "ready")

    def test_running(self):
        row = self.earlier_attempt(str(uuid.uuid4()), status="running", minutes=1)
        self.assertEqual(self.get(row["request_id"]), {"status": "running"})

    def test_failed_with_code_message_and_refund(self):
        row = self.earlier_attempt(
            str(uuid.uuid4()), status="failed", failed_step="ai_call", http_status=503,
            user_message="The AI service is busy. Please try again in a few minutes.", credits_refunded=50,
        )
        self.assertEqual(
            self.get(row["request_id"]),
            {"status": "failed", "error_code": "ai_busy",
             "message": "The AI service is busy. Please try again in a few minutes.", "credits_refunded": 50},
        )

    def test_open_for_more_than_5_minutes_is_failed_timed_out(self):
        row = self.earlier_attempt(str(uuid.uuid4()), status="running", minutes=6)
        body = self.get(row["request_id"])
        self.assertEqual((body["status"], body["error_code"]), ("failed", "timed_out"))

    def test_a_409_repeat_does_not_hide_the_real_outcome(self):
        ticket = str(uuid.uuid4())
        self.earlier_attempt(ticket, status="running", minutes=2)
        repeat = self.earlier_attempt(ticket, status="failed", minutes=1, http_status=409, failed_step="validation")
        repeat["request_id"] = repeat["id"]
        self.assertEqual(self.get(ticket), {"status": "running"})

    def test_unknown(self):
        self.assertEqual(self.get(str(uuid.uuid4())), {"status": "unknown"})
        self.assertEqual(self.get("not-a-uuid"), {"status": "unknown"})

    def test_never_another_shops_data(self):
        for status, stored in (("succeeded", True), ("running", False), ("failed", False)):
            row = self.earlier_attempt(str(uuid.uuid4()), shop=OTHER_SHOP, status=status, stored=stored)
            self.assertEqual(self.get(row["request_id"]), {"status": "unknown"}, status)


# ---------------------------------------------------------------- PART 3: same ticket on a try-on POST

class SameTicketTryonTests(Round2ATestCase):
    def setUp(self) -> None:
        super().setUp()
        fakes.install_tryon_credits(self.db, balance=1000)
        self.ticket = str(uuid.uuid4())

    def post_v2(self):
        return self.client.post(
            "/tryon/quick/v2",
            data={"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": "true"},
            files={"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")},
            headers={"X-Client-Request-Id": self.ticket},
        )

    def debits(self) -> list[dict]:
        return [row for row in self.db.tables["credit_ledger"] if row["reason"] == "web_tryon"]

    def test_stored_result_is_sent_again_uncharged(self):
        earlier = self.earlier_attempt(self.ticket, stored=True, data=b"the-earlier-picture")
        with capture()[1]:
            response = self.post_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"the-earlier-picture")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(self.gemini.calls, 0)
        self.assertEqual(self.rpc_calls("debit_credits_for_tryon"), [])
        self.assertEqual(self.debits(), [])
        [current] = self.new_attempts(earlier)
        self.assertEqual((current["status"], current["credits_charged"]), ("succeeded", 0))
        self.assertIn("Repeated an earlier request", current["user_message"])
        self.assertIsNone(current.get("shop_result_path"))  # not listed twice

    def test_json_route_replays_in_the_same_shape(self):
        self.earlier_attempt(self.ticket, stored=True, data=b"the-earlier-picture")
        with capture()[1]:
            response = self.client.post(
                "/tryon/quick",
                json={"fabric_image_id": FABRIC, "folder_id": GARMENT, "customer_photo_b64": "Y3VzdG9tZXI=",
                      "consent_confirmed": True},
                headers={"X-Client-Request-Id": self.ticket},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(base64.b64decode(body["result_b64"]), b"the-earlier-picture")
        self.assertEqual(body["result_mime"], "image/png")
        self.assertEqual(self.gemini.calls, 0)
        self.assertEqual(self.debits(), [])

    def test_409_while_an_earlier_one_is_still_running(self):
        earlier = self.earlier_attempt(self.ticket, status="running", minutes=2)
        with capture()[1]:
            response = self.post_v2()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "This try-on is still being made.")
        self.assertEqual(response.json()["error_code"], "still_running")
        self.assertEqual(self.gemini.calls, 0)
        self.assertEqual(self.debits(), [])
        [current] = self.new_attempts(earlier)
        self.assertEqual((current["status"], current["http_status"]), ("failed", 409))
        self.assertIsNone(current.get("credits_charged"))

    def test_otherwise_a_normal_new_try_on(self):
        cases = [
            lambda: self.earlier_attempt(self.ticket, status="failed"),
            lambda: self.earlier_attempt(self.ticket, status="running", minutes=6),  # timed out
            lambda: self.earlier_attempt(self.ticket, status="succeeded"),  # nothing kept
            lambda: self.earlier_attempt(self.ticket, shop=OTHER_SHOP, stored=True),
            lambda: self.earlier_attempt(self.ticket, shop=OTHER_SHOP, status="running"),
            lambda: self.earlier_attempt(self.ticket, stored=True, minutes=25 * 60),  # older than 24 h
        ]
        for index, make in enumerate(cases, start=1):
            self.db.tables["generation_attempts"] = []
            self.ticket = str(uuid.uuid4())
            make()
            with capture()[1]:
                response = self.post_v2()
            self.assertEqual(response.status_code, 200, index)
            self.assertEqual(response.content, self.result_png, index)
            self.assertEqual(len(self.debits()), index, index)
        self.assertEqual(self.gemini.calls, len(cases))

    def test_without_a_ticket_nothing_is_looked_up(self):
        self.earlier_attempt(self.ticket, stored=True)
        with capture()[1]:
            response = self.quick_v2()
        self.assertEqual(response.content, self.result_png)
        self.assertEqual(len(self.debits()), 1)

    def test_before_the_sql_runs_try_ons_carry_on(self):
        self.earlier_attempt(self.ticket, status="running", minutes=2)
        self.db.missing_columns = {"generation_attempts": ("shop_result_path",)}
        with capture()[1]:
            response = self.post_v2()
        # Without the new columns the running rule still holds; nothing is charged.
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.debits(), [])


# ---------------------------------------------------------------- PART 3: 24-hour clean-up

class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        tryon_recent._warned.clear()
        self.now = datetime(2026, 11, 13, 12, 30, tzinfo=timezone.utc)
        self.rows = []
        self.old = self.row(self.now - timedelta(hours=24, minutes=5))
        self.fresh = self.row(self.now - timedelta(hours=23))
        self.bad = {"id": str(uuid.uuid4()), "shop_result_path": "../escape.png",
                    "shop_result_saved_at": (self.now - timedelta(hours=30)).isoformat()}
        self.db.tables["generation_attempts"] = self.rows + [self.bad]
        # Files never linked: an hour-folder that started 26 h ago (all expired), and one that
        # started 25 h - 30 min ago (may still hold files younger than 24 h: kept).
        self.orphan_old = f"2026-11-12T10/{uuid.uuid4()}.jpg"
        self.orphan_young = f"2026-11-12T12/{uuid.uuid4()}.webp"
        for path in (self.orphan_old, tryon_recent.thumb_path(self.orphan_old), self.orphan_young,
                     "2026-11-12T10/notes.txt", "other-folder/x.png", "../escape.png"):
            self.db.files[(tryon_recent.BUCKET, path)] = b"x"
        # Same-looking path in another bucket: never touched.
        self.db.files[("tryon-results", self.orphan_old)] = b"x"

    def row(self, saved_at: datetime) -> dict:
        aid = str(uuid.uuid4())
        path = tryon_recent.result_path(aid, "image/png", saved_at)
        row = {"id": aid, "shop_result_path": path, "shop_result_saved_at": saved_at.isoformat()}
        self.db.files[(tryon_recent.BUCKET, path)] = b"x"
        self.db.files[(tryon_recent.BUCKET, tryon_recent.thumb_path(path))] = b"x"
        self.rows.append(row)
        return row

    def run_cleanup(self) -> int:
        with capture()[1]:
            return tryon_recent.delete_expired(self.db, now=self.now)

    def has(self, path: str, bucket: str = tryon_recent.BUCKET) -> bool:
        return (bucket, path) in self.db.files

    def test_deletes_expired_results_and_old_hour_folders_only(self):
        old_path, fresh_path = self.old["shop_result_path"], self.fresh["shop_result_path"]
        self.assertEqual(self.run_cleanup(), 4)
        self.assertFalse(self.has(old_path))
        self.assertFalse(self.has(tryon_recent.thumb_path(old_path)))
        self.assertIsNone(self.old["shop_result_path"])
        self.assertIsNone(self.old["shop_result_saved_at"])
        self.assertTrue(self.has(fresh_path))
        self.assertTrue(self.has(tryon_recent.thumb_path(fresh_path)))
        self.assertEqual(self.fresh["shop_result_path"], fresh_path)

        self.assertFalse(self.has(self.orphan_old))
        self.assertFalse(self.has(tryon_recent.thumb_path(self.orphan_old)))
        self.assertTrue(self.has(self.orphan_young))
        for path in ("2026-11-12T10/notes.txt", "other-folder/x.png", "../escape.png"):
            self.assertTrue(self.has(path), path)  # not the exact shape: never deleted
        self.assertTrue(self.has(self.orphan_old, "tryon-results"))
        self.assertIsNone(self.bad["shop_result_path"])  # link cleared, odd file left alone

    def test_removes_only_from_this_bucket(self):
        removed = []
        real = fakes._Bucket.remove

        def spy(bucket_self, paths):
            removed.append((bucket_self.bucket, list(paths)))
            return real(bucket_self, paths)

        with mock.patch.object(fakes._Bucket, "remove", spy):
            self.run_cleanup()
        self.assertTrue(removed)
        for bucket, paths in removed:
            self.assertEqual(bucket, tryon_recent.BUCKET)
            for path in paths:
                self.assertTrue(tryon_recent.is_result_path(path) or tryon_recent.is_thumb_path(path), path)

    def test_hours_setting_is_used(self):
        with mock.patch.object(tryon_recent, "keep_hours", return_value=48):
            self.run_cleanup()
        self.assertTrue(self.has(self.old["shop_result_path"]))

    def test_before_the_sql_runs_it_only_warns(self):
        self.db.missing_columns = {"generation_attempts": ("shop_result_path",)}
        out, redirect = capture()
        with redirect, mock.patch.object(fakes._Bucket, "list", side_effect=fakes.FakeDBError("Bucket not found")):
            self.assertEqual(tryon_recent.delete_expired(self.db, now=self.now), 0)
        self.assertIn("shop_result_path missing", out.getvalue())
        self.assertIn("storage bucket tryon-recent missing", out.getvalue())

    def test_worker_runs_it_hourly(self):
        import worker

        calls = []
        with mock.patch.object(worker, "get_supabase", return_value=self.db), \
                mock.patch.object(worker.tryon_recent, "delete_expired", side_effect=lambda c: calls.append(c) or 0), \
                mock.patch.object(worker.tryon_results, "delete_expired", return_value=0), \
                mock.patch.object(worker.worker_housekeeping, "delete_expired_pushed_tryons", return_value=0), \
                mock.patch.object(worker.worker_housekeeping, "sweep_stuck_looks", return_value=0), \
                mock.patch.object(worker.worker_housekeeping, "sweep_missed_tryon_refunds", return_value=0), \
                mock.patch.object(worker.attempt_log, "sweep_timed_out", return_value=0), \
                mock.patch.object(worker.attempt_log, "delete_older_than_retention", return_value=0):
            state: dict = {}
            worker.run_attempt_log_housekeeping(state)
            worker.run_attempt_log_housekeeping(state)  # within the hour: not again
        self.assertEqual(calls, [self.db])


# ---------------------------------------------------------------- PART 4: refund sweep

class RefundSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        fakes.install_tryon_credits(self.db, balance=1000)
        self.db.tables["generation_attempts"] = []
        worker_housekeeping._warned.clear()

    def attempt(self, **extra) -> str:
        aid = str(uuid.uuid4())
        self.db.tables["generation_attempts"].append({
            "id": aid, "request_id": aid, "attempt_type": "tryon", "status": "timed_out", "shop_id": SHOP,
            "credits_charged": 50, "credits_refunded": None, "started_at": ago(minutes=11),
            "created_at": ago(minutes=11), "result_copy_path": None, "tryon_result_path": None, **extra,
        })
        self.db.rpc_handlers["debit_credits_for_tryon"]({"p_shop_id": SHOP, "p_attempt_id": aid, "p_amount": 50})
        return aid

    def found(self) -> set:
        return {row["id"] for row in worker_housekeeping.find_missed_tryon_refunds(self.db)}

    def test_a_kept_shop_result_proves_success(self):
        kept = self.attempt(shop_result_path=f"2026-11-13T09/{uuid.uuid4()}.png")
        lost = self.attempt()
        self.assertEqual(self.found(), {lost})
        with capture()[1]:
            self.assertEqual(worker_housekeeping.sweep_missed_tryon_refunds(self.db), 1)
        row = next(r for r in self.db.attempts() if r["id"] == kept)
        self.assertIsNone(row["credits_refunded"])

    def test_failed_rows_are_still_refunded_whatever_was_kept(self):
        failed = self.attempt(status="failed", shop_result_path=None)
        self.assertEqual(self.found(), {failed})

    def test_without_the_new_column_the_old_check_still_runs(self):
        lost = self.attempt()
        self.attempt(result_copy_path="2026-11-13/x.jpg")
        self.db.missing_columns = {"generation_attempts": ("shop_result_path",)}
        out, redirect = capture()
        with redirect:
            self.assertEqual(self.found(), {lost})
            self.assertEqual(self.found(), {lost})
        self.assertEqual(out.getvalue().count("shop_result_path missing"), 1)

    def test_without_result_copy_path_timed_out_is_still_skipped(self):
        self.attempt()
        self.db.missing_columns = {"generation_attempts": ("result_copy_path", "shop_result_path")}
        with capture()[1]:
            self.assertEqual(self.found(), set())


if __name__ == "__main__":
    unittest.main()
