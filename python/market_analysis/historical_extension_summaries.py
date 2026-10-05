"""Bounded projection counters and exact disk-backed retained statistics."""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile


def projection_summaries(points, symbols, partitions, windows, family, ratio):
    counts = {(partition, minute, symbol): Counter() for partition in partitions
              for minute in windows for symbol in symbols}
    reasons = {key: Counter() for key in counts}
    point_counts = Counter()
    unique_field = {"oi": "unique_source_endpoint_pair_count",
                    "funding": "unique_settlement_event_count",
                    "liquidation": "observed_snapshot_projection_count"}[family]
    with tempfile.TemporaryDirectory(prefix="historical-summary-") as directory:
        connection = sqlite3.connect(str(Path(directory) / "unique.sqlite"))
        try:
            connection.execute("CREATE TABLE endpoints (partition TEXT, minute INTEGER, symbol TEXT, identity TEXT, PRIMARY KEY(partition, minute, symbol, identity))")
            for point in points:
                for partition in (point.partition, "all"):
                    point_counts[partition] += 1
                    for window in point.windows:
                        minute = window["window_minutes"]
                        for row in window["symbols"]:
                            key = (partition, minute, row["symbol"])
                            counter = counts[key]
                            counter["count"] += 1
                            ready = row["status"] != "UNAVAILABLE"
                            if ready:
                                counter["ready"] += 1
                                if family == "liquidation":
                                    counter["projection"] += row["observed_snapshot_row_count"]
                                else:
                                    identity = (str((row["current_source_time_ms"], row["prior_source_time_ms"]))
                                                if family == "oi" else str(row["settlement_time_ms"]))
                                    connection.execute("INSERT OR IGNORE INTO endpoints VALUES (?, ?, ?, ?)", (*key, identity))
                            else:
                                reasons[key][row["reason"]] += 1
            result = {}
            for partition in partitions:
                summaries = []
                for minute in windows:
                    per_symbol = []
                    for symbol in symbols:
                        key = (partition, minute, symbol)
                        counter = counts[key]
                        unique = (counter["projection"] if family == "liquidation" else
                                  connection.execute("SELECT COUNT(*) FROM endpoints WHERE partition=? AND minute=? AND symbol=?", key).fetchone()[0])
                        per_symbol.append({"symbol": symbol, "point_count": counter["count"],
                            "ready_count": counter["ready"],
                            "unavailable_count": counter["count"] - counter["ready"],
                            "ready_coverage_ratio": ratio(counter["ready"], counter["count"]),
                            "unavailable_reasons": dict(sorted(reasons[key].items())), unique_field: unique})
                    total = sum(row["point_count"] for row in per_symbol)
                    ready = sum(row["ready_count"] for row in per_symbol)
                    summaries.append({"window_minutes": minute, "point_count": point_counts[partition],
                        "configured_symbol_point_count": total, "ready_symbol_point_count": ready,
                        "unavailable_symbol_point_count": total - ready, "summary_denominator": ready,
                        "per_symbol": per_symbol, unique_field: sum(row[unique_field] for row in per_symbol)})
                result[partition] = summaries
            return result
        finally:
            connection.close()


class DiskDecimalValues:
    """Insertion-order arithmetic and Decimal-order exact median endpoints."""
    def __init__(self, connection, key):
        self.connection, self.key = connection, key
        self.count = 0

    def append(self, value):
        self.connection.execute("INSERT INTO values_table (series, value) VALUES (?, ?)",
                                (self.key, str(value)))
        self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        for row in self.connection.execute("SELECT value FROM values_table WHERE series=? ORDER BY rowid", (self.key,)):
            yield Decimal(row[0])

    def middle(self):
        offset = (self.count - 1) // 2
        number = 1 if self.count % 2 else 2
        return tuple(Decimal(row[0]) for row in self.connection.execute(
            "SELECT value FROM values_table WHERE series=? ORDER BY value COLLATE exact_decimal, rowid LIMIT ? OFFSET ?",
            (self.key, number, offset)))


