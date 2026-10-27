"""Prompt library: blank filling, the paste-from-skill parser, slugs, slot planning and versions.
All mocked: no browser, no real database, no AI calls."""
from tests import fakes  # noqa: F401  (fake settings first)

import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

import attempt_log
import prompt_library as lib
import prompt_library_api
from main import app

ADMIN_HEADERS = {"x-admin-secret": "fake-admin-secret-0123456789"}
SHOP = str(uuid.uuid4())

TWO_LOOK = "Sew [DESIGN].\nKeep: [KEEP].\n[PART1_LABEL] uses Fabric 1, [PART2_LABEL] uses Fabric 2 ([part1]? no: [PART1] / [PART2]).\n{fabric_mapping}"
TWO_TRYON = "Customer wears the [PART1] and [PART2]. [PART1_LABEL]!"
TWO_LOOK_TRYON = "Look try-on for [PART1] + [PART2]."

SKILL_EXAMPLE = """Template: v2 TWO-FABRIC
Name: Shirt + Pant
Fabrics: Two
Part 1: Shirt (SAM word: shirt)
Part 2: Pant (SAM word: pants), default: Black
Design: a half-sleeve shirt with a spread collar, full button front, tucked in, worn with straight pants to the ankle
Keep: collar, half sleeves, buttons, tuck, pant cut and length
"""


class BlankFillingTests(unittest.TestCase):
    def two_values(self, **overrides):
        garment = {
            "design_text": "a kurta with a mandarin collar",
            "keep_text": "collar, length",
            "part1_name": "Kurta Top",
            "part2_name": "Churidar",
            **overrides,
        }
        return lib.blank_values("two", garment)

    def test_every_two_fabric_blank_with_upper_and_lower_case(self):
        values = self.two_values()
        out = lib.fill_blanks(TWO_LOOK, "two", values)
        self.assertEqual(
            out,
            "Sew a kurta with a mandarin collar.\nKeep: collar, length.\n"
            "KURTA TOP uses Fabric 1, CHURIDAR uses Fabric 2 ([part1]? no: kurta top / churidar).\n{fabric_mapping}",
        )

    def test_single_blank_is_the_name_exactly(self):
        values = lib.blank_values("single", {"name": "Nehru Jacket"})
        self.assertEqual(lib.fill_blanks("A [GARMENT] and a [GARMENT].", "single", values), "A Nehru Jacket and a Nehru Jacket.")

    def test_nothing_else_in_the_template_changes(self):
        template = "  lead spaces\n\n\tTabs [GARMENT]  double  spaces \n{garment_name} {image_count} [UNKNOWN] [garment]\n\n"
        out = lib.fill_blanks(template, "single", {"GARMENT": "Shirt"})
        self.assertEqual(out, template.replace("[GARMENT]", "Shirt"))

    def test_filled_text_is_not_filled_again(self):
        values = self.two_values(design_text="see [KEEP]")
        self.assertEqual(lib.fill_blanks("[DESIGN] | [KEEP]", "two", values), "see [KEEP] | collar, length")

    def test_single_set_ignores_two_fabric_blanks(self):
        self.assertEqual(lib.fill_blanks("[PART1] [GARMENT]", "single", {"GARMENT": "Coat"}), "[PART1] Coat")

    def test_missing_blank_blocks_render(self):
        version = {"look_prompt": TWO_LOOK, "tryon_prompt": TWO_TRYON, "look_tryon_prompt": None}
        garment = {"design_text": "", "keep_text": "k", "part1_name": "Shirt", "part2_name": "Pant"}
        with self.assertRaises(lib.PromptLibraryError) as ctx:
            lib.render_prompts("two", version, garment)
        self.assertIn("Design", str(ctx.exception))

    def test_blank_not_used_by_the_templates_may_stay_empty(self):
        version = {"look_prompt": "[PART1]/[PART2]", "tryon_prompt": "[PART1_LABEL]", "look_tryon_prompt": ""}
        out = lib.render_prompts("two", version, {"part1_name": "Shirt", "part2_name": "Pant"})
        self.assertEqual(out, {"look_prompt": "shirt/pant", "tryon_prompt": "SHIRT", "look_tryon_prompt": None})

    def test_missing_lists_each_field_once(self):
        templates = {"look_prompt": "[PART2_LABEL] [PART2] [KEEP]", "tryon_prompt": "", "look_tryon_prompt": None}
        self.assertEqual(lib.missing_blanks("two", templates, lib.blank_values("two", {})), ["Keep", "Part 2 name"])

    def test_version_note(self):
        self.assertEqual(lib.version_note("two", 2), "Two fabrics · Version 2")
        self.assertEqual(lib.version_note("single", 1, " fixed collar "), "Single fabric · Version 1 · fixed collar")


