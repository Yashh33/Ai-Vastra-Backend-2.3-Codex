"""attempt_log helper: scrubbing, cost, sweep, worker finishing, client reports, write failures."""
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
import attempt_logs_api
from auth_deps import CurrentShopContext, get_current_shop_context
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
OTHER_SHOP = "22222222-2222-4222-8222-222222222222"


class AttemptLogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        attempt_log.WorkerAttempt.LOOKUP_DELAY_SECONDS = 0

    def seed_attempt(self, **fields) -> dict:
        row = {
            "id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "attempt_type": "look",
            "status": "started",
            "shop_id": SHOP,
            "started_at": attempt_log.iso(attempt_log.utc_now()),
            "created_at": attempt_log.iso(attempt_log.utc_now()),
        }
        row.update(fields)
        self.db.tables.setdefault("generation_attempts", []).append(row)
        return row


class ScrubTests(unittest.TestCase):
    def test_configured_secret_values_are_removed(self):
        text = f"call failed: key={fakes.FAKE_GEMINI_KEY} service={fakes.FAKE_SERVICE_KEY}"
        cleaned = attempt_log.scrub(text)
        self.assertNotIn(fakes.FAKE_GEMINI_KEY, cleaned)
        self.assertNotIn(fakes.FAKE_SERVICE_KEY, cleaned)

    def test_tokens_jwts_keys_and_base64_are_removed(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        blob = "QUJD" * 100
        text = (
            f"Authorization: Bearer abc.def-123 | token {jwt} | AIzaSyD-0123456789abcdefghijklmnop "
            f"| ?apikey=supersecret&x=1 | photo={blob}"
        )
        cleaned = attempt_log.scrub(text)
        self.assertNotIn("abc.def-123", cleaned)
        self.assertNotIn(jwt, cleaned)
        self.assertNotIn("AIzaSyD-0123456789abcdefghijklmnop", cleaned)
        self.assertNotIn("supersecret", cleaned)
        self.assertNotIn(blob, cleaned)
        self.assertIn("[base64 removed]", cleaned)

    def test_truncated_to_4000(self):
        self.assertEqual(len(attempt_log.scrub("x " * 5000)), attempt_log.ERROR_DETAIL_MAX)

    def test_describe_exception_is_scrubbed(self):
        try:
            raise RuntimeError(f"Gemini 403 for key {fakes.FAKE_GEMINI_KEY}")
        except RuntimeError as exc:
            detail = attempt_log.describe_exception(exc)
        self.assertTrue(detail.startswith("RuntimeError: Gemini 403"))
        self.assertNotIn(fakes.FAKE_GEMINI_KEY, detail)
        self.assertLessEqual(len(detail), attempt_log.ERROR_DETAIL_MAX)


class CostAndResponseTests(unittest.TestCase):
    def test_cost_from_usage_and_price_table(self):
        fields = attempt_log.ai_response_fields(
            fakes.image_response(input_tokens=1000, output_tokens=1200, thoughts=90), "gemini-3.1-flash-image-preview"
        )
        self.assertEqual(fields["ai_input_tokens"], 1000)
        self.assertEqual(fields["ai_output_tokens"], 1290)
        # (1000 * 0.50 + 1290 * 60) / 1M USD * 84
        self.assertAlmostEqual(fields["ai_cost_inr"], 6.5436, places=4)
        self.assertEqual(fields["ai_finish_reason"], "STOP")

    def test_unknown_model_or_missing_usage_gives_null_cost(self):
        self.assertIsNone(attempt_log.ai_cost_inr("some-other-model", 10, 10))
        self.assertIsNone(attempt_log.ai_cost_inr("gemini-3.1-flash-image-preview", None, 10))

    def test_blocked_vs_no_image(self):
        self.assertEqual(attempt_log.no_image_step(fakes.blocked_response()), "ai_blocked")
        self.assertEqual(attempt_log.no_image_step(fakes.empty_response()), "ai_no_image")
        fields = attempt_log.ai_response_fields(fakes.blocked_response(), "gemini-3.1-flash-image-preview")
        self.assertEqual(fields["ai_finish_reason"], "PROMPT_BLOCKED:SAFETY")
        self.assertEqual(fields["ai_safety"]["prompt_block_reason"], "SAFETY")

    def test_device_label(self):
        iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1"
        android = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
        self.assertEqual(attempt_log.device_label(iphone), "iPhone · Safari")
        self.assertEqual(attempt_log.device_label(android), "Android · Chrome")
        self.assertIsNone(attempt_log.device_label(None))


class RecordTests(AttemptLogTestCase):
    def test_final_status_written_once(self):
        record = attempt_log.begin("tryon", "web_tryon_quick")
        record.succeed()
        record.fail("ai_call", user_message="late failure")
        row = self.db.only_attempt()
        self.assertEqual(row["status"], "succeeded")
        self.assertIsNone(row.get("user_message"))
        self.assertIsNotNone(row["duration_ms"])

    def test_client_id_reused_and_duplicate_falls_back(self):
        client_id = str(uuid.uuid4())
        first = attempt_log.begin("tryon", "web_tryon_quick", client_request_id=client_id)
        second = attempt_log.begin("tryon", "web_tryon_quick", client_request_id=client_id)
        rows = {row["id"]: row for row in self.db.attempts()}
        self.assertEqual(rows[first.id]["request_id"], client_id)
        self.assertEqual(rows[second.id]["request_id"], second.id)
        self.assertEqual(rows[second.id]["client_request_id"], client_id)

    def test_dropped_connection_retries_once_without_a_duplicate_row(self):
        import httpx

        original_execute = fakes._Query.execute
        dropped = []

        def flaky_execute(query):
            result = original_execute(query)
            if query.table == "generation_attempts" and query.op == "insert" and not dropped:
                dropped.append(True)
                raise httpx.ReadError("connection dropped after the write landed")
            return result

        with mock.patch.object(fakes._Query, "execute", flaky_execute):
            record = attempt_log.begin("tryon", "web_tryon_quick")
            record.succeed()
        row = self.db.only_attempt()
        self.assertEqual((row["request_id"], row["status"]), (record.request_id, "succeeded"))

    def test_invalid_client_id_is_ignored(self):
        record = attempt_log.begin("look", "web_look", client_request_id="not-a-uuid")
        self.assertEqual(record.request_id, record.id)

    def test_log_write_failure_never_raises(self):
        self.db.fail[("generation_attempts", "*")] = "attempt table down"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            record = attempt_log.begin("tryon", "web_tryon_quick")
            record.update(prompt_text="p")
            record.succeed()
            attempt_log.WorkerAttempt(str(uuid.uuid4())).succeed(output_path="x")
            attempt_log.sweep_timed_out()
        self.assertIn(f"attempt_log write failed: {record.request_id} ", out.getvalue())

    def test_background_writer_failure_never_raises(self):
        attempt_log.configure(sync=False)
        try:
            self.db.fail[("generation_attempts", "*")] = "attempt table down"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                record = attempt_log.begin("tryon", "web_tryon_quick")
                record.succeed()
                self.assertTrue(attempt_log.flush(timeout=5))
            self.assertIn("attempt_log write failed", out.getvalue())
        finally:
            attempt_log.configure(sync=True)

    def test_background_writer_keeps_insert_before_updates(self):
        attempt_log.configure(sync=False)
        try:
            record = attempt_log.begin("tryon", "web_tryon_quick")
            record.update(prompt_text="p")
            record.succeed()
            self.assertTrue(attempt_log.flush(timeout=5))
        finally:
            attempt_log.configure(sync=True)
        ops = [op for table, op, _ in self.db.log if table == "generation_attempts"]
        self.assertEqual(ops, ["insert", "update", "update"])
        self.assertEqual(self.db.only_attempt()["status"], "succeeded")


class RequestIdSpliceTests(unittest.TestCase):
    def test_splices_into_objects_only(self):
        rid = "0b6f2a4e-1111-4222-8333-444455556666"
        self.assertEqual(json.loads(attempt_log._with_request_id(b'{"a": 1}', rid)), {"request_id": rid, "a": 1})
        self.assertEqual(json.loads(attempt_log._with_request_id(b"{ }", rid)), {"request_id": rid})
        self.assertEqual(attempt_log._with_request_id(b"[1, 2]", rid), b"[1, 2]")
        existing = b'{"request_id": "x"}'
        self.assertEqual(attempt_log._with_request_id(existing, rid), existing)


class SweepAndWorkerTests(AttemptLogTestCase):
    def test_sweep_marks_only_stuck_open_rows(self):
        old = attempt_log.iso(attempt_log.utc_now() - timedelta(minutes=6))
        stuck_started = self.seed_attempt(status="started", started_at=old)
        stuck_running = self.seed_attempt(status="running", started_at=old)
        fresh = self.seed_attempt(status="running")
        done = self.seed_attempt(status="succeeded", started_at=old)
        failed = self.seed_attempt(status="failed", failed_step="ai_call", started_at=old)

        self.assertEqual(attempt_log.sweep_timed_out(), 2)

        for row in (stuck_started, stuck_running):
            self.assertEqual(row["status"], "timed_out")
            self.assertEqual(row["failed_step"], "timeout")
        self.assertEqual(fresh["status"], "running")
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(failed["failed_step"], "ai_call")

    def test_effective_status_shows_stuck_rows_as_timed_out(self):
        old = attempt_log.iso(attempt_log.utc_now() - timedelta(minutes=6))
        self.assertEqual(attempt_log.effective_status({"status": "running", "started_at": old}), "timed_out")
        self.assertEqual(
            attempt_log.effective_status({"status": "running", "started_at": attempt_log.iso(attempt_log.utc_now())}),
            "running",
        )
        self.assertEqual(attempt_log.effective_status({"status": "failed", "started_at": old}), "failed")

    def test_worker_corrects_timed_out_but_never_other_final_status(self):
        gen_timed_out, gen_succeeded = str(uuid.uuid4()), str(uuid.uuid4())
        timed_out = self.seed_attempt(status="timed_out", failed_step="timeout", generation_id=gen_timed_out)
        succeeded = self.seed_attempt(status="succeeded", generation_id=gen_succeeded)

        attempt_log.WorkerAttempt(gen_timed_out).succeed(output_path="shop/gen/output_v1.png")
        attempt_log.WorkerAttempt(gen_succeeded).fail("ai_call", error="late")

        self.assertEqual(timed_out["status"], "succeeded")
        self.assertIsNone(timed_out["failed_step"])
        self.assertEqual(succeeded["status"], "succeeded")

    def test_worker_failure_keeps_whatsapp_message(self):
        generation_id = str(uuid.uuid4())
        row = self.seed_attempt(status="running", generation_id=generation_id)
        attempt_log.record_user_message_for_generation(generation_id, "Look nahi ban paya")
        attempt_log.WorkerAttempt(generation_id).fail("ai_call", error="boom")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["user_message"], "Look nahi ban paya")

    def test_retention_deletes_only_old_rows(self):
        old = self.seed_attempt(created_at=attempt_log.iso(attempt_log.utc_now() - timedelta(days=91)))
        recent = self.seed_attempt(created_at=attempt_log.iso(attempt_log.utc_now() - timedelta(days=89)))
        self.assertEqual(attempt_log.delete_older_than_retention(), 1)
        ids = {row["id"] for row in self.db.attempts()}
        self.assertNotIn(old["id"], ids)
        self.assertIn(recent["id"], ids)


