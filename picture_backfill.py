"""Worker-only background fill-in of light pictures for EXISTING files. Started from worker.main,
never from the API.

- Looks: generations with generation_type 'look', status 'done' and an output_path, newest first,
  100 at a time. ONE batch check per 100 (image_thumbs.sign_existing_paths) says which originals,
  thumbnails and view copies exist. A missing original is skipped without any download
  (originals_missing). A missing thumbnail / view copy is made from the original.
- Fabrics: fabric_images rows, newest first, 100 at a time, same ONE batch check (photo and its
  small copy) in the fabric-images bucket; a missing "<name>_thumb.jpg" is made.

Memory (the worker runs close to its limit, and a look job must never be pushed over it):
- every picture is bounded: originals over 8 MB (the download stops there) or over 16 megapixels
  (read from the header, before decoding) are skipped as too_big; JPEGs are decoded in draft
  mode at the size actually needed; the decoded image itself may not exceed 24 MB;
- jobs_in_flight is checked before the download AND again before decoding;
- after every picture the buffers are released and gc.collect() runs; every 20 pictures
  malloc_trim(0) hands freed memory back to the OS;
- before every picture the process memory (RSS) is read: above PICTURE_BACKFILL_MAX_MEMORY_MB
  nothing is done until it drops.

Pace: one picture at a time, at least 1 s apart, nothing while any look job is in flight. First
pass 5 minutes after the worker starts; at most 150 pictures per pass; a pass that made pictures
rests 15 minutes, one that made nothing rests 6 hours. A picture that fails (or is too big) is
not tried again for 24 hours, and failures never make the next pass come sooner.

Safety: uploads use upsert OFF ("already exists" is fine), so no file is ever overwritten; this
module never deletes anything. One image in memory at a time. Never raises out of its thread.
One summary line per pass, counts only. Try-on rows are never read or touched.
"""
from __future__ import annotations

import io
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import image_thumbs
import process_memory
from image_thumbs import (
    FABRIC_BUCKET,
    GENERATED_OUTPUTS_BUCKET,
    fabric_thumbnail_path,
    look_thumbnail_path,
    look_view_path,
)

BATCH = 100
FIRST_PASS_DELAY_SECONDS = 5 * 60
MIN_SECONDS_BETWEEN_PICTURES = 1.0
IDLE_CHECK_SECONDS = 5.0
MEMORY_WAIT_SECONDS = 30.0
MAX_PICTURES_PER_PASS = 150
REST_AFTER_WORK_SECONDS = 15 * 60  # a pass that made pictures
REST_WHEN_DONE_SECONDS = 6 * 60 * 60  # a pass that made nothing
RETRY_AFTER_FAILURE_SECONDS = 24 * 60 * 60
TRIM_EVERY_PICTURES = 20
BUSY_RETRIES = 3  # a job started during the download: drop the bytes, wait, download again
SIGN_CHECK_SECONDS = 60  # the links from the batch check are only used as "exists"
DEFAULT_MAX_MEMORY_MB = 320.0

MAX_ORIGINAL_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DECODED_BYTES = 24 * 1024 * 1024  # the decoded image (after draft), before any resize

_VIEW_MAX_EDGE = 1440
_VIEW_JPEG_QUALITY = 82
_VIEW_SKIP_MAX_BYTES = 400 * 1024
_LOOK_THUMB_WIDTH = 400  # as worker.build_thumbnail_jpeg_bytes
_LOOK_THUMB_JPEG_QUALITY = 70
_FABRIC_THUMB_MAX_EDGE = 400
_FABRIC_THUMB_JPEG_QUALITY = 72
_UNBOUNDED = 10**9

_NOT_NEEDED_MAX = 50_000
_RETRY_MAX = 50_000

LOG = "[picture_backfill]"


def enabled(value: Optional[str]) -> bool:
    """PICTURE_BACKFILL_ENABLED: "false" (any case) switches it off; unset or anything else = on."""
    return str(value if value is not None else "true").strip().lower() != "false"