class SkillParserTests(unittest.TestCase):
    def test_example_block(self):
        parsed = lib.parse_skill_block(SKILL_EXAMPLE)
        self.assertEqual(
            parsed["fields"],
            {
                "name": "Shirt + Pant",
                "prompt_set": "two",
                "part1_name": "Shirt",
                "part1_sam_word": "shirt",
                "part2_name": "Pant",
                "part2_sam_word": "pants",
                "part2_default": "black",
                "design_text": "a half-sleeve shirt with a spread collar, full button front, tucked in, "
                "worn with straight pants to the ankle",
                "keep_text": "collar, half sleeves, buttons, tuck, pant cut and length",
            },
        )
        self.assertEqual(
            parsed["filled"],
            ["Name", "Fabrics", "Part 1", "Part 1 SAM word", "Part 2", "Part 2 SAM word", "Part 2 default", "Design", "Keep"],
        )

    def test_messy_case_spacing_markdown_and_sam_words(self):
        block = """
        **TEMPLATE** :   v1 one-fabric
          - name:   Nehru Jacket
        PART1 :  Jacket   ( sam WORDS : jacket , coat )
        part 2: Lining,  DEFAULT:white
        design:    a short jacket
           with a band collar
        """
        fields = lib.parse_skill_block(block)["fields"]
        self.assertEqual(fields["prompt_set"], "single")
        self.assertEqual(fields["name"], "Nehru Jacket")
        self.assertEqual(fields["part1_name"], "Jacket")
        self.assertEqual(fields["part1_sam_word"], "jacket , coat")
        self.assertEqual(fields["part2_name"], "Lining")
        self.assertEqual(fields["part2_default"], "white")
        self.assertEqual(fields["design_text"], "a short jacket with a band collar")

    def test_multi_means_two_and_missing_lines_stay_empty(self):
        parsed = lib.parse_skill_block("Template: MULTI fabric\nName: Saree")
        self.assertEqual(parsed["fields"], {"prompt_set": "two", "name": "Saree"})
        self.assertEqual(parsed["filled"], ["Name", "Fabrics"])

    def test_template_line_wins_over_fabrics_line(self):
        self.assertEqual(lib.parse_skill_block("Fabrics: Two\nTemplate: ONE-FABRIC")["fields"]["prompt_set"], "single")
        self.assertEqual(lib.parse_skill_block("Fabrics: one")["fields"]["prompt_set"], "single")

    def test_sam_word_without_brackets_and_category(self):
        fields = lib.parse_skill_block("Part 1: Kurta, SAM word: kurta\nCategory: Women")["fields"]
        self.assertEqual((fields["part1_name"], fields["part1_sam_word"], fields["category"]), ("Kurta", "kurta", "women"))

    def test_empty_or_unrelated_text(self):
        self.assertEqual(lib.parse_skill_block(""), {"fields": {}, "filled": []})
        self.assertEqual(lib.parse_skill_block("hello there\nno keys here")["fields"], {})


