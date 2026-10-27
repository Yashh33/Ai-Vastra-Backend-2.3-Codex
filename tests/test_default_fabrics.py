"""Optional fabric slots: an empty optional slot gets a built-in WHITE / BLACK fabric image in
exactly that slot's position. Mocked: Supabase (in memory), Gemini. No network, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

import attempt_log
import default_fabrics
import worker
from config import get_settings
from tests.test_generation_paths import CUSTOMER_PHOTO, FABRIC, GARMENT, HERO, SHOP, PathTestCase

SAREE = "99999999-9999-4999-8999-999999999999"
SAREE_HERO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PALLU_FABRIC = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

WHITE, _ = default_fabrics.load("white")
BLACK, _ = default_fabrics.load("black")

SLOTS = [
    {"label": "Body", "apply_to": "body", "sort_order": 0},
    {"label": "Pallu", "apply_to": "pallu", "sort_order": 1},
]


class RecordingGemini:
    """Records what each call sent; always returns one image."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        recorder = self

        class _Models:
            def generate_content(self, **kwargs):
                recorder.calls.append(kwargs)
                return fakes.image_response()

        class _AioModels:
            async def generate_content(self, **kwargs):
                recorder.calls.append(kwargs)
                return fakes.image_response()

        self.models = _Models()
        self.aio = SimpleNamespace(models=_AioModels())

    def sent_images(self) -> list[bytes]:
        return [part.inline_data.data for part in self.calls[-1]["contents"][1:]]

    def sent_prompt(self) -> str:
        return self.calls[-1]["contents"][0]


def seed_saree(db: fakes.FakeSupabase) -> None:
    db.tables["garment_types"].append(
        {
            "id": SAREE,
            "shop_id": SHOP,
            "name": "Saree",
            "default_hero_image_id": SAREE_HERO,
            "look_prompt": "Sew a {garment_name}.\n{fabric_mapping}\nImages: {image_count}",
            "tryon_prompt": "Put the {garment_name} on the customer.\n{fabric_mapping}\nImages: {image_count}",
        }
    )
    db.tables["hero_images"].append(
        {"id": SAREE_HERO, "shop_id": SHOP, "folder_id": SAREE, "storage_path": f"{SHOP}/saree.jpg", "mime_type": "image/jpeg"}
    )
    db.tables["fabric_images"].append(
        {"id": PALLU_FABRIC, "shop_id": SHOP, "storage_path": f"{SHOP}/pallu.jpg", "mime_type": "image/jpeg"}
    )
    db.tables["garment_fabric_slots"].extend(
        {"id": str(uuid.uuid4()), "shop_id": SHOP, "folder_id": SAREE, **slot} for slot in SLOTS
    )
    db.files[("hero-images", f"{SHOP}/saree.jpg")] = b"hero-bytes"
    db.files[("fabric-images", f"{SHOP}/fabric.jpg")] = b"body-fabric-bytes"
    db.files[("fabric-images", f"{SHOP}/pallu.jpg")] = b"pallu-fabric-bytes"


@contextlib.contextmanager
def missing_default_files():
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        with mock.patch.object(default_fabrics, "_DIR", Path(tmp) / "nope"), mock.patch.dict(
            default_fabrics._cache, clear=True
        ):
            yield


# ---------------------------------------------------------------- slot resolving (pure)

