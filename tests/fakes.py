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
        self._offset = 0
        self._orders: list[tuple[str, bool]] = []
        self._negate_next = False
        self._filter_columns: list[str] = []
        self._count = None
        self.columns: tuple = ()

    # --- operations
    def select(self, *columns, count=None, head=None):
        self.op, self._count = "select", count
        self.columns = columns
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
    def _add(self, check: Callable[[dict], bool]) -> "_Query":
        if self._negate_next:
            self._negate_next = False
            self.filters.append(lambda row: not check(row))
        else:
            self.filters.append(check)
        return self

    @property
    def not_(self):
        self._negate_next = True
        return self

    def is_(self, column, value):
        if str(value).lower() != "null":
            raise ValueError("fake is_ supports only 'null'")
        self._filter_columns.append(column)
        return self._add(lambda row: row.get(column) is None)

    def eq(self, column, value):
        return self._add(lambda row: str(row.get(column)) == str(value))

    def in_(self, column, values):
        allowed = {str(v) for v in values}
        self.filters.append(lambda row: str(row.get(column)) in allowed)
        return self

    def lt(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and _as_time(row.get(column)) < _as_time(value))
        return self

    def gt(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and _as_time(row.get(column)) > _as_time(value))
        return self

    def gte(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and _as_time(row.get(column)) >= _as_time(value))
        return self

    def order(self, column, desc=False):
        self._orders.append((column, desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._offset = start
        self._limit = end - start + 1
        return self

    def _check_columns(self) -> None:
        items = self.payload if isinstance(self.payload, list) else [self.payload] if self.payload else []
        used = " ".join(self.columns) + " " + " ".join(key for item in items for key in item)
        used += " " + " ".join(self._filter_columns)
        for column in self.db.missing_columns.get(self.table, ()):
            if column in used:
                raise FakeDBError(f"column {self.table}.{column} does not exist (42703)")

    # --- run
    def execute(self):
        self.db.log.append((self.table, self.op, copy.deepcopy(self.payload)))
        hook = self.db.fail.get((self.table, self.op)) or self.db.fail.get((self.table, "*"))
        if hook:
            raise hook if isinstance(hook, BaseException) else FakeDBError(str(hook))
        once = self.db.fail_once.pop((self.table, self.op), None)
        if once is not None:
            raise once
        self._check_columns()

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

        # Like PostgREST: the first order() is the main key (stable sorts, last key first).
        for column, desc in reversed(self._orders):
            matched = sorted(matched, key=lambda row: str(row.get(column) or ""), reverse=desc)
        count = len(matched)
        matched = matched[self._offset:]
        if self._limit is not None:
            matched = matched[: self._limit]
        return SimpleNamespace(data=copy.deepcopy(matched), count=count if self._count else None)


class _Rpc:
    def __init__(self, db: "FakeSupabase", name: str, params: dict) -> None:
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.log.append(("rpc", self.name, copy.deepcopy(self.params)))
        handler = self.db.rpc_handlers.get(self.name)
        if handler is None:
            # Like PostgREST for a function the SQL has not created yet.
            raise FakeDBError(f"PGRST202: Could not find the function public.{self.name}")
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

    def copy(self, from_path, to_path):
        self._check("copy")
        self.db.files[(self.bucket, to_path)] = self.db.files[(self.bucket, from_path)]
        return {"Key": to_path}

    def remove(self, paths):
        self._check("remove")
        for path in paths:
            self.db.files.pop((self.bucket, path), None)
        return []

    def list(self, path="", options=None):
        """Immediate children of a folder: folders as {"name", "id": None}, files with an id."""
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries: dict[str, dict] = {}
        for bucket, key in self.db.files:
            if bucket != self.bucket or not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            name, _, more = rest.partition("/")
            entries.setdefault(name, {"name": name, "id": None if more else "file"})
        return list(entries.values())


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.log: list[tuple] = []
        self.fail: dict[tuple[str, str], Any] = {}
        self.storage_fail: dict[tuple[str, str], Any] = {}
        self.files: dict[tuple[str, str], bytes] = {}
        self.rpc_handlers: dict[str, Callable[[dict], list]] = {}
        self.unique = {"generation_attempts": ("request_id",)}
        self.missing_columns: dict[str, tuple[str, ...]] = {}
        # (table, op) -> exception raised by the next such call only (a dropped connection).
        self.fail_once: dict[tuple[str, str], BaseException] = {}
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


def install_tryon_credits(db: "FakeSupabase", *, balance: int = 150) -> None:
    """The two try-on credit RPCs of sql/2026-11-04_tryon_credits_and_results.sql, on an
    in-memory ledger (db.tables["credit_ledger"]): idempotent per attempt, like the SQL."""
    ledger = db.tables.setdefault("credit_ledger", [])
    ledger.append({"shop_id": "*", "delta": balance, "reason": "opening", "balance_after": balance, "attempt_id": None})

    def current(shop_id):
        rows = [row for row in ledger if row["shop_id"] in (shop_id, "*")]
        return rows[-1]["balance_after"] if rows else 0

    def find(attempt_id, reason):
        return next((row for row in ledger if row["attempt_id"] == attempt_id and row["reason"] == reason), None)

    def debit(params):
        existing = find(params["p_attempt_id"], "web_tryon")
        if existing:
            return [{"charged": True, "credits_used": -existing["delta"],
                     "balance_before": existing["balance_after"] - existing["delta"],
                     "balance_after": existing["balance_after"]}]
        balance_now = current(params["p_shop_id"])
        if balance_now < params["p_amount"]:
            raise FakeDBError("P0001: Insufficient credits")
        ledger.append({"shop_id": params["p_shop_id"], "delta": -params["p_amount"], "reason": "web_tryon",
                       "balance_after": balance_now - params["p_amount"], "attempt_id": params["p_attempt_id"]})
        return [{"charged": True, "credits_used": params["p_amount"], "balance_before": balance_now,
                 "balance_after": balance_now - params["p_amount"]}]

    def refund(params):
        charged = find(params["p_attempt_id"], "web_tryon")
        if not charged:
            return [{"refunded": False, "refund_amount": 0, "balance_after": None}]
        existing = find(params["p_attempt_id"], "web_tryon_refund")
        if existing:
            return [{"refunded": True, "refund_amount": existing["delta"], "balance_after": existing["balance_after"]}]
        balance_now = current(params["p_shop_id"]) - charged["delta"]
        ledger.append({"shop_id": params["p_shop_id"], "delta": -charged["delta"], "reason": "web_tryon_refund",
                       "balance_after": balance_now, "attempt_id": params["p_attempt_id"]})
        return [{"refunded": True, "refund_amount": -charged["delta"], "balance_after": balance_now}]

    db.rpc_handlers["debit_credits_for_tryon"] = debit
    db.rpc_handlers["refund_credits_for_tryon"] = refund


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
        self.requests: list[dict] = []
        fake = self

        class _Models:
            def generate_content(self, **kwargs):
                fake.calls += 1
                fake.requests.append(kwargs)
                if isinstance(fake.outcome, BaseException):
                    raise fake.outcome
                return fake.outcome

        class _AioModels:
            async def generate_content(self, **kwargs):
                fake.calls += 1
                fake.requests.append(kwargs)
                if isinstance(fake.outcome, BaseException):
                    raise fake.outcome
                return fake.outcome

        self.models = _Models()
        self.aio = SimpleNamespace(models=_AioModels())