class ClientReportTests(AttemptLogTestCase):
    def setUp(self) -> None:
        super().setUp()
        attempt_logs_api._rate_hits.clear()
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id="33333333-3333-4333-8333-333333333333", email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def report(self, request_id: str, **extra):
        body = {"request_id": request_id, "attempt_type": "tryon", "user_message": "Network error", **extra}
        return self.client.post("/attempt-logs/client-report", json=body)

    def test_inserts_when_backend_never_saw_the_attempt(self):
        request_id = str(uuid.uuid4())
        response = self.report(request_id, client_error="TypeError: Failed to fetch")
        self.assertEqual(response.json()["recorded"], True)
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"]), ("failed", "client_network"))
        self.assertEqual(row["request_id"], request_id)
        self.assertEqual(row["shop_id"], SHOP)
        self.assertEqual(row["user_message"], "Network error")

    def test_never_overwrites_a_final_or_running_backend_status(self):
        for backend_status in ("succeeded", "failed", "timed_out", "running"):
            with self.subTest(backend_status=backend_status):
                row = self.seed_attempt(status=backend_status, failed_step=None)
                response = self.report(row["request_id"])
                self.assertEqual(response.json()["recorded"], False)
                self.assertEqual(row["status"], backend_status)
                self.assertIsNone(row.get("failed_step"))

    def test_fills_an_attempt_still_started(self):
        row = self.seed_attempt(status="started")
        self.assertEqual(self.report(row["request_id"]).json()["recorded"], True)
        self.assertEqual((row["status"], row["failed_step"]), ("failed", "client_network"))

    def test_matches_on_client_request_id_and_other_shops_are_ignored(self):
        client_id = str(uuid.uuid4())
        row = self.seed_attempt(status="succeeded", client_request_id=client_id)
        self.assertEqual(self.report(client_id).json()["recorded"], False)
        self.assertEqual(row["status"], "succeeded")

        other = self.seed_attempt(status="started", shop_id=OTHER_SHOP)
        self.assertEqual(self.report(other["request_id"]).json()["recorded"], False)
        self.assertEqual(other["status"], "started")

    def test_rejects_non_uuid_and_rate_limits(self):
        self.assertEqual(self.report("not-a-uuid").status_code, 400)
        codes = [self.report(str(uuid.uuid4())).status_code for _ in range(31)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30], 429)


if __name__ == "__main__":
    unittest.main()
