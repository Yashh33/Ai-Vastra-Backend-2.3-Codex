"""Every generation path writes one attempt row from arrival to a final status.
Mocked: Supabase (in memory), Gemini, WhatsApp transport. No network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import json
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from fastapi.testclient import TestClient

import attempt_log
import generations_api
import tryon_api
import whatsapp_state
import worker
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
USER = "33333333-3333-4333-8333-333333333333"
GARMENT = "44444444-4444-4444-8444-444444444444"
HERO = "55555555-5555-4555-8555-555555555555"
FABRIC = "66666666-6666-4666-8666-666666666666"
LOOK = "77777777-7777-4777-8777-777777777777"
CUSTOMER_PHOTO = b"customer-photo-bytes-never-stored"


def seed_shop(db: fakes.FakeSupabase) -> None:
    db.tables.update(
        {
            "garment_types": [
                {
                    "id": GARMENT,
                    "shop_id": SHOP,
                    "name": "IndoWestern",
                    "default_hero_image_id": HERO,
                    "look_prompt": "Dress the model in a {garment_name}.",
                    "tryon_prompt": "Put the {garment_name} on the customer.",
                    "look_tryon_prompt": "Put this look on the customer.",
                }
            ],
            "hero_images": [
                {"id": HERO, "shop_id": SHOP, "folder_id": GARMENT, "storage_path": f"{SHOP}/hero.jpg", "mime_type": "image/jpeg"}
            ],
            "fabric_images": [
                {"id": FABRIC, "shop_id": SHOP, "storage_path": f"{SHOP}/fabric.jpg", "mime_type": "image/jpeg"}
            ],
            "garment_fabric_slots": [],
            "garment_prompt_versions": [
                {
                    "id": "88888888-8888-4888-8888-888888888888",
                    "garment_type_id": GARMENT,
                    "created_at": "2026-10-24T06:30:00+00:00",
                    "note": "warmer light",
                    "look_prompt": "Dress the model in a {garment_name}.",
                    "tryon_prompt": "Put the {garment_name} on the customer.",
                    "look_tryon_prompt": "Put this look on the customer.",
                }
            ],
            "generations": [
                {"id": LOOK, "shop_id": SHOP, "status": "done", "output_path": f"{SHOP}/{LOOK}/output_v1.png",
                 "folder_id": GARMENT, "original_output_path": None}
            ],
        }
    )


def assert_no_customer_photo(test: unittest.TestCase, db: fakes.FakeSupabase) -> None:
    for table, _op, payload in db.log:
        test.assertNotIn(CUSTOMER_PHOTO.decode(), repr(payload), f"customer photo reached table {table}")
    test.assertNotIn(CUSTOMER_PHOTO, db.files.values(), "customer photo was stored")


class PathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        seed_shop(self.db)
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        attempt_log.WorkerAttempt.LOOKUP_DELAY_SECONDS = 0
        tryon_api._storage_bytes_cache.clear()
        self.gemini = fakes.FakeGemini(fakes.image_response())
        patches = [
            mock.patch.object(tryon_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(generations_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(tryon_api, "_get_gemini_client", lambda: self.gemini),
            mock.patch.object(tryon_api, "_downscale_image_if_needed", lambda data, mime: (data, mime)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def assert_first_write_is_the_attempt(self) -> None:
        table, op, payload = self.db.log[0]
        self.assertEqual((table, op, payload["status"]), ("generation_attempts", "insert", "started"))


# ---------------------------------------------------------------- web: try-on (sync in the API)

class WebTryonTests(PathTestCase):
    def quick_v2(self, *, consent="true", headers=None, files=True):
        data = {"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": consent}
        upload = {"customer_photo": ("customer.jpg", CUSTOMER_PHOTO, "image/jpeg")} if files else None
        return self.client.post("/tryon/quick/v2", data=data, files=upload, headers=headers or {})

    def test_success_records_inputs_prompt_cost_and_request_id(self):
        client_id = str(uuid.uuid4())
        response = self.quick_v2(headers={"X-Client-Request-Id": client_id, "User-Agent": "Mozilla/5.0 (iPhone) Safari/604.1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"generated-image")
        self.assertEqual(response.headers["x-request-id"], client_id)
        self.assert_first_write_is_the_attempt()

        row = self.db.only_attempt()
        self.assertEqual(row["status"], "succeeded")
        self.assertIsNone(row["failed_step"])
        self.assertEqual(row["request_id"], client_id)
        self.assertEqual((row["shop_id"], row["user_id"], row["user_email"]), (SHOP, USER, "demo@gmail.com"))
        self.assertEqual((row["attempt_type"], row["endpoint"]), ("tryon", "web_tryon_quick"))
        self.assertEqual((row["garment_type_id"], row["garment_type_name"], row["fabric_mode"]), (GARMENT, "IndoWestern", "single"))
        self.assertEqual(row["hero_path"], f"{SHOP}/hero.jpg")
        self.assertEqual(row["fabric_inputs"][0]["storage_path"], f"{SHOP}/fabric.jpg")
        self.assertFalse(row["fabric_inputs"][0]["colour_corrected"])
        self.assertEqual(row["prompt_text"], "Put the IndoWestern on the customer.")
        self.assertTrue(row["prompt_version_label"].startswith("saved 24 Oct 2026"))
        self.assertEqual(row["model_id"], "gemini-3.1-flash-image-preview")
        self.assertEqual((row["ai_input_tokens"], row["ai_output_tokens"]), (1000, 1290))
        self.assertAlmostEqual(row["ai_cost_inr"], 6.5436, places=4)
        self.assertEqual(row["device_label"], "iPhone · Safari")
        for key in ("ai_started_at", "ai_finished_at", "finished_at", "duration_ms"):
            self.assertIsNotNone(row[key], key)
        self.assertIsNone(row.get("credits_charged"))  # web try-ons charge no credits
        assert_no_customer_photo(self, self.db)

    def test_consent_missing_is_validation_with_exact_message(self):
        response = self.quick_v2(consent="false")
        self.assertEqual(response.status_code, 400)
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"], row["http_status"]), ("failed", "validation", 400))
        self.assertEqual(row["user_message"], tryon_api._CONSENT_REJECTED_DETAIL)
        self.assertEqual(row["user_message"], response.json()["detail"])
        self.assertEqual(response.json()["request_id"], row["request_id"])
        self.assertEqual(self.gemini.calls, 0)

    def test_missing_form_field_422_is_validation(self):
        response = self.quick_v2(files=False)
        self.assertEqual(response.status_code, 422)
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["http_status"]), ("validation", 422))
        self.assertEqual(json.loads(row["user_message"]), response.json()["detail"])

    def test_no_login_is_auth(self):
        app.dependency_overrides.clear()
        response = self.quick_v2()
        self.assertEqual(response.status_code, 401)
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["user_message"]), ("auth", "Missing bearer token"))
        self.assertIsNone(row.get("shop_id"))

    def test_gemini_exception_is_ai_call(self):
        self.gemini.outcome = RuntimeError("503 UNAVAILABLE upstream overloaded")
        response = self.quick_v2()
        self.assertEqual(response.status_code, 500)
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"], row["http_status"]), ("failed", "ai_call", 500))
        self.assertEqual(row["user_message"], "Internal Server Error")
        self.assertEqual(row["user_message"], response.text)
        self.assertIn("503 UNAVAILABLE", row["error_detail"])
        self.assertIsNotNone(row["ai_started_at"])

    def test_gemini_empty_response_is_ai_no_image(self):
        self.gemini.outcome = fakes.empty_response()
        response = self.quick_v2()
        self.assertEqual(response.status_code, 502)
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["http_status"]), ("ai_no_image", 502))
        self.assertEqual(row["user_message"], "Gemini did not return an image for try-on")
        self.assertEqual(row["ai_input_tokens"], 900)

    def test_gemini_blocked_response_is_ai_blocked_502_not_a_crash(self):
        self.gemini.outcome = fakes.blocked_response()
        response = self.quick_v2()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Gemini did not return an image for try-on")
        row = self.db.only_attempt()
        self.assertEqual(row["failed_step"], "ai_blocked")
        self.assertEqual(row["ai_finish_reason"], "PROMPT_BLOCKED:SAFETY")
        self.assertEqual(row["ai_safety"]["prompt_block_reason"], "SAFETY")

    def test_stored_input_unreadable_is_upload(self):
        self.db.storage_fail[("hero-images", "download")] = "object store timeout"
        response = self.quick_v2()
        self.assertEqual(response.status_code, 502)
        row = self.db.only_attempt()
        self.assertEqual(row["failed_step"], "upload")
        self.assertEqual(row["user_message"], response.json()["detail"])

    def test_log_write_failure_does_not_break_the_try_on(self):
        self.db.fail[("generation_attempts", "*")] = "attempt table missing"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"generated-image")
        self.assertIn("attempt_log write failed:", out.getvalue())

    def test_look_tryon_json_records_source_look_and_request_id_in_body(self):
        body = {"generation_id": LOOK, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}
        response = self.client.post("/tryon/", json=body)
        self.assertEqual(response.status_code, 200)
        row = self.db.only_attempt()
        self.assertEqual(response.json()["request_id"], row["request_id"])
        self.assertEqual((row["attempt_type"], row["status"]), ("look_tryon", "succeeded"))
        self.assertEqual(row["source_generation_id"], LOOK)
        self.assertEqual(row["hero_path"], f"{SHOP}/{LOOK}/output_v1.png")
        self.assertEqual(row["hero_colour_corrected"], False)
        self.assertEqual(row["prompt_text"], "Put this look on the customer.")

    def test_look_tryon_without_prompt_is_prompt_build(self):
        self.db.tables["garment_types"][0]["look_tryon_prompt"] = None
        files = {"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")}
        response = self.client.post("/tryon/v2", data={"generation_id": LOOK, "consent_confirmed": "true"}, files=files)
        self.assertEqual(response.status_code, 400)
        row = self.db.only_attempt()
        self.assertEqual(row["failed_step"], "prompt_build")
        self.assertEqual(row["user_message"], tryon_api._LOOK_TRYON_PROMPT_MISSING_DETAIL)

    def test_push_to_screen_links_the_stored_result(self):
        client_id = str(uuid.uuid4())
        self.quick_v2(headers={"X-Client-Request-Id": client_id})
        response = self.client.post(
            "/tryon/push-to-screen",
            data={"folder_id": GARMENT},
            files={"result_image": ("r.png", b"generated-image", "image/png")},
            headers={"X-Client-Request-Id": client_id},
        )
        self.assertEqual(response.status_code, 200)
        row = self.db.only_attempt()
        self.assertTrue(row["tryon_result_path"].startswith(f"{SHOP}/"))
        self.assertEqual(row["generation_id"], response.json()["generation_id"])


# ---------------------------------------------------------------- web: look (API queues, worker generates)

class WebLookTests(PathTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.new_generation_id = str(uuid.uuid4())
        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = lambda params: [
            {"generation_id": self.new_generation_id, "status": "queued", "credits_used": 50,
             "balance_before": 150, "balance_after": 100}
        ]

    def post(self, body=None):
        return self.client.post("/generations", json=body or {"hero_image_id": HERO, "fabric_image_id": FABRIC})

    def test_success_ends_api_part_as_running_with_generation_and_credits(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assert_first_write_is_the_attempt()
        row = self.db.only_attempt()
        self.assertEqual(response.json()["request_id"], row["request_id"])
        self.assertEqual(response.headers["x-request-id"], row["request_id"])
        self.assertEqual(response.json()["credits_used"], 50)
        self.assertEqual((row["status"], row["generation_id"], row["credits_charged"]), ("running", self.new_generation_id, 50))
        self.assertEqual((row["attempt_type"], row["fabric_mode"]), ("look", "single"))
        self.assertEqual(row["hero_path"], f"{SHOP}/hero.jpg")
        self.assertEqual(row["prompt_text"], "Dress the model in a IndoWestern.")
        self.assertEqual(row["fabric_inputs"][0]["apply_to"], "suit_full_body")

    def test_insufficient_credits(self):
        def no_credits(_params):
            raise fakes.FakeDBError("P0001: Insufficient credits")

        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = no_credits
        response = self.post()
        self.assertEqual(response.status_code, 402)
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"], row["http_status"]), ("failed", "credits", 402))
        self.assertEqual(row["user_message"], "Insufficient credits")
        self.assertIsNone(row.get("credits_charged"))

    def test_blank_hero_is_validation(self):
        response = self.post({"hero_image_id": "   ", "fabric_image_id": FABRIC})
        self.assertEqual(response.status_code, 400)
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["user_message"]), ("validation", "hero_image_id cannot be blank"))

    def test_database_failure_creating_the_generation_is_db_save(self):
        def db_down(_params):
            raise fakes.FakeDBError("connection reset by peer")

        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = db_down
        response = self.post()
        self.assertEqual(response.status_code, 500)
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["user_message"]), ("db_save", "Failed to create generation"))
        self.assertIn("connection reset", row["error_detail"])


# ---------------------------------------------------------------- worker (finishes looks)

class WorkerTests(PathTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.generation_id = str(uuid.uuid4())
        self.db.tables["generations"].append({"id": self.generation_id, "shop_id": SHOP, "status": "processing"})
        self.row = {
            "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "attempt_type": "look", "status": "running",
            "generation_id": self.generation_id, "shop_id": SHOP, "credits_charged": 50,
            "started_at": attempt_log.iso(attempt_log.utc_now() - timedelta(seconds=40)),
            "created_at": attempt_log.iso(attempt_log.utc_now()),
        }
        self.db.tables.setdefault("generation_attempts", []).append(self.row)
        self.db.rpc_handlers["mark_generation_failed_with_refund"] = lambda params: [
            {"refunded": True, "refund_amount": 50, "balance_after": 150}
        ]
        self.assets = {
            "hero": {"storage_path": f"{SHOP}/hero.jpg", "mime_type": "image/jpeg"},
            "fabrics": [{"storage_path": f"{SHOP}/fabric.jpg", "mime_type": "image/jpeg", "apply_to": "suit_full_body"}],
            "prompt_used": "Dress the model.",
        }
        patches = [
            mock.patch.object(worker, "get_supabase", lambda: self.db),
            mock.patch.object(worker, "fetch_generation_assets", lambda supabase, job: self.fetch_assets()),
            mock.patch.object(worker, "downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(worker, "build_thumbnail_jpeg_bytes", lambda data: b"thumb"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fetch_assets(self):
        if isinstance(self.assets, BaseException):
            raise self.assets
        return self.assets

    def run_job(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            worker.process_job(self.gemini, get_settings(), {"id": self.generation_id, "shop_id": SHOP})

    def test_success(self):
        self.run_job()
        self.assertEqual(self.row["status"], "succeeded")
        self.assertTrue(self.row["output_path"].startswith(f"{SHOP}/{self.generation_id}/output_v"))
        self.assertEqual((self.row["ai_input_tokens"], self.row["ai_output_tokens"]), (1000, 1290))
        self.assertAlmostEqual(self.row["ai_cost_inr"], 6.5436, places=4)
        self.assertEqual(self.row["credits_charged"], 50)  # set by the API, untouched
        self.assertGreaterEqual(self.row["duration_ms"], 40000)
        self.assertIsNotNone(self.row["ai_started_at"])
        self.assertEqual(self.db.tables["generations"][-1]["status"], "done")

    def test_gemini_exception_is_ai_call_with_refund(self):
        self.gemini.outcome = RuntimeError("429 RESOURCE_EXHAUSTED")
        self.run_job()
        self.assertEqual((self.row["status"], self.row["failed_step"]), ("failed", "ai_call"))
        self.assertEqual(self.row["credits_refunded"], 50)
        self.assertIn("429 RESOURCE_EXHAUSTED", self.row["error_detail"])

    def test_gemini_empty_and_blocked(self):
        for outcome, step in ((fakes.empty_response(), "ai_no_image"), (fakes.blocked_response(), "ai_blocked")):
            with self.subTest(step=step):
                self.row.update(status="running", failed_step=None)
                self.gemini.outcome = outcome
                self.run_job()
                self.assertEqual((self.row["status"], self.row["failed_step"]), ("failed", step))

    def test_storage_failure_is_storage_save(self):
        self.db.storage_fail[("generated-outputs", "upload")] = "bucket unavailable"
        self.run_job()
        self.assertEqual((self.row["failed_step"], self.row["credits_refunded"]), ("storage_save", 50))

    def test_database_failure_is_db_save(self):
        self.db.fail[("generations", "update")] = "statement timeout"
        self.run_job()
        self.assertEqual(self.row["failed_step"], "db_save")
        self.assertIn("statement timeout", self.row["error_detail"])

    def test_missing_prompt_is_prompt_build_and_unreadable_input_is_upload(self):
        self.assets = RuntimeError("prompt_used is empty; create generation again after Step 21")
        self.run_job()
        self.assertEqual(self.row["failed_step"], "prompt_build")

        self.row.update(status="running", failed_step=None)
        self.assets = RuntimeError("Hero image metadata missing")
        self.run_job()
        self.assertEqual(self.row["failed_step"], "upload")

    def test_refund_call_failing_leaves_refund_unknown(self):
        def refund_down(_params):
            raise fakes.FakeDBError("rpc timeout")

        self.db.rpc_handlers["mark_generation_failed_with_refund"] = refund_down
        self.gemini.outcome = RuntimeError("boom")
        self.run_job()
        self.assertEqual(self.row["status"], "failed")
        self.assertIsNone(self.row["credits_refunded"])

    def test_worker_crash_leaves_row_open_then_sweep_times_it_out(self):
        self.row["started_at"] = attempt_log.iso(attempt_log.utc_now() - timedelta(minutes=6))
        # The process died mid-job: nothing wrote a final status.
        self.assertEqual(attempt_log.sweep_timed_out(), 1)
        self.assertEqual((self.row["status"], self.row["failed_step"]), ("timed_out", "timeout"))

    def test_log_write_failure_does_not_break_the_job(self):
        self.db.fail[("generation_attempts", "*")] = "attempt table missing"
        self.run_job()
        self.assertEqual(self.db.tables["generations"][-1]["status"], "done")

    def test_housekeeping_runs_sweep_and_never_raises(self):
        self.row["started_at"] = attempt_log.iso(attempt_log.utc_now() - timedelta(minutes=6))
        state: dict = {}
        with contextlib.redirect_stdout(io.StringIO()):
            worker.run_attempt_log_housekeeping(state)
        self.assertEqual(self.row["status"], "timed_out")
        self.db.fail[("generation_attempts", "*")] = "down"
        state.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            worker.run_attempt_log_housekeeping(state)


# ---------------------------------------------------------------- WhatsApp

class WhatsAppTests(PathTestCase):
    PHONE = "919999999999"

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[str] = []
        self.balance = 150
        self.new_generation_id = str(uuid.uuid4())
        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = lambda params: [
            {"generation_id": self.new_generation_id, "status": "queued", "credits_used": 50,
             "balance_before": 150, "balance_after": 100}
        ]
        patches = [
            mock.patch.object(whatsapp_state, "send_text", lambda phone, body: self.sent.append(body)),
            mock.patch.object(whatsapp_state, "download_media", lambda media_id: (CUSTOMER_PHOTO, "image/jpeg")),
            mock.patch.object(whatsapp_state, "upload_media", lambda data, mime: "media-1"),
            mock.patch.object(whatsapp_state, "send_image_by_media_id", lambda *args, **kwargs: None),
            mock.patch.object(whatsapp_state, "_get_shop_balance", lambda supabase, shop_id: self.balance),
            mock.patch.object(whatsapp_state, "_update_session", lambda *args, **kwargs: None),
            mock.patch.object(whatsapp_state, "_downscale_image_if_needed", lambda data, mime: (data, mime)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def session(self, **fields):
        base = {
            "id": "session-1", "phone_number": self.PHONE, "shop_id": SHOP, "folder_id": GARMENT,
            "hero_image_id": HERO, "fabric_image_id": FABRIC, "pending_fabrics": [],
            "active_generation_id": None, "free_generations_used": 0,
        }
        base.update(fields)
        return base

    def test_look_success_is_running_with_generation(self):
        whatsapp_state.create_generation_for_session(self.db, self.session())
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["generation_id"], row["credits_charged"]), ("running", self.new_generation_id, 50))
        self.assertEqual((row["endpoint"], row["user_label"]), ("whatsapp_look", "WhatsApp"))
        self.assertNotIn(self.PHONE, repr(row))

    def test_look_no_credits_records_exact_whatsapp_text(self):
        self.balance = 10
        whatsapp_state.create_generation_for_session(self.db, self.session())
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"]), ("failed", "credits"))
        self.assertEqual(row["user_message"], whatsapp_state._MSG_NO_CREDITS)
        self.assertEqual(self.sent[-1], whatsapp_state._MSG_NO_CREDITS)

    def test_look_crash_records_tech_error_text(self):
        def db_down(_params):
            raise fakes.FakeDBError("connection reset")

        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = db_down
        with self.assertRaises(fakes.FakeDBError):
            whatsapp_state.create_generation_for_session(self.db, self.session())
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["user_message"]), ("db_save", whatsapp_state._MSG_TECH_ERROR))

    def test_watcher_records_failure_text_for_the_look(self):
        whatsapp_state.create_generation_for_session(self.db, self.session())
        attempt_log.record_user_message_for_generation(self.new_generation_id, "Look failed text")
        self.assertEqual(self.db.only_attempt()["user_message"], "Look failed text")

    def test_direct_tryon_success_charges_and_stores_result(self):
        with contextlib.redirect_stdout(io.StringIO()):
            whatsapp_state.run_tryon_for_session(self.db, self.session(), "media-123", "image/jpeg")
        row = self.db.only_attempt()
        self.assertEqual((row["attempt_type"], row["status"], row["credits_charged"]), ("tryon", "succeeded", 50))
        self.assertTrue(row["tryon_result_path"].startswith(f"{SHOP}/"))
        self.assertIsNotNone(row["generation_id"])
        ledger = self.db.tables["credit_ledger"][0]
        self.assertEqual(-ledger["delta"], row["credits_charged"])
        self.assertNotIn(self.PHONE, repr(row))
        assert_no_customer_photo(self, self.db)

    def test_tryon_gemini_failure_records_tryon_failed_text(self):
        self.gemini.outcome = RuntimeError("Gemini 500")
        with contextlib.redirect_stdout(io.StringIO()):
            whatsapp_state.run_tryon_for_session(self.db, self.session(), "media-123", "image/jpeg")
        row = self.db.only_attempt()
        self.assertEqual((row["failed_step"], row["user_message"], row["credits_charged"]), ("ai_call", whatsapp_state._MSG_TRYON_FAILED, 0))
        self.assertNotIn("credit_ledger", self.db.tables)

    def test_tryon_from_look_is_free_look_tryon(self):
        with contextlib.redirect_stdout(io.StringIO()):
            whatsapp_state.run_tryon_for_session(self.db, self.session(active_generation_id=LOOK), "media-123", "image/jpeg")
        row = self.db.only_attempt()
        self.assertEqual((row["attempt_type"], row["status"], row["credits_charged"]), ("look_tryon", "succeeded", 0))
        self.assertEqual(row["source_generation_id"], LOOK)
        self.assertEqual(row["garment_type_id"], GARMENT)


if __name__ == "__main__":
    unittest.main()