def mark_summaries(points, symbols, partitions, windows, mean, median, decimal_text, divergence_sign):
    counts = {(partition, minute): Counter() for partition in partitions for minute in windows}
    symbol_counts = {(*key, symbol): Counter() for key in counts for symbol in symbols}
    reason_counts = {key: Counter() for key in symbol_counts}
    crosstabs = {key: Counter() for key in counts}
    fields = ("mark_return", "trade_return", "basis_at_start", "basis_at_end", "divergence")
    with tempfile.TemporaryDirectory(prefix="historical-mark-statistics-") as directory:
        connection = sqlite3.connect(str(Path(directory) / "values.sqlite"))
        connection.create_collation("exact_decimal", lambda left, right:
            (Decimal(left) > Decimal(right)) - (Decimal(left) < Decimal(right)))
        connection.execute("CREATE TABLE values_table (series TEXT, value TEXT)")
        connection.execute("CREATE INDEX values_series ON values_table(series)")
        values = {(*key, field): DiskDecimalValues(connection, str((*key, field)))
                  for key in counts for field in fields}
        try:
            for point in points:
                for partition in (point.partition, "all"):
                    for window in point.windows:
                        key = (partition, window.window_minutes)
                        counts[key]["points"] += 1
                        for row in window.symbols:
                            symbol_key = (*key, row.symbol)
                            symbol_counts[symbol_key]["count"] += 1
                            symbol_counts[symbol_key]["ready"] += row.status == "READY"
                            reason_counts[symbol_key].update(reason.reason for reason in row.reasons)
                        if window.all_configured_symbols_ready:
                            counts[key]["common"] += 1
                            for row in window.symbols:
                                for field in fields:
                                    values[(*key, field)].append(Decimal(getattr(row, field)))
                                divergence = Decimal(row.divergence)
                                counts[key]["positive"] += divergence > 0
                                counts[key]["negative"] += divergence < 0
                                counts[key]["zero"] += divergence == 0
                                crosstabs[key][(row.v1_direction or "UNAVAILABLE", divergence_sign(row.divergence))] += 1
            result = {}
            for partition in partitions:
                summaries = []
                for minute in windows:
                    key = (partition, minute)
                    counter = counts[key]
                    per_symbol = []
                    for symbol in symbols:
                        symbol_key = (*key, symbol)
                        count = symbol_counts[symbol_key]
                        per_symbol.append({"symbol": symbol, "minute_point_count": count["count"],
                            "ready_count": count["ready"], "unavailable_count": count["count"] - count["ready"],
                            "coverage_ratio": count["ready"] / count["count"] if count["count"] else None,
                            "reason_counts": dict(sorted(reason_counts[symbol_key].items()))})
                    diagnostic = {"symbol_window_observation_count": len(values[(*key, "divergence")])}
                    for field, label in (("mark_return", "mark_return"), ("trade_return", "trade_return"),
                                         ("basis_at_start", "basis_at_start"), ("basis_at_end", "basis_at_end"),
                                         ("divergence", "divergence")):
                        diagnostic[f"median_{label}"] = decimal_text(median(values[(*key, field)]))
                        if field in ("mark_return", "trade_return", "divergence"):
                            diagnostic[f"mean_{label}"] = decimal_text(mean(values[(*key, field)]))
                    diagnostic.update({"positive_divergence_count": counter["positive"],
                        "negative_divergence_count": counter["negative"], "zero_divergence_count": counter["zero"],
                        "same_time_v1_direction_by_divergence_sign": tuple(
                            {"v1_direction": direction, "divergence_sign": sign, "count": count}
                            for (direction, sign), count in sorted(crosstabs[key].items()))})
                    summaries.append({"window_minutes": minute, "minute_point_count": counter["points"],
                        "configured_symbol_window_count": counter["points"] * len(symbols),
                        "ready_symbol_window_count": sum(row["ready_count"] for row in per_symbol),
                        "unavailable_symbol_window_count": sum(row["unavailable_count"] for row in per_symbol),
                        "all_configured_symbols_ready_boundary_count": counter["common"],
                        "all_configured_symbols_ready_coverage_ratio": counter["common"] / counter["points"] if counter["points"] else None,
                        "per_symbol_coverage": tuple(per_symbol), "common_ready_diagnostic_summary": diagnostic})
                result[partition] = tuple(summaries)
            return result
        finally:
            connection.close()
