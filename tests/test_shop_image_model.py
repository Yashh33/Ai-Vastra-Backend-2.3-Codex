"""Per-shop image model and the 1K output size, across worker, try-on, WhatsApp and admin.
Mocked: Supabase (in memory), Gemini, WhatsApp transport. No network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

import admin_api
import attempt_log
import generations_api
import image_models
import tryon_api
import whatsapp_state
import worker
from admin_deps import verify_admin_secret
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
USER = "33333333-3333-4333-8333-333333333333"
GARMENT = "44444444-4444-4444-8444-444444444444"
HERO = "55555555-5555-4555-8555-555555555555"
FABRIC = "66666666-6666-4666-8666-666666666666"
LOOK = "77777777-7777-4777-8777-777777777777"
NB2 = "gemini-3.1-flash-image-preview"
NB21 = "gemini-nano-banana-2.1"
MISSING_COLUMNS = {"shops": ("image_model_id",), "generation_attempts": ("image_size",)}


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ModelTestCase(unittest.TestCase):
    """The env default is NB 2.1 in every test here, so NB 2 can only come from the shop."""

    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update(
            {
                "shops": [{"id": SHOP, "name": "Demo", "image_model_id": None}],
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
                "generations": [
                    {"id": LOOK, "shop_id": SHOP, "status": "done", "generation_type": "look",
                     "output_path": f"{SHOP}/{LOOK}/output_v1.png", "folder_id": GARMENT, "model_used": NB21,
                     "original_output_path": None}
                ],
            }
        )
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        attempt_log.WorkerAttempt.LOOKUP_DELAY_SECONDS = 0
        tryon_api._storage_bytes_cache.clear()
        self.gemini = fakes.FakeGemini(fakes.image_response())
        patches = [
            mock.patch.object(get_settings(), "GEMINI_IMAGE_MODEL_ID", NB21),
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

    def set_shop_model(self, value) -> None:
        self.db.tables["shops"][0]["image_model_id"] = value

    def assert_gemini_called_with(self, model: str, *, aspect_ratio=None) -> None:
        request = self.gemini.requests[-1]
        self.assertEqual(request["model"], model)
        self.assertEqual(request["config"].image_config.image_size, "1K")
        self.assertEqual(request["config"].image_config.aspect_ratio, aspect_ratio)

    def only_attempt_row(self) -> dict:
        return self.db.only_attempt()


class ResolveTests(ModelTestCase):
    def test_no_shop_setting_uses_the_env_model(self):
        self.assertEqual(image_models.resolve_image_model(self.db, SHOP), NB21)

    def test_shop_setting_beats_the_env_model(self):
        self.set_shop_model(NB2)
        self.assertEqual(image_models.resolve_image_model(self.db, SHOP), NB2)

    def test_unknown_stored_value_uses_the_env_model_and_warns(self):
        self.set_shop_model("gemini-made-up")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(image_models.resolve_image_model(self.db, SHOP), NB21)
        self.assertIn("WARNING", out.getvalue())
        self.assertIn("gemini-made-up", out.getvalue())

    def test_missing_column_uses_the_env_model_and_warns(self):
        self.db.missing_columns = MISSING_COLUMNS
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(image_models.resolve_image_model(self.db, SHOP), NB21)
        self.assertIn("WARNING", out.getvalue())

    def test_cost_uses_the_nb21_prices(self):
        # (1000 x 1.50 + 1290 x 30) / 1M USD x 84 INR
        self.assertAlmostEqual(attempt_log.ai_cost_inr(NB21, 1000, 1290), 3.38, places=2)
        self.assertAlmostEqual(attempt_log.ai_cost_inr(NB2, 1000, 1290), 6.54, places=2)


class TryonTests(ModelTestCase):
    def quick_v2(self):
        return self.client.post(
            "/tryon/quick/v2",
            data={"fabric_image_id": FABRIC, "folder_id": GARMENT, "consent_confirmed": "true"},
            files={"customer_photo": ("c.jpg", b"photo", "image/jpeg")},
        )

    def assert_recorded(self, model: str, cost: float) -> None:
        row = self.only_attempt_row()
        self.assertEqual((row["model_id"], row["image_size"], row["status"]), (model, "1K", "succeeded"))
        self.assertAlmostEqual(row["ai_cost_inr"], cost, places=2)

    def test_shop_model_is_called_at_1k_with_no_aspect_ratio_and_recorded(self):
        self.set_shop_model(NB2)
        self.assertEqual(self.quick_v2().status_code, 200)
        self.assert_gemini_called_with(NB2)
        self.assert_recorded(NB2, 6.5436)

    def test_default_shop_uses_env_model_and_its_prices(self):
        self.assertEqual(self.quick_v2().status_code, 200)
        self.assert_gemini_called_with(NB21)
        self.assert_recorded(NB21, 3.3768)

    def test_sync_quick_endpoint(self):
        self.set_shop_model(NB2)
        body = {"fabric_image_id": FABRIC, "folder_id": GARMENT, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}
        self.assertEqual(self.client.post("/tryon/quick", json=body).status_code, 200)
        self.assert_gemini_called_with(NB2)

    def test_look_tryon_both_endpoints(self):
        self.set_shop_model(NB2)
        body = {"generation_id": LOOK, "customer_photo_b64": "Y3VzdG9tZXI=", "consent_confirmed": True}
        self.assertEqual(self.client.post("/tryon/", json=body).status_code, 200)
        self.assert_gemini_called_with(NB2)
        self.gemini.requests.clear()
        response = self.client.post(
            "/tryon/v2",
            data={"generation_id": LOOK, "consent_confirmed": "true"},
            files={"customer_photo": ("c.jpg", b"photo", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)
        self.assert_gemini_called_with(NB2)

    def test_multi_endpoint(self):
        self.set_shop_model(NB2)
        response = self.client.post(
            "/tryon/multi/v2",
            data={"hero_image_id": HERO, "folder_id": GARMENT, "consent_confirmed": "true",
                  "fabric_image_id_1": FABRIC, "apply_to_1": "suit_full_body"},
            files={"customer_photo": ("c.jpg", b"photo", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_gemini_called_with(NB2)

    def test_gemini_failure_still_records_model_and_size(self):
        self.set_shop_model(NB2)
        self.gemini.outcome = RuntimeError("503 UNAVAILABLE")
        self.assertEqual(self.quick_v2().status_code, 500)
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"], row["image_size"]), ("failed", NB2, "1K"))

    def test_missing_columns_still_generate_on_the_env_model(self):
        self.db.missing_columns = MISSING_COLUMNS
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.quick_v2()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"generated-image")
        self.assert_gemini_called_with(NB21)
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"]), ("succeeded", NB21))
        self.assertNotIn("image_size", row)
        self.assertIn("attempt_log write failed", out.getvalue())

    def test_push_to_screen_records_the_shop_model(self):
        self.set_shop_model(NB2)
        response = self.client.post(
            "/tryon/push-to-screen",
            data={"folder_id": GARMENT},
            files={"result_image": ("r.png", b"generated-image", "image/png")},
        )
        self.assertEqual(response.status_code, 200)
        row = next(r for r in self.db.tables["generations"] if r["id"] == response.json()["generation_id"])
        self.assertEqual(row["model_used"], NB2)


class LookPathTests(ModelTestCase):
    def setUp(self) -> None:
        super().setUp()
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
        patches = [
            mock.patch.object(worker, "get_supabase", lambda: self.db),
            mock.patch.object(worker, "fetch_generation_assets", lambda supabase, job: assets),
            mock.patch.object(worker, "downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(worker, "build_thumbnail_jpeg_bytes", lambda data: b"thumb"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def queue_look(self):
        response = self.client.post("/generations", json={"hero_image_id": HERO, "fabric_image_id": FABRIC})
        self.assertEqual(response.status_code, 200, response.text)
        # The fake RPC makes no row; the real one does, in this state.
        self.db.tables["generations"].append(
            {"id": self.generation_id, "shop_id": SHOP, "status": "processing", "model_used": "from-api"}
        )

    def run_job(self):
        with quiet(), contextlib.redirect_stderr(io.StringIO()):
            worker.process_job(self.gemini, get_settings(), {"id": self.generation_id, "shop_id": SHOP})

    def generation(self) -> dict:
        return next(r for r in self.db.tables["generations"] if r["id"] == self.generation_id)

    def test_worker_calls_shop_model_at_1k_3_4_and_records_it(self):
        self.set_shop_model(NB2)
        self.queue_look()
        self.run_job()

        self.assert_gemini_called_with(NB2, aspect_ratio="3:4")
        self.assertEqual(self.gemini.requests[-1]["config"].response_modalities, ["IMAGE"])
        self.assertEqual(self.generation()["model_used"], NB2)
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"], row["image_size"]), ("succeeded", NB2, "1K"))
        self.assertAlmostEqual(row["ai_cost_inr"], 6.54, places=2)

    def test_admin_change_after_queueing_applies_to_the_job(self):
        self.queue_look()
        self.set_shop_model(NB2)
        self.run_job()
        self.assert_gemini_called_with(NB2, aspect_ratio="3:4")
        self.assertEqual(self.generation()["model_used"], NB2)
        self.assertEqual(self.only_attempt_row()["model_id"], NB2)

    def test_api_records_the_resolved_model_on_the_attempt(self):
        self.set_shop_model(NB2)
        self.queue_look()
        row = self.only_attempt_row()
        self.assertEqual((row["model_id"], row["image_size"]), (NB2, "1K"))

    def test_default_shop_worker_uses_env_model(self):
        self.queue_look()
        self.run_job()
        self.assert_gemini_called_with(NB21, aspect_ratio="3:4")
        self.assertEqual(self.generation()["model_used"], NB21)

    def test_missing_columns_still_generate_and_finish_the_attempt(self):
        self.db.missing_columns = MISSING_COLUMNS
        self.queue_look()
        self.run_job()
        self.assertEqual(self.gemini.calls, 1)
        self.assert_gemini_called_with(NB21, aspect_ratio="3:4")
        self.assertEqual(self.generation()["status"], "done")
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"]), ("succeeded", NB21))
        self.assertNotIn("image_size", row)

    def test_failed_job_still_records_model_and_size(self):
        self.set_shop_model(NB2)
        self.gemini.outcome = RuntimeError("boom")
        self.queue_look()
        self.run_job()
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"], row["image_size"]), ("failed", NB2, "1K"))

    def test_push_look_keeps_the_model_that_made_it(self):
        self.set_shop_model(NB2)  # changed after the look was made with NB 2.1
        response = self.client.post(f"/generations/{LOOK}/push-to-screen")
        self.assertEqual(response.status_code, 200)
        row = next(r for r in self.db.tables["generations"] if r["id"] == response.json()["generation_id"])
        self.assertEqual(row["model_used"], NB21)


class WhatsAppTests(ModelTestCase):
    def setUp(self) -> None:
        super().setUp()
        patches = [
            mock.patch.object(whatsapp_state, "send_text", lambda phone, body: None),
            mock.patch.object(whatsapp_state, "download_media", lambda media_id: (b"photo", "image/jpeg")),
            mock.patch.object(whatsapp_state, "upload_media", lambda data, mime: "media-1"),
            mock.patch.object(whatsapp_state, "send_image_by_media_id", lambda *args, **kwargs: None),
            mock.patch.object(whatsapp_state, "_get_shop_balance", lambda supabase, shop_id: 150),
            mock.patch.object(whatsapp_state, "_update_session", lambda *args, **kwargs: None),
            mock.patch.object(whatsapp_state, "_downscale_image_if_needed", lambda data, mime: (data, mime)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_direct_tryon_uses_the_shop_model_and_records_it(self):
        self.set_shop_model(NB2)
        session = {
            "id": "session-1", "phone_number": "919999999999", "shop_id": SHOP, "folder_id": GARMENT,
            "hero_image_id": HERO, "fabric_image_id": FABRIC, "pending_fabrics": [],
            "active_generation_id": None, "free_generations_used": 0,
        }
        whatsapp_state.run_tryon_for_session(self.db, session, "media-id", "image/jpeg")

        self.assert_gemini_called_with(NB2)
        row = self.only_attempt_row()
        self.assertEqual((row["status"], row["model_id"], row["image_size"]), ("succeeded", NB2, "1K"))
        stored = next(r for r in self.db.tables["generations"] if r["generation_type"] == "tryon")
        self.assertEqual(stored["model_used"], NB2)


class AdminTests(ModelTestCase):
    URL = f"/admin/shops/{SHOP}/image-model"

    def test_set_and_clear_a_model(self):
        response = self.client.patch(self.URL, json={"image_model_id": NB2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["image_model_id"], NB2)
        self.assertEqual(self.db.tables["shops"][0]["image_model_id"], NB2)

        response = self.client.patch(self.URL, json={"image_model_id": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.db.tables["shops"][0]["image_model_id"])

    def test_invalid_value_is_400_and_changes_nothing(self):
        self.set_shop_model(NB2)
        response = self.client.patch(self.URL, json={"image_model_id": "gemini-made-up"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.tables["shops"][0]["image_model_id"], NB2)

    def test_unknown_shop_is_404(self):
        url = "/admin/shops/99999999-9999-4999-8999-999999999999/image-model"
        self.assertEqual(self.client.patch(url, json={"image_model_id": NB2}).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_read_includes_the_current_env_default(self):
        self.set_shop_model(NB2)
        self.assertEqual(
            self.client.get(self.URL).json(),
            {"shop_id": SHOP, "image_model_id": NB2, "default_image_model_id": NB21},
        )

    def test_shop_get_includes_model_fields(self):
        body = self.client.get(f"/admin/shops/{SHOP}").json()
        self.assertIsNone(body["image_model_id"])
        self.assertEqual(body["default_image_model_id"], NB21)

    def test_saving_before_the_sql_ran_says_so(self):
        self.db.missing_columns = MISSING_COLUMNS
        response = self.client.patch(self.URL, json={"image_model_id": NB2})
        self.assertEqual(response.status_code, 409)
        self.assertIn("2026-11-02_shop_image_model.sql", response.json()["detail"])

    def test_shops_list_includes_model_fields_with_and_without_the_column(self):
        self.set_shop_model(NB2)
        self.assertEqual(self.client.get("/admin/shops").json()[0]["image_model_id"], NB2)
        del self.db.tables["shops"][0]["image_model_id"]  # select * before the SQL has run
        row = self.client.get("/admin/shops").json()[0]
        self.assertIsNone(row["image_model_id"])
        self.assertEqual(row["default_image_model_id"], NB21)

    def test_operations_list_shows_size_and_works_before_the_sql_ran(self):
        self.db.tables["generation_attempts"] = [
            {"id": str(uuid.uuid4()), "shop_id": SHOP, "status": "succeeded", "model_id": NB2, "image_size": "1K"}
        ]
        patch = mock.patch.object(admin_api, "_count_attempts", lambda *args, **kwargs: 1)  # tiles: not under test
        patch.start()
        self.addCleanup(patch.stop)
        item = self.client.get("/admin/attempts").json()["items"][0]
        self.assertEqual((item["model_id"], item["image_size"]), (NB2, "1K"))

        self.db.missing_columns = MISSING_COLUMNS
        with quiet():
            response = self.client.get("/admin/attempts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["model_id"], NB2)

    def test_requires_the_admin_secret(self):
        del app.dependency_overrides[verify_admin_secret]
        self.assertIn(self.client.patch(self.URL, json={"image_model_id": NB2}).status_code, (401, 403))
        self.assertIsNone(self.db.tables["shops"][0]["image_model_id"])


if __name__ == "__main__":
    unittest.main()
