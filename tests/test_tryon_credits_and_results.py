"""Dropped-connection retries (503), credits for web try-ons, and the 7-day admin-only copy of
each try-on result. Mocked: Supabase and storage (in memory), Gemini. No network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import threading
import unittest
import uuid
from datetime import timedelta
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import admin_api
import attempt_log
import generations_api
import supabase_client
import tryon_api
import tryon_results
import worker
from admin_deps import verify_admin_secret
from auth_deps import CurrentShopContext, get_current_shop_context
from main import app
from supabase_client import CONNECTION_PROBLEM_DETAIL, SupabaseUnavailable, retry_once

SHOP = "11111111-1111-4111-8111-111111111111"
USER = "33333333-3333-4333-8333-333333333333"
GARMENT = "44444444-4444-4444-8444-444444444444"
HERO = "55555555-5555-4555-8555-555555555555"
FABRIC = "66666666-6666-4666-8666-666666666666"
LOOK = "77777777-7777-4777-8777-777777777777"
CUSTOMER_PHOTO = b"customer-photo-bytes-never-stored"


def png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    return Image.open(io.BytesIO(data)).size


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


# ---------------------------------------------------------------- retry helper

class RetryOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(supabase_client, "RETRY_DELAY_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def calls(self, *outcomes):
        """A call that raises / returns each outcome in turn, counting how often it ran."""
        state = {"count": 0}

        def fn():
            outcome = outcomes[state["count"]]
            state["count"] += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return fn, state

    def test_read_error_is_retried_once_on_a_fresh_client(self):
        fn, state = self.calls(httpx.ReadError("[Errno 11] Resource temporarily unavailable"), "rows")
        with mock.patch.object(supabase_client, "reset_thread_client") as reset, quiet():
            self.assertEqual(retry_once(fn), "rows")
        self.assertEqual(state["count"], 2)
        reset.assert_called_once()

    def test_still_failing_after_the_retry_is_a_503(self):
        fn, state = self.calls(httpx.ReadError("boom"), httpx.RemoteProtocolError("boom"))
        with quiet(), self.assertRaises(SupabaseUnavailable) as caught:
            retry_once(fn)
        self.assertEqual(state["count"], 2)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (503, CONNECTION_PROBLEM_DETAIL))

    def test_a_4xx_is_never_retried(self):
        error = APIError({"message": "bad request", "code": "400", "details": None, "hint": None})
        fn, state = self.calls(error, "never")
        with self.assertRaises(APIError):
            retry_once(fn)
        self.assertEqual(state["count"], 1)

    def test_a_real_database_error_is_never_retried(self):
        fn, state = self.calls(fakes.FakeDBError("P0001: Insufficient credits"), "never")
        with self.assertRaises(fakes.FakeDBError):
            retry_once(fn)
        self.assertEqual(state["count"], 1)

    def test_insert_whose_retry_hits_its_own_row_counts_as_done(self):
        fn, state = self.calls(httpx.ReadError("lost reply"), fakes.FakeDBError("duplicate key value (23505)"))
        with quiet():
            self.assertIsNone(retry_once(fn, duplicate_ok=True))
        self.assertEqual(state["count"], 2)

    def test_each_thread_gets_its_own_client(self):
        made = []

        def fake_create_client(url, key):
            made.append(object())
            return mock.Mock(name=f"client-{len(made)}", table=mock.Mock(return_value=len(made)))

        seen: dict[str, object] = {}
        with mock.patch.object(supabase_client, "create_client", fake_create_client):
            proxy = supabase_client.get_supabase_admin_client()

            def use(name):
                seen[name] = proxy.table("x")
                seen[name + "-again"] = proxy.table("x")

            threads = [threading.Thread(target=use, args=(f"t{i}",)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(made), 2)
        self.assertNotEqual(seen["t0"], seen["t1"])
        self.assertEqual(seen["t0"], seen["t0-again"])


# ---------------------------------------------------------------- shared API setup

class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update(
            {
                "shops": [{"id": SHOP, "name": "Demo"}],
                "garment_types": [
                    {
                        "id": GARMENT, "shop_id": SHOP, "name": "IndoWestern", "default_hero_image_id": HERO,
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
                "generations": [
                    {"id": LOOK, "shop_id": SHOP, "status": "done", "generation_type": "look",
                     "output_path": f"{SHOP}/{LOOK}/output_v1.png", "folder_id": GARMENT, "original_output_path": None}
                ],
            }
        )
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        tryon_api._storage_bytes_cache.clear()
        self.result_png = png(2000, 1000)
        self.gemini = fakes.FakeGemini(fakes.image_response(data=self.result_png))
        patches = [
            mock.patch.object(supabase_client, "RETRY_DELAY_SECONDS", 0),
            mock.patch.object(tryon_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(generations_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(admin_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(tryon_api, "_get_gemini_client", lambda: self.gemini),
            mock.patch.object(tryon_api, "_downscale_image_if_needed", lambda data, mime: (data, mime)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        app.dependency_overrides[verify_admin_secret] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def quick_v2(self):
        return self.client.post(
            "/tryon/quick/v2",
            data={"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": "true"},
            files={"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")},
        )

    def ledger(self, reason: str) -> list[dict]:
        return [row for row in self.db.tables.get("credit_ledger", []) if row["reason"] == reason]


# ---------------------------------------------------------------- dropped connections in requests

class ConnectionDropTests(ApiTestCase):
    def test_one_dropped_read_is_retried_and_the_try_on_works(self):
        self.db.fail_once[("garment_types", "select")] = httpx.ReadError("[Errno 11] Resource temporarily unavailable")
        with quiet():
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)

    def test_a_connection_that_stays_down_is_a_503_with_the_retry_text(self):
        self.db.fail[("garment_types", "select")] = httpx.ReadError("[Errno 11] Resource temporarily unavailable")
        with quiet():
            response = self.quick_v2()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], CONNECTION_PROBLEM_DETAIL)
        self.assertEqual(self.gemini.calls, 0)
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["http_status"]), ("failed", 503))

    def test_a_failed_to_fetch_500_caused_by_a_drop_becomes_503(self):
        self.db.fail[("generations", "select")] = httpx.ConnectError("connection refused")
        with quiet():
            response = self.client.get(f"/generations/{LOOK}")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], CONNECTION_PROBLEM_DETAIL)

    def test_a_real_error_stays_a_500(self):
        self.db.fail[("generations", "select")] = "permission denied"
        response = self.client.get(f"/generations/{LOOK}")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Failed to fetch generation")


# ---------------------------------------------------------------- credits

class TryonCreditTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        fakes.install_tryon_credits(self.db, balance=150)

    def test_try_on_charges_50_once_and_logs_it(self):
        response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        debits = self.ledger("web_tryon")
        self.assertEqual([(row["delta"], row["balance_after"]) for row in debits], [(-50, 100)])
        self.assertEqual(self.ledger("web_tryon_refund"), [])
        row = self.db.only_attempt()
        self.assertEqual(debits[0]["attempt_id"], row["id"])
        self.assertEqual((row["status"], row["credits_charged"]), ("succeeded", 50))
        self.assertIsNone(row.get("credits_refunded"))
        self.assertEqual(len([entry for entry in self.db.log if entry[:2] == ("rpc", "debit_credits_for_tryon")]), 1)

    def test_every_try_on_endpoint_charges(self):
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
        fakes.install_tryon_credits(self.db, balance=1000)
        for index, send in enumerate(requests, start=1):
            self.assertEqual(send().status_code, 200, index)
            self.assertEqual(len(self.ledger("web_tryon")), index)

    def test_ai_failure_refunds_once(self):
        self.gemini.outcome = RuntimeError("503 UNAVAILABLE upstream")
        response = self.quick_v2()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.ledger("web_tryon")), 1)
        refunds = self.ledger("web_tryon_refund")
        self.assertEqual([(row["delta"], row["balance_after"]) for row in refunds], [(50, 150)])
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"]), ("failed", "ai_call"))
        self.assertEqual((row["credits_charged"], row["credits_refunded"]), (50, 50))

    def test_no_image_refunds_once(self):
        self.gemini.outcome = fakes.empty_response()
        self.assertEqual(self.quick_v2().status_code, 502)
        self.assertEqual(len(self.ledger("web_tryon_refund")), 1)
        self.assertEqual(self.db.only_attempt()["credits_refunded"], 50)

    def test_refund_is_never_repeated(self):
        call = tryon_api._AiCall(self.db, SHOP, str(uuid.uuid4()), 0, "m")
        self.assertEqual(call.refund(), 0)  # nothing was charged: nothing to refund
        key = str(uuid.uuid4())
        self.db.rpc_handlers["debit_credits_for_tryon"]({"p_shop_id": SHOP, "p_attempt_id": key, "p_amount": 50})
        call = tryon_api._AiCall(self.db, SHOP, key, 50, "m")
        self.assertEqual((call.refund(), call.refund()), (50, 50))
        self.assertEqual(len(self.ledger("web_tryon_refund")), 1)
        self.assertEqual(len([entry for entry in self.db.log if entry[:2] == ("rpc", "refund_credits_for_tryon")]), 1)

    def test_not_enough_credits_is_402_and_no_ai_call(self):
        self.db.tables["credit_ledger"][-1]["balance_after"] = 10
        response = self.quick_v2()
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["detail"], "Insufficient credits")
        self.assertEqual(self.gemini.calls, 0)
        self.assertEqual(self.ledger("web_tryon"), [])
        row = self.db.only_attempt()
        self.assertEqual((row["status"], row["failed_step"], row["http_status"]), ("failed", "credits", 402))

    def test_debit_repeated_after_a_lost_reply_charges_once(self):
        debit = self.db.rpc_handlers["debit_credits_for_tryon"]
        state = {"first": True}

        def landed_then_dropped(params):
            result = debit(params)
            if state.pop("first", False):
                raise httpx.ReadError("reply lost")
            return result

        self.db.rpc_handlers["debit_credits_for_tryon"] = landed_then_dropped
        with quiet():
            self.assertEqual(self.quick_v2().status_code, 200)
        self.assertEqual(len(self.ledger("web_tryon")), 1)
        self.assertEqual(self.db.only_attempt()["credits_charged"], 50)

    def test_before_the_sql_runs_try_ons_still_work_uncharged(self):
        del self.db.rpc_handlers["debit_credits_for_tryon"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertIn("try-on not charged", out.getvalue())
        self.assertEqual(self.db.only_attempt()["credits_charged"], 0)

    def test_push_to_screen_charges_nothing(self):
        response = self.client.post(
            "/tryon/push-to-screen",
            data={"folder_id": GARMENT},
            files={"result_image": ("r.png", b"generated-image", "image/png")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ledger("web_tryon"), [])


# ---------------------------------------------------------------- result copies

class ResultCopyTests(ApiTestCase):
    def copies(self) -> dict:
        return {path: data for (bucket, path), data in self.db.files.items() if bucket == tryon_results.BUCKET}

    def test_success_keeps_a_small_private_copy_linked_to_the_attempt(self):
        response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.result_png)  # the user gets the full result

        row = self.db.only_attempt()
        copies = self.copies()
        self.assertEqual(list(copies), [row["result_copy_path"]])
        self.assertTrue(tryon_results.is_copy_path(row["result_copy_path"]))
        self.assertTrue(row["result_copy_path"].endswith(f"/{row['id']}.jpg"))
        self.assertIsNotNone(row["result_copy_saved_at"])
        data = copies[row["result_copy_path"]]
        self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG
        self.assertEqual(image_size(data), (768, 384))
        self.assertNotIn(CUSTOMER_PHOTO, self.db.files.values())

    def test_sync_endpoint_keeps_a_copy_too(self):
        body = {"generation_id": LOOK, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}
        self.assertEqual(self.client.post("/tryon/", json=body).status_code, 200)
        self.assertEqual(len(self.copies()), 1)

    def test_failing_to_save_the_copy_never_fails_the_try_on(self):
        self.db.storage_fail[(tryon_results.BUCKET, "upload")] = "bucket missing"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.result_png)
        self.assertIn("result copy not saved", out.getvalue())
        row = self.db.only_attempt()
        self.assertEqual(row["status"], "succeeded")
        self.assertNotIn("result_copy_path", row)

    def test_failed_try_on_keeps_nothing(self):
        self.gemini.outcome = RuntimeError("boom")
        self.quick_v2()
        self.assertEqual(self.copies(), {})


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.now = attempt_log.utc_now()
        self.old_id, self.new_id, self.orphan_id = (str(uuid.uuid4()) for _ in range(3))
        old_at, new_at = self.now - timedelta(days=8), self.now - timedelta(days=2)
        self.old_path = tryon_results.copy_path(self.old_id, old_at)
        self.new_path = tryon_results.copy_path(self.new_id, new_at)
        self.orphan_path = tryon_results.copy_path(self.orphan_id, self.now - timedelta(days=10))
        self.db.tables["generation_attempts"] = [
            {"id": self.old_id, "result_copy_path": self.old_path, "result_copy_saved_at": attempt_log.iso(old_at)},
            {"id": self.new_id, "result_copy_path": self.new_path, "result_copy_saved_at": attempt_log.iso(new_at)},
        ]
        old_day = self.old_path.split("/")[0]
        for key in (
            (tryon_results.BUCKET, self.old_path),
            (tryon_results.BUCKET, self.new_path),
            (tryon_results.BUCKET, self.orphan_path),  # never linked (the link write failed)
            (tryon_results.BUCKET, f"{old_day}/notes.txt"),  # not a copy: never touched
            ("generated-outputs", self.old_path),  # same name, another bucket: never touched
        ):
            self.db.files[key] = b"jpeg"

    def run_cleanup(self) -> int:
        with quiet():
            return tryon_results.delete_expired(self.db, now=self.now)

    def test_deletes_only_expired_copies_in_the_results_bucket(self):
        self.assertEqual(self.run_cleanup(), 2)
        remaining = set(self.db.files)
        self.assertNotIn((tryon_results.BUCKET, self.old_path), remaining)
        self.assertNotIn((tryon_results.BUCKET, self.orphan_path), remaining)
        self.assertIn((tryon_results.BUCKET, self.new_path), remaining)
        self.assertIn((tryon_results.BUCKET, f"{self.old_path.split('/')[0]}/notes.txt"), remaining)
        self.assertIn(("generated-outputs", self.old_path), remaining)

        rows = {row["id"]: row for row in self.db.tables["generation_attempts"]}
        self.assertIsNone(rows[self.old_id]["result_copy_path"])
        self.assertIsNone(rows[self.old_id]["result_copy_saved_at"])
        self.assertEqual(rows[self.new_id]["result_copy_path"], self.new_path)
        self.assertEqual(self.run_cleanup(), 0)  # nothing left to do

    def test_a_link_to_something_that_is_not_a_copy_is_cleared_but_never_deleted(self):
        self.db.tables["generation_attempts"][0]["result_copy_path"] = f"{SHOP}/{LOOK}/output_v1.png"
        self.db.files[(tryon_results.BUCKET, f"{SHOP}/{LOOK}/output_v1.png")] = b"x"
        self.run_cleanup()
        self.assertIn((tryon_results.BUCKET, f"{SHOP}/{LOOK}/output_v1.png"), self.db.files)
        self.assertIsNone(self.db.tables["generation_attempts"][0]["result_copy_path"])

    def test_worker_runs_it_hourly_and_never_raises(self):
        state: dict = {}
        with mock.patch.object(worker, "get_supabase", lambda: self.db), quiet():
            worker.run_attempt_log_housekeeping(state)
            self.assertNotIn((tryon_results.BUCKET, self.old_path), self.db.files)
            self.db.files[(tryon_results.BUCKET, self.old_path)] = b"jpeg"  # not deleted again within the hour
            worker.run_attempt_log_housekeeping(state)
            self.assertIn((tryon_results.BUCKET, self.old_path), self.db.files)
            self.db.missing_columns = {"generation_attempts": ("result_copy_saved_at",)}
            state.clear()
            worker.run_attempt_log_housekeeping(state)  # before the SQL ran: logged, no crash


class AdminCopyTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.attempt_id = str(uuid.uuid4())
        self.copy_path = tryon_results.copy_path(self.attempt_id, attempt_log.utc_now())
        self.db.tables["generation_attempts"] = [
            {"id": self.attempt_id, "shop_id": SHOP, "status": "succeeded", "attempt_type": "tryon",
             "result_copy_path": self.copy_path, "result_copy_saved_at": attempt_log.iso(attempt_log.utc_now())}
        ]
        self.signed: list[tuple[str, list[str], int]] = []

        def fake_sign(bucket, paths, expires_in=3600):
            self.signed.append((bucket, list(paths), expires_in))
            return {path: f"https://signed/{bucket}/{path}" for path in paths}

        for patch in (
            mock.patch.object(admin_api, "sign_existing_paths", fake_sign),
            mock.patch.object(admin_api, "_count_attempts", lambda *args, **kwargs: 1),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_list_shows_a_thumbnail_from_the_private_copy(self):
        item = self.client.get("/admin/attempts").json()["items"][0]
        self.assertEqual(item["thumb_url"], f"https://signed/{tryon_results.BUCKET}/{self.copy_path}")
        self.assertTrue(item["has_result"])
        self.assertNotIn("result_copy_path", item)
        self.assertIn((tryon_results.BUCKET, [self.copy_path], tryon_results.SIGNED_URL_SECONDS), self.signed)

    def test_detail_shows_the_copy_with_a_short_signed_url(self):
        body = self.client.get(f"/admin/attempts/{self.attempt_id}").json()
        self.assertEqual(body["result_copy_url"], f"https://signed/{tryon_results.BUCKET}/{self.copy_path}")
        self.assertEqual(body["result_retention_days"], 7)

    def test_after_cleanup_nothing_is_signed(self):
        self.db.tables["generation_attempts"][0].update(result_copy_path=None, result_copy_saved_at=None)
        body = self.client.get(f"/admin/attempts/{self.attempt_id}").json()
        self.assertIsNone(body["result_copy_url"])
        item = self.client.get("/admin/attempts").json()["items"][0]
        self.assertFalse(item["has_result"])

    def test_shops_never_see_the_copy(self):
        # No shop endpoint reads the bucket or the column: only the admin attempts endpoints do.
        import pathlib

        users = [
            path.name
            for path in pathlib.Path(".").glob("*.py")
            if "tryon_results.BUCKET" in path.read_text(encoding="utf-8-sig") or "result_copy_path" in path.read_text(encoding="utf-8-sig")
        ]
        self.assertEqual(sorted(users), ["admin_api.py", "attempt_log.py", "tryon_results.py"])


if __name__ == "__main__":
    unittest.main()
