"""Memory-safe picture backfill (originals checked first, failures remembered for 24 h, bounded
pictures, no overlap with jobs, memory guard, per-pass cap) and POST
/fabric-images/{id}/replace-photo. Mocked: Supabase and storage (in memory). No network, no real
database, no real AI."""
from tests import fakes  # noqa: F401  (fake settings first)

import contextlib
import io
import itertools
import unittest
from collections import Counter
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image, ImageFile
from PIL.JpegImagePlugin import JpegImageFile

import images_api
import picture_backfill
import process_memory
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from image_thumbs import fabric_thumbnail_path, look_thumbnail_path, look_view_path
from main import app
from tests.test_speed_round import jpeg, opened, png, strict_db, upload_options, StrictBucket

SHOP = "11111111-1111-4111-8111-111111111111"
OTHER_SHOP = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
OUT = "generated-outputs"
FAB = "fabric-images"
YEAR = "31536000"
HOUR = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(seconds)

    def monotonic(self) -> float:
        return self.now


def fake_sign(db, calls):
    def _sign(bucket, paths, expires_in=3600, *, raise_on_error=False):
        calls.append((bucket, list(paths)))
        return {p: ("https://signed/" + p if (bucket, p) in db.files else None) for p in paths}

    return _sign


def bilevel_png(width: int, height: int) -> bytes:
    """A real PNG with a big header size but tiny bytes (1 bit per pixel, all white)."""
    buf = io.BytesIO()
    Image.new("1", (width, height), 1).save(buf, format="PNG")
    return buf.getvalue()


class BackfillCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = strict_db()
        self.clock = Clock()
        self.sign_calls: list = []
        self.in_flight = itertools.repeat(0)
        self.rss = 100.0
        self.releases: list[bool] = []

    def backfill(self, **overrides) -> picture_backfill.PictureBackfill:
        def _flight():
            value = next(self.in_flight)
            self.db.events.append(("in_flight", value))
            return value

        options = dict(
            client=self.db,
            jobs_in_flight=_flight,
            download=lambda bucket, path: bytes(self.db.storage.from_(bucket).download(path)),
            sign_existing=fake_sign(self.db, self.sign_calls),
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
            memory_mb=lambda: self.rss,
            release=self.releases.append,
            max_memory=320.0,
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
        content = image if image is not None else png(1200, 1600)
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
        self.output = out.getvalue()
        return counts

    def downloads(self) -> list:
        return [e[2] for e in self.db.events if e[0] == "download"]


# ---------------------------------------------------------------- selection and safety


class SelectionTests(BackfillCase):
    def test_only_finished_looks_and_only_missing_files(self):
        missing_both = self.add_generation(1)
        has_thumb = self.add_generation(2, files=("output", "thumb"))
        complete = self.add_generation(3, files=("output", "thumb", "view"))
        tryon = self.add_generation(4, generation_type="tryon")
        look_tryon = self.add_generation(5, generation_type="look_tryon")
        queued = self.add_generation(6, status="queued")

        counts = self.run_pass()

        self.assertEqual((counts.thumbs_made, counts.views_made, counts.failed), (1, 2, 0))
        self.assertEqual(set(self.downloads()), {missing_both, has_thumb})
        for path in (complete, tryon, look_tryon, queued):
            self.assertNotIn(path, self.downloads())
        for path in (tryon, look_tryon, queued):
            self.assertNotIn((OUT, look_view_path(path)), self.db.files)
            self.assertNotIn((OUT, look_thumbnail_path(path)), self.db.files)
        # ONE batch check per page: originals, thumbnails and view copies together.
        look_checks = [c for c in self.sign_calls if c[0] == OUT]
        self.assertEqual(len(look_checks), 1)
        self.assertEqual(len(look_checks[0][1]), 9)
        self.assertIn(missing_both, look_checks[0][1])

    def test_made_pictures_follow_the_rules(self):
        path = self.add_generation(1, image=png(1800, 2400))
        self.run_pass()
        view = opened(self.db.files[(OUT, look_view_path(path))])
        thumb = opened(self.db.files[(OUT, look_thumbnail_path(path))])
        self.assertEqual((view.format, view.size), ("JPEG", (1080, 1440)))
        self.assertEqual((thumb.format, thumb.size), ("JPEG", (400, 533)))
        for target in (look_view_path(path), look_thumbnail_path(path)):
            options = upload_options(self.db, OUT, target)
            self.assertEqual((options["upsert"], options["cache-control"]), ("false", YEAR))

    def test_never_overwrites_and_never_deletes(self):
        path = self.add_generation(1, files=("output", "thumb", "view"))
        before = dict(self.db.files)

        def stale(bucket, paths, expires_in=3600, *, raise_on_error=False):
            # Stale answer: originals exist, thumbnails and view copies "missing" (they are there).
            return {p: ("https://signed/" + p if "/output" in p else None) for p in paths}

        counts = self.run_pass(self.backfill(sign_existing=stale))
        self.assertEqual(self.db.files, before)
        self.assertEqual(self.db.removed, [])
        self.assertTrue(self.db.uploads)
        self.assertTrue(all(options["upsert"] == "false" for _b, _p, options in self.db.uploads))
        self.assertEqual(counts.failed, 0)  # "already exists" is fine
        self.assertEqual(self.db.files[(OUT, look_view_path(path))], b"existing-view")

    def test_pages_newest_first(self):
        for n in range(1, 28):
            self.add_generation(n, files=("output", "thumb", "view"))
        with mock.patch.object(picture_backfill, "BATCH", 10):
            counts = self.run_pass()
        self.assertEqual(counts.looks_checked, 27)
        look_checks = [c for c in self.sign_calls if c[0] == OUT]
        self.assertEqual(len(look_checks), 3)
        self.assertTrue(look_checks[0][1][0].endswith("/output_v27.png"))

    def test_fabric_thumbnails(self):
        self.db.tables["fabric_images"] = [
            {"id": "f1", "storage_path": f"{SHOP}/fab/a.png", "created_at": "2026-10-03"},
            {"id": "f2", "storage_path": f"{SHOP}/fab/b.jpg", "created_at": "2026-10-02"},
        ]
        self.db.files[(FAB, f"{SHOP}/fab/a.png")] = png(1200, 800)
        self.db.files[(FAB, f"{SHOP}/fab/b.jpg")] = jpeg(800, 800)
        self.db.files[(FAB, f"{SHOP}/fab/b_thumb.jpg")] = b"existing"
        counts = self.run_pass()
        self.assertEqual(counts.fabric_thumbs_made, 1)
        self.assertEqual(opened(self.db.files[(FAB, f"{SHOP}/fab/a_thumb.jpg")]).size, (400, 267))
        self.assertEqual(self.db.files[(FAB, f"{SHOP}/fab/b_thumb.jpg")], b"existing")
        fabric_checks = [c for c in self.sign_calls if c[0] == FAB]
        self.assertEqual(len(fabric_checks), 1)
        self.assertEqual(len(fabric_checks[0][1]), 4)  # photos + small copies, ONE call

    def test_failed_batch_check_skips_batch(self):
        self.add_generation(1)

        def failing(bucket, paths, expires_in=3600, *, raise_on_error=False):
            self.assertTrue(raise_on_error)
            raise RuntimeError("storage down")

        counts = self.run_pass(self.backfill(sign_existing=failing))
        self.assertEqual(self.downloads(), [])
        self.assertGreaterEqual(counts.batches_skipped, 1)

    def test_run_pass_never_raises(self):
        self.db.fail[("generations", "select")] = "db down"
        self.db.fail[("fabric_images", "select")] = "db down"
        counts = self.run_pass()
        self.assertEqual(counts.batches_skipped, 2)
        self.assertIn("pass done", self.output)


# ---------------------------------------------------------------- 1a originals checked first


class MissingOriginalTests(BackfillCase):
    def test_missing_original_is_not_downloaded(self):
        gone = self.add_generation(1, files=())  # row exists, output file does not
        present = self.add_generation(2)
        counts = self.run_pass()
        self.assertNotIn(gone, self.downloads())
        self.assertIn(present, self.downloads())
        self.assertEqual((counts.originals_missing, counts.failed), (1, 0))
        self.assertIn("originals_missing=1", self.output)

    def test_missing_fabric_photo_is_not_downloaded(self):
        self.db.tables["fabric_images"] = [{"id": "f1", "storage_path": f"{SHOP}/fab/gone.png", "created_at": "1"}]
        counts = self.run_pass()
        self.assertEqual(self.downloads(), [])
        self.assertEqual((counts.originals_missing, counts.failed), (1, 0))


# ---------------------------------------------------------------- 1b failures remembered


class FailureMemoryTests(BackfillCase):
    def test_failed_picture_not_retried_within_24_hours(self):
        bad = self.add_generation(1, image=b"broken")
        good = self.add_generation(2)
        backfill = self.backfill()

        counts = self.run_pass(backfill)
        self.assertEqual(counts.failed, 1)
        self.assertEqual(counts.errors, Counter({"UnidentifiedImageError": 1}))
        self.assertIn("errors=UnidentifiedImageError:1", self.output)
        self.assertIn((OUT, look_view_path(good)), self.db.files)

        self.db.events.clear()
        self.clock.now += 23 * HOUR
        counts = self.run_pass(backfill)
        self.assertNotIn(bad, self.downloads())
        self.assertEqual((counts.retry_later, counts.failed), (1, 0))

        self.db.events.clear()
        self.clock.now += 2 * HOUR
        counts = self.run_pass(backfill)
        self.assertIn(bad, self.downloads())
        self.assertEqual(counts.failed, 1)

    def test_failures_never_shorten_the_rest(self):
        backfill = self.backfill()
        results = [
            picture_backfill.PassCounts(errors=Counter({"OSError": 824})),
            picture_backfill.PassCounts(views_made=1),
            picture_backfill.PassCounts(),
        ]

        def _pass():
            counts = results.pop(0)
            if not results:
                backfill.stop.set()
            return counts

        backfill.run_pass = _pass
        backfill.run_forever()
        self.assertEqual(
            self.clock.sleeps,
            [
                picture_backfill.FIRST_PASS_DELAY_SECONDS,
                picture_backfill.REST_WHEN_DONE_SECONDS,  # only failures: 6 hours
                picture_backfill.REST_AFTER_WORK_SECONDS,  # made something: 15 minutes
                picture_backfill.REST_WHEN_DONE_SECONDS,
            ],
        )

    def test_pace_constants(self):
        self.assertEqual(picture_backfill.FIRST_PASS_DELAY_SECONDS, 5 * 60)
        self.assertEqual(picture_backfill.REST_AFTER_WORK_SECONDS, 15 * 60)
        self.assertEqual(picture_backfill.REST_WHEN_DONE_SECONDS, 6 * 60 * 60)
        self.assertEqual(picture_backfill.MAX_PICTURES_PER_PASS, 150)
        self.assertEqual(picture_backfill.RETRY_AFTER_FAILURE_SECONDS, 24 * 60 * 60)


# ---------------------------------------------------------------- 1c bounded pictures


class TooBigTests(BackfillCase):
    def test_too_many_pixels_skipped_before_decoding(self):
        path = self.add_generation(1, image=bilevel_png(5000, 4000))  # 20 megapixels, tiny file
        loads = []
        real_load = ImageFile.ImageFile.load
        with mock.patch.object(ImageFile.ImageFile, "load", lambda im: loads.append(im.size) or real_load(im)):
            counts = self.run_pass()
        self.assertEqual(loads, [])
        self.assertEqual((counts.too_big, counts.failed), (1, 0))
        self.assertIn("too_big=1", self.output)
        self.assertNotIn((OUT, look_view_path(path)), self.db.files)

    def test_over_8_mb_skipped_and_remembered(self):
        path = self.add_generation(1, image=b"x" * (8 * 1024 * 1024 + 1))
        backfill = self.backfill()
        counts = self.run_pass(backfill)
        self.assertEqual(counts.too_big, 1)
        self.db.events.clear()
        self.run_pass(backfill)
        self.assertNotIn(path, self.downloads())

    def test_download_stops_at_8_mb(self):
        class Response:
            def __init__(self, length, chunks):
                self.headers = {"content-length": str(length)} if length else {}
                self.chunks = chunks
                self.read = 0

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                for chunk in self.chunks:
                    self.read += 1
                    yield chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        declared = Response(9 * 1024 * 1024, [b"x"])
        with mock.patch("httpx.stream", lambda *a, **k: declared):
            with self.assertRaises(picture_backfill.TooBig):
                picture_backfill.download_capped(OUT, "s/g/output.png")
        self.assertEqual(declared.read, 0)  # refused from the header, nothing read

        chunked = Response(None, itertools.repeat(b"x" * (1024 * 1024)))
        with mock.patch("httpx.stream", lambda *a, **k: chunked):
            with self.assertRaises(picture_backfill.TooBig):
                picture_backfill.download_capped(OUT, "s/g/output.png")
        self.assertEqual(chunked.read, 9)

        small = Response(5, [b"ab", b"cde"])
        with mock.patch("httpx.stream", lambda *a, **k: small):
            self.assertEqual(picture_backfill.download_capped(OUT, "s/g/output.png"), b"abcde")

    def test_jpeg_decoded_in_draft_mode(self):
        drafts = []
        real_draft = JpegImageFile.draft

        def spy(im, mode, size):
            result = real_draft(im, mode, size)
            drafts.append(im.size)
            return result

        with mock.patch.object(JpegImageFile, "draft", spy):
            pictures = picture_backfill.make_look_pictures(jpeg(4000, 3000), need_thumb=True, need_view=True)
        self.assertEqual(drafts, [(2000, 1500)])  # the decoder never built the 12 MP image
        self.assertEqual(opened(pictures.view).size, (1440, 1080))
        self.assertEqual(opened(pictures.thumb).size, (400, 300))

    def test_decoded_size_is_bounded_for_non_jpeg(self):
        with self.assertRaises(picture_backfill.TooBig):
            picture_backfill.make_look_pictures(bilevel_png(3200, 2600), need_thumb=True, need_view=True)

    def test_light_jpeg_needs_no_view_and_no_decode(self):
        loads = []
        real_load = ImageFile.ImageFile.load
        with mock.patch.object(ImageFile.ImageFile, "load", lambda im: loads.append(1) or real_load(im)):
            pictures = picture_backfill.make_look_pictures(jpeg(1080, 1440), need_thumb=False, need_view=True)
        self.assertEqual((pictures.view, pictures.thumb, pictures.view_not_needed), (None, None, True))
        self.assertEqual(loads, [])

    def test_upright_and_transparency(self):
        pictures = picture_backfill.make_look_pictures(
            jpeg(1600, 1000, exif_orientation=6), need_thumb=True, need_view=True
        )
        self.assertEqual(opened(pictures.view).size, (900, 1440))
        self.assertEqual(opened(pictures.thumb).size, (400, 640))
        clear = picture_backfill.make_fabric_thumbnail_bounded(png(500, 500, mode="RGBA", color=(0, 0, 0, 0)))
        self.assertGreater(min(opened(clear).getpixel((5, 5))), 245)


# ---------------------------------------------------------------- 1d never overlap a job


class JobOverlapTests(BackfillCase):
    def test_pauses_while_jobs_in_flight(self):
        self.add_generation(1)
        self.in_flight = itertools.chain([1, 1, 1], itertools.repeat(0))
        self.run_pass()
        first_download = next(i for i, e in enumerate(self.db.events) if e[0] == "download")
        busy = [i for i, e in enumerate(self.db.events) if e == ("in_flight", 1)]
        self.assertEqual(len(busy), 3)
        self.assertTrue(all(i < first_download for i in busy))

    def test_job_started_during_download_drops_bytes_before_decoding(self):
        path = self.add_generation(1)
        state = {"busy": 0, "downloads": 0}

        def download(bucket, p):
            state["downloads"] += 1
            if state["downloads"] == 1:
                state["busy"] = 1  # a look job was claimed while we were downloading
            return bytes(self.db.storage.from_(bucket).download(p))

        def on_sleep(_seconds):
            state["busy"] = 0  # the job finished

        self.clock.on_sleep = on_sleep
        decoded_while = []
        real = picture_backfill.make_look_pictures

        def spy(data, **kwargs):
            decoded_while.append(state["busy"])
            return real(data, **kwargs)

        backfill = self.backfill(download=download, jobs_in_flight=lambda: state["busy"])
        with mock.patch.object(picture_backfill, "make_look_pictures", spy):
            counts = self.run_pass(backfill)
        self.assertEqual(state["downloads"], 2)
        self.assertEqual(decoded_while, [0])
        self.assertEqual(counts.paused_for_job, 1)
        self.assertIn((OUT, look_view_path(path)), self.db.files)

    def test_no_work_at_all_while_busy(self):
        self.add_generation(1)
        backfill = self.backfill()
        self.in_flight = itertools.repeat(1)
        calls = {"n": 0}

        def _sleep(_seconds):
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


# ---------------------------------------------------------------- 1e / 1f memory


class MemoryTests(BackfillCase):
    def test_release_after_every_picture_and_trim_every_20(self):
        for n in range(1, 6):
            self.add_generation(n)
        with mock.patch.object(picture_backfill, "TRIM_EVERY_PICTURES", 2):
            self.run_pass()
        # 5 pictures: gc after each, trim after the 2nd and 4th; plus one trim at the end of the pass.
        self.assertEqual(self.releases, [False, True, False, True, False, True])

    def test_above_the_limit_nothing_is_processed(self):
        self.add_generation(1)
        self.add_generation(2)
        self.rss = 400.0
        backfill = self.backfill()
        calls = {"n": 0}

        def _sleep(seconds):
            calls["n"] += 1
            if seconds == picture_backfill.MEMORY_WAIT_SECONDS and calls["n"] >= 4:
                backfill.stop.set()

        backfill.sleep = _sleep
        counts = self.run_pass(backfill)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.db.uploads, [])
        self.assertEqual(counts.made, 0)
        self.assertTrue(self.releases and all(self.releases))  # freed memory, with malloc_trim
        warnings = [line for line in self.output.splitlines() if "PICTURE_BACKFILL_MAX_MEMORY_MB" in line]
        self.assertEqual(len(warnings), 1)

    def test_resumes_when_memory_drops(self):
        path = self.add_generation(1)
        self.rss = 400.0
        self.clock.on_sleep = lambda seconds: setattr(self, "rss", 200.0)
        self.run_pass()
        self.assertIn((OUT, look_view_path(path)), self.db.files)
        self.assertIn(picture_backfill.MEMORY_WAIT_SECONDS, self.clock.sleeps)

    def test_memory_lines_at_pass_start_and_end(self):
        self.rss = 212.4
        self.run_pass()
        self.assertIn("[worker] memory: 212 MB (backfill pass start)", self.output)
        self.assertIn("[worker] memory: 212 MB (backfill pass end)", self.output)

    def test_settings(self):
        self.assertEqual(picture_backfill.max_memory_mb("320"), 320.0)
        self.assertEqual(picture_backfill.max_memory_mb("450"), 450.0)
        for bad in ("", None, "abc", "0", "-5"):
            self.assertEqual(picture_backfill.max_memory_mb(bad), 320.0)
        self.assertEqual(get_settings().PICTURE_BACKFILL_MAX_MEMORY_MB, "320")
        self.assertTrue(picture_backfill.enabled(None))
        self.assertFalse(picture_backfill.enabled(" FALSE "))

    def test_process_memory_never_raises(self):
        value = process_memory.rss_mb()
        self.assertTrue(value is None or value > 0)
        self.assertIsInstance(process_memory.describe(), str)
        process_memory.release(True)  # malloc_trim is a no-op where glibc is missing