class SlugAndSlotTests(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(lib.make_slug("Kurta & trousers"), "kurta_trousers")
        self.assertEqual(lib.make_slug("  Shirt  "), "shirt")
        self.assertEqual(lib.make_slug("Outer-Koti (long)"), "outer_koti_long")
        self.assertEqual(lib.make_slug("&&"), "")

    def test_desired_slots(self):
        self.assertEqual(
            lib.desired_slots("Kurta & trousers", "Dupatta"),
            [
                {"sort_order": 1, "label": "Kurta & trousers fabric", "apply_to": "kurta_trousers"},
                {"sort_order": 2, "label": "Dupatta fabric", "apply_to": "dupatta"},
            ],
        )
        with self.assertRaises(lib.PromptLibraryError):
            lib.desired_slots("Shirt", "shirt!")

    def test_part_names(self):
        self.assertEqual(lib.clean_part_name("  Shirt   top ", "Part 1"), "Shirt top")
        for bad in ("", "   ", "%%", "x" * 41):
            with self.assertRaises(lib.PromptLibraryError):
                lib.clean_part_name(bad, "Part 1")

    def test_plan_without_generations_reshapes_freely(self):
        existing = [
            {"id": "a", "apply_to": "pant", "label": "Pant", "sort_order": 0},
            {"id": "b", "apply_to": "koti", "label": "Koti", "sort_order": 1},
            {"id": "c", "apply_to": "shirt", "label": "Shirt", "sort_order": 2},
        ]
        plan = lib.plan_slot_changes(existing, lib.desired_slots("Shirt", "Trouser"), has_generations=False)
        self.assertEqual(plan["updates"][0], {"id": "c", "label": "Shirt fabric", "sort_order": 1})
        self.assertEqual(plan["updates"][1], {"id": "a", "label": "Trouser fabric", "apply_to": "trouser", "sort_order": 2})
        self.assertEqual([s["id"] for s in plan["deletes"]], ["b"])
        self.assertEqual(plan["inserts"], [])

    def test_plan_with_generations_only_updates_labels_in_place(self):
        existing = [
            {"id": "a", "apply_to": "shirt", "label": "Shirt", "sort_order": 0},
            {"id": "b", "apply_to": "pant", "label": "Pant", "sort_order": 1},
        ]
        plan = lib.plan_slot_changes(existing, lib.desired_slots("Shirt", "Pant"), has_generations=True)
        self.assertEqual(plan["deletes"], [])
        self.assertEqual(
            plan["updates"],
            [{"id": "a", "label": "Shirt fabric", "sort_order": 1}, {"id": "b", "label": "Pant fabric", "sort_order": 2}],
        )

    def test_plan_with_generations_refuses_rename_or_delete(self):
        existing = [
            {"id": "a", "apply_to": "shirt", "sort_order": 1, "label": "Shirt fabric"},
            {"id": "b", "apply_to": "pant", "sort_order": 2, "label": "Pant fabric"},
        ]
        with self.assertRaises(lib.PromptLibraryError):
            lib.plan_slot_changes(existing, lib.desired_slots("Shirt", "Trouser"), has_generations=True)
        with self.assertRaises(lib.PromptLibraryError):
            lib.plan_slot_changes(existing, [], has_generations=True)
        self.assertEqual(
            lib.plan_slot_changes(existing, lib.desired_slots("Shirt", "Pant"), has_generations=True),
            {"updates": [], "inserts": [], "deletes": []},
        )

    def test_plan_inserts_missing_slots(self):
        plan = lib.plan_slot_changes([], lib.desired_slots("Shirt", "Pant"), has_generations=False)
        self.assertEqual([s["apply_to"] for s in plan["inserts"]], ["shirt", "pant"])


class VersionNumberTests(unittest.TestCase):
    def test_next_version(self):
        self.assertEqual(lib.next_version([]), 1)
        self.assertEqual(lib.next_version([1, 3, 2]), 4)
        self.assertEqual(lib.next_version(["2", None]), 3)

    def test_restore_note(self):
        self.assertEqual(lib.restore_note(2), "Restored from Version 2")


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables["shops"] = [{"id": SHOP, "name": "Demo"}]
        attempt_log.configure(client_factory=lambda: self.db, sync=True)
        patch = mock.patch.object(prompt_library_api, "get_supabase_admin_client", lambda: self.db)
        patch.start()
        self.addCleanup(patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=ADMIN_HEADERS)

    def add_version(self, set_key="two", **texts):
        body = {"look_prompt": TWO_LOOK, "tryon_prompt": TWO_TRYON, "look_tryon_prompt": TWO_LOOK_TRYON, **texts}
        return self.post(f"/admin/prompt-library/{set_key}/versions", body)

    def two_garment_body(self, **overrides):
        return {
            "name": "Shirt + Pant",
            "category": "men",
            "mode": "library",
            "fabric_mode": "two",
            "design_text": "a half-sleeve shirt",
            "keep_text": "collar",
            "part1_name": "Shirt",
            "part1_sam_word": "shirt",
            "part2_name": "Pant",
            "part2_sam_word": "pants",
            "part2_default": "black",
            **overrides,
        }


class VersionApiTests(ApiTestCase):
    def test_admin_secret_required(self):
        self.assertEqual(self.client.get("/admin/prompt-library").status_code, 401)

    def test_versions_count_up_and_never_overwrite(self):
        first = self.add_version(note="first")
        second = self.add_version(look_prompt="changed [DESIGN]")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual((first.json()["version"], second.json()["version"]), (1, 2))
        rows = sorted(self.db.tables["prompt_set_versions"], key=lambda r: r["version"])
        self.assertEqual(rows[0]["look_prompt"], TWO_LOOK)
        self.assertEqual(rows[0]["note"], "first")
        # The other set numbers on its own.
        self.assertEqual(self.add_version("single", look_prompt="[GARMENT]", tryon_prompt="t").json()["version"], 1)

    def test_restore_saves_old_text_as_new_latest(self):
        self.add_version()
        self.add_version(look_prompt="v2 [DESIGN]")
        restored = self.post("/admin/prompt-library/two/versions/1/restore", {"note": "back"})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["version"], 3)
        self.assertEqual(restored.json()["look_prompt"], TWO_LOOK)
        self.assertEqual(restored.json()["note"], "Restored from Version 1 · back")
        self.assertEqual(len(self.db.tables["prompt_set_versions"]), 3)

    def test_bad_inputs(self):
        self.assertEqual(self.add_version("three").status_code, 400)
        self.assertEqual(self.add_version(tryon_prompt="  ").status_code, 400)
        self.assertEqual(self.post("/admin/prompt-library/two/versions/9/restore").status_code, 404)

    def test_library_lists_sets_with_counts(self):
        self.add_version()
        self.db.tables["garment_types"] = [
            {"id": "g1", "shop_id": SHOP, "prompt_set": "two", "prompt_set_version": 1},
            {"id": "g2", "shop_id": SHOP, "prompt_set": None},
        ]
        sets = {s["key"]: s for s in self.client.get("/admin/prompt-library", headers=ADMIN_HEADERS).json()["sets"]}
        self.assertEqual(sets["two"]["garments_count"], 1)
        self.assertEqual(sets["two"]["latest_version"], 1)
        self.assertEqual(sets["two"]["versions"][0]["garments_count"], 1)
        self.assertIsNone(sets["single"]["latest_version"])

    def test_parse_skill_endpoint(self):
        response = self.post("/admin/prompt-library/parse-skill", {"text": SKILL_EXAMPLE})
        self.assertEqual(response.json()["fields"]["part2_default"], "black")


