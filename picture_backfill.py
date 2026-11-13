"""Worker-only background fill-in of light pictures for EXISTING files. Started from worker.main,
never from the API.

- Looks: generations with generation_type 'look', status 'done' and an output_path, newest first,
  100 at a time. ONE batch check per 100 (image_thumbs.sign_existing_paths) says which thumbnails
  and view copies already exist; a missing thumbnail is made with build_thumbnail_jpeg_bytes, a
  missing view copy with make_look_view_copy (None = not needed: the original is already light).
- Fabrics: fabric_images rows, newest first, 100 at a time, same batch check in the
  fabric-images bucket; a missing "<name>_thumb.jpg" is made with make_fabric_thumbnail.

Pace: one picture at a time, at least 1 s apart, and nothing at all while any look job is in
flight; the thread only ever waits, it never holds anything the job loop needs. First pass about
60 s after the worker starts; after a pass with nothing left to do, sleep 6 hours.

Safety: uploads use upsert OFF ("already exists" is fine), so no file is ever overwritten; this
module never deletes anything. One image in memory at a time. A picture that fails is skipped
until the next pass. Never raises out of its thread. One summary line per pass, counts only.
Try-on rows (any generation_type other than 'look') are never read or touched.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import image_thumbs
from image_thumbs import (
    FABRIC_BUCKET,
    GENERATED_OUTPUTS_BUCKET,
    fabric_thumbnail_path,
    look_thumbnail_path,
    look_view_path,
)

BATCH = 100
FIRST_PASS_DELAY_SECONDS = 60
MIN_SECONDS_BETWEEN_PICTURES = 1.0
IDLE_CHECK_SECONDS = 5.0
REST_AFTER_WORK_SECONDS = 10 * 60  # a pass that made pictures: look again soon
REST_WHEN_DONE_SECONDS = 6 * 60 * 60  # a pass with nothing left to do
SIGN_CHECK_SECONDS = 60  # the links from the batch check are only used as "exists"
_NOT_NEEDED_MAX = 50_000

LOG = "[picture_backfill]"


def enabled(value: Optional[str]) -> bool:
    """PICTURE_BACKFILL_ENABLED: "false" (any case) switches it off; unset or anything else = on."""
    return str(value if value is not None else "true").strip().lower() != "false"


@dataclass
class PassCounts:
    looks_checked: int = 0
    thumbs_made: int = 0
    views_made: int = 0
    views_not_needed: int = 0
    fabrics_checked: int = 0
    fabric_thumbs_made: int = 0
    failed: int = 0
    batches_skipped: int = 0

    @property
    def made(self) -> int:
        return self.thumbs_made + self.views_made + self.fabric_thumbs_made

    def summary(self) -> str:
        return (
            f"{LOG} pass done: looks_checked={self.looks_checked} thumbs_made={self.thumbs_made} "
            f"views_made={self.views_made} views_not_needed={self.views_not_needed} "
            f"fabrics_checked={self.fabrics_checked} fabric_thumbs_made={self.fabric_thumbs_made} "
            f"failed={self.failed} batches_skipped={self.batches_skipped}"
        )


def _rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _clean_path(value: Any) -> str:
    path = str(value or "").strip()
    return "" if (not path or ".." in path or path.startswith("/")) else path


def _default_make_thumbnail(image_bytes: bytes) -> bytes:
    from worker import build_thumbnail_jpeg_bytes  # lazy: worker imports this module

    return build_thumbnail_jpeg_bytes(image_bytes)


@dataclass
class PictureBackfill:
    client: Any
    jobs_in_flight: Callable[[], int]
    make_thumbnail: Callable[[bytes], bytes] = _default_make_thumbnail
    sign_existing: Callable[..., dict] = image_thumbs.sign_existing_paths
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    stop: threading.Event = field(default_factory=threading.Event)
    _last_picture_at: float = float("-inf")
    # Looks whose original is already light: no need to download them again every pass.
    _view_not_needed: set = field(default_factory=set)

    # ------------------------------------------------------------ pacing
    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            if self.sleep is time.sleep:
                self.stop.wait(seconds)
            else:
                self.sleep(seconds)

    def _wait_until_idle(self) -> None:
        """Never work while a look job is in flight."""
        while not self.stop.is_set():
            try:
                busy = int(self.jobs_in_flight() or 0) > 0
            except Exception:
                busy = True  # unsure: assume busy
            if not busy:
                return
            self._wait(IDLE_CHECK_SECONDS)

    def _before_picture(self) -> None:
        self._wait_until_idle()
        gap = MIN_SECONDS_BETWEEN_PICTURES - (self.monotonic() - self._last_picture_at)
        self._wait(gap)
        self._wait_until_idle()

    def _after_picture(self) -> None:
        self._last_picture_at = self.monotonic()

    # ------------------------------------------------------------ storage
    def _download(self, bucket: str, path: str) -> bytes:
        return bytes(self.client.storage.from_(bucket).download(path))

    def _upload(self, bucket: str, path: str, data: bytes) -> None:
        image_thumbs.upload_new_file(self.client, bucket, path, data)

    def _existing(self, bucket: str, paths: list[str]) -> Optional[dict]:
        """ONE batch call. None = the check itself failed (skip this batch, never guess "missing")."""
        try:
            return self.sign_existing(bucket, paths, SIGN_CHECK_SECONDS, raise_on_error=True)
        except Exception as exc:
            print(f"{LOG} batch check failed for {bucket}: {exc.__class__.__name__}", flush=True)
            return None

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

    def _do_look(self, output_path: str, need_thumb: bool, need_view: bool, counts: PassCounts) -> None:
        self._before_picture()
        if self.stop.is_set():
            return
        try:
            original = self._download(GENERATED_OUTPUTS_BUCKET, output_path)
            if need_thumb:
                self._upload(GENERATED_OUTPUTS_BUCKET, look_thumbnail_path(output_path), self.make_thumbnail(original))
                counts.thumbs_made += 1
            if need_view:
                view_bytes = image_thumbs.make_look_view_copy(original)
                if view_bytes is None:
                    counts.views_not_needed += 1
                    if len(self._view_not_needed) < _NOT_NEEDED_MAX:
                        self._view_not_needed.add(output_path)
                else:
                    self._upload(GENERATED_OUTPUTS_BUCKET, look_view_path(output_path), view_bytes)
                    counts.views_made += 1
            del original
        except Exception as exc:
            counts.failed += 1
            print(f"{LOG} look picture skipped ({exc.__class__.__name__})", flush=True)
        finally:
            self._after_picture()

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
                check = [look_thumbnail_path(p) for p in output_paths] + [look_view_path(p) for p in output_paths]
                existing = self._existing(GENERATED_OUTPUTS_BUCKET, check)
                if existing is None:
                    counts.batches_skipped += 1
                else:
                    for path in output_paths:
                        if self.stop.is_set():
                            return
                        need_thumb = not existing.get(look_thumbnail_path(path))
                        need_view = not existing.get(look_view_path(path)) and path not in self._view_not_needed
                        if need_thumb or need_view:
                            self._do_look(path, need_thumb, need_view, counts)

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

    def _do_fabric(self, storage_path: str, counts: PassCounts) -> None:
        self._before_picture()
        if self.stop.is_set():
            return
        try:
            original = self._download(FABRIC_BUCKET, storage_path)
            thumb = image_thumbs.make_fabric_thumbnail(original)
            del original
            self._upload(FABRIC_BUCKET, fabric_thumbnail_path(storage_path), thumb)
            counts.fabric_thumbs_made += 1
        except Exception as exc:
            counts.failed += 1
            print(f"{LOG} fabric picture skipped ({exc.__class__.__name__})", flush=True)
        finally:
            self._after_picture()

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
                existing = self._existing(FABRIC_BUCKET, [fabric_thumbnail_path(p) for p in paths])
                if existing is None:
                    counts.batches_skipped += 1
                else:
                    for path in paths:
                        if self.stop.is_set():
                            return
                        if not existing.get(fabric_thumbnail_path(path)):
                            self._do_fabric(path, counts)

            if len(page) < BATCH:
                return

    # ------------------------------------------------------------ passes
    def run_pass(self) -> PassCounts:
        """One full pass. Never raises."""
        counts = PassCounts()
        for part in (self.run_looks, self.run_fabrics):
            try:
                part(counts)
            except Exception as exc:
                counts.batches_skipped += 1
                print(f"{LOG} {part.__name__} stopped early ({exc.__class__.__name__})", flush=True)
        print(counts.summary(), flush=True)
        return counts

    def run_forever(self) -> None:
        try:
            self._wait(FIRST_PASS_DELAY_SECONDS)
            while not self.stop.is_set():
                counts = self.run_pass()
                # Failed pictures wait for the next pass; nothing made = nothing left: 6 hours.
                self._wait(REST_AFTER_WORK_SECONDS if counts.made > 0 else REST_WHEN_DONE_SECONDS)
        except BaseException as exc:  # never take the worker down
            print(f"{LOG} stopped ({exc.__class__.__name__})", flush=True)


def start_in_background(
    jobs_in_flight: Callable[[], int],
    client_factory: Callable[[], Any],
    make_thumbnail: Callable[[bytes], bytes] = _default_make_thumbnail,
) -> Optional[threading.Thread]:
    """Start the fill-in as a daemon thread with its OWN Supabase client. Never raises."""
    try:
        def _run() -> None:
            try:
                client = client_factory()
            except Exception as exc:
                print(f"{LOG} not started: no client ({exc.__class__.__name__})", flush=True)
                return
            PictureBackfill(
                client=client, jobs_in_flight=jobs_in_flight, make_thumbnail=make_thumbnail
            ).run_forever()

        thread = threading.Thread(target=_run, name="picture-backfill", daemon=True)
        thread.start()
        print(f"{LOG} started (first pass in about {FIRST_PASS_DELAY_SECONDS}s)", flush=True)
        return thread
    except Exception as exc:
        print(f"{LOG} not started ({exc.__class__.__name__})", flush=True)
        return None
