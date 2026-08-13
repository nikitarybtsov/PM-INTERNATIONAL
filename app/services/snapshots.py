"""Формирование immutable-снимков рынка."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.market_data import (
    MarketDataProvider,
    MarketNotAvailable,
    MarketQuote,
    MarketRef,
    get_market_data_provider,
)
from app.config import get_settings
from app.constants import MarketStatus, Phase
from app.db.models import Market, RawMarketPayload, Snapshot
from app.schemas.snapshot import (
    BookLevel,
    MarketSnapshot,
    SiblingMarket,
    SnapshotBook,
    SnapshotMarketInfo,
)
from app.services import audit

logger = logging.getLogger(__name__)


def _raw_sink_for(db: Session):
    def sink(source: str, external_id: str, endpoint: str, payload) -> None:
        db.add(
            RawMarketPayload(
                source=source,
                external_id=str(external_id),
                endpoint=endpoint,
                payload=payload if isinstance(payload, dict | list) else {"value": str(payload)},
            )
        )

    return sink


def provider_for(db: Session) -> MarketDataProvider:
    return get_market_data_provider(raw_sink=_raw_sink_for(db))


def upsert_market(db: Session, ref: MarketRef) -> Market:
    """Создать или обновить рынок, сохранив сырой ответ источника."""
    market = db.scalar(
        select(Market).where(Market.source == ref.source, Market.external_id == ref.external_id)
    )
    before = None
    if market is None:
        market = Market(source=ref.source, external_id=ref.external_id, title=ref.title)
        db.add(market)
    else:
        before = {"title": market.title, "status": market.status}

    market.slug = ref.slug
    market.title = ref.title
    market.event_title = ref.event_title
    market.tournament = ref.tournament
    market.market_type = ref.market_type
    # без токенов боевой ордер отправить нельзя
    market.yes_token_id = ref.yes_token_id or market.yes_token_id
    market.no_token_id = ref.no_token_id or market.no_token_id
    market.team_a = ref.team_a
    market.team_b = ref.team_b
    market.yes_label = ref.yes_label
    market.no_label = ref.no_label
    market.starts_at = ref.starts_at
    db.flush()

    if ref.raw:
        db.add(
            RawMarketPayload(
                source=ref.source,
                external_id=ref.external_id,
                endpoint="market",
                payload=ref.raw if isinstance(ref.raw, dict) else {"value": str(ref.raw)},
            )
        )
    audit.record(
        db,
        entity_type="market",
        entity_id=market.id,
        action="upsert",
        before=before,
        after={"title": market.title, "external_id": market.external_id},
    )
    return market


def _to_book(bids, asks) -> SnapshotBook:
    return SnapshotBook(
        bids=[BookLevel(price=p, size=s) for p, s in bids if s > 0],
        asks=[BookLevel(price=p, size=s) for p, s in asks if s > 0],
    )


def build_snapshot_model(
    market: Market,
    quote: MarketQuote,
    phase: Phase,
    *,
    operator_context: str | None = None,
    map_number: int | None = None,
    ttl_seconds: int | None = None,
    siblings: list | None = None,
) -> MarketSnapshot:
    settings = get_settings()
    return MarketSnapshot(
        sibling_markets=siblings or [],
        phase=phase,
        map_number=map_number,
        market=SnapshotMarketInfo(
            market_id=market.id,
            external_id=market.external_id,
            source=market.source,
            title=market.title,
            event_title=market.event_title,
            tournament=market.tournament,
            market_type=market.market_type,
            team_a=market.team_a,
            team_b=market.team_b,
            yes_label=market.yes_label,
            no_label=market.no_label,
            starts_at=market.starts_at,
        ),
        yes_price=quote.yes_price,
        no_price=quote.no_price,
        yes_book=_to_book(quote.yes_bids, quote.yes_asks),
        no_book=_to_book(quote.no_bids, quote.no_asks),
        liquidity_usdc=quote.liquidity_usdc,
        volume_24h_usdc=quote.volume_24h_usdc,
        price_change_1h=quote.price_change_1h,
        price_change_24h=quote.price_change_24h,
        recent_prices=quote.recent_prices,
        operator_context=operator_context or market.operator_notes,
        captured_at=quote.fetched_at or datetime.now(UTC),
        ttl_seconds=ttl_seconds or settings.risk.snapshot_ttl_seconds,
    )


def _collect_sibling_markets(db: Session, provider, ref) -> list[SiblingMarket]:
    """Остальные рынки матча — чтобы участник мог ставить не только на исход серии.

    Рынки заводятся в БД: без этого исполнить по ним ставку было бы некуда.
    Сбой на этом шаге не должен ронять снимок — эксперимент продолжится, просто
    участники увидят один рынок.
    """
    try:
        refs = provider.list_event_markets(ref)
    except Exception:  # noqa: BLE001 — соседние рынки не критичны
        logger.warning("не удалось получить соседние рынки для %s", ref.external_id)
        return []

    settings = get_settings()
    book_types = {
        t.strip().upper()
        for t in settings.polymarket_sibling_book_types.split(",")
        if t.strip()
    }
    books_left = settings.polymarket_sibling_book_limit

    siblings: list[SiblingMarket] = []
    for sibling_ref in refs:
        try:
            row = upsert_market(db, sibling_ref)
            prices = (sibling_ref.raw or {}).get("outcomePrices")
            values = _as_prices(prices)
            if not values:
                continue
            yes_price = values[0]
            no_price = values[1] if len(values) > 1 else round(1.0 - yes_price, 4)

            # Стакан тянем только для торгуемых типов и в пределах лимита:
            # без него рынок остаётся видимым, но неисполнимым.
            yes_book = no_book = SnapshotBook()
            yes_ask = no_ask = None
            if sibling_ref.market_type in book_types and books_left > 0:
                try:
                    quote = provider.get_quote(sibling_ref)
                    yes_book = _to_book(quote.yes_bids, quote.yes_asks)
                    no_book = _to_book(quote.no_bids, quote.no_asks)
                    yes_ask, no_ask = yes_book.best_ask, no_book.best_ask
                    books_left -= 1
                except Exception:  # noqa: BLE001 — рынок останется без стакана
                    logger.warning("нет стакана для рынка %s", sibling_ref.external_id)

            siblings.append(
                SiblingMarket(
                    market_id=row.id,
                    external_id=sibling_ref.external_id,
                    question=sibling_ref.title,
                    market_type=sibling_ref.market_type,
                    yes_label=sibling_ref.yes_label,
                    no_label=sibling_ref.no_label,
                    yes_price=min(max(yes_price, 0.0), 1.0),
                    no_price=min(max(no_price, 0.0), 1.0),
                    yes_best_ask=yes_ask,
                    no_best_ask=no_ask,
                    yes_book=yes_book,
                    no_book=no_book,
                    liquidity_usdc=float(
                        (sibling_ref.raw or {}).get("liquidityNum")
                        or (sibling_ref.raw or {}).get("liquidity")
                        or 0.0
                    ),
                )
            )
        except Exception:  # noqa: BLE001 — битый рынок пропускаем
            continue
    return siblings


def _as_prices(raw) -> list[float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for value in raw:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return []
    return out


def capture_snapshot(
    db: Session,
    market: Market,
    phase: Phase,
    *,
    operator_context: str | None = None,
    map_number: int | None = None,
    actor: str = "operator",
) -> Snapshot:
    """Зафиксировать снимок. Предыдущий активный снимок помечается устаревшим."""
    provider = provider_for(db)
    try:
        ref = provider.get_market(market.external_id)
        quote = provider.get_quote(ref)
    except MarketNotAvailable as exc:
        market.status = MarketStatus.UNAVAILABLE.value
        audit.record(
            db,
            entity_type="market",
            entity_id=market.id,
            action="unavailable",
            after={"error": str(exc)},
        )
        db.flush()
        raise

    # актуализируем метаданные (рынок мог измениться у источника)
    market = upsert_market(db, ref)

    siblings = _collect_sibling_markets(db, provider, ref)

    model = build_snapshot_model(
        market, quote, phase,
        operator_context=operator_context,
        map_number=map_number,
        siblings=siblings,
    )
    row = Snapshot(
        market_id=market.id,
        phase=phase.value,
        payload=model.payload(),
        payload_hash=model.content_hash(),
        captured_at=model.captured_at,
        ttl_seconds=model.ttl_seconds,
        map_number=map_number,
    )
    db.add(row)
    db.flush()

    # snapshot_id внутри payload — чтобы участники ссылались на конкретный снимок
    payload = dict(row.payload)
    payload["snapshot_id"] = row.id
    row.payload = payload
    db.flush()

    _supersede_previous(db, market.id, row)
    audit.record(
        db,
        entity_type="snapshot",
        entity_id=row.id,
        action="capture",
        actor=actor,
        after={"market_id": market.id, "phase": phase.value, "hash": row.payload_hash},
    )
    return row


def _supersede_previous(db: Session, market_id: int, current: Snapshot) -> None:
    previous = db.scalars(
        select(Snapshot)
        .where(
            Snapshot.market_id == market_id,
            Snapshot.id != current.id,
            Snapshot.superseded_by_id.is_(None),
        )
        .order_by(Snapshot.id)
    ).all()
    for snap in previous:
        snap.superseded_by_id = current.id
    if previous:
        db.flush()


def load_snapshot_model(row: Snapshot) -> MarketSnapshot:
    model = MarketSnapshot.model_validate(row.payload)
    if model.snapshot_id is None:
        model = model.model_copy(update={"snapshot_id": row.id})
    return model


def is_stale(row: Snapshot, now: datetime | None = None) -> tuple[bool, str | None]:
    """Устарел ли снимок: по TTL или потому что его заменил более новый."""
    if row.superseded_by_id is not None:
        return True, f"snapshot заменён более новым (#{row.superseded_by_id})"
    now = now or datetime.now(UTC)
    captured = row.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    age = (now - captured).total_seconds()
    if age > row.ttl_seconds:
        return True, f"snapshot устарел: возраст {int(age)}с > TTL {row.ttl_seconds}с"
    return False, None
