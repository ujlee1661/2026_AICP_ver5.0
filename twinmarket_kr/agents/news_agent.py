from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import config


class SealedNewsBundleError(RuntimeError):
    """Raised when the production real-news bundle is missing or has drifted."""


_SEALED_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_SEALED_EVENT_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/(?:AM|PM)$")
_SEALED_REPO_SOURCE_RE = re.compile(
    r"^repo://data/(?:samsung_split|semiconductor_split|"
    r"macro_economic-policy_split|macro_business-index_split|"
    r"macro_trade_split)/\d+\.json#article=\d+$"
)
_SEALED_FAKE_FIELD_RE = re.compile(
    r"(?:^|_)(?:fake|synthetic|injection|false_claim)(?:_|$)",
    re.IGNORECASE,
)
_SEALED_SYNTHETIC_MARKER_RE = re.compile(
    r"(?:\[\s*fake\s*\]|\b(?:fake|synthetic|fabricated|invented|fictitious)\b|"
    r"(?:가짜|합성|조작된|허위\s*뉴스))",
    re.IGNORECASE,
)
_SEALED_LEAKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"당일\s*(?:종가|고가|저가)",
        r"장\s*마감(?:\s*후|값)?",
        r"개인(?:투자자)?\s*(?:순매수|순매도|순거래)",
        r"individuals?\s*(?:net\s*)?(?:buy|sell|flow)",
        r"final\s*(?:close|high|low)",
    )
)
_SEALED_ARTICLE_FIELDS = frozenset(
    {
        "article_id",
        "payload_sha256",
        "title",
        "summary",
        "published_at",
        "observed_at",
        "last_modified_at",
        "source_url",
        "source",
        "raw_body_sha256",
        "version_sha256",
        "cutoff_version_sha256",
    }
)
_SEALED_SLOT_FIELDS = frozenset(
    {"event_id", "slot_ordinal", "article_id", "payload_sha256"}
)
_SEALED_BUNDLE_FIELDS = frozenset(
    {
        "artifact_type",
        "bundle_sha256",
        "stock_code",
        "target_real_news_per_event",
        "fake_news_per_event",
        "articles",
        "slots",
        "accepted_shortages",
        "known_fake_ids",
        "known_fake_payload_hashes",
        "fake_registry_sha256",
    }
)
_SEOUL_TIMEZONE = timezone(timedelta(hours=9))
_SEALED_SHORTAGE_FIELDS = frozenset(
    {
        "target_real_count",
        "selected_safe_count",
        "serialized_count",
        "delivered_real_count",
        "actual_real_count",
        "missing_real_count",
        "coverage_status",
        "ordered_article_ids",
        "ordered_payload_sha256",
    }
)


