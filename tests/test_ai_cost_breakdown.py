"""AI cost split by token type: saving the new columns (or not, before their SQL ran) and the
Operations list tiles / detail breakdown. Mocked Supabase and Gemini: no network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import uuid
from unittest import mock

import admin_api
import attempt_log
import worker
from config import get_settings
from tests import test_shop_image_model as base

SHOP, HERO, FABRIC, GARMENT = base.SHOP, base.HERO, base.FABRIC, base.GARMENT
NB21 = base.NB21
NEW_COLUMNS = ("ai_image_output_tokens", "ai_text_output_tokens")
SQL_FILE = "2026-11-05_ai_cost_breakdown.sql"


class CostColumnsTests(base.ModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.gemini.outcome = fakes.image_response(input_tokens=3771, output_tokens=1120, thoughts=761)
        self.generation_id = str(uuid.uuid4())
        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = lambda params: [
            {"generation_id": self.generation_id, "status": "queued", "credits_used": 50,
             "balance_before": 150, "balance_after": 100}
        ]
        self.db.rpc_handlers["mark_generation_failed_with_refund"] = lambda params: [
            {"refunded": True, "refund_amount": 50, "balance_after": 150}
        ]
        assets = {
            "hero": {"storage_path": f"{SHOP}/hero.jpg", "mime_type": "image/jpeg"},
            "fabrics": [{"storage_path": f"{SHOP}/fabric.jpg", "mime_type": "image/jpeg", "apply_to": "suit_full_body"}],
            "prompt_used": "Dress the model.",
        }
        for patch in (
            mock.patch.object(worker, "get_supabase", lambda: self.db),
            mock.patch.object(worker, "fetch_generation_assets", lambda supabase, job: assets),
            mock.patch.object(worker, "downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(worker, "build_thumbnail_jpeg_bytes", lambda data: b"thumb"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def quick_tryon(self, out: io.StringIO):
        with contextlib.redirect_stdout(out):
            return self.client.post(
                "/tryon/quick/v2",
                data={"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": "true"},
                files={"customer_photo": ("c.jpg", b"photo", "image/jpeg")},
            )

    def run_worker_look(self, out: io.StringIO) -> None:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            response = self.client.post("/generations", json={"hero_image_id": HERO, "fabric_image_id": FABRIC})
            self.assertEqual(response.status_code, 200, response.text)
            self.db.tables["generations"].append(
                {"id": self.generation_id, "shop_id": SHOP, "status": "processing", "model_used": "from-api"}
            )
            worker.process_job(self.gemini, get_settings(), {"id": self.generation_id, "shop_id": SHOP})

    def assert_cost_row(self, row: dict, *, with_columns: bool) -> None:
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual((row["ai_input_tokens"], row["ai_output_tokens"]), (3771, 1881))  # as before
        self.assertEqual(row["ai_cost_inr"], 3.78)
        self.assertEqual(row["image_size"], "1K")  # other optional columns are unaffected
        if with_columns:
            self.assertEqual((row["ai_image_output_tokens"], row["ai_text_output_tokens"]), (1120, 761))
        else:
            for column in NEW_COLUMNS:
                self.assertNotIn(column, row)

    def test_tryon_saves_the_split(self):
        response = self.quick_tryon(io.StringIO())
        self.assertEqual(response.status_code, 200)
        self.assert_cost_row(self.db.only_attempt(), with_columns=True)

    def test_tryon_succeeds_before_the_sql_ran(self):
        self.db.missing_columns = {"generation_attempts": NEW_COLUMNS}
        out = io.StringIO()
        response = self.quick_tryon(out)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"generated-image")
        self.assert_cost_row(self.db.only_attempt(), with_columns=False)
        self.assertIn("WARNING", out.getvalue())
        self.assertIn(SQL_FILE, out.getvalue())

    def test_worker_look_saves_the_split(self):
        self.run_worker_look(io.StringIO())
        self.assertEqual(self.db.tables["generations"][-1]["status"], "done")
        self.assert_cost_row(self.db.only_attempt(), with_columns=True)

    def test_worker_look_succeeds_before_the_sql_ran(self):
        self.db.missing_columns = {"generation_attempts": NEW_COLUMNS}
        out = io.StringIO()
        self.run_worker_look(out)
        self.assertEqual(self.gemini.calls, 1)
        self.assertEqual(self.db.tables["generations"][-1]["status"], "done")
        self.assert_cost_row(self.db.only_attempt(), with_columns=False)
        self.assertIn(SQL_FILE, out.getvalue())

    def test_cost_logging_crash_never_fails_the_generation(self):
        with mock.patch.object(attempt_log, "ai_token_breakdown", side_effect=RuntimeError("boom")):
            response = self.quick_tryon(io.StringIO())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.only_attempt()["status"], "succeeded")


class OperationsCostTests(base.ModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(admin_api, "_count_attempts", lambda *args, **kwargs: 4)  # not under test
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, status: str, cost, **fields) -> str:
        row_id = str(uuid.uuid4())
        self.db.tables.setdefault("generation_attempts", []).append(
            {"id": row_id, "shop_id": SHOP, "status": status, "model_id": NB21, "ai_cost_inr": cost,
             "created_at": attempt_log.iso(attempt_log.utc_now()), **fields}
        )
        return row_id

    def test_list_rows_tiles_sum_and_average(self):
        self.seed("succeeded", 3.78)
        self.seed("succeeded", 4.65)
        self.seed("failed", 1.03)  # the AI was called: its cost counts, but it made no image
        self.seed("succeeded", None)  # no cost recorded: not in the average
        body = self.client.get("/admin/attempts").json()
        self.assertEqual(sorted(item["ai_cost_inr"] for item in body["items"] if item["ai_cost_inr"]), [1.03, 3.78, 4.65])
        summary = body["summary"]
        self.assertEqual(summary["ai_cost_inr"], 9.46)
        self.assertEqual(summary["ai_cost_succeeded_with_cost"], 2)
        self.assertEqual(summary["ai_cost_avg_per_image_inr"], 4.73)  # 9.46 / 2

    def test_tiles_follow_the_shop_filter(self):
        self.seed("succeeded", 3.78)
        self.seed("succeeded", 10.0, shop_id=base.LOOK)  # another shop
        summary = self.client.get(f"/admin/attempts?shop_id={SHOP}").json()["summary"]
        self.assertEqual((summary["ai_cost_inr"], summary["ai_cost_avg_per_image_inr"]), (3.78, 3.78))

    def test_tile_failure_never_breaks_the_list(self):
        self.seed("succeeded", 3.78)
        out = io.StringIO()
        # .gt() is only used by the cost tile query.
        with mock.patch.object(fakes._Query, "gt", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(out):
            response = self.client.get("/admin/attempts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertIsNone(response.json()["summary"]["ai_cost_inr"])
        self.assertIn("AI cost tile not computed", out.getvalue())

    def test_detail_breakdown_for_new_and_old_rows(self):
        new_id = self.seed(
            "succeeded", 3.78, ai_input_tokens=3771, ai_output_tokens=1881,
            ai_image_output_tokens=1120, ai_text_output_tokens=761,
        )
        old_id = self.seed("succeeded", 5.2151, ai_input_tokens=3771, ai_output_tokens=1881)
        new = self.client.get(f"/admin/attempts/{new_id}").json()["ai_cost_breakdown"]
        self.assertEqual(new["method"], "split")
        self.assertEqual((new["image"]["tokens"], new["text"]["tokens"], new["total_inr"]), (1120, 761, 3.78))
        old = self.client.get(f"/admin/attempts/{old_id}").json()["ai_cost_breakdown"]
        self.assertEqual(old, {"method": "old", "total_inr": 5.2151})