class ResolveTests(unittest.TestCase):
    def body(self, apply_to="body"):
        return {"fabric_image_id": FABRIC, "apply_to": apply_to}

    def test_required_slot_missing_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            default_fabrics.resolve_slot_entries(SLOTS, {"pallu": self.body("pallu")}, {})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Body", ctx.exception.detail)

    def test_optional_slot_with_fabric_uses_the_fabric(self):
        pallu = {"fabric_image_id": PALLU_FABRIC, "apply_to": "pallu"}
        entries = default_fabrics.resolve_slot_entries(SLOTS, {"body": self.body(), "pallu": pallu}, {"pallu": "black"})
        self.assertEqual(entries, [self.body(), pallu])

    def test_optional_slot_black_is_in_that_slots_position(self):
        entries = default_fabrics.resolve_slot_entries(SLOTS, {"body": self.body()}, {"pallu": "black"})
        self.assertEqual(entries[0], self.body())
        self.assertEqual(entries[1], {"default_fabric": "black", "apply_to": "pallu", "slot_label": "Pallu"})

    def test_optional_slot_empty_is_white(self):
        entries = default_fabrics.resolve_slot_entries(SLOTS, {"body": self.body()}, {})
        self.assertEqual(entries[1]["default_fabric"], "white")

    def test_bad_value_is_400(self):
        for raw in ({"pallu": "red"}, {"pallu": 1}, '{"pallu": "grey"}', "not json", ["white"]):
            with self.assertRaises(HTTPException) as ctx:
                default_fabrics.parse_choices(raw)
            self.assertEqual(ctx.exception.status_code, 400, raw)

    def test_values_are_trimmed_case_insensitive_and_blank_means_white(self):
        self.assertEqual(default_fabrics.parse_choices({" Pallu ": " BLACK "}), {"pallu": "black"})
        self.assertEqual(default_fabrics.parse_choices('{"pallu": ""}'), {})
        self.assertEqual(default_fabrics.parse_choices(""), {})
        self.assertEqual(default_fabrics.parse_choices(None), {})

    def test_unknown_slot_key_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            default_fabrics.resolve_slot_entries(SLOTS, {"body": self.body()}, {"dupatta": "white"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_record_and_merge_put_defaults_back_in_place(self):
        entries = [
            {"fabric_image_id": "a", "apply_to": "body"},
            {"default_fabric": "black", "apply_to": "pallu", "slot_label": "Pallu"},
            {"fabric_image_id": "c", "apply_to": "blouse"},
        ]
        record = default_fabrics.default_slots_record(entries)
        self.assertEqual(
            record,
            [{"position": 1, "apply_to": "pallu", "slot_label": "Pallu", "default_fabric": "black", "label": "Default black"}],
        )
        merged = default_fabrics.merge_into([entries[0], entries[2]], record)
        self.assertEqual([m.get("fabric_image_id") or m["default_fabric"] for m in merged], ["a", "black", "c"])

    def test_images_load_from_the_repo_and_are_cached(self):
        data, mime = default_fabrics.load("white")
        self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG
        self.assertEqual(mime, "image/jpeg")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read again")):
            self.assertEqual(default_fabrics.load("white")[0], data)

    def test_missing_file_is_a_clear_error(self):
        with missing_default_files():
            with self.assertRaises(default_fabrics.DefaultFabricMissing) as ctx:
                default_fabrics.load("black")
        self.assertIn("Default black", str(ctx.exception))


# ---------------------------------------------------------------- Sew a look (API)

class LookDefaultTests(PathTestCase):
    def setUp(self) -> None:
        super().setUp()
        seed_saree(self.db)
        self.new_generation_id = str(uuid.uuid4())
        self.db.tables["generations"].append({"id": self.new_generation_id, "shop_id": SHOP, "status": "queued"})
        self.db.rpc_handlers["create_generation_with_credit_debit_v2"] = lambda params: [
            {"generation_id": self.new_generation_id, "status": "queued", "credits_used": params["p_credits_cost"],
             "balance_before": 150, "balance_after": 100}
        ]

    def post(self, fabrics, default_fabrics_value=None, hero=SAREE_HERO):
        body = {"hero_image_id": hero, "fabrics": fabrics}
        if default_fabrics_value is not None:
            body["default_fabrics"] = default_fabrics_value
        return self.client.post("/generations", json=body)

    def rpc_calls(self):
        return [entry[2] for entry in self.db.log if entry[:2] == ("rpc", "create_generation_with_credit_debit_v2")]

    def generation(self):
        return next(row for row in self.db.tables["generations"] if row["id"] == self.new_generation_id)

    def test_empty_optional_slot_uses_white_in_its_position(self):
        response = self.post([{"fabric_image_id": FABRIC, "apply_to": "body"}])
        self.assertEqual(response.status_code, 200, response.text)

        (rpc,) = self.rpc_calls()
        self.assertEqual([f["fabric_image_id"] for f in rpc["p_fabrics"]], [FABRIC])
        self.assertEqual(rpc["p_credits_cost"], get_settings().CREDITS_PER_IMAGE)

        generation = self.generation()
        self.assertEqual(generation["prompt_used"], "Sew a Saree.\nFabric 1 -> BODY\nFabric 2 -> PALLU\nImages: 3")
        self.assertEqual(
            generation["default_fabric_slots"],
            [{"position": 1, "apply_to": "pallu", "slot_label": "Pallu", "default_fabric": "white", "label": "Default white"}],
        )

        row = self.db.only_attempt()
        self.assertEqual(row["fabric_mode"], "multi")
        self.assertEqual(row["fabric_inputs"][0]["storage_path"], f"{SHOP}/fabric.jpg")
        self.assertEqual(
            {k: row["fabric_inputs"][1][k] for k in ("slot_label", "storage_path", "default_fabric", "fabric_label")},
            {"slot_label": "Pallu", "storage_path": None, "default_fabric": "white", "fabric_label": "Default white"},
        )

    def test_black_choice_is_recorded(self):
        response = self.post([{"fabric_image_id": FABRIC, "apply_to": "body"}], {"pallu": "black"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.generation()["default_fabric_slots"][0]["default_fabric"], "black")

    def test_both_fabrics_sent_means_no_defaults(self):
        response = self.post(
            [{"fabric_image_id": PALLU_FABRIC, "apply_to": "pallu"}, {"fabric_image_id": FABRIC, "apply_to": "body"}],
            {"pallu": "black"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        (rpc,) = self.rpc_calls()
        self.assertEqual([f["apply_to"] for f in rpc["p_fabrics"]], ["body", "pallu"])
        self.assertNotIn("default_fabric_slots", self.generation())

    def test_required_slot_missing_is_400_and_nothing_charged(self):
        response = self.post([{"fabric_image_id": PALLU_FABRIC, "apply_to": "pallu"}])
        self.assertEqual(response.status_code, 400)
        self.assertIn("Body", response.json()["detail"])
        self.assertEqual(self.rpc_calls(), [])

    def test_bad_default_value_is_400_and_nothing_charged(self):
        response = self.post([{"fabric_image_id": FABRIC, "apply_to": "body"}], {"pallu": "red"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("white, black", response.json()["detail"])
        self.assertEqual(self.rpc_calls(), [])

    def test_missing_default_file_fails_before_any_charge(self):
        with missing_default_files():
            response = self.post([{"fabric_image_id": FABRIC, "apply_to": "body"}])
        self.assertEqual(response.status_code, 500)
        self.assertIn("Default white", response.json()["detail"])
        self.assertEqual(self.rpc_calls(), [])

    def test_single_fabric_garment_is_unchanged(self):
        response = self.client.post(
            "/generations",
            json={"hero_image_id": HERO, "fabric_image_id": FABRIC, "default_fabrics": {"pallu": "black"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        (rpc,) = self.rpc_calls()
        self.assertEqual([(f["fabric_image_id"], f["apply_to"]) for f in rpc["p_fabrics"]], [(FABRIC, "suit_full_body")])
        updates = [p for t, op, p in self.db.log if (t, op) == ("generations", "update")]
        self.assertTrue(updates)
        self.assertTrue(all("default_fabric_slots" not in p for p in updates))

    def test_history_summary_names_the_default(self):
        self.post([{"fabric_image_id": FABRIC, "apply_to": "body"}], {"pallu": "black"})
        self.db.tables["generation_fabrics"] = [
            {"generation_id": self.new_generation_id, "shop_id": SHOP, "fabric_image_id": FABRIC,
             "apply_to": "body", "sort_order": 1, "fabric_code": "F12"}
        ]
        rows = self.client.get("/generations").json()
        row = next(r for r in rows if r["id"] == self.new_generation_id)
        self.assertEqual(row["fabric_summary_label"], "body: F12 | pallu: Default black")


# ---------------------------------------------------------------- Sew a look (worker)

class _NoDefaultColumnDB(fakes.FakeSupabase):
    """generations has no default_fabric_slots column yet (SQL not run)."""

    def table(self, name):
        query = super().table(name)
        if name != "generations":
            return query
        select, execute = query.select, query.execute
        state = {"columns": ""}

        def recording_select(*columns, **kwargs):
            state["columns"] = " ".join(columns)
            return select(*columns, **kwargs)

        def failing_execute():
            if "default_fabric_slots" in state["columns"]:
                raise fakes.FakeDBError("column generations.default_fabric_slots does not exist (42703)")
            return execute()

        query.select, query.execute = recording_select, failing_execute
        return query


class WorkerDefaultTests(PathTestCase):
    def setUp(self) -> None:
        super().setUp()
        seed_saree(self.db)
        self.generation_id = str(uuid.uuid4())
        self.db.tables["generations"].append(
            {
                "id": self.generation_id, "shop_id": SHOP, "status": "processing", "hero_image_id": SAREE_HERO,
                "fabric_image_id": FABRIC, "prompt_used": "Sew a Saree.",
                "default_fabric_slots": [
                    {"position": 1, "apply_to": "pallu", "slot_label": "Pallu", "default_fabric": "black", "label": "Default black"}
                ],
            }
        )
        self.db.tables["generation_fabrics"] = [
            {"generation_id": self.generation_id, "shop_id": SHOP, "fabric_image_id": FABRIC, "apply_to": "body", "sort_order": 1}
        ]
        self.attempt_row = {
            "id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "attempt_type": "look", "status": "running",
            "generation_id": self.generation_id, "shop_id": SHOP, "credits_charged": 50,
            "started_at": attempt_log.iso(attempt_log.utc_now() - timedelta(seconds=40)),
        }
        self.db.tables.setdefault("generation_attempts", []).append(self.attempt_row)
        self.db.rpc_handlers["mark_generation_failed_with_refund"] = lambda params: [
            {"refunded": True, "refund_amount": 50, "balance_after": 150}
        ]
        self.gemini = RecordingGemini()
        for patch in (
            mock.patch.object(worker, "get_supabase", lambda: self.db),
            mock.patch.object(worker, "downscale_image_if_needed", lambda data, mime: (data, mime)),
            mock.patch.object(worker, "build_thumbnail_jpeg_bytes", lambda data: b"thumb"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_job(self):
        job = {"id": self.generation_id, "shop_id": SHOP, "hero_image_id": SAREE_HERO, "fabric_image_id": FABRIC}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            worker.process_job(self.gemini, get_settings(), job)

    def refunds(self):
        return [entry for entry in self.db.log if entry[:2] == ("rpc", "mark_generation_failed_with_refund")]

    def test_default_image_goes_in_the_slots_position(self):
        self.run_job()
        self.assertEqual(self.gemini.sent_images(), [b"hero-bytes", b"body-fabric-bytes", BLACK])
        self.assertEqual(self.attempt_row["status"], "succeeded")
        self.assertEqual(self.refunds(), [])

    def test_missing_default_file_fails_and_refunds_once(self):
        with missing_default_files():
            self.run_job()
        self.assertEqual(self.gemini.calls, [])
        self.assertEqual(len(self.refunds()), 1)
        self.assertEqual((self.attempt_row["status"], self.attempt_row["credits_refunded"]), ("failed", 50))

    def test_works_before_the_column_exists(self):
        old_db = self.db
        self.db = _NoDefaultColumnDB()
        self.db.tables, self.db.files, self.db.rpc_handlers = old_db.tables, old_db.files, old_db.rpc_handlers
        attempt_log.configure(client_factory=lambda: self.db)
        self.db.tables["generations"][-1].pop("default_fabric_slots")
        self.run_job()
        self.assertEqual(self.gemini.sent_images(), [b"hero-bytes", b"body-fabric-bytes"])
        self.assertEqual(self.attempt_row["status"], "succeeded")


# ---------------------------------------------------------------- Try on customer

class TryonDefaultTests(PathTestCase):
    def setUp(self) -> None:
        super().setUp()
        seed_saree(self.db)
        self.gemini = RecordingGemini()

    def multi(self, folder=SAREE, hero=SAREE_HERO, defaults=None, **pairs):
        data = {"hero_image_id": hero, "folder_id": folder, "consent_confirmed": "true", **pairs}
        if defaults is not None:
            data["default_fabrics"] = defaults
        return self.client.post(
            "/tryon/multi/v2", data=data, files={"customer_photo": ("c.jpg", CUSTOMER_PHOTO, "image/jpeg")}
        )

    def test_multi_black_default_sits_between_fabric_and_customer(self):
        response = self.multi(defaults='{"pallu": "black"}', fabric_image_id_1=FABRIC, apply_to_1="body")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.gemini.sent_images(), [b"hero-bytes", b"body-fabric-bytes", BLACK, CUSTOMER_PHOTO])
        self.assertIn("Fabric 2 -> PALLU\nImages: 4", self.gemini.sent_prompt())
        fabric_inputs = self.db.only_attempt()["fabric_inputs"]
        self.assertEqual((fabric_inputs[1]["default_fabric"], fabric_inputs[1]["storage_path"]), ("black", None))
        self.assertEqual(fabric_inputs[1]["slot_label"], "Pallu")

    def test_multi_fabrics_go_in_slot_order_and_beat_defaults(self):
        response = self.multi(
            defaults='{"pallu": "black"}',
            fabric_image_id_1=PALLU_FABRIC, apply_to_1="pallu", fabric_image_id_2=FABRIC, apply_to_2="body",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.gemini.sent_images(), [b"hero-bytes", b"body-fabric-bytes", b"pallu-fabric-bytes", CUSTOMER_PHOTO]
        )

    def test_multi_required_slot_missing_and_bad_value_are_400(self):
        response = self.multi(fabric_image_id_1=PALLU_FABRIC, apply_to_1="pallu")
        self.assertEqual(response.status_code, 400)
        response = self.multi(defaults='{"pallu": "pink"}', fabric_image_id_1=FABRIC, apply_to_1="body")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.gemini.calls, [])

    def test_multi_on_single_fabric_garment_is_unchanged(self):
        response = self.multi(
            folder=GARMENT, hero=HERO, defaults='{"pallu": "black"}', fabric_image_id_1=FABRIC, apply_to_1="shirt"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.gemini.sent_images()), 3)  # hero, fabric, customer
        self.assertNotIn("default_fabric", self.db.only_attempt()["fabric_inputs"][0])

    def test_quick_json_fills_the_empty_optional_slot_with_white(self):
        import base64

        response = self.client.post(
            "/tryon/quick",
            json={
                "fabric_image_id": FABRIC,
                "fabric_image_ids": [FABRIC],
                "folder_id": SAREE,
                "customer_photo_b64": base64.b64encode(CUSTOMER_PHOTO).decode(),
                "consent_confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.gemini.sent_images(), [b"hero-bytes", b"body-fabric-bytes", WHITE, CUSTOMER_PHOTO])
        self.assertIn("Images: 4", self.gemini.sent_prompt())
        self.assertEqual(self.db.only_attempt()["fabric_inputs"][1]["fabric_label"], "Default white")


if __name__ == "__main__":
    unittest.main()