# ---------------------------------------------------------------- 1g cap per pass


class CapTests(BackfillCase):
    def test_at_most_n_pictures_per_pass(self):
        for n in range(1, 6):
            self.add_generation(n)
        self.db.tables["fabric_images"] = [{"id": "f1", "storage_path": f"{SHOP}/fab/a.png", "created_at": "1"}]
        self.db.files[(FAB, f"{SHOP}/fab/a.png")] = png(600, 600)
        with mock.patch.object(picture_backfill, "MAX_PICTURES_PER_PASS", 3):
            counts = self.run_pass()
        self.assertEqual(counts.pictures_made, 3)
        self.assertEqual(len(self.downloads()), 3)
        self.assertTrue(counts.cap_reached)
        self.assertEqual(counts.fabric_thumbs_made, 0)  # the pass ended at the cap
        self.assertIn("cap_reached=True", self.output)


# ---------------------------------------------------------------- PART 3: replace a fabric's photo


FABRIC = "66666666-6666-4666-8666-666666666666"
OLD = f"{SHOP}/fabrics/old.png"
NEW = f"{SHOP}/fabrics/new-matched.jpg"


class ReplaceFabricPhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = strict_db()
        self.db.tables["fabric_images"] = [
            {"id": FABRIC, "shop_id": SHOP, "storage_path": OLD, "original_filename": "silk.png",
             "mime_type": "image/png", "file_size_bytes": 1000, "width": 800, "height": 600,
             "created_at": "2026-10-01T00:00:00+00:00"},
        ]
        for path in (OLD, fabric_thumbnail_path(OLD), NEW):
            self.db.files[(FAB, path)] = b"x"
        patch = mock.patch.object(images_api, "get_supabase_admin_client", lambda: self.db)
        patch.start()
        self.addCleanup(patch.stop)
        app.dependency_overrides[get_current_shop_context] = lambda: CurrentShopContext(
            auth_user_id=USER, email="demo@gmail.com", shop_id=SHOP, role="owner"
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def replace(self, body: dict, fabric_id: str = FABRIC):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            response = self.client.post(f"/fabric-images/{fabric_id}/replace-photo", json=body)
        self.output = out.getvalue()
        return response

    def row(self) -> dict:
        return next(r for r in self.db.tables["fabric_images"] if r["id"] == FABRIC)

    def assert_untouched(self):
        self.assertEqual(self.row()["storage_path"], OLD)
        self.assertIn((FAB, OLD), self.db.files)
        self.assertEqual(self.db.removed, [])

    def test_other_shops_fabric_is_404(self):
        self.row()["shop_id"] = OTHER_SHOP
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 404)
        self.assert_untouched()

    def test_unknown_fabric_is_404(self):
        self.assertEqual(self.replace({"storage_path": NEW}, fabric_id="nope").status_code, 404)
        self.assert_untouched()

    def test_bad_paths_are_400(self):
        for path in (f"{OTHER_SHOP}/fabrics/x.jpg", f"{SHOP}/../x.jpg", "/abs.jpg", f"temp/{SHOP}/x.jpg",
                     f"{SHOP}/fabrics/new_thumb.jpg", f"{SHOP}/fabrics/NEW_THUMB.JPG"):
            with self.subTest(path=path):
                self.assertEqual(self.replace({"storage_path": path}).status_code, 400)
        self.assert_untouched()

    def test_same_path_is_400(self):
        self.assertEqual(self.replace({"storage_path": OLD}).status_code, 400)
        self.assert_untouched()

    def test_missing_storage_path_is_422(self):
        self.assertEqual(self.replace({}).status_code, 422)
        self.assert_untouched()

    def test_success(self):
        response = self.replace(
            {"storage_path": NEW, "mime_type": "image/jpeg", "file_size_bytes": 2048, "width": 900, "height": 700}
        )
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row()
        self.assertEqual(
            {k: row[k] for k in ("id", "storage_path", "original_filename", "mime_type", "file_size_bytes",
                                 "width", "height", "created_at", "shop_id")},
            {"id": FABRIC, "storage_path": NEW, "original_filename": "silk.png", "mime_type": "image/jpeg",
             "file_size_bytes": 2048, "width": 900, "height": 700,
             "created_at": "2026-10-01T00:00:00+00:00", "shop_id": SHOP},
        )
        self.assertEqual(len(self.db.tables["fabric_images"]), 1)
        # Same shape as GET /fabric-images/{id}.
        self.assertEqual(response.json(), self.client.get(f"/fabric-images/{FABRIC}").json())
        # The previous photo and its small copy are gone; the new photo stays.
        self.assertNotIn((FAB, OLD), self.db.files)
        self.assertNotIn((FAB, fabric_thumbnail_path(OLD)), self.db.files)
        self.assertIn((FAB, NEW), self.db.files)
        self.assertEqual(sorted(self.db.removed), sorted([(FAB, OLD), (FAB, fabric_thumbnail_path(OLD))]))

    def test_optional_fields_left_out_are_cleared(self):
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row()
        self.assertEqual((row["mime_type"], row["file_size_bytes"], row["width"], row["height"]),
                         (None, None, None, None))

    # --- the three delete guards

    def test_guard_old_path_outside_own_folder_is_kept(self):
        legacy = f"{OTHER_SHOP}/fabrics/shared.png"
        self.row()["storage_path"] = legacy
        self.db.files[(FAB, legacy)] = b"x"
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn((FAB, legacy), self.db.files)
        self.assertEqual(self.db.removed, [])

    def test_guard_never_deletes_the_new_path(self):
        # Pretend the old photo's small copy has the new photo's path: it must never be deleted.
        with mock.patch.object(images_api, "fabric_thumbnail_path", lambda p: NEW if p == OLD else p + "_t.jpg"):
            response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn((FAB, NEW), self.db.files)
        self.assertNotIn((FAB, NEW), self.db.removed)
        self.assertEqual(self.db.removed, [(FAB, OLD)])

    def test_guard_file_used_by_another_row_is_kept(self):
        self.db.tables["fabric_images"].append(
            {"id": "77777777-7777-4777-8777-777777777777", "shop_id": SHOP, "storage_path": OLD,
             "original_filename": "silk copy.png", "created_at": "2026-10-02T00:00:00+00:00"}
        )
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn((FAB, OLD), self.db.files)
        self.assertIn((FAB, fabric_thumbnail_path(OLD)), self.db.files)
        self.assertEqual(self.db.removed, [])

    def test_delete_failure_still_succeeds(self):
        self.db.storage_fail[(FAB, "remove")] = "storage down"
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row()["storage_path"], NEW)
        self.assertEqual(len([l for l in self.output.splitlines() if "previous fabric photo not deleted" in l]), 1)

    def test_failed_update_deletes_nothing(self):
        self.db.fail[("fabric_images", "update")] = "db down"
        response = self.replace({"storage_path": NEW})
        self.assertEqual(response.status_code, 500)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
