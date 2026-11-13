"""Speed Round: light view copies of looks, fabric thumbnails, one-year cache on never-changing
files, the worker's background picture fill-in and garment-card link reuse.
Mocked: Supabase and storage (in memory), Gemini. No network, no real database, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import itertools
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

import garment_types_api
import generations_api
import image_thumbs
import picture_backfill
import worker
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from image_thumbs import (
    fabric_thumbnail_path,
    look_thumbnail_path,
    look_view_path,
    make_fabric_thumbnail,
    make_look_view_copy,
)
from main import app

SHOP = "11111111-1111-4111-8111-111111111111"
USER = "33333333-3333-4333-8333-333333333333"
OUT = "generated-outputs"
FAB = "fabric-images"
YEAR = "31536000"


def png(width: int, height: int, mode: str = "RGB", color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def jpeg(width: int, height: int, quality: int = 80, exif_orientation: int = 0) -> bytes:
    buf = io.BytesIO()
    image = Image.new("RGB", (width, height), (40, 90, 160))
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(buf, format="JPEG", quality=quality, exif=exif.tobytes())
    else:
        image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def noisy_jpeg(width: int, height: int) -> bytes:
    """A JPEG over 400 KB (random noise does not compress)."""
    import os

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


class AlreadyExists(Exception):
    status = 409

    def __str__(self) -> str:
        return "{'statusCode': 409, 'error': Duplicate, 'message': The resource already exists}"


class StrictBucket(fakes._Bucket):
    """Like real storage: upsert off + an existing file = refused. Records every call."""

    def download(self, path):
        self.db.events.append(("download", self.bucket, path))
        return super().download(path)

    def upload(self, path, data, options=None):
        self._check("upload")
        options = dict(options or {})
        self.db.uploads.append((self.bucket, path, options))
        if (self.bucket, path) in self.db.files and str(options.get("upsert", "false")).lower() != "true":
            raise AlreadyExists()
        self.db.files[(self.bucket, path)] = data
        return {"path": path}

    update = upload

    def remove(self, paths):
        self.db.removed.extend((self.bucket, p) for p in paths)
        return super().remove(paths)


def strict_db() -> fakes.FakeSupabase:
    db = fakes.FakeSupabase()
    db.events, db.uploads, db.removed = [], [], []
    db.storage = SimpleNamespace(from_=lambda bucket: StrictBucket(db, bucket))
    return db


def upload_options(db, bucket, path) -> dict:
    return next(opts for b, p, opts in db.uploads if (b, p) == (bucket, path))


# ---------------------------------------------------------------- paths


class PathRuleTests(unittest.TestCase):
    def test_look_view_path(self):
        cases = {
            "shop/gen/output_v123.jpg": "shop/gen/view_v123.jpg",
            "shop/gen/output-cc-17000.jpg": "shop/gen/view-cc-17000.jpg",
            "shop/gen/output.png": "shop/gen/view.jpg",
            "shop/gen/other.png": "shop/gen/view_other.jpg",
            "shop/gen/output_v9.webp": "shop/gen/view_v9.jpg",
            "output.png": "view.jpg",
        }
        for output_path, expected in cases.items():
            with self.subTest(output_path=output_path):
                self.assertEqual(look_view_path(output_path), expected)

    def test_view_path_never_equals_output_or_thumbnail(self):
        for path in ("s/g/output_v1.jpg", "s/g/output.jpg", "s/g/x.jpg"):
            self.assertNotIn(look_view_path(path), (path, look_thumbnail_path(path)))

    def test_thumbnail_rule_unchanged(self):
        self.assertEqual(look_thumbnail_path("shop/gen/output_v123.jpg"), "shop/gen/thumb_v123.jpg")
        self.assertEqual(look_thumbnail_path("shop/gen/other.png"), "shop/gen/thumb_other.jpg")

    def test_fabric_thumbnail_path(self):
        self.assertEqual(
            fabric_thumbnail_path("shop/fabrics/1720000000-ab12.png"), "shop/fabrics/1720000000-ab12_thumb.jpg"
        )
        self.assertEqual(fabric_thumbnail_path("fabric.jpeg"), "fabric_thumb.jpg")
        self.assertEqual(fabric_thumbnail_path("a/b"), "a/b_thumb.jpg")


# ---------------------------------------------------------------- view copy


class ViewCopyTests(unittest.TestCase):
    def test_large_png_is_limited_to_1440(self):
        out = make_look_view_copy(png(1800, 2400))
        image = opened(out)
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (1080, 1440))

    def test_landscape_longest_edge(self):
        image = opened(make_look_view_copy(png(3000, 2000)))
        self.assertEqual(max(image.size), 1440)
        self.assertEqual(image.size, (1440, 960))

    def test_never_upscaled(self):
        image = opened(make_look_view_copy(png(600, 800)))  # a PNG always gets a copy
        self.assertEqual(image.size, (600, 800))

    def test_light_jpeg_needs_no_copy(self):
        data = jpeg(1080, 1440)
        self.assertLessEqual(len(data), 400 * 1024)
        self.assertIsNone(make_look_view_copy(data))

    def test_big_dimension_jpeg_gets_copy(self):
        image = opened(make_look_view_copy(jpeg(1600, 2000)))
        self.assertEqual(max(image.size), 1440)

    def test_heavy_jpeg_gets_copy(self):
        data = noisy_jpeg(1000, 1000)
        self.assertGreater(len(data), 400 * 1024)
        out = make_look_view_copy(data)
        self.assertIsNotNone(out)
        self.assertEqual(opened(out).size, (1000, 1000))
        self.assertLess(len(out), len(data))

    def test_transparency_on_white_and_progressive(self):
        image = opened(make_look_view_copy(png(500, 500, mode="RGBA", color=(0, 0, 0, 0))))
        self.assertEqual(image.mode, "RGB")
        r, g, b = image.getpixel((10, 10))
        self.assertGreater(min(r, g, b), 245)
        self.assertTrue(image.info.get("progressive") or image.info.get("progression"))

    def test_exif_rotation_applied(self):
        data = jpeg(1600, 1000, exif_orientation=6)  # stored landscape, shown portrait
        image = opened(make_look_view_copy(data))
        self.assertEqual(image.size, (900, 1440))

    def test_fabric_thumbnail(self):
        image = opened(make_fabric_thumbnail(png(1200, 900)))
        self.assertEqual((image.format, image.size), ("JPEG", (400, 300)))
        small = opened(make_fabric_thumbnail(png(200, 100)))
        self.assertEqual(small.size, (200, 100))


class UploadNewFileTests(unittest.TestCase):
    def test_options_and_never_overwrites(self):
        db = strict_db()
        db.files[(OUT, "s/g/view.jpg")] = b"old"
        image_thumbs.upload_new_file(db, OUT, "s/g/view.jpg", b"new")  # already exists: fine
        self.assertEqual(db.files[(OUT, "s/g/view.jpg")], b"old")
        self.assertEqual(
            db.uploads[-1][2], {"content-type": "image/jpeg", "upsert": "false", "cache-control": YEAR}
        )

    def test_other_errors_raise(self):
        db = strict_db()
        db.storage_fail[(OUT, "upload")] = "boom"
        with self.assertRaises(Exception):
            image_thumbs.upload_new_file(db, OUT, "s/g/view.jpg", b"new")

    def test_best_effort_never_raises(self):
        db = strict_db()
        db.storage_fail[(OUT, "upload")] = "boom"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(image_thumbs.store_look_view_copy_best_effort(db, "s/g/output.png", png(2000, 2000)))
            self.assertIsNone(image_thumbs.store_look_view_copy_best_effort(db, "s/g/output.png", b"not-an-image"))

    def test_hero_thumbnail_gets_year_cache(self):
        db = strict_db()
        image_thumbs.store_hero_thumbnail(db, "s/f/hero.png", png(900, 1200))
        self.assertEqual(upload_options(db, "hero-images", "s/f/hero_thumb.jpg")["cache-control"], YEAR)


# ---------------------------------------------------------------- creating view copies


class WorkerViewCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = strict_db()
        self.generation_id = str(uuid.uuid4())
        self.db.tables["generations"] = [{"id": self.generation_id, "shop_id": SHOP, "status": "processing"}]
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
            mock.patch.object(worker, "resolve_job_image_model", lambda shop_id: "gemini-3.1-flash-image-preview"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_job(self, image: bytes, job_extra: dict | None = None):
        gemini = fakes.FakeGemini(fakes.image_response(data=image))
        job = {"id": self.generation_id, "shop_id": SHOP, **(job_extra or {})}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            worker.process_job(gemini, get_settings(), job)
        return self.db.tables["generations"][0]

    def test_view_copy_created_with_year_cache(self):
        row = self.run_job(png(1800, 2400))
        self.assertEqual(row["status"], "done")
        out = row["output_path"]
        view = look_view_path(out)
        self.assertIn((OUT, view), self.db.files)
        self.assertEqual(max(opened(self.db.files[(OUT, view)]).size), 1440)
        for path in (out, look_thumbnail_path(out), view):
            self.assertEqual(upload_options(self.db, OUT, path)["cache-control"], YEAR)

    def test_view_copy_failure_never_fails_the_look(self):
        row = self.run_job(b"not-really-an-image")
        self.assertEqual(row["status"], "done")
        self.assertNotIn((OUT, look_view_path(row["output_path"])), self.db.files)

    def test_never_for_tryon_jobs(self):
        row = self.run_job(png(1800, 2400), {"generation_type": "tryon"})
        self.assertNotIn((OUT, look_view_path(row["output_path"])), self.db.files)


class GenerationsApiViewCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = strict_db()
        patch = mock.patch.object(generations_api, "get_supabase_admin_client", lambda: self.db)
        patch.start()
        self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def add_row(self, generation_type: str, output_path: str, original: str | None = None) -> str:
        generation_id = output_path.split("/")[1]
        self.db.tables.setdefault("generations", []).append(
            {"id": generation_id, "shop_id": SHOP, "status": "done", "generation_type": generation_type,
             "output_path": output_path, "original_output_path": original}
        )
        for path in (output_path, look_thumbnail_path(output_path), look_view_path(output_path)):
            self.db.files[(OUT, path)] = b"x"
        return generation_id

    def test_color_corrected_save_makes_view_and_replaces_earlier_one(self):
        gid = str(uuid.uuid4())
        original = f"{SHOP}/{gid}/output_v1.png"
        earlier = f"{SHOP}/{gid}/output-cc-1.jpg"
        self.add_row("look", earlier, original=original)
        for path in (original, look_thumbnail_path(original), look_view_path(original)):
            self.db.files[(OUT, path)] = b"orig"

        with contextlib.redirect_stdout(io.StringIO()):
            response = self.client.post(
                f"/generations/{gid}/color-corrected", files={"file": ("cc.png", png(1800, 2400), "image/png")}
            )
        self.assertEqual(response.status_code, 200, response.text)
        new_path = response.json()["output_path"]
        self.assertIn((OUT, look_view_path(new_path)), self.db.files)
        for path in (new_path, look_thumbnail_path(new_path), look_view_path(new_path)):
            self.assertEqual(upload_options(self.db, OUT, path)["cache-control"], YEAR)
        # The earlier corrected version goes with its thumbnail and view copy; the original stays.
        for path in (earlier, look_thumbnail_path(earlier), look_view_path(earlier)):
            self.assertNotIn((OUT, path), self.db.files)
        for path in (original, look_thumbnail_path(original), look_view_path(original)):
            self.assertEqual(self.db.files[(OUT, path)], b"orig")

    def test_restore_original_removes_current_view(self):
        gid = str(uuid.uuid4())
        original = f"{SHOP}/{gid}/output_v1.png"
        current = f"{SHOP}/{gid}/output-cc-1.jpg"
        self.add_row("look", current, original=original)
        response = self.client.post(f"/generations/{gid}/restore-original")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn((OUT, look_view_path(current)), self.db.files)

    def test_delete_look_removes_view_copies(self):
        gid = str(uuid.uuid4())
        original = f"{SHOP}/{gid}/output_v1.png"
        current = f"{SHOP}/{gid}/output-cc-1.jpg"
        self.add_row("look", current, original=original)
        self.db.files[(OUT, look_view_path(original))] = b"x"
        with contextlib.redirect_stdout(io.StringIO()):
            response = self.client.delete(f"/generations/{gid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn((OUT, look_view_path(current)), self.db.files)
        self.assertNotIn((OUT, look_view_path(original)), self.db.files)

    def test_delete_tryon_never_touches_view_path(self):
        gid = str(uuid.uuid4())
        path = f"{SHOP}/{gid}/output_v1.png"
        self.add_row("tryon", path)
        with contextlib.redirect_stdout(io.StringIO()):
            response = self.client.delete(f"/generations/{gid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn((OUT, look_view_path(path)), self.db.files)
        self.assertNotIn((OUT, look_view_path(path)), self.db.removed)


# ---------------------------------------------------------------- background fill-in


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def fake_sign(db, calls):
    def _sign(bucket, paths, expires_in=3600, *, raise_on_error=False):
        calls.append((bucket, list(paths)))
        return {p: ("https://signed/" + p if (bucket, p) in db.files else None) for p in paths}

    return _sign


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = strict_db()
        self.clock = Clock()
        self.sign_calls: list = []
        self.in_flight = itertools.repeat(0)

    def backfill(self, **overrides) -> picture_backfill.PictureBackfill:
        def _flight():
            value = next(self.in_flight)
            self.db.events.append(("in_flight", value))
            return value

        options = dict(
            client=self.db,
            jobs_in_flight=_flight,
            make_thumbnail=lambda data: b"thumb-from-" + str(len(data)).encode(),
            sign_existing=fake_sign(self.db, self.sign_calls),
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
        )
        options.update(overrides)
        return picture_backfill.PictureBackfill(**options)

    def add_generation(self, n: int, generation_type="look", status="done", files=("output",), image=None) -> str:
        gid = f"{n:08d}-0000-4000-8000-000000000000"
        path = f"{SHOP}/{gid}/output_v{n}.png"
        self.db.tables.setdefault("generations", []).append(
            {"id": gid, "shop_id": SHOP, "generation_type": generation_type, "status": status,
             "output_path": path, "created_at": f"2026-10-{n:02d}T00:00:00+00:00"}
        )
        content = image if image is not None else png(1600, 2000)
        if "output" in files:
            self.db.files[(OUT, path)] = content
        if "thumb" in files:
            self.db.files[(OUT, look_thumbnail_path(path))] = b"existing-thumb"
        if "view" in files:
            self.db.files[(OUT, look_view_path(path))] = b"existing-view"
        return path

    def run_pass(self, backfill=None) -> picture_backfill.PassCounts:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            counts = (backfill or self.backfill()).run_pass()
        self.summary = out.getvalue()
        return counts

    def downloads(self) -> list:
        return [e for e in self.db.events if e[0] == "download"]

    def test_only_finished_looks_and_only_missing_files(self):
        missing_both = self.add_generation(1)
        has_thumb = self.add_generation(2, files=("output", "thumb"))
        complete = self.add_generation(3, files=("output", "thumb", "view"))
        tryon = self.add_generation(4, generation_type="tryon")
        look_tryon = self.add_generation(5, generation_type="look_tryon")
        queued = self.add_generation(6, status="queued")

        counts = self.run_pass()

        self.assertEqual((counts.thumbs_made, counts.views_made, counts.failed), (1, 2, 0))
        self.assertIn((OUT, look_thumbnail_path(missing_both)), self.db.files)
        self.assertIn((OUT, look_view_path(missing_both)), self.db.files)
        self.assertIn((OUT, look_view_path(has_thumb)), self.db.files)
        downloaded = {path for _, _, path in self.downloads()}
        self.assertEqual(downloaded, {missing_both, has_thumb})
        for path in (complete, tryon, look_tryon, queued):
            self.assertNotIn(path, downloaded)
        for path in (tryon, look_tryon, queued):
            self.assertNotIn((OUT, look_view_path(path)), self.db.files)
            self.assertNotIn((OUT, look_thumbnail_path(path)), self.db.files)
        # ONE batch check for the page of looks, thumbnails and view copies together.
        look_checks = [c for c in self.sign_calls if c[0] == OUT]
        self.assertEqual(len(look_checks), 1)
        self.assertEqual(len(look_checks[0][1]), 6)
        self.assertIn("looks_checked=3", self.summary)

    def test_never_overwrites_and_never_deletes(self):
        path = self.add_generation(1, files=("output", "thumb"))
        before = dict(self.db.files)
        # The batch check says "missing" (stale), but the files are really there.
        stale = lambda bucket, paths, expires_in=3600, *, raise_on_error=False: {p: None for p in paths}  # noqa: E731
        self.db.files[(OUT, look_view_path(path))] = b"existing-view"
        counts = self.run_pass(self.backfill(sign_existing=stale))
        self.assertEqual(self.db.files[(OUT, look_thumbnail_path(path))], b"existing-thumb")
        self.assertEqual(self.db.files[(OUT, look_view_path(path))], b"existing-view")
        self.assertEqual(self.db.files[(OUT, path)], before[(OUT, path)])
        self.assertEqual(self.db.removed, [])
        for _bucket, _path, options in self.db.uploads:
            self.assertEqual(options["upsert"], "false")
            self.assertEqual(options["cache-control"], YEAR)
        self.assertEqual(counts.failed, 0)  # "already exists" is fine

    def test_pauses_while_jobs_in_flight(self):
        self.add_generation(1)
        self.in_flight = itertools.chain([1, 1, 1], itertools.repeat(0))
        self.run_pass()
        first_download = next(i for i, e in enumerate(self.db.events) if e[0] == "download")
        busy = [i for i, e in enumerate(self.db.events) if e == ("in_flight", 1)]
        self.assertEqual(len(busy), 3)
        self.assertTrue(all(i < first_download for i in busy))
        self.assertGreaterEqual(self.clock.sleeps.count(picture_backfill.IDLE_CHECK_SECONDS), 3)

    def test_no_work_at_all_while_busy(self):
        self.add_generation(1)
        backfill = self.backfill()
        self.in_flight = itertools.repeat(1)
        calls = {"n": 0}

        def _sleep(seconds):
            calls["n"] += 1
            if calls["n"] >= 5:
                backfill.stop.set()

        backfill.sleep = _sleep
        self.run_pass(backfill)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.db.uploads, [])
        self.assertNotIn(("generations", "select", None), self.db.log)

    def test_one_second_between_pictures(self):
        for n in range(1, 5):
            self.add_generation(n)
        times = []
        original = StrictBucket.download

        def _download(bucket_self, path):
            times.append(self.clock.now)
            return original(bucket_self, path)

        with mock.patch.object(StrictBucket, "download", _download):
            self.run_pass()
        self.assertEqual(len(times), 4)
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(times, times[1:])), times)

    def test_light_original_needs_no_view_and_is_remembered(self):
        path = self.add_generation(1, files=("output", "thumb"), image=jpeg(1080, 1440))
        counts = self.run_pass()
        self.assertEqual((counts.views_made, counts.views_not_needed), (0, 1))
        self.assertNotIn((OUT, look_view_path(path)), self.db.files)
        backfill = self.backfill()
        backfill._view_not_needed.add(path)
        self.db.events.clear()
        self.run_pass(backfill)
        self.assertEqual(self.downloads(), [])

    def test_failed_picture_is_skipped_and_others_continue(self):
        bad = self.add_generation(1, image=b"broken")
        good = self.add_generation(2)
        counts = self.run_pass()
        self.assertEqual(counts.failed, 1)
        self.assertIn((OUT, look_view_path(good)), self.db.files)
        self.assertNotIn((OUT, look_view_path(bad)), self.db.files)

    def test_failed_batch_check_skips_batch(self):
        self.add_generation(1)

        def _failing(bucket, paths, expires_in=3600, *, raise_on_error=False):
            self.assertTrue(raise_on_error)
            raise RuntimeError("storage down")

        counts = self.run_pass(self.backfill(sign_existing=_failing))
        self.assertEqual(self.downloads(), [])
        self.assertGreaterEqual(counts.batches_skipped, 1)

    def test_pages_of_100_newest_first(self):
        for n in range(1, 28):
            self.add_generation(n, files=("output", "thumb", "view"))
        with mock.patch.object(picture_backfill, "BATCH", 10):
            counts = self.run_pass()
        self.assertEqual(counts.looks_checked, 27)
        look_checks = [c for c in self.sign_calls if c[0] == OUT]
        self.assertEqual(len(look_checks), 3)
        self.assertTrue(look_checks[0][1][0].endswith("/thumb_v27.jpg"), look_checks[0][1][0])

    def test_fabric_thumbnails(self):
        self.db.tables["fabric_images"] = [
            {"id": "f1", "storage_path": f"{SHOP}/fab/a.png", "created_at": "2026-10-02"},
            {"id": "f2", "storage_path": f"{SHOP}/fab/b.jpg", "created_at": "2026-10-01"},
        ]
        self.db.files[(FAB, f"{SHOP}/fab/a.png")] = png(1200, 800)
        self.db.files[(FAB, f"{SHOP}/fab/b.jpg")] = jpeg(800, 800)
        self.db.files[(FAB, f"{SHOP}/fab/b_thumb.jpg")] = b"existing"
        counts = self.run_pass()
        self.assertEqual(counts.fabric_thumbs_made, 1)
        thumb = opened(self.db.files[(FAB, f"{SHOP}/fab/a_thumb.jpg")])
        self.assertEqual(thumb.size, (400, 267))
        self.assertEqual(self.db.files[(FAB, f"{SHOP}/fab/b_thumb.jpg")], b"existing")
        self.assertEqual(upload_options(self.db, FAB, f"{SHOP}/fab/a_thumb.jpg")["cache-control"], YEAR)

    def test_run_pass_never_raises(self):
        self.db.fail[("generations", "select")] = "db down"
        self.db.fail[("fabric_images", "select")] = "db down"
        counts = self.run_pass()
        self.assertEqual(counts.batches_skipped, 2)
        self.assertIn("pass done", self.summary)

    def test_run_forever_timing(self):
        backfill = self.backfill()
        passes = []

        def _pass():
            passes.append(self.clock.now)
            if len(passes) == 2:
                backfill.stop.set()
            return picture_backfill.PassCounts(views_made=1 if len(passes) == 1 else 0)

        backfill.run_pass = _pass
        backfill.run_forever()
        self.assertEqual(self.clock.sleeps[0], picture_backfill.FIRST_PASS_DELAY_SECONDS)
        self.assertEqual(self.clock.sleeps[1], picture_backfill.REST_AFTER_WORK_SECONDS)
        self.assertEqual(self.clock.sleeps[2], picture_backfill.REST_WHEN_DONE_SECONDS)

    def test_setting(self):
        self.assertTrue(picture_backfill.enabled(None))
        self.assertTrue(picture_backfill.enabled("true"))
        self.assertTrue(picture_backfill.enabled(""))
        self.assertFalse(picture_backfill.enabled("false"))
        self.assertFalse(picture_backfill.enabled(" FALSE "))
        self.assertEqual(get_settings().PICTURE_BACKFILL_ENABLED, "true")


# ---------------------------------------------------------------- garment card links


class GarmentLinkReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fakes.FakeSupabase()
        self.db.tables.update(
            {
                "garment_types": [
                    {"id": "g1", "shop_id": SHOP, "name": "Kurta", "prompt_template": None,
                     "default_hero_image_id": "h1", "is_active": True},
                    {"id": "g2", "shop_id": SHOP, "name": "Sherwani", "prompt_template": None,
                     "default_hero_image_id": "h2", "is_active": True},
                ],
                "hero_images": [
                    {"id": "h1", "shop_id": SHOP, "storage_path": f"{SHOP}/f/h1.png"},
                    {"id": "h2", "shop_id": SHOP, "storage_path": f"{SHOP}/f/h2.png"},
                ],
                "garment_fabric_slots": [],
            }
        )
        garment_types_api._hero_image_url_cache.clear()
        garment_types_api._hero_thumb_url_cache.clear()
        self.addCleanup(garment_types_api._hero_image_url_cache.clear)
        self.addCleanup(garment_types_api._hero_thumb_url_cache.clear)
        self.counter = itertools.count(1)
        self.hero_sign_calls: list = []
        self.thumb_sign_calls: list = []

        def _hero_batch(supabase, paths):
            self.hero_sign_calls.append(list(paths))
            return {p: f"https://signed/{p}?token={next(self.counter)}" for p in paths}

        def _thumb_sign(bucket, paths, expires_in=3600, **_kw):
            self.thumb_sign_calls.append((list(paths), expires_in))
            return {p: f"https://signed/{p}?token={next(self.counter)}" for p in paths}

        for patch in (
            mock.patch.object(garment_types_api, "get_supabase_admin_client", lambda: self.db),
            mock.patch.object(garment_types_api, "_create_hero_image_signed_urls_batch", _hero_batch),
            mock.patch.object(garment_types_api, "sign_existing_paths", _thumb_sign),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.current = SimpleNamespace(shop_id=SHOP)

    def call(self):
        return garment_types_api.list_garment_types(current=self.current)

    def test_same_links_are_returned_while_valid(self):
        first = self.call()
        second = self.call()
        self.assertEqual(first, second)
        self.assertEqual(len(self.hero_sign_calls), 1)
        self.assertEqual(len(self.thumb_sign_calls), 1)
        self.assertEqual(self.thumb_sign_calls[0][1], 6 * 60 * 60)
        self.assertTrue(all(item["hero_image_signed_url"] for item in first))
        self.assertTrue(all(item["hero_thumb_signed_url"] for item in first))

    def test_response_shape_unchanged(self):
        item = self.call()[0]
        self.assertEqual(
            set(item),
            {"id", "name", "prompt_template", "default_hero_image_id", "hero_image_signed_url",
             "hero_thumb_signed_url", "fabric_slots"},
        )

    def test_reuse_window_shorter_than_link_life(self):
        self.assertEqual(garment_types_api._SIGNED_URL_SECONDS, 6 * 60 * 60)
        self.assertEqual(garment_types_api._hero_image_url_cache.ttl, 4 * 60 * 60)
        self.assertEqual(garment_types_api._hero_thumb_url_cache.ttl, 4 * 60 * 60)

    def test_only_new_paths_are_signed(self):
        self.call()
        self.db.tables["garment_types"].append(
            {"id": "g3", "shop_id": SHOP, "name": "Achkan", "prompt_template": None,
             "default_hero_image_id": "h3", "is_active": True}
        )
        self.db.tables["hero_images"].append({"id": "h3", "shop_id": SHOP, "storage_path": f"{SHOP}/f/h3.png"})
        self.call()
        self.assertEqual(self.hero_sign_calls[-1], [f"{SHOP}/f/h3.png"])

    def test_missing_links_are_not_remembered(self):
        def _none(supabase, paths):
            self.hero_sign_calls.append(list(paths))
            return {p: None for p in paths}

        with mock.patch.object(garment_types_api, "_create_hero_image_signed_urls_batch", _none):
            self.call()
        self.call()
        self.assertEqual(len(self.hero_sign_calls), 2)

    def test_expired_entries_are_signed_again(self):
        self.call()
        garment_types_api._hero_image_url_cache.clear()  # what the 4-hour expiry does
        garment_types_api._hero_thumb_url_cache.clear()
        third = self.call()
        self.assertEqual(len(self.hero_sign_calls), 2)
        self.assertTrue(all(item["hero_image_signed_url"] for item in third))


if __name__ == "__main__":
    unittest.main()
