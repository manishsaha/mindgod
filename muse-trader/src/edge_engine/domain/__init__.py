"""Domain model: canonical markets, quotes, settlement rules, fair value.

Every detector and the execution layer stand on these types. Venue data is
never trusted directly: it is normalized into domain objects, linked to a
canonical market through the mapper, and only then priced or traded.
"""
from .events import CanonicalEvent
from .fairvalue import FairSource, FairValue, build_fair_value
from .log import QuoteLog
from .mapping import MarketMapper, RuleMismatch
from .markets import MARKET_STATUSES, MARKET_TYPES, CanonicalMarket, Outcome
from .quotes import MarketBook, OrderBookLevel, Quote, snapshot_to_quote
from .settlement import SettlementRules

__all__ = [
    "CanonicalEvent",
    "CanonicalMarket",
    "FairSource",
    "FairValue",
    "MarketBook",
    "MarketMapper",
    "MARKET_STATUSES",
    "MARKET_TYPES",
    "OrderBookLevel",
    "Outcome",
    "Quote",
    "QuoteLog",
    "RuleMismatch",
    "SettlementRules",
    "build_fair_value",
    "snapshot_to_quote",
]
