"""In-memory stand-ins for Supabase and Gemini. No network, no real database, no real AI.

Import this module before any app module: it sets fake settings in the environment
(environment variables win over a local .env), so tests never use real keys or URLs.
"""
import os

FAKE_GEMINI_KEY = "AIzaFAKEFAKEFAKEFAKEFAKEFAKE1234"
FAKE_SERVICE_KEY = "fake-service-role-key-0123456789"

os.environ.update(
    {
        "APP_ENV": "test",
        "SUPABASE_URL": "https://fake-project.supabase.test",
        "SUPABASE_SERVICE_ROLE_KEY": FAKE_SERVICE_KEY,
        "GEMINI_API_KEY": FAKE_GEMINI_KEY,
        "GEMINI_IMAGE_MODEL_ID": "gemini-3.1-flash-image-preview",
        "ADMIN_PANEL_SECRET": "fake-admin-secret-0123456789",
        "WHATSAPP_ACCESS_TOKEN": "fake-whatsapp-token-0123456789",
        "WHATSAPP_WATCHER_ENABLED": "false",
        "FAL_KEY": "fake-fal-key-0123456789",
    }
)

import copy  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any, Callable, Optional  # noqa: E402


class FakeDBError(Exception):
    pass


def _as_time(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


class _Query:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self.db = db
        self.table = table
        self.op = "select"
        self.payload: Any = None
        self.filters: list[Callable[[dict], bool]] = []
        self._limit: Optional[int] = None
        self._order: Optional[tuple[str, bool]] = None
        self._count = None

    # --- operations
    def select(self, *_columns, count=None, head=None):
        self.op, self._count = "select", count
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, **_kwargs):
        self.op, self.payload = "upsert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    # --- filters
    def eq(self, column, value):
        self.filters.append(lambda row: str(row.get(column)) == str(value))
        return self

    def in_(self, column, values):
        allowed = {str(v) for v in values}
        self.filters.append(lambda row: str(row.get(column)) in allowed)
        return self

    def lt(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and _as_time(row.get(column)) < _as_time(value))
        return self

    def gte(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and _as_time(row.get(column)) >= _as_time(value))
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._limit = end - start + 1
        return self

    # --- run
    def execute(self):
        self.db.log.append((self.table, self.op, copy.deepcopy(self.payload)))
        hook = self.db.fail.get((self.table, self.op)) or self.db.fail.get((self.table, "*"))
        if hook:
            raise hook if isinstance(hook, BaseException) else FakeDBError(str(hook))

        rows = self.db.tables.setdefault(self.table, [])
        if self.op in ("insert", "upsert"):
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                item = dict(item)
                item.setdefault("id", str(uuid.uuid4()))
                unique = self.db.unique.get(self.table, ())
                for column in unique:
                    if any(row.get(column) == item.get(column) for row in rows):
                        if self.op == "upsert":
                            break
                        raise FakeDBError(
                            f'duplicate key value violates unique constraint "{self.table}_{column}_key" (23505)'
                        )
                rows.append(item)
                out.append(copy.deepcopy(item))
            return SimpleNamespace(data=out, count=None)

        matched = [row for row in rows if all(f(row) for f in self.filters)]
        if self.op == "update":
            for row in matched:
                row.update(copy.deepcopy(self.payload))
            return SimpleNamespace(data=copy.deepcopy(matched), count=None)
        if self.op == "delete":
            for row in matched:
                rows.remove(row)
            return SimpleNamespace(data=copy.deepcopy(matched), count=None)

        if self._order:
            column, desc = self._order
            matched = sorted(matched, key=lambda row: str(row.get(column) or ""), reverse=desc)
        count = len(matched)
        if self._limit is not None:
            matched = matched[: self._limit]
        return SimpleNamespace(data=copy.deepcopy(matched), count=count if self._count else None)


class _Rpc:
    def __init__(self, db: "FakeSupabase", name: str, params: dict) -> None:
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.log.append(("rpc", self.name, copy.deepcopy(self.params)))
        handler = self.db.rpc_handlers[self.name]
        return SimpleNamespace(data=handler(self.params))


class _Bucket:
    def __init__(self, db: "FakeSupabase", bucket: str) -> None:
        self.db, self.bucket = db, bucket

    def _check(self, op: str) -> None:
        hook = self.db.storage_fail.get((self.bucket, op))
        if hook:
            raise FakeDBError(str(hook))

    def download(self, path):
        self._check("download")
        return self.db.files.get((self.bucket, path), b"fake-image-bytes")

    def upload(self, path, data, options=None):
        self._check("upload")
        self.db.files[(self.bucket, path)] = data
        return {"path": path}

    update = upload


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.log: list[tuple] = []
        self.fail: dict[tuple[str, str], Any] = {}
        self.storage_fail: dict[tuple[str, str], Any] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.rpc_handlers: dict[str, Callable[[dict], list]] = {}
        self.unique = {"generation_attempts": ("request_id",)}
        self.storage = SimpleNamespace(from_=lambda bucket: _Bucket(self, bucket))

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rpc(self, name: str, params: dict) -> _Rpc:
        return _Rpc(self, name, params)

    # helpers
    def attempts(self) -> list[dict]:
        return self.tables.get("generation_attempts", [])

    def only_attempt(self) -> dict:
        rows = self.attempts()
        assert len(rows) == 1, f"expected exactly one attempt row, found {len(rows)}"
        return rows[0]


# ---------------------------------------------------------------- Gemini

def image_response(*, input_tokens=1000, output_tokens=1290, thoughts=0, data=b"generated-image"):
    part = SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type="image/png"))
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP", safety_ratings=[])],
        usage_metadata=SimpleNamespace(
            prompt_token_count=input_tokens, candidates_token_count=output_tokens, thoughts_token_count=thoughts
        ),
        prompt_feedback=None,
        response_id="fake-response-id",
    )


def empty_response():
    """Finished normally but returned only text, no image."""
    part = SimpleNamespace(inline_data=None, text="I can't draw that.")
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP", safety_ratings=[])],
        usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=12, thoughts_token_count=0),
        prompt_feedback=None,
    )


def blocked_response():
    """Request refused: no candidates at all, a prompt block reason instead."""
    rating = SimpleNamespace(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", probability="HIGH", blocked=True)
    return SimpleNamespace(
        candidates=[],
        usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=None, thoughts_token_count=None),
        prompt_feedback=SimpleNamespace(block_reason="SAFETY", safety_ratings=[rating]),
    )


class FakeGemini:
    """Both client.models.generate_content and client.aio.models.generate_content."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0
        fake = self

        class _Models:
            def generate_content(self, **_kwargs):
                fake.calls += 1
                if isinstance(fake.outcome, BaseException):
                    raise fake.outcome
                return fake.outcome

        class _AioModels:
            async def generate_content(self, **_kwargs):
                fake.calls += 1
                if isinstance(fake.outcome, BaseException):
                    raise fake.outcome
                return fake.outcome

        self.models = _Models()
        self.aio = SimpleNamespace(models=_AioModels())