class GarmentApiTests(ApiTestCase):
    def test_save_library_garment_renders_prompts_slots_and_history(self):
        self.add_version()
        response = self.post(f"/admin/shops/{SHOP}/garments", self.two_garment_body())
        self.assertEqual(response.status_code, 200, response.text)
        garment = self.db.tables["garment_types"][0]
        self.assertEqual(garment["look_prompt"], lib.fill_blanks(TWO_LOOK, "two", lib.blank_values("two", garment)))
        self.assertEqual(garment["tryon_prompt"], "Customer wears the shirt and pant. SHIRT!")
        self.assertEqual(garment["look_tryon_prompt"], "Look try-on for shirt + pant.")
        self.assertEqual((garment["prompt_set"], garment["prompt_set_version"]), ("two", 1))
        self.assertEqual((garment["part2_default"], garment["part2_sam_word"]), ("black", "pants"))
        slots = sorted(self.db.tables["garment_fabric_slots"], key=lambda s: s["sort_order"])
        self.assertEqual(
            [(s["sort_order"], s["label"], s["apply_to"]) for s in slots],
            [(1, "Shirt fabric", "shirt"), (2, "Pant fabric", "pant")],
        )
        history = self.db.tables["garment_prompt_versions"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["note"], "Two fabrics · Version 1")
        self.assertEqual(history[0]["tryon_prompt"], garment["tryon_prompt"])

    def test_missing_blank_blocks_save_and_writes_nothing(self):
        self.add_version()
        response = self.post(f"/admin/shops/{SHOP}/garments", self.two_garment_body(design_text="  "))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Design", response.json()["detail"])
        self.assertNotIn("garment_types", self.db.tables)

    def test_set_without_versions_blocks_save(self):
        response = self.post(f"/admin/shops/{SHOP}/garments", self.two_garment_body())
        self.assertEqual(response.status_code, 400)
        self.assertIn("no version yet", response.json()["detail"])

    def test_garment_with_looks_keeps_its_slots(self):
        self.add_version()
        created = self.post(f"/admin/shops/{SHOP}/garments", self.two_garment_body()).json()
        self.db.tables["generations"] = [{"id": "gen1", "shop_id": SHOP, "folder_id": created["id"]}]
        renamed = self.client.put(
            f"/admin/shops/{SHOP}/garments/{created['id']}", json=self.two_garment_body(part2_name="Trouser"), headers=ADMIN_HEADERS
        )
        self.assertEqual(renamed.status_code, 400)
        self.assertEqual(sorted(s["apply_to"] for s in self.db.tables["garment_fabric_slots"]), ["pant", "shirt"])
        to_one = self.client.put(
            f"/admin/shops/{SHOP}/garments/{created['id']}",
            json=self.two_garment_body(fabric_mode="one"),
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(to_one.status_code, 400)
        self.assertEqual(len(self.db.tables["garment_fabric_slots"]), 2)

    def test_move_all_and_move_one_to_latest(self):
        self.add_version()
        created = self.post(f"/admin/shops/{SHOP}/garments", self.two_garment_body()).json()
        self.add_version(tryon_prompt="V2 [PART2_LABEL]")

        moved = self.post(f"/admin/shops/{SHOP}/garments/{created['id']}/move-to-latest")
        self.assertEqual(moved.status_code, 200)
        self.assertTrue(moved.json()["moved"])
        garment = self.db.tables["garment_types"][0]
        self.assertEqual((garment["tryon_prompt"], garment["prompt_set_version"]), ("V2 PANT", 2))

        result = self.post("/admin/prompt-library/two/move-all", {"version": 1}).json()
        self.assertEqual((result["moved"], result["unchanged"], result["failed"]), (1, 0, []))
        garment = self.db.tables["garment_types"][0]
        self.assertEqual((garment["tryon_prompt"], garment["prompt_set_version"]), ("Customer wears the shirt and pant. SHIRT!", 1))
        notes = [row["note"] for row in self.db.tables["garment_prompt_versions"]]
        self.assertEqual(notes, ["Two fabrics · Version 1", "Two fabrics · Version 2", "Two fabrics · Version 1"])

    def test_own_prompts_save_clears_the_set(self):
        self.db.tables["garment_types"] = [
            {"id": "g1", "shop_id": SHOP, "name": "Old", "prompt_set": "single", "prompt_set_version": 1}
        ]
        self.db.tables["garment_fabric_slots"] = [
            {"id": "s1", "shop_id": SHOP, "folder_id": "g1", "apply_to": "a", "sort_order": 0},
            {"id": "s2", "shop_id": SHOP, "folder_id": "g1", "apply_to": "b", "sort_order": 1},
            {"id": "s3", "shop_id": SHOP, "folder_id": "g1", "apply_to": "c", "sort_order": 2},
        ]
        response = self.client.put(
            f"/admin/shops/{SHOP}/garments/g1",
            json={"name": "Old", "category": "unisex", "mode": "own", "look_prompt": " L ", "tryon_prompt": "T", "look_tryon_prompt": ""},
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        garment = self.db.tables["garment_types"][0]
        self.assertEqual((garment["look_prompt"], garment["look_tryon_prompt"], garment["prompt_set"]), ("L", None, None))
        # fabric_mode not sent: custom slots are left exactly as they were.
        self.assertEqual(len(self.db.tables["garment_fabric_slots"]), 3)


if __name__ == "__main__":
    unittest.main()
