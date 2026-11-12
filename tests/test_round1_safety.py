"""Round 1 safety fixes: AI time limit + single retry + failure codes, stuck-job sweepers, the
prompt wait, one true balance, the storage path check, the pushed try-on clean-up, the worker
start guard and /healthz/queue. Mocked: Supabase and storage (in memory), Gemini. No network,
no real database, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import asyncio
import contextlib
import io
import os
import uuid
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import ai_call
import admin_api
import attempt_log
import credit_balance
import generations_api
import images_api
import main
import queue_health
import supabase_client
import tryon_api
import tryon_results
import worker
import worker_housekeeping
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
OTHER_SHOP = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
GARMENT = "44444444-4444-4444-8444-444444444444"
HERO = "55555555-5555-4555-8555-555555555555"
FABRIC = "66666666-6666-4666-8666-666666666666"
LOOK = "77777777-7777-4777-8777-777777777777"
CUSTOMER_PHOTO = b"customer-photo-bytes-never-stored"


@contextlib.contextmanager
def silent():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def google(code: int) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "upstream", "status": "X"}})


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


class SeqGemini:
    """Gemini fake that gives each outcome in turn (sync and aio), counting calls."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        fake = self

        def next_outcome():
            outcome = fake.outcomes[min(fake.calls, len(fake.outcomes) - 1)]
            fake.calls += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        class _Models:
            def generate_content(self, **_kwargs):
                return next_outcome()

        class _AioModels:
            async def generate_content(self, **_kwargs):
                return next_outcome()

        self.models = _Models()
        self.aio = SimpleNamespace(models=_AioModels())


# ================================================================ PART 1: retry rules

class RetryRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(ai_call, "RETRY_DELAY_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def run_calls(self, *outcomes, retry_on_timeout: bool):
        state = {"n": 0}

        def fn():
            outcome = outcomes[min(state["n"], len(outcomes) - 1)]
            state["n"] += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with silent():
            try:
                result = ai_call.call_with_retry(fn, retry_on_timeout=retry_on_timeout)
            except Exception as exc:  # noqa: BLE001
                result = exc
        return result, state["n"]

    def test_busy_google_codes_are_retried_once(self):
        for code in (429, 500, 502, 503, 504):
            for retry_on_timeout in (True, False):
                with self.subTest(code=code, retry_on_timeout=retry_on_timeout):
                    self.assertEqual(self.run_calls(google(code), "ok", retry_on_timeout=retry_on_timeout), ("ok", 2))

    def test_connection_errors_are_retried_once(self):
        for exc in (httpx.ConnectError("refused"), httpx.ReadError("reset"), httpx.RemoteProtocolError("bye")):
            for retry_on_timeout in (True, False):
                with self.subTest(exc=type(exc).__name__, retry_on_timeout=retry_on_timeout):
                    self.assertEqual(self.run_calls(exc, "ok", retry_on_timeout=retry_on_timeout), ("ok", 2))

    def test_timeout_is_retried_only_when_allowed(self):
        self.assertEqual(self.run_calls(httpx.ReadTimeout("slow"), "ok", retry_on_timeout=True), ("ok", 2))
        result, calls = self.run_calls(httpx.ReadTimeout("slow"), "ok", retry_on_timeout=False)
        self.assertIsInstance(result, httpx.ReadTimeout)
        self.assertEqual(calls, 1)
        result, calls = self.run_calls(asyncio.TimeoutError(), "ok", retry_on_timeout=False)
        self.assertEqual(calls, 1)

    def test_other_errors_are_never_retried(self):
        for exc in (google(400), google(403), google(404), RuntimeError("bad prompt"), ValueError("x")):
            with self.subTest(exc=repr(exc)):
                result, calls = self.run_calls(exc, "ok", retry_on_timeout=True)
                self.assertIs(result, exc)
                self.assertEqual(calls, 1)

    def test_never_more_than_one_retry(self):
        result, calls = self.run_calls(google(503), google(503), google(503), "ok", retry_on_timeout=True)
        self.assertIsInstance(result, genai_errors.APIError)
        self.assertEqual(calls, 2)

    def test_async_follows_the_same_rules(self):
        async def run(*outcomes, retry_on_timeout):
            state = {"n": 0}

            async def fn():
                outcome = outcomes[min(state["n"], len(outcomes) - 1)]
                state["n"] += 1
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            try:
                result = await ai_call.call_with_retry_async(fn, retry_on_timeout=retry_on_timeout)
            except Exception as exc:  # noqa: BLE001
                result = exc
            return result, state["n"]

        with silent():
            self.assertEqual(asyncio.run(run(google(503), "ok", retry_on_timeout=False)), ("ok", 2))
            self.assertEqual(asyncio.run(run(httpx.ReadTimeout("slow"), "ok", retry_on_timeout=False))[1], 1)
            self.assertEqual(asyncio.run(run(google(503), google(503), "ok", retry_on_timeout=False))[1], 2)
            self.assertEqual(asyncio.run(run(google(400), "ok", retry_on_timeout=False))[1], 1)

    def test_wrapped_transport_error_counts_as_busy(self):
        try:
            try:
                raise httpx.ConnectError("refused")
            except httpx.ConnectError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            self.assertEqual(ai_call.busy_kind(outer), "connection")

    def test_time_limit_is_in_milliseconds(self):
        self.assertEqual(get_settings().GEMINI_TIMEOUT_SECONDS, 150)
        self.assertEqual(ai_call.http_options().timeout, 150_000)
        with mock.patch.object(ai_call, "get_settings", lambda: SimpleNamespace(GEMINI_TIMEOUT_SECONDS=2.5)):
            self.assertEqual(ai_call.http_options().timeout, 2500)

    def test_tryon_client_gets_the_time_limit(self):
        created = []
        old = tryon_api._gemini_client
        tryon_api._gemini_client = None
        self.addCleanup(setattr, tryon_api, "_gemini_client", old)
        with mock.patch.object(tryon_api.genai, "Client", lambda **kwargs: created.append(kwargs) or "client"):
            self.assertEqual(tryon_api._get_gemini_client(), "client")
        self.assertEqual(created[0]["http_options"].timeout, 150_000)


# ================================================================ PART 1: worker looks

class WorkerAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        attempt_log.WorkerAttempt.LOOKUP_DELAY_SECONDS = 0
        self.generation_id = str(uuid.uuid4())
        self.generation = {"id": self.generation_id, "shop_id": SHOP, "status": "processing"}
        self.db.tables["generations"] = [self.generation]
        self.row = {
            "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "attempt_type": "look", "status": "running",
            "generation_id": self.generation_id, "shop_id": SHOP, "credits_charged": 50,
            "started_at": ago(seconds=40), "created_at": ago(seconds=40),
        }
        self.db.tables["generation_attempts"] = [self.row]
        self.refund_calls = 0

        def mark_failed(params):
            self.refund_calls += 1
            for gen in self.db.tables["generations"]:
                if gen["id"] == params["p_generation_id"]:
                    gen.update(status="failed", error=params["p_error"])
            return [{"refunded": True, "refund_amount": 50, "balance_after": 150}]

        self.db.rpc_handlers["mark_generation_failed_with_refund"] = mark_failed
        assets = {
            "hero": {"storage_path": f"{SHOP}/hero.jpg", "mime_type": "image/jpeg"},
            "fabrics": [{"storage_path": f"{SHOP}/fabric.jpg", "mime_type": "image/jpeg", "apply_to": "suit_full_body"}],
            "prompt_used": "Dress the model.",
        }
        patches = [
            mock.patch.object(ai_call, "RETRY_DELAY_SECONDS", 0),
            mock.patch.object(worker, "get_supabase", lambda: self.db),
            mock.patch.object(worker, "fetch_generation_assets", lambda supabase, job: assets),
            mock.patch.object(worker, "downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(worker, "build_thumbnail_jpeg_bytes", lambda data: b"thumb"),
            mock.patch.object(worker, "_error_code_warned", False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_job(self, gemini) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            worker.process_job(gemini, get_settings(), {"id": self.generation_id, "shop_id": SHOP})
        return out.getvalue()

    def test_busy_then_ok_succeeds_on_the_one_retry(self):
        gemini = SeqGemini(google(503), fakes.image_response())
        self.run_job(gemini)
        self.assertEqual(gemini.calls, 2)
        self.assertEqual(self.generation["status"], "done")
        self.assertEqual(self.refund_calls, 0)

    def test_timeout_is_retried_once_then_ai_busy(self):
        gemini = SeqGemini(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), fakes.image_response())
        self.run_job(gemini)
        self.assertEqual(gemini.calls, 2)
        self.assertEqual(self.generation["status"], "failed")
        self.assertEqual(self.generation["error_code"], "ai_busy")
        self.assertEqual(self.refund_calls, 1)  # refunded once, charged never (the API charged)
        self.assertEqual((self.row["status"], self.row["failed_step"]), ("failed", "ai_call"))

    def test_refused_or_no_image_is_never_retried(self):
        for outcome, code, step in (
            (fakes.blocked_response(), "ai_blocked", "ai_blocked"),
            (fakes.empty_response(), "ai_no_image", "ai_no_image"),
        ):
            with self.subTest(code=code):
                self.generation.update(status="processing", error_code=None)
                self.row.update(status="running", failed_step=None)
                gemini = SeqGemini(outcome, fakes.image_response())
                self.run_job(gemini)
                self.assertEqual(gemini.calls, 1)
                self.assertEqual(self.generation["error_code"], code)
                self.assertEqual(self.row["failed_step"], step)  # attempt log steps unchanged

    def test_failures_not_about_the_ai_leave_error_code_empty(self):
        self.db.storage_fail[("generated-outputs", "upload")] = "bucket unavailable"
        self.run_job(SeqGemini(fakes.image_response()))
        self.assertEqual(self.generation["status"], "failed")
        self.assertIsNone(self.generation.get("error_code"))
        self.assertEqual(self.row["failed_step"], "storage_save")

    def test_other_ai_errors_leave_error_code_empty(self):
        gemini = SeqGemini(google(400), fakes.image_response())
        self.run_job(gemini)
        self.assertEqual(gemini.calls, 1)
        self.assertIsNone(self.generation.get("error_code"))

    def test_missing_error_code_column_warns_once_and_carries_on(self):
        self.db.missing_columns = {"generations": ("error_code",)}
        out = self.run_job(SeqGemini(fakes.empty_response()))
        self.assertEqual(self.generation["status"], "failed")
        self.assertEqual(self.refund_calls, 1)
        self.assertIn("generations.error_code missing", out)
        self.generation["status"] = "processing"
        self.row.update(status="running")
        out = self.run_job(SeqGemini(fakes.empty_response()))
        self.assertNotIn("generations.error_code missing", out)
        self.assertEqual(self.refund_calls, 2)


# ================================================================ PART 1: try-on routes

class TryonAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update(
            {
                "shops": [{"id": SHOP, "name": "Demo"}],
                "garment_types": [
                    {
                        "id": GARMENT, "shop_id": SHOP, "name": "IndoWestern", "default_hero_image_id": HERO,
                        "tryon_prompt": "Put the {garment_name} on the customer.",
                        "look_tryon_prompt": "Put this look on the customer.",
                    }
                ],
                "hero_images": [
                    {"id": HERO, "shop_id": SHOP, "folder_id": GARMENT, "storage_path": f"{SHOP}/hero.jpg", "mime_type": "image/jpeg"}
                ],
                "fabric_images": [{"id": FABRIC, "shop_id": SHOP, "storage_path": f"{SHOP}/fabric.jpg", "mime_type": "image/jpeg"}],
                "garment_fabric_slots": [],
                "generations": [
                    {"id": LOOK, "shop_id": SHOP, "status": "done", "generation_type": "look",
                     "output_path": f"{SHOP}/{LOOK}/output_v1.png", "folder_id": GARMENT}
                ],
            }
        )
        fakes.install_tryon_credits(self.db, balance=150)
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        tryon_api._storage_bytes_cache.clear()
        self.gemini = SeqGemini(fakes.image_response())
        patches = [
            mock.patch.object(ai_call, "RETRY_DELAY_SECONDS", 0),
            mock.patch.object(supabase_client, "RETRY_DELAY_SECONDS", 0),
            mock.patch.object(tryon_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(generations_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(tryon_api, "_get_gemini_client", lambda: self.gemini),
            mock.patch.object(tryon_api, "_downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(tryon_results, "save_copy", lambda *args, **kwargs: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def ledger(self, reason: str) -> list[dict]:
        return [row for row in self.db.tables["credit_ledger"] if row["reason"] == reason]

    def routes(self):
        photo = {"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")}
        return {
            "/tryon/": lambda: self.client.post(
                "/tryon/", json={"generation_id": LOOK, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}
            ),
            "/tryon/quick": lambda: self.client.post(
                "/tryon/quick",
                json={"fabric_image_id": FABRIC, "folder_id": GARMENT, "customer_photo_b64": "Y3VzdG9tZXI=",
                      "consent_confirmed": True},
            ),
            "/tryon/v2": lambda: self.client.post(
                "/tryon/v2", data={"generation_id": LOOK, "consent_confirmed": "true"}, files=photo
            ),
            "/tryon/quick/v2": lambda: self.client.post(
                "/tryon/quick/v2", data={"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": "true"},
                files=photo,
            ),
            "/tryon/multi/v2": lambda: self.client.post(
                "/tryon/multi/v2",
                data={"hero_image_id": HERO, "folder_id": GARMENT, "consent_confirmed": "true",
                      "fabric_image_id_1": FABRIC, "apply_to_1": "suit_full_body"},
                files=photo,
            ),
        }

    def send(self, route: str):
        with silent():
            return self.routes()[route]()

    def test_busy_after_the_retry_is_503_ai_busy_on_all_five_routes(self):
        for route in self.routes():
            with self.subTest(route=route):
                self.db.tables["generation_attempts"] = []
                self.gemini = SeqGemini(google(503), google(503), fakes.image_response())
                debits, refunds = len(self.ledger("web_tryon")), len(self.ledger("web_tryon_refund"))
                response = self.send(route)
                self.assertEqual(response.status_code, 503)
                body = response.json()
                self.assertEqual(body["detail"], ai_call.AI_BUSY_DETAIL)
                self.assertIsInstance(body["detail"], str)
                self.assertEqual(body["error_code"], "ai_busy")
                self.assertIn("request_id", body)  # the attempt-log middleware still adds it
                self.assertEqual(self.gemini.calls, 2)
                self.assertEqual(len(self.ledger("web_tryon")), debits + 1)  # charged once
                self.assertEqual(len(self.ledger("web_tryon_refund")), refunds + 1)  # refunded once
                row = self.db.only_attempt()
                self.assertEqual((row["status"], row["failed_step"], row["http_status"]), ("failed", "ai_call", 503))
                self.assertEqual(row["user_message"], ai_call.AI_BUSY_DETAIL)

    def test_timeout_is_not_retried_and_is_not_a_connection_problem(self):
        for route in ("/tryon/quick", "/tryon/quick/v2"):
            with self.subTest(route=route):
                self.gemini = SeqGemini(httpx.ReadTimeout("slow"), fakes.image_response())
                response = self.send(route)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error_code"], "ai_busy")
                self.assertNotEqual(response.json()["detail"], supabase_client.CONNECTION_PROBLEM_DETAIL)
                self.assertEqual(self.gemini.calls, 1)

    def test_dropped_connection_then_ok_is_charged_once_and_not_refunded(self):
        self.gemini = SeqGemini(httpx.ConnectError("refused"), fakes.image_response())
        response = self.send("/tryon/quick/v2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.gemini.calls, 2)
        self.assertEqual(len(self.ledger("web_tryon")), 1)
        self.assertEqual(self.ledger("web_tryon_refund"), [])

    def test_blocked_and_no_image_keep_502_and_the_text_and_add_the_code(self):
        for outcome, code in ((fakes.blocked_response(), "ai_blocked"), (fakes.empty_response(), "ai_no_image")):
            for route in ("/tryon/quick", "/tryon/multi/v2"):
                with self.subTest(code=code, route=route):
                    self.gemini = SeqGemini(outcome, fakes.image_response())
                    response = self.send(route)
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.json()["detail"], tryon_api._NO_IMAGE_DETAIL)
                    self.assertEqual(response.json()["error_code"], code)
                    self.assertEqual(self.gemini.calls, 1)  # a refusal is never retried

    def test_other_errors_keep_their_old_shape(self):
        self.db.tables["credit_ledger"][-1]["balance_after"] = 10
        response = self.send("/tryon/quick/v2")
        self.assertEqual(response.status_code, 402)
        self.assertNotIn("error_code", response.json())

    def test_push_to_screen_uses_the_shared_marker(self):
        self.assertEqual(tryon_results.PUSHED_TRYON_PROMPT, "web-app try-on (pushed to screen)")
        with silent():
            response = self.client.post(
                "/tryon/push-to-screen",
                data={"folder_id": GARMENT},
                files={"result_image": ("r.png", b"png-bytes", "image/png")},
            )
        self.assertEqual(response.status_code, 200)
        row = next(g for g in self.db.tables["generations"] if g["id"] == response.json()["generation_id"])
        self.assertEqual(row["prompt_used"], tryon_results.PUSHED_TRYON_PROMPT)


# ================================================================ PART 2a: stuck looks

class StuckLookSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        self.calls: list[str] = []
        self.refuse: set[str] = set()
        self.broken: set[str] = set()

        def mark_failed(params):
            gid = params["p_generation_id"]
            self.calls.append(gid)
            self.assertEqual(params["p_error"], "Timed out: the worker stopped before finishing")
            if gid in self.refuse:
                raise fakes.FakeDBError("P0001: Cannot refund a completed generation")
            if gid in self.broken:
                raise fakes.FakeDBError("statement timeout")
            return [{"refunded": True, "refund_amount": 50, "balance_after": 150}]

        self.db.rpc_handlers["mark_generation_failed_with_refund"] = mark_failed
        self.db.tables["generations"] = []
        self.db.tables["generation_attempts"] = []

    def look(self, status: str, started_minutes: float) -> str:
        gid = str(uuid.uuid4())
        self.db.tables["generations"].append(
            {"id": gid, "shop_id": SHOP, "status": status, "started_at": ago(minutes=started_minutes)}
        )
        self.db.tables["generation_attempts"].append(
            {"id": str(uuid.uuid4()), "generation_id": gid, "status": "timed_out", "failed_step": "timeout",
             "started_at": ago(minutes=started_minutes), "attempt_type": "look"}
        )
        return gid

    def attempt_for(self, gid: str) -> dict:
        return next(row for row in self.db.tables["generation_attempts"] if row["generation_id"] == gid)

    def sweep(self, active=()):
        with silent():
            return worker_housekeeping.sweep_stuck_looks(self.db, active)

    def test_only_processing_rows_older_than_15_minutes_not_in_flight(self):
        old = self.look("processing", 16)
        young = self.look("processing", 14)
        done = self.look("done", 60)
        queued = self.look("queued", 60)
        active = self.look("processing", 30)
        self.assertEqual(self.sweep(active={active}), 1)
        self.assertEqual(self.calls, [old])
        row = self.attempt_for(old)
        self.assertEqual((row["status"], row["failed_step"], row["credits_refunded"]), ("failed", "timeout", 50))
        for untouched in (young, done, queued, active):
            self.assertNotIn("credits_refunded", self.attempt_for(untouched))

    def test_completed_refusal_is_nothing_to_do_and_one_bad_row_does_not_stop_others(self):
        finished = self.look("processing", 20)
        broken = self.look("processing", 19)
        fine = self.look("processing", 18)
        self.refuse.add(finished)
        self.broken.add(broken)
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(sorted(self.calls), sorted([finished, broken, fine]))
        self.assertEqual(self.attempt_for(fine)["status"], "failed")
        self.assertEqual(self.attempt_for(finished)["status"], "timed_out")  # untouched
        self.assertEqual(self.attempt_for(broken)["status"], "timed_out")

    def test_at_most_20_per_run(self):
        for _ in range(25):
            self.look("processing", 30)
        self.assertEqual(self.sweep(), 20)
        self.assertEqual(len(self.calls), 20)

    def test_read_failure_never_raises(self):
        self.db.fail[("generations", "select")] = "down"
        self.assertEqual(self.sweep(), 0)


# ================================================================ PART 2b: missed try-on refunds

class TryonRefundSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        fakes.install_tryon_credits(self.db, balance=10_000)
        self.db.tables["generation_attempts"] = []
        patch = mock.patch.object(supabase_client, "RETRY_DELAY_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def attempt(self, *, status="failed", minutes=11, charged=50, refunded=None, attempt_type="tryon",
                shop=SHOP, created_hours=None, **extra) -> str:
        aid = str(uuid.uuid4())
        row = {
            "id": aid, "request_id": aid, "attempt_type": attempt_type, "status": status, "shop_id": shop,
            "credits_charged": charged, "credits_refunded": refunded, "started_at": ago(minutes=minutes),
            "created_at": ago(hours=created_hours) if created_hours else ago(minutes=minutes),
            "result_copy_path": None, "tryon_result_path": None, **extra,
        }
        self.db.tables["generation_attempts"].append(row)
        if charged and shop:
            self.db.rpc_handlers["debit_credits_for_tryon"]({"p_shop_id": shop, "p_attempt_id": aid, "p_amount": charged})
        return aid

    def row(self, aid: str) -> dict:
        return next(r for r in self.db.tables["generation_attempts"] if r["id"] == aid)

    def refunds(self) -> list[dict]:
        return [r for r in self.db.tables["credit_ledger"] if r["reason"] == "web_tryon_refund"]

    def sweep(self) -> int:
        with silent():
            return worker_housekeeping.sweep_missed_tryon_refunds(self.db)

    def test_selection_rules(self):
        due = [
            self.attempt(status="failed"),
            self.attempt(status="failed", attempt_type="look_tryon"),
            self.attempt(status="timed_out"),
        ]
        not_due = [
            self.attempt(status="failed", minutes=9),  # may still be running: the 10-minute rule
            self.attempt(status="timed_out", minutes=9),
            self.attempt(status="failed", refunded=50),  # already refunded
            self.attempt(status="timed_out", result_copy_path="2026-11-13/x.jpg"),  # result was kept
            self.attempt(status="timed_out", tryon_result_path=f"{SHOP}/g/output.png"),  # pushed to the TV
            self.attempt(status="succeeded"),
            self.attempt(status="running"),
            self.attempt(status="failed", attempt_type="look"),
            self.attempt(status="failed", charged=0),
            self.attempt(status="failed", minutes=26 * 60, created_hours=26),  # older than 24 h
        ]
        no_shop = self.attempt(status="failed", shop=None, charged=50)
        self.assertEqual(self.sweep(), 3)
        for aid in due:
            self.assertEqual(self.row(aid)["credits_refunded"], 50)
        for aid in not_due + [no_shop]:
            self.assertEqual(self.row(aid)["credits_refunded"], 50 if aid == not_due[2] else None)
        self.assertEqual(len(self.refunds()), 3)
        self.assertEqual({r["attempt_id"] for r in self.refunds()}, set(due))

    def test_never_refunds_twice(self):
        aid = self.attempt(status="failed")
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.sweep(), 0)
        self.row(aid)["credits_refunded"] = None  # even if the recorded amount were lost
        self.sweep()
        self.assertEqual(len(self.refunds()), 1)  # the RPC refunds at most once
        self.assertEqual(self.row(aid)["credits_refunded"], 50)

    def test_at_most_20_per_run(self):
        for _ in range(25):
            self.attempt(status="failed")
        self.assertEqual(self.sweep(), 20)

    def test_one_failing_refund_does_not_stop_the_others(self):
        bad = self.attempt(status="failed")
        good = self.attempt(status="failed", minutes=12)
        refund = self.db.rpc_handlers["refund_credits_for_tryon"]

        def flaky(params):
            if params["p_attempt_id"] == bad:
                raise fakes.FakeDBError("statement timeout")
            return refund(params)

        self.db.rpc_handlers["refund_credits_for_tryon"] = flaky
        self.assertEqual(self.sweep(), 1)
        self.assertIsNone(self.row(bad)["credits_refunded"])  # tried again next sweep
        self.assertEqual(self.row(good)["credits_refunded"], 50)

    def test_without_result_copy_path_the_timed_out_case_is_skipped(self):
        failed = self.attempt(status="failed")
        timed_out = self.attempt(status="timed_out")
        self.db.missing_columns = {"generation_attempts": ("result_copy_path",)}
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.row(failed)["credits_refunded"], 50)
        self.assertIsNone(self.row(timed_out)["credits_refunded"])

    def test_housekeeping_runs_both_sweeps_and_never_raises(self):
        aid = self.attempt(status="failed")
        self.db.tables["generations"] = []
        state: dict = {}
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        with mock.patch.object(worker, "get_supabase", lambda: self.db), silent():
            worker.run_attempt_log_housekeeping(state, set())
            self.assertEqual(self.row(aid)["credits_refunded"], 50)
            self.db.fail[("generation_attempts", "*")] = "down"
            self.db.fail[("generations", "*")] = "down"
            state.clear()
            worker.run_attempt_log_housekeeping(state, set())  # logged, no crash


# ================================================================ PART 3: prompt wait

class PromptWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.gid = str(uuid.uuid4())
        self.db.tables["generations"] = [{"id": self.gid, "shop_id": SHOP, "prompt_used": None}]
        self.sleeps: list[float] = []
        patch = mock.patch.object(worker.time, "sleep", lambda seconds: self.sleeps.append(seconds))
        patch.start()
        self.addCleanup(patch.stop)

    def wait(self, created_seconds_ago: float, arrives_after: int = 99):
        reads = {"n": 0}
        original = self.db.table

        def table(name):
            query = original(name)
            if name == "generations":
                reads["n"] += 1
                if reads["n"] >= arrives_after:
                    self.db.tables["generations"][0]["prompt_used"] = "Dress the model."
            return query

        generation = {"prompt_used": None, "created_at": ago(seconds=created_seconds_ago)}
        with mock.patch.object(self.db, "table", table), silent():
            return worker.wait_for_prompt(self.db, generation, generation_id=self.gid, shop_id=SHOP), reads["n"]

    def test_new_row_waits_until_the_prompt_arrives(self):
        prompt, reads = self.wait(5, arrives_after=3)
        self.assertEqual(prompt, "Dress the model.")
        self.assertEqual(reads, 3)
        self.assertEqual(self.sleeps, [2.0, 2.0, 2.0])

    def test_new_row_gives_up_after_5_reads(self):
        prompt, reads = self.wait(5)
        self.assertIsNone(prompt)
        self.assertEqual((reads, len(self.sleeps)), (5, 5))

    def test_old_row_does_not_wait(self):
        prompt, reads = self.wait(31)
        self.assertIsNone(prompt)
        self.assertEqual((reads, self.sleeps), (0, []))

    def test_a_prompt_already_there_is_used_at_once(self):
        prompt = worker.wait_for_prompt(self.db, {"prompt_used": "P", "created_at": ago(seconds=1)},
                                        generation_id=self.gid, shop_id=SHOP)
        self.assertEqual((prompt, self.sleeps), ("P", []))

    def test_fetch_generation_assets_still_fails_as_today_when_it_never_arrives(self):
        self.db.tables["generations"][0]["created_at"] = ago(seconds=2)
        self.db.tables["generations"][0]["hero_image_id"] = HERO
        self.db.tables["hero_images"] = [{"id": HERO, "shop_id": SHOP, "storage_path": "x", "mime_type": "image/jpeg"}]
        self.db.tables["generation_fabrics"] = [
            {"generation_id": self.gid, "shop_id": SHOP, "fabric_image_id": FABRIC, "apply_to": "suit_full_body", "sort_order": 1}
        ]
        self.db.tables["fabric_images"] = [{"id": FABRIC, "shop_id": SHOP, "storage_path": "f", "mime_type": "image/jpeg"}]
        with self.assertRaisesRegex(RuntimeError, "prompt_used is empty"), silent():
            worker.fetch_generation_assets(self.db, {"id": self.gid, "shop_id": SHOP, "hero_image_id": HERO})
        self.assertEqual(len(self.sleeps), 5)


# ================================================================ PART 4: one true balance

class BalanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables["shops"] = [{"id": SHOP, "name": "Demo", "header_display_text": "Hi"}]
        self.db.tables["credit_ledger"] = [
            {"shop_id": SHOP, "delta": 999, "reason": "x", "balance_after": 999, "created_at": ago(minutes=1)}
        ]
        self.balance = 120
        self.db.rpc_handlers["get_shop_credit_balance"] = lambda params: self.balance
        main._me_shop_summary_cache.clear()
        self.addCleanup(main._me_shop_summary_cache.clear)
        patches = [
            mock.patch.object(main, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(credit_balance, "_arg_name", "p_shop_id"),
            mock.patch.object(credit_balance, "_warned", True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_me_uses_the_ledger_total_and_never_caches_it(self):
        body = self.client.get("/me").json()
        self.assertEqual(body["credits_balance"], 120)
        self.assertEqual(
            set(body), {"auth_user_id", "email", "shop_id", "role", "shop_name", "header_display_text", "credits_balance"}
        )
        self.assertEqual((body["shop_name"], body["header_display_text"]), ("Demo", "Hi"))
        self.balance = 70
        self.assertEqual(self.client.get("/me").json()["credits_balance"], 70)
        rpc_calls = [entry for entry in self.db.log if entry[:2] == ("rpc", "get_shop_credit_balance")]
        self.assertEqual(rpc_calls[0][2], {"p_shop_id": SHOP})

    def test_me_falls_back_to_the_newest_ledger_row(self):
        del self.db.rpc_handlers["get_shop_credit_balance"]
        self.assertEqual(self.client.get("/me").json()["credits_balance"], 999)
        self.db.fail[("credit_ledger", "select")] = "down"
        self.assertEqual(self.client.get("/me").json()["credits_balance"], 0)  # as before

    def test_a_different_argument_name_is_found_from_the_hint(self):
        def strict(params):
            if "shop_uuid" not in params:
                raise fakes.FakeDBError(
                    "PGRST202 Could not find the function public.get_shop_credit_balance(p_shop_id). "
                    "Perhaps you meant to call the function public.get_shop_credit_balance(shop_uuid)"
                )
            return [{"get_shop_credit_balance": 33}]

        self.db.rpc_handlers["get_shop_credit_balance"] = strict
        self.assertEqual(credit_balance.rpc_balance(self.db, SHOP), 33)
        self.assertEqual(credit_balance._arg_name, "shop_uuid")
        self.assertEqual(credit_balance.rpc_balance(self.db, SHOP), 33)

    def test_null_balance_is_zero(self):
        self.balance = None
        self.assertEqual(credit_balance.rpc_balance(self.db, SHOP), 0)

    def test_admin_balance_uses_the_rpc_with_the_same_fallback(self):
        self.assertEqual(admin_api._get_shop_balance(self.db, SHOP), 120)
        del self.db.rpc_handlers["get_shop_credit_balance"]
        self.assertEqual(admin_api._get_shop_balance(self.db, SHOP), 999)


# ================================================================ PART 5: storage path check

class StoragePathTests(unittest.TestCase):
    def test_rules(self):
        ok = [(f"{SHOP}/fabric.jpg", False), (f"{SHOP}/a/b/c.png", False), (f"temp/{SHOP}/h.jpg", True),
              (f" {SHOP}/x.jpg ", False), (f"{SHOP.upper()}/x.jpg", False)]
        for path, allow_temp in ok:
            with self.subTest(path=path):
                images_api.check_storage_path(path, SHOP, allow_temp=allow_temp)
        bad = [
            (f"{OTHER_SHOP}/x.jpg", True), (f"temp/{SHOP}/h.jpg", False), (f"temp/{OTHER_SHOP}/h.jpg", True),
            (f"{SHOP}/../{OTHER_SHOP}/x.jpg", True), (f"/{SHOP}/x.jpg", True), (f"{SHOP}\\x.jpg", True),
            (f"{SHOP}/a\\b.jpg", True), (f"{SHOP}/", True), (SHOP, True), ("", True), ("x.jpg", True),
            (f"{SHOP}x/y.jpg", True), (f"temp/{SHOP}/", True), (f"{SHOP}/..", True),
        ]
        for path, allow_temp in bad:
            with self.subTest(path=path):
                with self.assertRaises(images_api.HTTPException) as caught:
                    images_api.check_storage_path(path, SHOP, allow_temp=allow_temp)
                self.assertEqual((caught.exception.status_code, caught.exception.detail), (400, "Invalid storage path"))

    def test_endpoints(self):
        db = fakes.FakeSupabase()
        db.tables["garment_types"] = [{"id": GARMENT, "shop_id": SHOP}]
        patches = [
            mock.patch.object(images_api, "get_supabase_admin_client", lambda: db),
            mock.patch.object(images_api, "store_hero_thumbnail_best_effort", lambda *args: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        client = TestClient(app, raise_server_exceptions=False)
        cases = [
            ("/fabric-images", {}, f"{SHOP}/f.jpg", 200), ("/fabric-images", {}, f"{OTHER_SHOP}/f.jpg", 400),
            ("/fabric-images", {}, f"temp/{SHOP}/f.jpg", 400),
            ("/hero-images", {"folder_id": GARMENT}, f"{SHOP}/{GARMENT}/h.jpg", 200),
            ("/hero-images", {"folder_id": GARMENT}, f"temp/{SHOP}/h.jpg", 200),
            ("/hero-images", {"folder_id": GARMENT}, f"temp/{OTHER_SHOP}/h.jpg", 400),
            ("/catalog-images", {"folder_id": GARMENT}, f"{SHOP}/c.jpg", 200),
            ("/catalog-images", {"folder_id": GARMENT}, f"../{SHOP}/c.jpg", 400),
        ]
        for url, extra, path, expected in cases:
            with self.subTest(url=url, path=path):
                response = client.post(url, json={"storage_path": path, **extra})
                self.assertEqual(response.status_code, expected, response.text)
                if expected == 400:
                    self.assertEqual(response.json()["detail"], "Invalid storage path")
        self.assertEqual(len(db.tables.get("fabric_images", [])), 1)  # rejected paths never stored


# ================================================================ PART 6: pushed try-on clean-up

class PushedTryonCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update({"generations": [], "credit_ledger": [], "shop_screen_state": []})

    def gen(self, *, days=8, generation_type="tryon", prompt=tryon_results.PUSHED_TRYON_PROMPT, credits=0,
            path=None, shop=SHOP, with_files=True) -> str:
        gid = str(uuid.uuid4())
        path = path if path is not None else f"{shop}/{gid}/output_v1700000000.png"
        self.db.tables["generations"].append(
            {"id": gid, "shop_id": shop, "generation_type": generation_type, "prompt_used": prompt,
             "credits_used": credits, "output_path": path, "status": "done", "created_at": ago(days=days)}
        )
        if with_files and path:
            self.db.files[("generated-outputs", path)] = b"img"
            self.db.files[("generated-outputs", path.rsplit("/", 1)[0] + "/thumb_v1700000000.jpg")] = b"thumb"
        return gid

    def ids(self) -> set[str]:
        return {row["id"] for row in self.db.tables["generations"]}

    def run_cleanup(self) -> int:
        with silent():
            return worker_housekeeping.delete_expired_pushed_tryons(self.db)

    def test_only_old_pushed_customer_tryons_are_deleted(self):
        target = self.gen()
        self.db.tables["shop_screen_state"].append(
            {"shop_id": SHOP, "live_generation_id": target, "mode": "live", "updated_at": ago(days=1), "slug": "demo"}
        )
        other_screen = {"shop_id": OTHER_SHOP, "live_generation_id": LOOK, "mode": "live", "updated_at": "t"}
        self.db.tables["shop_screen_state"].append(other_screen)
        keep = [
            self.gen(days=6),  # not old enough
            self.gen(generation_type="look", prompt="Dress the model."),  # a look
            self.gen(prompt="whatsapp try-on", credits=50),  # WhatsApp
            self.gen(prompt="whatsapp try-on", credits=0),
            self.gen(prompt="web-app look (pushed to screen)"),  # a pushed look copy
            self.gen(generation_type="look", prompt="web-app look (pushed to screen)"),
            self.gen(credits=50),
        ]
        self.assertEqual(self.run_cleanup(), 1)
        self.assertNotIn(target, self.ids())
        self.assertEqual(self.ids(), set(keep))
        self.assertNotIn(("generated-outputs", f"{SHOP}/{target}/output_v1700000000.png"), self.db.files)
        self.assertNotIn(("generated-outputs", f"{SHOP}/{target}/thumb_v1700000000.jpg"), self.db.files)
        for gid in keep:
            self.assertIn(("generated-outputs", f"{SHOP}/{gid}/output_v1700000000.png"), self.db.files)
        screen = self.db.tables["shop_screen_state"][0]
        self.assertIsNone(screen["live_generation_id"])
        self.assertEqual((screen["mode"], screen["slug"]), ("live", "demo"))  # nothing else touched
        self.assertNotEqual(screen["updated_at"], ago(days=1)[:10])
        self.assertEqual(self.db.tables["shop_screen_state"][1], other_screen)

    def test_a_ledger_row_or_an_odd_path_keeps_the_row_and_its_file(self):
        with_ledger = self.gen()
        self.db.tables["credit_ledger"].append({"shop_id": SHOP, "generation_id": with_ledger, "delta": 0})
        odd = [
            self.gen(path=f"{OTHER_SHOP}/x/output_v1.png"),
            self.gen(path=f"{SHOP}/elsewhere/output_v1.png"),
            self.gen(path=""),
        ]
        nested_id = self.gen(with_files=False)
        nested_path = f"{SHOP}/{nested_id}/sub/output_v1.png"
        next(r for r in self.db.tables["generations"] if r["id"] == nested_id)["output_path"] = nested_path
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(self.ids(), {with_ledger, nested_id, *odd})
        self.assertIn(("generated-outputs", f"{SHOP}/{with_ledger}/output_v1700000000.png"), self.db.files)
        self.assertIn(("generated-outputs", f"{OTHER_SHOP}/x/output_v1.png"), self.db.files)

    def test_path_shape_guard(self):
        gid = str(uuid.uuid4())
        ok = worker_housekeeping.pushed_output_path_ok
        self.assertTrue(ok(f"{SHOP}/{gid}/output_v17.png", SHOP, gid))
        self.assertTrue(ok(f"{SHOP}/{gid}/output.jpg", SHOP, gid))
        for path in (f"{SHOP}/{gid}/thumb_v17.jpg", f"{SHOP}/{gid}/sub/output.png", f"{OTHER_SHOP}/{gid}/output.png",
                     f"{SHOP}/{LOOK}/output.png", f"/{SHOP}/{gid}/output.png", f"{SHOP}/{gid}/output..png",
                     f"x{SHOP}/{gid}/output.png", None, ""):
            with self.subTest(path=path):
                self.assertFalse(ok(path, SHOP, gid))

    def test_batches_skip_kept_rows_and_stop_at_five(self):
        kept = [self.gen(days=30, with_files=False) for _ in range(100)]
        for gid in kept:
            self.db.tables["credit_ledger"].append({"shop_id": SHOP, "generation_id": gid})
        deletable = [self.gen(days=9, with_files=False) for _ in range(50)]
        self.assertEqual(self.run_cleanup(), 50)
        self.assertEqual(self.ids(), set(kept))

        self.db.tables["generations"] = []
        self.db.tables["credit_ledger"] = []
        for _ in range(600):
            self.gen(with_files=False)
        self.assertEqual(self.run_cleanup(), 500)  # 5 batches of 100
        self.assertEqual(len(self.ids()), 100)
        del deletable

    def test_one_failing_row_does_not_stop_the_others(self):
        bad, good = self.gen(days=10), self.gen(days=9)
        real = worker_housekeeping._delete_one_pushed

        def flaky(client, row, now_iso):
            if row["id"] == bad:
                raise fakes.FakeDBError("delete failed")
            return real(client, row, now_iso)

        with mock.patch.object(worker_housekeeping, "_delete_one_pushed", flaky):
            self.assertEqual(self.run_cleanup(), 1)
        self.assertEqual(self.ids(), {bad})
        self.assertNotIn(good, self.ids())

    def test_worker_runs_it_hourly_and_never_raises(self):
        target = self.gen()
        self.db.tables["generation_attempts"] = []
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        state: dict = {}
        with mock.patch.object(worker, "get_supabase", lambda: self.db), silent():
            worker.run_attempt_log_housekeeping(state, set())
            self.assertNotIn(target, self.ids())
            again = self.gen()
            worker.run_attempt_log_housekeeping(state, set())  # not again within the hour
            self.assertIn(again, self.ids())
            self.db.fail[("generations", "*")] = "down"
            state.clear()
            worker.run_attempt_log_housekeeping(state, set())


# ================================================================ PART 8: worker start guard

class WorkerStartGuardTests(unittest.TestCase):
    def test_decision(self):
        reason = worker.worker_start_reason
        self.assertEqual(reason({"RENDER": "true"}), "render")
        self.assertEqual(reason({"RENDER_SERVICE_ID": "srv-1"}), "render")
        self.assertEqual(reason({"RENDER_INSTANCE_ID": "i-1"}), "render")
        self.assertEqual(reason({"WORKER_ALLOW_LOCAL": "true"}), "WORKER_ALLOW_LOCAL=true")
        self.assertEqual(reason({"WORKER_ALLOW_LOCAL": " TRUE "}), "WORKER_ALLOW_LOCAL=true")
        self.assertEqual(reason({}, {"WORKER_ALLOW_LOCAL": "true"}), "WORKER_ALLOW_LOCAL=true")
        for env in ({}, {"RENDER": ""}, {"WORKER_ALLOW_LOCAL": "false"}, {"WORKER_ALLOW_LOCAL": "1"},
                    {"WORKER_ALLOW_LOCAL": "yes"}, {"WHATSAPP_WATCHER_ENABLED": "false"}):
            with self.subTest(env=env):
                self.assertIsNone(reason(env))
        self.assertIsNone(reason({"WORKER_ALLOW_LOCAL": "false"}, {"WORKER_ALLOW_LOCAL": "true"}))

    def test_main_refuses_to_start_on_a_laptop_before_anything_else(self):
        out = io.StringIO()
        clean_env = {k: v for k, v in os.environ.items() if k not in ("RENDER", "RENDER_SERVICE_ID",
                                                                     "RENDER_INSTANCE_ID", "WORKER_ALLOW_LOCAL")}
        with mock.patch.dict(os.environ, clean_env, clear=True), \
                mock.patch.object(worker, "_local_dotenv", lambda: {}), \
                mock.patch.object(worker, "get_settings", side_effect=AssertionError("must not get this far")), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                worker.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(
            "[worker] NOT STARTING: this machine is not Render. A local worker would take real shops' jobs. "
            "Set WORKER_ALLOW_LOCAL=true only if you really mean it.",
            out.getvalue(),
        )

    def test_logs_which_reason_allowed_it(self):
        for env, text in (({"RENDER": "true"}, "running on Render"), ({"WORKER_ALLOW_LOCAL": "true"}, "WORKER_ALLOW_LOCAL=true")):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(worker, "_local_dotenv", lambda: {}), \
                    contextlib.redirect_stdout(out):
                worker.enforce_worker_start_guard()
            self.assertIn(text, out.getvalue())


# ================================================================ PART 7: /healthz/queue

class QueueHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update({"generations": [], "generation_attempts": []})
        queue_health.clear_cache()
        self.addCleanup(queue_health.clear_cache)

    def look(self, status: str, **times) -> None:
        self.db.tables["generations"].append(
            {"id": str(uuid.uuid4()), "shop_id": SHOP, "status": status,
             **{key: ago(minutes=value) for key, value in times.items()}}
        )

    def attempt(self, status: str, minutes: float = 5, failed_step=None) -> None:
        self.db.tables["generation_attempts"].append(
            {"id": str(uuid.uuid4()), "status": status, "failed_step": failed_step,
             "created_at": ago(minutes=minutes), "started_at": ago(minutes=minutes), "shop_id": SHOP}
        )

    def evaluate(self):
        with silent():
            return queue_health.evaluate(self.db)

    def test_all_quiet_is_ok(self):
        self.look("queued", created_at=2)
        self.look("processing", created_at=25, started_at=19)
        self.look("done", created_at=100, started_at=100)
        self.attempt("failed")
        self.attempt("failed")
        self.attempt("succeeded")
        self.assertEqual(self.evaluate(), (True, [], {}))

    def test_queued_over_3_minutes(self):
        self.look("queued", created_at=4)
        ok, reasons, counts = self.evaluate()
        self.assertFalse(ok)
        self.assertEqual(reasons, ["looks_queued_over_3_min"])
        self.assertEqual(counts, {"looks_queued_over_3_min": 1})

    def test_processing_over_20_minutes(self):
        self.look("processing", created_at=30, started_at=21)
        self.look("processing", created_at=30, started_at=25)
        ok, reasons, counts = self.evaluate()
        self.assertEqual((ok, reasons, counts), (False, ["looks_processing_over_20_min"], {"looks_processing_over_20_min": 2}))

    def test_every_recent_attempt_failed(self):
        self.attempt("failed")
        self.attempt("timed_out")
        self.attempt("failed", failed_step="ai_call")
        self.attempt("failed", minutes=20)  # outside the 15 minutes: ignored either way
        ok, reasons, _counts = self.evaluate()
        self.assertEqual((ok, reasons), (False, ["all_recent_attempts_failed"]))

    def test_user_side_failures_are_not_counted(self):
        for step in ("validation", "auth", "credits", "client_network"):
            self.attempt("failed", failed_step=step)
        self.attempt("failed", failed_step="ai_call")
        self.attempt("failed", failed_step="ai_call")
        self.assertTrue(self.evaluate()[0])  # only 2 counted: below 3
        self.attempt("failed", failed_step="ai_call")
        self.assertFalse(self.evaluate()[0])
        self.attempt("running", minutes=1)
        self.assertTrue(self.evaluate()[0])  # one still in progress: not "every one failed"

    def test_database_unreadable(self):
        self.db.fail[("generations", "select")] = "down"
        self.assertEqual(self.evaluate(), (False, ["database"], {}))

    def test_endpoint_is_cached_hidden_and_never_shows_ids(self):
        self.look("queued", created_at=10)
        with mock.patch.object(main, "get_supabase_admin_client", lambda: self.db), silent():
            client = TestClient(app)
            first = client.get("/healthz/queue")
            self.assertEqual(first.status_code, 503)
            self.assertEqual(first.json()["ok"], False)
            self.assertEqual(first.json()["reasons"], ["looks_queued_over_3_min"])
            self.assertNotIn(SHOP, first.text)
            self.db.tables["generations"] = []
            reads = len(self.db.log)
            self.assertEqual(client.get("/healthz/queue").status_code, 503)  # cached for 60 s
            self.assertEqual(len(self.db.log), reads)
            queue_health.clear_cache()
            second = client.get("/healthz/queue")
            self.assertEqual((second.status_code, second.json()), (200, {"ok": True}))
            self.assertNotIn("/healthz/queue", client.get("/openapi.json").json()["paths"])
            self.assertEqual(client.get("/healthz").status_code in (200, 500), True)  # unchanged route still there

    def test_cache_expires_after_60_seconds(self):
        clock = {"t": 1000.0}
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return self.db

        with silent():
            queue_health.cached_result(factory, clock=lambda: clock["t"])
            clock["t"] += 59
            queue_health.cached_result(factory, clock=lambda: clock["t"])
            self.assertEqual(calls["n"], 1)
            clock["t"] += 2
            queue_health.cached_result(factory, clock=lambda: clock["t"])
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
