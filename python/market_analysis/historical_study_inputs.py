"""Narrow archive contracts for scientific study workers after replay."""
from dataclasses import dataclass

from .binance_historical_archive import BinanceArchiveBundleManifest
from .historical_ohlc_evidence import BinanceTradeOHLCEvidence
from .historical_taker_flow_evidence import HistoricalTakerFlowEvidence
from .historical_replay import HistoricalReplayConfig
from .movement_metrics import MarketUniverseInput


@dataclass(frozen=True)
class HistoricalStudyArchiveInputs:
    archive_manifest: BinanceArchiveBundleManifest
    universe: MarketUniverseInput
    config: HistoricalReplayConfig
    ohlc_evidence: BinanceTradeOHLCEvidence | None = None
    taker_flow_evidence: HistoricalTakerFlowEvidence | None = None

    def __post_init__(self):
        identity = (self.archive_manifest.dataset_id, self.archive_manifest.dataset_version,
                    self.archive_manifest.content_sha256)
        if self.ohlc_evidence is not None:
            evidence = self.ohlc_evidence
            if ((evidence.dataset_id, evidence.dataset_version, evidence.dataset_content_sha256)
                    != identity or evidence.configured_symbols != self.universe.symbols):
                raise ValueError("study OHLC evidence differs from archive identity")
        if self.taker_flow_evidence is not None:
            if not self.taker_flow_evidence.matches_dataset(*identity, self.universe.symbols):
                raise ValueError("study taker-flow evidence differs from archive identity")