def max_memory_mb(value: Any) -> float:
    """PICTURE_BACKFILL_MAX_MEMORY_MB; blank, unreadable or <= 0 means the default (320)."""
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_MEMORY_MB
    return parsed if parsed > 0 else DEFAULT_MAX_MEMORY_MB


class TooBig(Exception):
    """The original is over 8 MB, over 16 megapixels, or would decode to over 24 MB."""


# ---------------------------------------------------------------- bounded downloads and images


def download_capped(bucket: str, path: str, max_bytes: int = MAX_ORIGINAL_BYTES) -> bytes:
    """Download one storage object, streaming; stops and raises TooBig past max_bytes."""
    import httpx

    from config import get_settings

    settings = get_settings()
    url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{quote(path, safe='/')}"
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
    }
    with httpx.stream("GET", url, headers=headers, timeout=30) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise TooBig()
        buf = bytearray()
        for chunk in response.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise TooBig()
    return bytes(buf)


def _fit(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Largest size inside box with the same shape; never larger than size."""
    width, height = size
    scale = min(box[0] / width, box[1] / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


_TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
              6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def _probe(data: bytes):
    """Open lazily (header only, nothing decoded). Raises TooBig over 8 MB / 16 megapixels."""
    from PIL import Image

    if len(data) > MAX_ORIGINAL_BYTES:
        raise TooBig()
    image = Image.open(io.BytesIO(data))
    width, height = image.size
    if width * height > MAX_PIXELS:
        image.close()
        raise TooBig()
    return image


def _orientation(image) -> int:
    try:
        return int(image.getexif().get(0x0112) or 1)
    except Exception:
        return 1


def _render(image, stored_box: tuple[int, int], orientation: int):
    """Decode at the smallest size that still fills stored_box (in the file's own orientation),
    then: fit the box, turn upright, flatten transparency onto white. Returns an RGB image."""
    from PIL import Image

    target = _fit(image.size, stored_box)
    if image.format == "JPEG" and target != image.size:
        image.draft(None, target)  # the decoder scales by 1/2, 1/4 or 1/8: never the full image
    bytes_per_pixel = len(image.getbands()) if image.format == "JPEG" else 4
    if image.size[0] * image.size[1] * bytes_per_pixel > MAX_DECODED_BYTES:
        image.close()
        raise TooBig()
    image.load()

    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    if image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA" if has_alpha else "RGB")

    target = _fit(image.size, stored_box)
    if target != image.size:
        image = image.resize(target, Image.Resampling.LANCZOS)  # the big decode is freed here

    method = _TRANSPOSE.get(orientation)
    if method:
        image = image.transpose(getattr(Image.Transpose, method))

    if image.mode in ("RGBA", "LA"):
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        return flattened
    return image if image.mode == "RGB" else image.convert("RGB")


def _jpeg(image, quality: int, *, progressive: bool = False) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True, progressive=progressive)
    return buf.getvalue()


@dataclass
class LookPictures:
    thumb: Optional[bytes] = None
    view: Optional[bytes] = None
    view_not_needed: bool = False


def make_look_pictures(data: bytes, *, need_thumb: bool, need_view: bool) -> LookPictures:
    """Thumbnail (width <= 400, quality 70) and/or view copy (make_look_view_copy rules) from ONE
    bounded decode. view_not_needed: the original is already a light JPEG. Raises TooBig."""
    from PIL import Image

    image = _probe(data)
    view_needed = need_view and not (
        image.format == "JPEG" and len(data) <= _VIEW_SKIP_MAX_BYTES and max(image.size) <= _VIEW_MAX_EDGE
    )
    result = LookPictures(view_not_needed=need_view and not view_needed)
    if not (need_thumb or view_needed):
        image.close()
        return result

    orientation = _orientation(image)
    if view_needed:
        box = (_VIEW_MAX_EDGE, _VIEW_MAX_EDGE)
    elif orientation in (5, 6, 7, 8):  # stored sideways: the shown width is the stored height
        box = (_UNBOUNDED, _LOOK_THUMB_WIDTH)
    else:
        box = (_LOOK_THUMB_WIDTH, _UNBOUNDED)
    picture = _render(image, box, orientation)
    del image

    if view_needed:
        result.view = _jpeg(picture, _VIEW_JPEG_QUALITY, progressive=True)
    if need_thumb:
        thumb = picture
        if thumb.width > _LOOK_THUMB_WIDTH:
            height = max(1, round(thumb.height * _LOOK_THUMB_WIDTH / thumb.width))
            thumb = thumb.resize((_LOOK_THUMB_WIDTH, height), Image.Resampling.LANCZOS)
        result.thumb = _jpeg(thumb, _LOOK_THUMB_JPEG_QUALITY)
    return result


def make_fabric_thumbnail_bounded(data: bytes) -> bytes:
    """make_fabric_thumbnail's rules (upright, longest edge <= 400, quality 72), bounded. Raises TooBig."""
    image = _probe(data)
    picture = _render(image, (_FABRIC_THUMB_MAX_EDGE, _FABRIC_THUMB_MAX_EDGE), _orientation(image))
    del image
    return _jpeg(picture, _FABRIC_THUMB_JPEG_QUALITY)


# ---------------------------------------------------------------- the pass


@dataclass
class PassCounts:
    looks_checked: int = 0
    thumbs_made: int = 0
    views_made: int = 0
    views_not_needed: int = 0
    fabrics_checked: int = 0
    fabric_thumbs_made: int = 0
    pictures_made: int = 0  # originals that gave at least one new file (capped per pass)
    originals_missing: int = 0
    too_big: int = 0
    retry_later: int = 0  # failed or too big in the last 24 hours: not tried this pass
    paused_for_job: int = 0
    batches_skipped: int = 0
    cap_reached: bool = False
    errors: Counter = field(default_factory=Counter)

    @property
    def made(self) -> int:
        return self.thumbs_made + self.views_made + self.fabric_thumbs_made

    @property
    def failed(self) -> int:
        return sum(self.errors.values())

    def summary(self) -> str:
        errors = ",".join(f"{name}:{count}" for name, count in sorted(self.errors.items())) or "none"
        return (
            f"{LOG} pass done: looks_checked={self.looks_checked} thumbs_made={self.thumbs_made} "
            f"views_made={self.views_made} views_not_needed={self.views_not_needed} "
            f"fabrics_checked={self.fabrics_checked} fabric_thumbs_made={self.fabric_thumbs_made} "
            f"originals_missing={self.originals_missing} too_big={self.too_big} "
            f"retry_later={self.retry_later} paused_for_job={self.paused_for_job} "
            f"cap_reached={self.cap_reached} batches_skipped={self.batches_skipped} "
            f"failed={self.failed} errors={errors}"
        )


def _rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _clean_path(value: Any) -> str:
    path = str(value or "").strip()
    return "" if (not path or ".." in path or path.startswith("/")) else path


class _Stop(Exception):
    """The pass ends early (stop requested or the per-pass cap reached)."""


@dataclass
class PictureBackfill:
    client: Any
    jobs_in_flight: Callable[[], int]
    download: Callable[[str, str], bytes] = download_capped
    sign_existing: Callable[..., dict] = image_thumbs.sign_existing_paths
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    memory_mb: Callable[[], Optional[float]] = process_memory.rss_mb
    release: Callable[[bool], None] = process_memory.release
    max_memory: float = DEFAULT_MAX_MEMORY_MB
    stop: threading.Event = field(default_factory=threading.Event)
    _last_picture_at: float = float("-inf")
    _pictures_since_trim: int = 0
    _memory_warned: bool = False
    # Looks whose original is already light: no need to download them again every pass.
    _view_not_needed: set = field(default_factory=set)
    # (bucket, path) -> monotonic time before which a failed / too-big original is not retried.
    _retry_after: dict = field(default_factory=dict)

    # ------------------------------------------------------------ pacing and memory
    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            if self.sleep is time.sleep:
                self.stop.wait(seconds)
            else:
                self.sleep(seconds)

    def _busy(self) -> bool:
        try:
            return int(self.jobs_in_flight() or 0) > 0
        except Exception:
            return True  # unsure: assume busy

    def _wait_until_idle(self) -> None:
        """Never work while a look job is in flight."""
        while not self.stop.is_set() and self._busy():
            self._wait(IDLE_CHECK_SECONDS)

    def _memory_now(self) -> Optional[float]:
        try:
            return self.memory_mb()
        except Exception:
            return None

    def _wait_for_memory(self) -> None:
        """Above the limit: give memory back and wait; the backfill must never be what pushes
        the worker towards its limit. One log line, the first time."""
        while not self.stop.is_set():
            rss = self._memory_now()
            if rss is None or rss <= self.max_memory:
                return
            self.release(True)
            if not self._memory_warned:
                self._memory_warned = True
                print(
                    f"{LOG} memory {rss:.0f} MB is above PICTURE_BACKFILL_MAX_MEMORY_MB="
                    f"{self.max_memory:.0f}: pausing until it drops",
                    flush=True,
                )
            self._wait(MEMORY_WAIT_SECONDS)

    def _before_picture(self) -> None:
        self._wait_until_idle()
        self._wait(MIN_SECONDS_BETWEEN_PICTURES - (self.monotonic() - self._last_picture_at))
        self._wait_for_memory()
        self._wait_until_idle()
        if self.stop.is_set():
            raise _Stop()

    def _after_picture(self) -> None:
        self._last_picture_at = self.monotonic()
        self._pictures_since_trim += 1
        trim = self._pictures_since_trim >= TRIM_EVERY_PICTURES
        if trim:
            self._pictures_since_trim = 0
        try:
            self.release(trim)
        except Exception:
            pass

    def _download_when_idle(self, bucket: str, path: str, counts: PassCounts) -> Optional[bytes]:
        """Download, then check again that no job started meanwhile before anything is decoded.
        A job started: drop the bytes, wait, try again (None after BUSY_RETRIES: next pass)."""
        for _ in range(BUSY_RETRIES):
            data = self.download(bucket, path)
            if not self._busy():
                return data
            del data
            counts.paused_for_job += 1
            self.release(False)
            self._wait_until_idle()
            if self.stop.is_set():
                raise _Stop()
        return None

    # ------------------------------------------------------------ failures
    def _skip_for_now(self, bucket: str, path: str) -> bool:
        until = self._retry_after.get((bucket, path))
        if until is None:
            return False
        if self.monotonic() < until:
            return True
        self._retry_after.pop((bucket, path), None)
        return False

    def _remember_failure(self, bucket: str, path: str) -> None:
        now = self.monotonic()
        if len(self._retry_after) >= _RETRY_MAX:
            for key in [key for key, until in self._retry_after.items() if until <= now]:
                self._retry_after.pop(key, None)
        if len(self._retry_after) < _RETRY_MAX:
            self._retry_after[(bucket, path)] = now + RETRY_AFTER_FAILURE_SECONDS

    def _check_cap(self, counts: PassCounts) -> None:
        if counts.pictures_made >= MAX_PICTURES_PER_PASS:
            counts.cap_reached = True
            raise _Stop()

    # ------------------------------------------------------------ storage
    def _upload(self, bucket: str, path: str, data: bytes) -> None:
        image_thumbs.upload_new_file(self.client, bucket, path, data)

    def _existing(self, bucket: str, paths: list[str]) -> Optional[dict]:
        """ONE batch call. None = the check itself failed (skip this batch, never guess "missing")."""
        try:
            return self.sign_existing(bucket, paths, SIGN_CHECK_SECONDS, raise_on_error=True)
        except Exception as exc:
            print(f"{LOG} batch check failed for {bucket}: {exc.__class__.__name__}", flush=True)
            return None

    def _one_picture(self, bucket: str, path: str, counts: PassCounts, work: Callable[[bytes], int]) -> None:
        """Download (bounded), run work(data) -> files made, count, release. Never raises
        except _Stop."""
        self._before_picture()
        data = None
        try:
            data = self._download_when_idle(bucket, path, counts)
            if data is None:
                return
            made = work(data)
            if made:
                counts.pictures_made += 1
        except _Stop:
            raise
        except TooBig:
            counts.too_big += 1
            self._remember_failure(bucket, path)
        except Exception as exc:
            counts.errors[exc.__class__.__name__] += 1
            self._remember_failure(bucket, path)
        finally:
            data = None
            self._after_picture()

    # ------------------------------------------------------------ looks
    def _look_page(self, offset: int) -> list[dict]:
        return _rows(
            self.client.table("generations")
            .select("id, generation_type, status, output_path")
            .eq("generation_type", "look")
            .eq("status", "done")
            .not_.is_("output_path", "null")
            .order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + BATCH - 1)
            .execute()
        )

    def _look_work(self, output_path: str, need_thumb: bool, need_view: bool, counts: PassCounts):
        def _work(data: bytes) -> int:
            pictures = make_look_pictures(data, need_thumb=need_thumb, need_view=need_view)
            made = 0
            if pictures.thumb is not None:
                self._upload(GENERATED_OUTPUTS_BUCKET, look_thumbnail_path(output_path), pictures.thumb)
                counts.thumbs_made += 1
                made += 1
            if pictures.view is not None:
                self._upload(GENERATED_OUTPUTS_BUCKET, look_view_path(output_path), pictures.view)
                counts.views_made += 1
                made += 1
            if pictures.view_not_needed:
                counts.views_not_needed += 1
                if len(self._view_not_needed) < _NOT_NEEDED_MAX:
                    self._view_not_needed.add(output_path)
            return made

        return _work

    def run_looks(self, counts: PassCounts) -> None:
        offset = 0
        while not self.stop.is_set():
            self._wait_until_idle()
            if self.stop.is_set():
                return
            page = self._look_page(offset)
            if not page:
                return
            offset += len(page)

            # Belt and braces: only finished looks, never try-ons or pushed copies.
            output_paths = []
            for row in page:
                path = _clean_path(row.get("output_path"))
                if row.get("generation_type") == "look" and row.get("status") == "done" and path:
                    output_paths.append(path)
            output_paths = list(dict.fromkeys(output_paths))
            counts.looks_checked += len(output_paths)

            if output_paths:
                check = (
                    output_paths
                    + [look_thumbnail_path(p) for p in output_paths]
                    + [look_view_path(p) for p in output_paths]
                )
                existing = self._existing(GENERATED_OUTPUTS_BUCKET, check)
                if existing is None:
                    counts.batches_skipped += 1
                else:
                    for path in output_paths:
                        need_thumb = not existing.get(look_thumbnail_path(path))
                        need_view = not existing.get(look_view_path(path)) and path not in self._view_not_needed
                        if not (need_thumb or need_view):
                            continue
                        if not existing.get(path):
                            counts.originals_missing += 1  # nothing to make it from: no download
                            continue
                        if self._skip_for_now(GENERATED_OUTPUTS_BUCKET, path):
                            counts.retry_later += 1
                            continue
                        self._check_cap(counts)
                        self._one_picture(
                            GENERATED_OUTPUTS_BUCKET, path, counts,
                            self._look_work(path, need_thumb, need_view, counts),
                        )

            if len(page) < BATCH:
                return

    # ------------------------------------------------------------ fabrics
    def _fabric_page(self, offset: int) -> list[dict]:
        return _rows(
            self.client.table("fabric_images")
            .select("id, storage_path")
            .order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + BATCH - 1)
            .execute()
        )

    def _fabric_work(self, storage_path: str, counts: PassCounts):
        def _work(data: bytes) -> int:
            thumb = make_fabric_thumbnail_bounded(data)
            self._upload(FABRIC_BUCKET, fabric_thumbnail_path(storage_path), thumb)
            counts.fabric_thumbs_made += 1
            return 1

        return _work

    def run_fabrics(self, counts: PassCounts) -> None:
        offset = 0
        while not self.stop.is_set():
            self._wait_until_idle()
            if self.stop.is_set():
                return
            page = self._fabric_page(offset)
            if not page:
                return
            offset += len(page)

            paths = []
            for row in page:
                path = _clean_path(row.get("storage_path"))
                if path and fabric_thumbnail_path(path) != path:
                    paths.append(path)
            paths = list(dict.fromkeys(paths))
            counts.fabrics_checked += len(paths)

            if paths:
                existing = self._existing(FABRIC_BUCKET, paths + [fabric_thumbnail_path(p) for p in paths])
                if existing is None:
                    counts.batches_skipped += 1
                else:
                    for path in paths:
                        if existing.get(fabric_thumbnail_path(path)):
                            continue
                        if not existing.get(path):
                            counts.originals_missing += 1
                            continue
                        if self._skip_for_now(FABRIC_BUCKET, path):
                            counts.retry_later += 1
                            continue
                        self._check_cap(counts)
                        self._one_picture(FABRIC_BUCKET, path, counts, self._fabric_work(path, counts))

            if len(page) < BATCH:
                return

    # ------------------------------------------------------------ passes
    def _log_memory(self, label: str) -> None:
        rss = self._memory_now()
        print(f"[worker] memory: {'unknown' if rss is None else f'{rss:.0f} MB'} ({label})", flush=True)

    def run_pass(self) -> PassCounts:
        """One full pass. Never raises."""
        counts = PassCounts()
        self._log_memory("backfill pass start")
        for part in (self.run_looks, self.run_fabrics):
            try:
                part(counts)
            except _Stop:
                break
            except Exception as exc:
                counts.batches_skipped += 1
                print(f"{LOG} {part.__name__} stopped early ({exc.__class__.__name__})", flush=True)
        try:
            self.release(True)
        except Exception:
            pass
        print(counts.summary(), flush=True)
        self._log_memory("backfill pass end")
        return counts

    def run_forever(self) -> None:
        try:
            self._wait(FIRST_PASS_DELAY_SECONDS)
            while not self.stop.is_set():
                counts = self.run_pass()
                # Only pictures actually made bring the next pass sooner; failures never do.
                self._wait(REST_AFTER_WORK_SECONDS if counts.made > 0 else REST_WHEN_DONE_SECONDS)
        except BaseException as exc:  # never take the worker down
            print(f"{LOG} stopped ({exc.__class__.__name__})", flush=True)


def start_in_background(
    jobs_in_flight: Callable[[], int],
    client_factory: Callable[[], Any],
    max_memory: float = DEFAULT_MAX_MEMORY_MB,
) -> Optional[threading.Thread]:
    """Start the fill-in as a daemon thread with its OWN Supabase client. Never raises."""
    try:
        def _run() -> None:
            try:
                client = client_factory()
            except Exception as exc:
                print(f"{LOG} not started: no client ({exc.__class__.__name__})", flush=True)
                return
            PictureBackfill(client=client, jobs_in_flight=jobs_in_flight, max_memory=max_memory).run_forever()

        thread = threading.Thread(target=_run, name="picture-backfill", daemon=True)
        thread.start()
        print(
            f"{LOG} started (first pass in about {FIRST_PASS_DELAY_SECONDS // 60} min, "
            f"memory limit {max_memory:.0f} MB)",
            flush=True,
        )
        return thread
    except Exception as exc:
        print(f"{LOG} not started ({exc.__class__.__name__})", flush=True)
        return None