def _sealed_canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sealed_bundle_content_sha256(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("bundle_sha256", None)
    return _sealed_canonical_sha256(body)


def canonical_news_sha256(value: Any) -> str:
    """Public deterministic hash used by the offline news-preparation steps."""

    return _sealed_canonical_sha256(value)


def news_bundle_content_sha256(value: Mapping[str, Any]) -> str:
    """Hash a bundle while excluding its self-referential digest field."""

    return _sealed_bundle_content_sha256(value)


def news_fake_registry_sha256(
    *,
    known_fake_ids: Iterable[str],
    known_fake_payload_hashes: Iterable[str],
) -> str:
    return _sealed_canonical_sha256(
        {
            "known_fake_ids": list(known_fake_ids),
            "known_fake_payload_hashes": list(
                known_fake_payload_hashes
            ),
        }
    )


def news_article_payload_sha256(value: Mapping[str, Any]) -> str:
    return _article_payload_sha256(value)


def reject_synthetic_news_marker(value: str, *, field: str) -> None:
    normalized = _sealed_visible_text(str(value))
    if _SEALED_SYNTHETIC_MARKER_RE.search(normalized):
        raise SealedNewsBundleError(
            f"{field} contains a synthetic/fake marker"
        )


def reject_target_leakage_text(value: str, *, field: str) -> None:
    normalized = _sealed_visible_text(str(value))
    if any(pattern.search(normalized) for pattern in _SEALED_LEAKAGE_PATTERNS):
        raise SealedNewsBundleError(
            f"{field} contains a prohibited target-leakage pattern"
        )


def _sealed_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SealedNewsBundleError(f"{label} must be a non-empty string")
    return value.strip()


def _sealed_sha256(value: Any, label: str) -> str:
    text = _sealed_text(value, label).lower()
    if not _SEALED_SHA256_RE.fullmatch(text):
        raise SealedNewsBundleError(f"{label} must be a SHA-256 hex digest")
    return text


def _news_depth(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise SealedNewsBundleError("news_depth must be one of 0, 1, or 2")
    return value


def _sealed_timestamp(value: Any, label: str) -> datetime:
    text = _sealed_text(value, label)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SealedNewsBundleError(f"{label} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise SealedNewsBundleError(f"{label} must include a timezone offset")
    return result


def _sealed_visible_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(normalized.split())


def _validate_sealed_visible_text(value: str, *, label: str) -> None:
    normalized = _sealed_visible_text(value)
    if _SEALED_SYNTHETIC_MARKER_RE.search(normalized):
        raise SealedNewsBundleError(f"{label} contains a synthetic/fake marker")
    if any(pattern.search(normalized) for pattern in _SEALED_LEAKAGE_PATTERNS):
        raise SealedNewsBundleError(f"{label} contains a prohibited target-leakage pattern")


def _reject_sealed_fake_fields(
    value: Mapping[str, Any],
    label: str,
    *,
    allowed: set[str] | None = None,
) -> None:
    allowed_keys = allowed or set()
    offenders = [
        str(key)
        for key in value
        if _SEALED_FAKE_FIELD_RE.search(str(key)) and str(key) not in allowed_keys
    ]
    if offenders:
        raise SealedNewsBundleError(
            f"{label} contains fake/synthetic metadata fields: {sorted(offenders)}"
        )


def _assert_regular_news_bundle_path(source: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(source)))
    try:
        mode = absolute.lstat().st_mode
    except FileNotFoundError as exc:
        raise SealedNewsBundleError(
            f"sealed news bundle path does not exist: {absolute}"
        ) from exc
    if stat.S_ISLNK(mode):
        raise SealedNewsBundleError(
            "sealed news bundle file may not be a symbolic link"
        )
    if not absolute.is_file():
        raise SealedNewsBundleError("sealed news bundle must be a regular file")


def _article_payload_sha256(value: Mapping[str, Any]) -> str:
    payload_fields = _SEALED_ARTICLE_FIELDS - {"payload_sha256"}
    if set(value) != payload_fields:
        raise SealedNewsBundleError(
            "article payload hash input has an invalid exact schema"
        )
    return _sealed_canonical_sha256(dict(value))


@dataclass(frozen=True)
class SealedNewsArticle:
    article_id: str
    payload_sha256: str
    title: str
    summary: str
    published_at: str
    observed_at: str
    last_modified_at: str | None
    source_url: str
    source: str
    raw_body_sha256: str
    version_sha256: str
    cutoff_version_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SealedNewsArticle":
        _reject_sealed_fake_fields(value, "sealed article")
        if set(value) != set(_SEALED_ARTICLE_FIELDS):
            raise SealedNewsBundleError(
                "sealed article fields do not match the provenance schema"
            )
        article = cls(
            article_id=_sealed_text(value["article_id"], "article_id"),
            payload_sha256=_sealed_sha256(
                value["payload_sha256"], "article payload_sha256"
            ),
            title=_sealed_text(value["title"], "article title"),
            summary=_sealed_text(value["summary"], "article summary"),
            published_at=_sealed_text(value["published_at"], "published_at"),
            observed_at=_sealed_text(value["observed_at"], "observed_at"),
            last_modified_at=(
                None
                if value["last_modified_at"] is None
                else _sealed_text(value["last_modified_at"], "last_modified_at")
            ),
            source_url=_sealed_text(value["source_url"], "source_url"),
            source=_sealed_text(value["source"], "source"),
            raw_body_sha256=_sealed_sha256(
                value["raw_body_sha256"], "raw_body_sha256"
            ),
            version_sha256=_sealed_sha256(
                value["version_sha256"], "version_sha256"
            ),
            cutoff_version_sha256=_sealed_sha256(
                value["cutoff_version_sha256"], "cutoff_version_sha256"
            ),
        )
        published = _sealed_timestamp(article.published_at, "published_at")
        observed = _sealed_timestamp(article.observed_at, "observed_at")
        if observed < published:
            raise SealedNewsBundleError(
                "article observed_at may not precede published_at"
            )
        if article.last_modified_at is not None:
            modified = _sealed_timestamp(
                article.last_modified_at, "last_modified_at"
            )
            if modified < published or modified > observed:
                raise SealedNewsBundleError(
                    "article last_modified_at must be between published_at and observed_at"
                )
        if not (
            re.fullmatch(r"https?://[^\s]+", article.source_url)
            or _SEALED_REPO_SOURCE_RE.fullmatch(article.source_url)
        ):
            raise SealedNewsBundleError(
                "article source_url must be an http(s) URL or a sealed repo://data split reference"
            )
        _validate_sealed_visible_text(article.article_id, label="article_id")
        _validate_sealed_visible_text(article.title, label="article title")
        _validate_sealed_visible_text(article.summary, label="article summary")
        expected_payload_sha256 = _article_payload_sha256(
            {
                "article_id": article.article_id,
                "title": article.title,
                "summary": article.summary,
                "published_at": article.published_at,
                "observed_at": article.observed_at,
                "last_modified_at": article.last_modified_at,
                "source_url": article.source_url,
                "source": article.source,
                "raw_body_sha256": article.raw_body_sha256,
                "version_sha256": article.version_sha256,
                "cutoff_version_sha256": article.cutoff_version_sha256,
            }
        )
        if article.payload_sha256 != expected_payload_sha256:
            raise SealedNewsBundleError(
                "article payload_sha256 does not bind its exact provenance projection"
            )
        return article

    def runtime_row(self) -> dict[str, Any]:
        published = _sealed_timestamp(self.published_at, "published_at")
        category = next(
            (
                candidate
                for candidate in ("종목", "섹터", "경제")
                if f"_{candidate}_" in self.article_id
            ),
            "기타",
        )
        return {
            "id": self.article_id,
            "article_id": self.article_id,
            "title": self.title,
            "summary": self.summary,
            "date": published.date().isoformat(),
            "time": published.strftime("%H:%M"),
            "category": category,
            "is_fake": False,
            "payload_sha256": self.payload_sha256,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "last_modified_at": self.last_modified_at,
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class SealedNewsSlot:
    event_id: str
    slot_ordinal: int
    article_id: str
    payload_sha256: str


class SealedNewsBundle:
    """Hash-validated, immutable event-to-article registry for production runs."""

    def __init__(
        self,
        *,
        source_path: Path,
        file_sha256: str,
        bundle_sha256: str,
        stock_code: str,
        target_real_count: int,
        articles: Mapping[str, SealedNewsArticle],
        slots_by_event: Mapping[str, tuple[SealedNewsSlot, ...]],
        accepted_shortages: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.source_path = source_path
        self.file_sha256 = file_sha256
        self.bundle_sha256 = bundle_sha256
        self.stock_code = stock_code
        self.target_real_count = target_real_count
        self.articles = dict(articles)
        self.slots_by_event = dict(slots_by_event)
        self.accepted_shortages = {
            str(event_id): dict(record)
            for event_id, record in accepted_shortages.items()
        }
        first_visible_at: dict[str, datetime] = {}
        for event_id, slots in self.slots_by_event.items():
            cutoff = self._event_cutoff(event_id)
            for slot in slots:
                current = first_visible_at.get(slot.article_id)
                if current is None or cutoff < current:
                    first_visible_at[slot.article_id] = cutoff
        self.first_visible_at_by_article = first_visible_at

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        expected_bundle_sha256: str | None = None,
        expected_stock_code: str | None = config.STOCK_CODE,
    ) -> "SealedNewsBundle":
        source = Path(os.path.abspath(os.fspath(path)))
        _assert_regular_news_bundle_path(source)
        raw_bytes = source.read_bytes()
        file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        try:
            payload = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            raise SealedNewsBundleError(
                "sealed news bundle is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SealedNewsBundleError("sealed news bundle root must be an object")
        _reject_sealed_fake_fields(
            payload,
            "sealed real-news bundle",
            allowed={
                "fake_news_per_event",
                "known_fake_ids",
                "known_fake_payload_hashes",
                "fake_registry_sha256",
            },
        )
        if set(payload) != set(_SEALED_BUNDLE_FIELDS):
            raise SealedNewsBundleError(
                "sealed news bundle has an invalid exact top-level schema"
            )
        if payload["artifact_type"] != "real_news_bundle_manifest":
            raise SealedNewsBundleError(
                "sealed news bundle has the wrong artifact_type"
            )
        bundle_sha256 = _sealed_sha256(
            payload["bundle_sha256"], "bundle_sha256"
        )
        if bundle_sha256 != _sealed_bundle_content_sha256(payload):
            raise SealedNewsBundleError(
                "bundle_sha256 does not bind the exact bundle content"
            )
        if expected_bundle_sha256 is not None and bundle_sha256 != _sealed_sha256(
            expected_bundle_sha256, "expected_bundle_sha256"
        ):
            raise SealedNewsBundleError(
                "bundle hash differs from the study-pinned expected hash"
            )
        stock_code = _sealed_text(payload["stock_code"], "stock_code")
        if (
            expected_stock_code is not None
            and stock_code != str(expected_stock_code)
        ):
            raise SealedNewsBundleError(
                f"sealed news stock_code={stock_code} differs from runtime "
                f"stock_code={expected_stock_code}"
            )
        target_real_count = payload["target_real_news_per_event"]
        if (
            isinstance(target_real_count, bool)
            or not isinstance(target_real_count, int)
            or target_real_count < 1
        ):
            raise SealedNewsBundleError(
                "target_real_news_per_event must be a positive integer"
            )
        if payload["fake_news_per_event"] != 0:
            raise SealedNewsBundleError(
                "the real-only baseline requires fake_news_per_event=0; "
                "future fake treatments must preserve these real slots in a "
                "separate sealed treatment bundle"
            )
        known_fake_ids = payload["known_fake_ids"]
        known_fake_hashes = payload["known_fake_payload_hashes"]
        if not isinstance(known_fake_ids, list) or not isinstance(
            known_fake_hashes, list
        ):
            raise SealedNewsBundleError("known fake registries must be arrays")
        fake_registry_sha256 = _sealed_sha256(
            payload["fake_registry_sha256"], "fake_registry_sha256"
        )
        if fake_registry_sha256 != _sealed_canonical_sha256(
            {
                "known_fake_ids": known_fake_ids,
                "known_fake_payload_hashes": known_fake_hashes,
            }
        ):
            raise SealedNewsBundleError(
                "fake registry hash does not bind its recorded closure"
            )
        if known_fake_ids or known_fake_hashes:
            raise SealedNewsBundleError(
                "the real-only baseline may not contain known fake entries"
            )

        raw_articles = payload["articles"]
        raw_slots = payload["slots"]
        shortages = payload["accepted_shortages"]
        if not isinstance(raw_articles, list) or not isinstance(raw_slots, list):
            raise SealedNewsBundleError("articles and slots must be arrays")
        if not isinstance(shortages, Mapping):
            raise SealedNewsBundleError(
                "accepted_shortages must be an object indexed by event_id"
            )

        articles: dict[str, SealedNewsArticle] = {}
        payload_hashes: set[str] = set()
        for raw_article in raw_articles:
            if not isinstance(raw_article, Mapping):
                raise SealedNewsBundleError(
                    "sealed article registry entry must be an object"
                )
            article = SealedNewsArticle.from_mapping(raw_article)
            if article.article_id in articles:
                raise SealedNewsBundleError(
                    "sealed article registry contains duplicate article_id"
                )
            if article.payload_sha256 in payload_hashes:
                raise SealedNewsBundleError(
                    "sealed article registry contains duplicate payload hashes"
                )
            articles[article.article_id] = article
            payload_hashes.add(article.payload_sha256)

        grouped_slots: dict[str, list[SealedNewsSlot]] = defaultdict(list)
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, Mapping) or set(raw_slot) != set(
                _SEALED_SLOT_FIELDS
            ):
                raise SealedNewsBundleError(
                    "news slot must use the exact event/ordinal/article/hash schema"
                )
            event_id = _sealed_text(raw_slot["event_id"], "slot event_id")
            if not _SEALED_EVENT_ID_RE.fullmatch(event_id):
                raise SealedNewsBundleError(
                    f"invalid sealed news event_id: {event_id}"
                )
            ordinal = raw_slot["slot_ordinal"]
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 1
            ):
                raise SealedNewsBundleError(
                    "slot_ordinal must be a positive integer"
                )
            grouped_slots[event_id].append(
                SealedNewsSlot(
                    event_id=event_id,
                    slot_ordinal=ordinal,
                    article_id=_sealed_text(
                        raw_slot["article_id"], "slot article_id"
                    ),
                    payload_sha256=_sealed_sha256(
                        raw_slot["payload_sha256"], "slot payload_sha256"
                    ),
                )
            )
        if not grouped_slots:
            raise SealedNewsBundleError(
                "sealed news bundle must contain at least one event"
            )

        slots_by_event: dict[str, tuple[SealedNewsSlot, ...]] = {}
        referenced_article_ids: set[str] = set()
        short_event_ids: set[str] = set()
        for event_id, unordered_slots in grouped_slots.items():
            ordered = tuple(
                sorted(unordered_slots, key=lambda slot: slot.slot_ordinal)
            )
            if [slot.slot_ordinal for slot in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise SealedNewsBundleError(
                    f"{event_id} slot ordinals must be contiguous and 1-based"
                )
            event_article_ids: set[str] = set()
            event_payload_hashes: set[str] = set()
            cutoff = cls._event_cutoff(event_id)
            for slot in ordered:
                article = articles.get(slot.article_id)
                if article is None:
                    raise SealedNewsBundleError(
                        f"{event_id} slot references an unknown article"
                    )
                if article.payload_sha256 != slot.payload_sha256:
                    raise SealedNewsBundleError(
                        f"{event_id} slot payload hash differs from the article"
                    )
                if (
                    slot.article_id in event_article_ids
                    or slot.payload_sha256 in event_payload_hashes
                ):
                    raise SealedNewsBundleError(
                        f"{event_id} contains a duplicate article or payload"
                    )
                for label, raw_timestamp in (
                    ("published_at", article.published_at),
                    ("observed_at", article.observed_at),
                    ("last_modified_at", article.last_modified_at),
                ):
                    if raw_timestamp is not None and _sealed_timestamp(
                        raw_timestamp, label
                    ) > cutoff:
                        raise SealedNewsBundleError(
                            f"{event_id} contains {label} after the event cutoff"
                        )
                event_article_ids.add(slot.article_id)
                event_payload_hashes.add(slot.payload_sha256)
                referenced_article_ids.add(slot.article_id)
            if len(ordered) > target_real_count:
                raise SealedNewsBundleError(
                    f"{event_id} exceeds target_real_news_per_event"
                )
            if len(ordered) < target_real_count:
                short_event_ids.add(event_id)
            slots_by_event[event_id] = ordered

        if referenced_article_ids != set(articles):
            raise SealedNewsBundleError(
                "article registry must exactly equal the articles referenced by slots"
            )
        if set(shortages) != short_event_ids:
            raise SealedNewsBundleError(
                "accepted shortage records must exactly cover all short events"
            )
        for event_id in sorted(short_event_ids):
            cls._validate_shortage(
                event_id=event_id,
                record=shortages[event_id],
                slots=slots_by_event[event_id],
                target_real_count=target_real_count,
            )
        return cls(
            source_path=source,
            file_sha256=file_sha256,
            bundle_sha256=bundle_sha256,
            stock_code=stock_code,
            target_real_count=target_real_count,
            articles=articles,
            slots_by_event=slots_by_event,
            accepted_shortages=shortages,
        )

    @staticmethod
    def _event_cutoff(event_id: str) -> datetime:
        date_text, subturn = event_id.split("/", 1)
        cutoff_time = "09:00:00" if subturn == "AM" else "15:30:00"
        return datetime.fromisoformat(
            f"{date_text}T{cutoff_time}+09:00"
        )

    @staticmethod
    def _validate_shortage(
        *,
        event_id: str,
        record: Any,
        slots: tuple[SealedNewsSlot, ...],
        target_real_count: int,
    ) -> None:
        if not isinstance(record, Mapping) or set(record) != set(
            _SEALED_SHORTAGE_FIELDS
        ):
            raise SealedNewsBundleError(
                f"{event_id} shortage record has an invalid exact schema"
            )
        article_ids = [slot.article_id for slot in slots]
        payload_hashes = [slot.payload_sha256 for slot in slots]
        if (
            record["target_real_count"] != target_real_count
            or record["selected_safe_count"] != len(slots)
            or record["serialized_count"] != len(slots)
            or record["delivered_real_count"] != len(slots)
            or record["actual_real_count"] != len(slots)
            or record["missing_real_count"] != target_real_count - len(slots)
            or record["coverage_status"] != "shortage_accepted"
            or list(record["ordered_article_ids"]) != article_ids
            or list(record["ordered_payload_sha256"]) != payload_hashes
        ):
            raise SealedNewsBundleError(
                f"{event_id} shortage record does not match its frozen slots"
            )

    def event_rows(self, event_id: str) -> list[dict[str, Any]]:
        slots = self.slots_by_event.get(event_id)
        if slots is None:
            raise SealedNewsBundleError(
                f"event is absent from sealed news slot map: {event_id}"
            )
        result: list[dict[str, Any]] = []
        for slot in slots:
            row = self.articles[slot.article_id].runtime_row()
            row["event_id"] = event_id
            row["slot_ordinal"] = slot.slot_ordinal
            result.append(row)
        return result

    def coverage_record(self, event_id: str) -> dict[str, Any]:
        slots = self.slots_by_event.get(event_id)
        if slots is None:
            raise SealedNewsBundleError(
                f"event is absent from sealed news slot map: {event_id}"
            )
        shortage = self.accepted_shortages.get(event_id)
        return {
            "event_id": event_id,
            "target_real_count": self.target_real_count,
            "delivered_real_count": len(slots),
            "missing_real_count": self.target_real_count - len(slots),
            "coverage_status": (
                "shortage_accepted" if shortage is not None else "complete"
            ),
        }

# These are decision-cutoff labels used by the sealed schedule validators and
# lineage logs. They do not select or re-sample runtime articles.
AM_NEWS_WINDOW_START_TIME = "15:30"
AM_NEWS_WINDOW_END_TIME = "08:59"
PM_NEWS_WINDOW_START_TIME = "09:00"
PM_NEWS_WINDOW_END_TIME = "15:29"

# These limits govern only the sealed bundle's depth-2 retrieval interface.
# The 5/3/2 per-event selection policy remains frozen in the news bundle and
# its sealing/validation scripts; runtime never re-samples source articles.
DEPTH2_MAX_KEYWORDS = 5
DEPTH2_MAX_SEARCH_ARTICLES = 5

TRUE_TEXTS = {"1", "true", "yes", "y"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_TEXTS


def _public_title_item(
    row: Mapping[str, Any],
    *,
    include_time: bool = False,
) -> dict[str, str]:
    item = {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "type": str(row.get("category", "")),
    }
    if include_time:
        item["time"] = str(row.get("time", ""))
    for key in (
        "article_id",
        "payload_sha256",
        "published_at",
        "event_id",
        "slot_ordinal",
    ):
        if row.get(key) is not None:
            item[key] = str(row[key])
    return item


def _public_content_item(row: Mapping[str, Any]) -> dict[str, str]:
    item = {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "content": str(row.get("summary", "")),
        "type": str(row.get("category", "")),
    }
    for key in (
        "article_id",
        "payload_sha256",
        "published_at",
        "event_id",
        "slot_ordinal",
    ):
        if row.get(key) is not None:
            item[key] = str(row[key])
    return item


def _public_search_item(
    row: Mapping[str, Any],
    score: float,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": str(row.get("id", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "category": str(row.get("category", "")),
        "summary": str(row.get("search_summary") or row.get("summary", "")),
        "relevance_score": score,
    }
    for key in ("article_id", "payload_sha256", "published_at"):
        if row.get(key) is not None:
            item[key] = str(row[key])
    return item


def _parse_date(value: str) -> date:
    text = str(value).strip()[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def _parse_time(value: str) -> time | None:
    text = str(value or "").strip()
    match = re.search(r"\d{2}:\d{2}", text)
    if not match:
        return None
    return datetime.strptime(match.group(0), "%H:%M").time()


def _combine_datetime(day: str, time_text: str) -> datetime | None:
    parsed_time = _parse_time(time_text)
    if parsed_time is None:
        return None
    return datetime.combine(_parse_date(day), parsed_time)


class NewsAgent:
    """Read exactly one hash-validated sealed news bundle for a simulation."""

    def __init__(
        self,
        *,
        news_bundle_path: Path | str,
        expected_bundle_sha256: str | None = None,
        expected_stock_code: str | None = config.STOCK_CODE,
    ) -> None:
        self._sealed_bundle = SealedNewsBundle.load(
            news_bundle_path,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_stock_code=expected_stock_code,
        )
        self._processed = [
            article.runtime_row()
            for article in self._sealed_bundle.articles.values()
        ]
        self.news_source = "sealed_bundle"
        self.news_bundle_path = self._sealed_bundle.source_path
        self.news_bundle_file_sha256 = self._sealed_bundle.file_sha256
        self.news_bundle_sha256 = self._sealed_bundle.bundle_sha256
        self._by_id = {str(row["id"]): row for row in self._processed}
        self._by_title_all: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._processed:
            self._by_title_all[str(row["title"])].append(row)
        self._by_title = {
            title: sorted(rows, key=lambda item: str(item.get("id") or ""))[0]
            for title, rows in self._by_title_all.items()
        }

    @property
    def is_sealed(self) -> bool:
        return True

    @property
    def sealed_event_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sealed_bundle.slots_by_event))

    def build_event_context(
        self,
        event_id: str,
        news_depth: int = 1,
    ) -> dict[str, Any]:
        """Build one exact sealed AM/PM feed without date-window re-sampling."""
        depth = _news_depth(news_depth)
        event_rows = self._sealed_bundle.event_rows(event_id)
        daily_titles = [
            _public_title_item(row, include_time=True)
            for row in event_rows
        ]
        coverage = self._sealed_bundle.coverage_record(event_id)
        return {
            "news_depth": depth,
            "event_id": event_id,
            "news_source": "sealed_bundle",
            "sealed_bundle_sha256": self._sealed_bundle.bundle_sha256,
            "sealed_bundle_file_sha256": self._sealed_bundle.file_sha256,
            "daily_titles": daily_titles,
            "read_contents": [],
            "search_results": {},
            "search_read_contents": [],
            "coverage": coverage,
            "limits": {
                "daily_read_max": 0 if depth == 0 else len(event_rows),
                "search_fields_max": 0,
                "search_read_max": (
                    DEPTH2_MAX_SEARCH_ARTICLES if depth >= 2 else 0
                ),
                "lookback_days": 7 if depth >= 2 else 0,
            },
        }

    def search_news_flat(
        self,
        *,
        keywords: list[str],
        current_date: str,
        as_of_time: str = "23:59",
        window_start_date: str | None = None,
        window_start_time: str | None = None,
        window_end_date: str | None = None,
        window_end_time: str | None = None,
        lookback_days: int = 7,
        top_n: int = DEPTH2_MAX_SEARCH_ARTICLES,
        exclude_article_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search only bundle articles visible by the supplied sealed cutoff."""
        normalized_keywords = [
            str(keyword).strip()
            for keyword in keywords
            if str(keyword).strip()
        ]
        if not normalized_keywords:
            return []
        excluded = {
            str(article_id).strip()
            for article_id in (exclude_article_ids or ())
            if str(article_id).strip()
        }
        # Depth 2 searches the trailing lookback window as of the decision
        # cutoff. The feed's AM/PM window does not narrow it to one subturn.
        end_dt = _combine_datetime(
            window_end_date or current_date,
            window_end_time or as_of_time,
        )
        if end_dt is None:
            raise ValueError("Depth 2 search requires a valid as-of date and time")
        sealed_end = end_dt.replace(tzinfo=_SEOUL_TIMEZONE)
        sealed_start = sealed_end - timedelta(days=lookback_days)
        candidates: list[dict[str, Any]] = []
        for row in self._processed:
            article_id = str(row.get("article_id") or row.get("id") or "")
            if article_id in excluded:
                continue
            published_at = _sealed_timestamp(
                row.get("published_at"),
                f"{article_id} published_at",
            )
            observed_at = _sealed_timestamp(
                row.get("observed_at"),
                f"{article_id} observed_at",
            )
            raw_modified_at = row.get("last_modified_at")
            modified_at = (
                None
                if raw_modified_at in (None, "")
                else _sealed_timestamp(
                    raw_modified_at,
                    f"{article_id} last_modified_at",
                )
            )
            first_visible_at = self._sealed_bundle.first_visible_at_by_article.get(
                article_id
            )
            if (
                sealed_start < published_at <= sealed_end
                and observed_at <= sealed_end
                and (modified_at is None or modified_at <= sealed_end)
                and first_visible_at is not None
                and first_visible_at <= sealed_end
            ):
                candidates.append(row)
        # 모델이 만드는 검색어는 "삼성전자 HBM 수주" 같은 구(phrase)라서
        # 전체 문자열 일치로 채점하면 사실상 아무 기사도 매칭되지 않는다.
        # 라이브 2일 실행에서 검색 60회가 전부 0건을 반환해 depth 2의 회수
        # 경로가 통째로 죽어 있었다. 공백 단위 토큰으로 나눠 채점한다.
        search_tokens = {
            token
            for keyword in normalized_keywords
            for token in keyword.split()
        }
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            search_summary = row.get("search_summary") or row.get("summary", "")
            title_hits = sum(
                str(row["title"]).count(token)
                for token in search_tokens
            )
            body_hits = sum(
                str(search_summary).count(token)
                for token in search_tokens
            )
            score = title_hits * 2.0 + body_hits
            if score > 0:
                scored.append((score, row))
        scored.sort(
            key=lambda item: (
                -item[0],
                -_parse_date(str(item[1]["date"])).toordinal(),
                str(item[1]["title"]),
            )
        )
        return [_public_search_item(row, score) for score, row in scored[:top_n]]

    def expand_context_from_selection(
        self,
        *,
        base_context: dict[str, Any],
        selected_news: list[Any] | None = None,
        current_date: str,
    ) -> dict[str, Any]:
        """Expand only the article IDs frozen in the event bundle."""
        del selected_news, current_date
        news_depth = (
            1
            if base_context.get("news_depth") is None
            else int(base_context["news_depth"])
        )
        daily_titles = base_context.get("daily_titles") or []
        allowed_daily_ids = {
            str(row.get("id"))
            for row in daily_titles
            if row.get("id")
        }
        if news_depth <= 0:
            read_contents: list[dict[str, str]] = []
        else:
            event_id = str(base_context.get("event_id") or "")
            event_rows = self._sealed_bundle.event_rows(event_id)
            event_by_id = {
                str(row["id"]): row
                for row in event_rows
            }
            if set(event_by_id) != allowed_daily_ids:
                raise SealedNewsBundleError(
                    "sealed base context article IDs differ from the frozen event slots"
                )
            read_contents = [
                _public_content_item(event_by_id[news_id])
                for news_id in (
                    str(row.get("id"))
                    for row in daily_titles
                    if row.get("id")
                )
            ]

        expanded = dict(base_context)
        expanded["read_contents"] = read_contents
        expanded["visible_news_ids"] = [
            str(row.get("id"))
            for row in daily_titles
            if row.get("id")
        ]
        expanded["read_news_ids"] = [
            str(row.get("id"))
            for row in read_contents
            if row.get("id")
        ]
        expanded.setdefault("search_results", {})
        expanded.setdefault("search_read_contents", [])
        return expanded

    def fake_audit_for_context(
        self,
        news_context: dict[str, Any],
        *,
        selected_news: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Keep the report-compatible audit shape for sealed real-only runs."""
        base_ids = self._ids_from_items(news_context.get("daily_titles") or [])
        read_ids = self._ids_from_items(news_context.get("read_contents") or [])
        search_ids = self._ids_from_items(
            news_context.get("search_read_contents") or []
        )
        selected_ids, selected_titles = self._normalize_selected_news(
            selected_news or []
        )
        selected_ids.extend(
            str(row["id"])
            for title in selected_titles
            for row in [self._by_title.get(title)]
            if row and row.get("id")
        )
        buckets = {
            "base": base_ids,
            "read": read_ids,
            "search": search_ids,
            "selected": selected_ids,
        }
        items_by_id: dict[str, dict[str, Any]] = {}
        sources_by_id: dict[str, set[str]] = defaultdict(set)
        for source, ids in buckets.items():
            for news_id in ids:
                row = self._by_id.get(news_id)
                if not row or not _truthy(row.get("is_fake")):
                    continue
                items_by_id[news_id] = row
                sources_by_id[news_id].add(source)
        items = [
            self._fake_audit_item(row, sorted(sources_by_id[news_id]))
            for news_id, row in sorted(items_by_id.items(), key=lambda item: item[0])
        ]
        fake_ids = [str(item["id"]) for item in items]
        fake_base_count = self._fake_count(base_ids)
        fake_read_count = self._fake_count(read_ids)
        fake_search_count = self._fake_count(search_ids)
        fake_selected_count = self._fake_count(selected_ids)
        return {
            "fake_exposed": bool(items),
            "fake_visible": fake_base_count > 0,
            "fake_read": fake_read_count > 0,
            "fake_searched": fake_search_count > 0,
            "fake_influential": fake_selected_count > 0,
            "fake_base_count": fake_base_count,
            "fake_read_count": fake_read_count,
            "fake_search_count": fake_search_count,
            "fake_selected_count": fake_selected_count,
            "fake_public_ids": fake_ids,
            "fake_synthetic_ids": [
                item.get("synthetic_id", "")
                for item in items
                if item.get("synthetic_id")
            ],
            "fake_related_events": [
                item.get("related_event", "")
                for item in items
                if item.get("related_event")
            ],
            "items": items,
        }

    def normalize_influential_news(
        self,
        selected_news: list[Any],
        news_context: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Resolve LLM references only against news available at this event."""
        allowed_ids = set(
            self._ids_from_items(news_context.get("daily_titles") or [])
        )
        allowed_ids.update(
            self._ids_from_items(news_context.get("read_contents") or [])
        )
        allowed_ids.update(
            self._ids_from_items(
                news_context.get("search_read_contents") or []
            )
        )
        resolved: list[dict[str, str]] = []
        unresolved: list[str] = []
        seen: set[str] = set()
        for item in selected_news:
            raw_id = (
                str(item.get("id") or "").strip()
                if isinstance(item, dict)
                else ""
            )
            raw_title = (
                str(item.get("title") or "").strip()
                if isinstance(item, dict)
                else str(item).strip()
            )
            row = self._by_id.get(raw_id) if raw_id else None
            if row is None and raw_title:
                title_candidates = [
                    candidate
                    for candidate in self._by_title_all.get(raw_title, [])
                    if str(candidate.get("id") or "") in allowed_ids
                ]
                row = (
                    sorted(
                        title_candidates,
                        key=lambda candidate: str(
                            candidate.get("id") or ""
                        ),
                    )[0]
                    if title_candidates
                    else None
                )
            if row is None or str(row.get("id") or "") not in allowed_ids:
                unresolved.append(raw_id or raw_title)
                continue
            news_id = str(row["id"])
            if news_id in seen:
                continue
            seen.add(news_id)
            resolved.append(_public_title_item(row, include_time=True))
        return resolved, unresolved

    @staticmethod
    def _normalize_selected_news(
        selected_news: list[Any],
    ) -> tuple[list[str], list[str]]:
        ids: list[str] = []
        titles: list[str] = []
        for item in selected_news:
            if isinstance(item, dict):
                raw_id = item.get("id")
                raw_title = item.get("title")
                if raw_id:
                    ids.append(str(raw_id))
                elif raw_title:
                    titles.append(str(raw_title))
            else:
                text = str(item).strip()
                if not text:
                    continue
                if text.startswith("news_"):
                    ids.append(text)
                else:
                    titles.append(text)
        return list(dict.fromkeys(ids)), list(dict.fromkeys(titles))

    @staticmethod
    def _ids_from_items(items: Any) -> list[str]:
        result: list[str] = []
        if isinstance(items, dict):
            iterable = [
                entry
                for values in items.values()
                if isinstance(values, list)
                for entry in values
            ]
        elif isinstance(items, list):
            iterable = items
        else:
            iterable = []
        for item in iterable:
            if isinstance(item, dict) and item.get("id"):
                result.append(str(item["id"]))
        return result

    def _fake_count(self, ids: list[str]) -> int:
        return sum(
            1
            for news_id in ids
            if _truthy((self._by_id.get(news_id) or {}).get("is_fake"))
        )

    @staticmethod
    def _fake_audit_item(
        row: dict[str, Any],
        sources: list[str],
    ) -> dict[str, Any]:
        return {
            "id": row.get("id", ""),
            "synthetic_id": row.get("synthetic_id", ""),
            "title": row.get("title", ""),
            "date": row.get("date", ""),
            "time": row.get("time", ""),
            "category": row.get("category", ""),
            "sources": sources,
            "linked_event_id": row.get("linked_event_id", ""),
            "related_event": row.get("related_event", ""),
            "misinformation_type": row.get("misinformation_type", ""),
        }
