from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from rivermind_core.models import GameType
from rivermind_core.stats import PlayerHandStatRow, StatsFilter


class StatMetric(StrEnum):
    VPIP = "vpip"
    PFR = "pfr"
    RFI = "rfi"
    THREE_BET = "three_bet"
    CALL_OPEN = "call_open"
    COLD_CALL = "cold_call"
    FOLD_TO_THREE_BET = "fold_to_three_bet"
    FLOP_CBET = "flop_cbet"
    FOLD_TO_FLOP_CBET = "fold_to_flop_cbet"


METRICS_WITH_OPPORTUNITIES = frozenset(
    metric for metric in StatMetric if metric not in {StatMetric.VPIP, StatMetric.PFR}
)


@dataclass(frozen=True, slots=True)
class HandQuery:
    stat_filter: StatsFilter = field(default_factory=StatsFilter)
    metric: StatMetric | None = None
    occurred: bool | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if self.metric is None and self.occurred is not None:
            raise ValueError("occurred requires a metric")
        if self.started_at is not None and self.ended_at is not None:
            if self.started_at > self.ended_at:
                raise ValueError("started_at cannot be after ended_at")
        if not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if self.offset < 0:
            raise ValueError("offset cannot be negative")


@dataclass(frozen=True, slots=True)
class PlayerHandReport:
    stats: PlayerHandStatRow
    played_at: datetime | None
    currency: str | None
    table_name: str
    invested: Decimal
    returned: Decimal
    collected: Decimal
    net_result: Decimal
    net_result_bb: Decimal
    accounting_balanced: bool

    @property
    def result_unit(self) -> str:
        if self.stats.game_type == GameType.TOURNAMENT:
            return "chips"
        return self.currency or "chips"
