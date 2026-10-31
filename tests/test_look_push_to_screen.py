"""POST /generations/{id}/push-to-screen: a look goes to the TV as a new try-on-type copy.
Mocked: Supabase and storage (in memory). No network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import generations_api
from auth_deps import CurrentShopContext, get_current_shop_context
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
OTHER_SHOP = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
GARMENT = "44444444-4444-4444-8444-444444444444"
HERO = "55555555-5555-4555-8555-555555555555"
FABRIC = "66666666-6666-4666-8666-666666666666"
LOOK = "77777777-7777-4777-8777-777777777777"
BUCKET = "generated-outputs"
LOOK_PATH = f"{SHOP}/{LOOK}/output_v1.png"


def look_row(**overrides) -> dict:
    return {
        "id": LOOK,
        "shop_id": SHOP,
        "generation_type": "look",
        "status": "done",
        "output_path": LOOK_PATH,
        "folder_id": GARMENT,
        "hero_image_id": HERO,
        "fabric_image_id": FABRIC,
        "credits_used": 50,
        **overrides,
    }


class PushLookToScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables["generations"] = [look_row()]
        self.db.files[(BUCKET, LOOK_PATH)] = b"look-image"
        patch = mock.patch.object(generations_api, "get_supabase_admin_client", lambda: self.db)
        patch.start()
        self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def push(self, generation_id: str = LOOK):
        return self.client.post(f"/generations/{generation_id}/push-to-screen")

    def row(self, generation_id: str) -> dict:
        return next(r for r in self.db.tables["generations"] if r["id"] == generation_id)

    def assert_refused_without_writes(self, response, code: int) -> None:
        self.assertEqual(response.status_code, code)
        self.assertEqual(len(self.db.tables["generations"]), 1)
        self.assertNotIn("shop_screen_state", self.db.tables)
        self.assertEqual(list(self.db.files), [(BUCKET, LOOK_PATH)])

    def test_other_shops_look_is_refused_like_a_missing_one(self):
        self.db.tables["generations"] = [look_row(shop_id=OTHER_SHOP)]
        response = self.push()
        self.assert_refused_without_writes(response, 404)
        self.assertEqual(response.json()["detail"], "Generation not found")

    def test_missing_look_and_malformed_id_are_404(self):
        self.assert_refused_without_writes(self.push("88888888-8888-4888-8888-888888888888"), 404)
        self.assert_refused_without_writes(self.push("not-a-uuid"), 404)

    def test_a_row_that_is_not_a_look_is_404(self):
        self.db.tables["generations"] = [look_row(generation_type="tryon")]
        self.assert_refused_without_writes(self.push(), 404)

    def test_not_done_is_refused(self):
        self.db.tables["generations"] = [look_row(status="processing")]
        response = self.push()
        self.assert_refused_without_writes(response, 400)
        self.assertEqual(response.json()["detail"], "Generation output is not ready yet")

    def test_no_output_is_refused(self):
        self.db.tables["generations"] = [look_row(output_path=None)]
        self.assert_refused_without_writes(self.push(), 400)

    def test_happy_path_makes_a_new_row_and_file_and_goes_live(self):
        response = self.push()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"generation_id", "live"})
        self.assertIs(body["live"], True)
        new_id = body["generation_id"]
        self.assertNotEqual(new_id, LOOK)

        row = self.row(new_id)
        self.assertRegex(row["output_path"], rf"^{SHOP}/{re.escape(new_id)}/output_v\d+\.png$")
        self.assertEqual(self.db.files[(BUCKET, row["output_path"])], b"look-image")
        self.assertEqual(
            set(row),
            {
                "id", "shop_id", "hero_image_id", "fabric_image_id", "folder_id", "status", "generation_type",
                "model_used", "prompt_used", "output_path", "credits_used", "show_on_screen",
                "created_at", "started_at", "completed_at",
            },
        )
        self.assertEqual(
            (row["shop_id"], row["folder_id"], row["hero_image_id"], row["fabric_image_id"]),
            (SHOP, GARMENT, HERO, FABRIC),
        )
        self.assertEqual((row["status"], row["generation_type"]), ("done", "tryon"))
        self.assertEqual(row["prompt_used"], "web-app look (pushed to screen)")
        self.assertEqual((row["credits_used"], row["show_on_screen"]), (0, True))
        self.assertTrue(row["created_at"] == row["started_at"] == row["completed_at"])

        state = self.db.tables["shop_screen_state"][-1]
        self.assertEqual(
            (state["shop_id"], state["live_generation_id"], state["mode"]),
            (SHOP, new_id, "live"),
        )

        self.assertEqual(self.row(LOOK), look_row())  # the look itself is untouched
        self.assertFalse([entry for entry in self.db.log if entry[0] in ("rpc", "credit_ledger")])
        self.assertEqual(self.db.attempts(), [])
        self.assertNotIn((BUCKET, row["output_path"].replace("output_", "thumb_")), self.db.files)

    def test_pushing_the_same_look_twice_gives_two_ids_and_paths(self):
        first = self.push().json()["generation_id"]
        second = self.push().json()["generation_id"]

        self.assertNotEqual(first, second)
        self.assertNotEqual(self.row(first)["output_path"], self.row(second)["output_path"])
        self.assertEqual(self.db.tables["shop_screen_state"][-1]["live_generation_id"], second)

    def test_pushes_the_current_image_after_match_colour(self):
        corrected = f"{SHOP}/{LOOK}/output_v2.jpg"
        self.db.files[(BUCKET, corrected)] = b"match-colour-image"
        self.row(LOOK)["output_path"] = corrected

        new_id = self.push().json()["generation_id"]

        path = self.row(new_id)["output_path"]
        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual(self.db.files[(BUCKET, path)], b"match-colour-image")

    def test_falls_back_to_download_and_upload_when_copy_fails(self):
        self.db.storage_fail[(BUCKET, "copy")] = "copy not available"
        with mock.patch.object(fakes._Bucket, "download", lambda _self, _path: b"look-image"):
            response = self.push()

        self.assertEqual(response.status_code, 200)
        path = self.row(response.json()["generation_id"])["output_path"]
        self.assertEqual(self.db.files[(BUCKET, path)], b"look-image")

    def test_storage_failure_saves_nothing(self):
        self.db.storage_fail[(BUCKET, "copy")] = "copy not available"
        self.db.storage_fail[(BUCKET, "download")] = "object store timeout"
        response = self.push()
        self.assert_refused_without_writes(response, 500)
        self.assertEqual(response.json()["detail"], "Failed to copy look image")

    def test_row_failure_removes_the_copied_file(self):
        self.db.fail[("generations", "insert")] = "insert failed"
        response = self.push()
        self.assert_refused_without_writes(response, 500)


if __name__ == "__main__":
    unittest.main()
