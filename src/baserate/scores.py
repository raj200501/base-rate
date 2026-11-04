"""Score files: labeled monitor scores, the input of the ``baserate`` command line.

A score file has one row per labeled trace that a monitor scored. Two formats
hold the same columns: CSV with a header row (``.csv``) and JSON Lines, one
object per line (``.jsonl`` or ``.ndjson``).

========================  ========  ===============================================
Column                    Needed    Meaning
========================  ========  ===============================================
``trace_id``              yes       Unique id of the trace.
``label``                 yes       ``benign`` or ``violation``.
``score``                 yes       The monitor's score; finite, higher is more
                                    suspicious.
``cluster``               yes       Id of the independent source run the trace
                                    descends from. Procedural variants and
                                    inserted-violation copies share their source
                                    run's id; a trace that is its own run can use
                                    its ``trace_id``. Every interval counts clusters,
                                    not traces, as the independent units.
``stage2_score``          no        Score of a second, more expensive monitor, for
                                    two-stage cascades. Empty (CSV) or ``null``
                                    (JSON Lines) where stage 2 did not score the
                                    trace.
``weight``                no        Positive sampling weight (inverse inclusion
                                    probability) for stratified or importance
                                    samples; every row needs one if the column
                                    exists.
``synthetic``             no        ``true`` or ``false``: whether the trace is
                                    synthetic or procedurally generated.
========================  ========  ===============================================

Column names are matched after trimming spaces and lowercasing. Other columns
are allowed and ignored (they are listed in :attr:`ScoreSet.ignored_columns`).
Lines whose first non-space character is ``#`` are comments; their text is kept
in :attr:`ScoreSet.comments`, so a file can say where its data came from. Blank
lines are skipped.

:func:`read_scores` reads and validates a file and raises
:class:`ScoreFileError` with the file name and line number of the first
problem. :func:`write_scores` writes either format.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

from baserate.cascade import TwoStageScores, two_stage_scores

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]
Format = Literal["csv", "jsonl"]

REQUIRED_COLUMNS: tuple[str, ...] = ("trace_id", "label", "score", "cluster")
"""Columns every score file must have."""

OPTIONAL_COLUMNS: tuple[str, ...] = ("stage2_score", "weight", "synthetic")
"""Columns a score file may have, in the order :func:`write_scores` writes them."""

LABELS: tuple[str, ...] = ("benign", "violation")
"""Allowed values of the ``label`` column."""

FORMATS: tuple[str, ...] = ("csv", "jsonl")
"""Supported file formats."""

_SUFFIX_FORMATS: dict[str, Format] = {".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0"})


class ScoreFileError(ValueError):
    """A score file, or score data, is missing, malformed, or inconsistent."""


def _fail(where: str, message: str) -> ScoreFileError:
    return ScoreFileError(f"{where}: {message}" if where else message)


@dataclass(frozen=True, eq=False)
class ScoreSet:
    """Labeled monitor scores, one entry per trace, stored as columns.

    Build one with :func:`read_scores` or directly; the constructor validates
    every column and raises :class:`ScoreFileError` on a problem. Array
    columns are converted to read-only NumPy arrays.

    Attributes:
        trace_ids: Unique trace ids.
        labels: ``"benign"`` or ``"violation"`` per trace.
        scores: Monitor score per trace (finite).
        clusters: Independent source-run id per trace.
        stage2_scores: Stage-2 score per trace (NaN where stage 2 did not
            score it), or None when the column is absent.
        weights: Positive sampling weight per trace, or None.
        synthetic: Whether each trace is synthetic, or None when unknown.
        source: Where the scores came from (a file path), for messages.
        sha256: SHA-256 of the file's bytes, when read from a file.
        comments: Text of the file's comment lines.
        ignored_columns: Columns present in the file that were not read.
    """

    trace_ids: tuple[str, ...]
    labels: tuple[str, ...]
    scores: FloatArray
    clusters: tuple[str, ...]
    stage2_scores: FloatArray | None = None
    weights: FloatArray | None = None
    synthetic: BoolArray | None = None
    source: str = ""
    sha256: str | None = None
    comments: tuple[str, ...] = ()
    ignored_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        where = self.source
        n = len(self.trace_ids)
        object.__setattr__(self, "trace_ids", tuple(str(t) for t in self.trace_ids))
        object.__setattr__(self, "labels", tuple(str(v) for v in self.labels))
        object.__setattr__(self, "clusters", tuple(str(c) for c in self.clusters))
        object.__setattr__(self, "comments", tuple(self.comments))
        object.__setattr__(self, "ignored_columns", tuple(self.ignored_columns))
        object.__setattr__(self, "scores", _column(self.scores, n, "score", where))
        if self.stage2_scores is not None:
            s2 = _column(self.stage2_scores, n, "stage2_score", where, allow_nan=True)
            object.__setattr__(self, "stage2_scores", s2)
        if self.weights is not None:
            w = _column(self.weights, n, "weight", where)
            if np.any(w <= 0):
                raise _fail(where, "every weight must be positive")
            object.__setattr__(self, "weights", w)
        if self.synthetic is not None:
            syn = np.array(self.synthetic, dtype=np.bool_).ravel()
            if syn.size != n:
                raise _fail(where, f"synthetic has {syn.size} values for {n} traces")
            syn.setflags(write=False)
            object.__setattr__(self, "synthetic", syn)
        if len(self.labels) != n or len(self.clusters) != n:
            raise _fail(where, "trace_id, label, and cluster must have one value per trace")
        if n == 0:
            raise _fail(where, "no scored traces")
        bad = sorted({v for v in self.labels if v not in LABELS})
        if bad:
            raise _fail(where, f"label must be 'benign' or 'violation', got {bad[0]!r}")
        if any(not t for t in self.trace_ids):
            raise _fail(where, "every trace needs a non-empty trace_id")
        if any(not c for c in self.clusters):
            raise _fail(where, "every trace needs a non-empty cluster")
        if len(set(self.trace_ids)) != n:
            seen: set[str] = set()
            for trace_id in self.trace_ids:
                if trace_id in seen:
                    raise _fail(where, f"trace_id {trace_id!r} appears more than once")
                seen.add(trace_id)
        n_pos = sum(v == "violation" for v in self.labels)
        if n_pos == 0:
            raise _fail(where, "no violation traces; need at least one of each label")
        if n_pos == n:
            raise _fail(where, "no benign traces; need at least one of each label")

    # ------------------------------------------------------------ masks and sizes

    @cached_property
    def is_violation(self) -> BoolArray:
        """Boolean mask of violation traces."""
        mask = np.array([v == "violation" for v in self.labels], dtype=np.bool_)
        mask.setflags(write=False)
        return mask

    @property
    def n_traces(self) -> int:
        """Number of traces."""
        return len(self.trace_ids)

    @property
    def n_neg(self) -> int:
        """Number of benign traces."""
        return int((~self.is_violation).sum())

    @property
    def n_pos(self) -> int:
        """Number of violation traces."""
        return int(self.is_violation.sum())

    @property
    def n_clusters(self) -> int:
        """Distinct clusters over both labels (a cluster may hold both)."""
        return len(set(self.clusters))

    @property
    def n_neg_clusters(self) -> int:
        """Distinct clusters among benign traces (independent benign runs)."""
        return len(set(self.neg_clusters.tolist()))

    @property
    def n_pos_clusters(self) -> int:
        """Distinct clusters among violation traces (independent violation runs)."""
        return len(set(self.pos_clusters.tolist()))

    # ------------------------------------------------------------- class columns

    @property
    def neg(self) -> FloatArray:
        """Scores of benign traces."""
        return np.asarray(self.scores[~self.is_violation], dtype=np.float64)

    @property
    def pos(self) -> FloatArray:
        """Scores of violation traces."""
        return np.asarray(self.scores[self.is_violation], dtype=np.float64)

    @cached_property
    def _cluster_array(self) -> NDArray[np.str_]:
        return np.array(self.clusters, dtype=np.str_)

    @property
    def neg_clusters(self) -> NDArray[np.str_]:
        """Cluster of each benign trace, aligned with :attr:`neg`."""
        return self._cluster_array[~self.is_violation]

    @property
    def pos_clusters(self) -> NDArray[np.str_]:
        """Cluster of each violation trace, aligned with :attr:`pos`."""
        return self._cluster_array[self.is_violation]

    @property
    def neg_weights(self) -> FloatArray | None:
        """Weights of benign traces, or None."""
        if self.weights is None:
            return None
        return np.asarray(self.weights[~self.is_violation], dtype=np.float64)

    @property
    def pos_weights(self) -> FloatArray | None:
        """Weights of violation traces, or None."""
        if self.weights is None:
            return None
        return np.asarray(self.weights[self.is_violation], dtype=np.float64)

    @property
    def has_stage2(self) -> bool:
        """Whether a ``stage2_score`` column is present."""
        return self.stage2_scores is not None

    @property
    def n_synthetic(self) -> int | None:
        """Traces marked synthetic, or None without a ``synthetic`` column."""
        return None if self.synthetic is None else int(self.synthetic.sum())

    @property
    def synthetic_share(self) -> float | None:
        """Fraction of traces marked synthetic, or None without a ``synthetic`` column."""
        n_syn = self.n_synthetic
        return None if n_syn is None else n_syn / self.n_traces

    @property
    def columns(self) -> tuple[str, ...]:
        """The columns :func:`write_scores` writes: required, then present optional ones."""
        present = {
            "stage2_score": self.stage2_scores is not None,
            "weight": self.weights is not None,
            "synthetic": self.synthetic is not None,
        }
        return REQUIRED_COLUMNS + tuple(c for c in OPTIONAL_COLUMNS if present[c])

    def two_stage(self) -> TwoStageScores:
        """Stage-1 (``score``) and stage-2 (``stage2_score``) scores for a cascade.

        Raises:
            ScoreFileError: If there is no ``stage2_score`` column.
        """
        if self.stage2_scores is None:
            raise _fail(
                self.source,
                "no stage2_score column; a cascade needs stage-1 scores (score) and "
                "stage-2 scores (stage2_score)",
            )
        mask = self.is_violation
        return two_stage_scores(
            self.scores[~mask],
            self.stage2_scores[~mask],
            self.scores[mask],
            self.stage2_scores[mask],
            self.neg_clusters,
            self.pos_clusters,
        )

    def records(self) -> Iterator[dict[str, Any]]:
        """One dict per trace with the keys of :attr:`columns` (NaN stage-2 as None)."""
        for i in range(self.n_traces):
            rec: dict[str, Any] = {
                "trace_id": self.trace_ids[i],
                "label": self.labels[i],
                "score": float(self.scores[i]),
                "cluster": self.clusters[i],
            }
            if self.stage2_scores is not None:
                s2 = float(self.stage2_scores[i])
                rec["stage2_score"] = None if math.isnan(s2) else s2
            if self.weights is not None:
                rec["weight"] = float(self.weights[i])
            if self.synthetic is not None:
                rec["synthetic"] = bool(self.synthetic[i])
            yield rec


def _column(
    values: ArrayLike, n: int, name: str, where: str, allow_nan: bool = False
) -> FloatArray:
    """A read-only float column of length ``n``; finite unless NaN is allowed."""
    try:
        arr = np.array(values, dtype=np.float64).ravel()
    except (TypeError, ValueError):
        raise _fail(where, f"{name} values must be numbers") from None
    if arr.size != n:
        raise _fail(where, f"{name} has {arr.size} values for {n} traces")
    ok = np.isfinite(arr)
    if allow_nan:
        ok |= np.isnan(arr)
    if not np.all(ok):
        raise _fail(where, f"{name} values must be finite numbers")
    arr.setflags(write=False)
    return arr


# ------------------------------------------------------------------ parsing


@dataclass
class _Columns:
    """Values gathered while parsing, before building the :class:`ScoreSet`."""

    trace_ids: list[str]
    labels: list[str]
    scores: list[float]
    clusters: list[str]
    stage2: list[float] | None
    weights: list[float] | None
    synthetic: list[bool] | None
    first_line: dict[str, int]


def _new_columns(present: set[str]) -> _Columns:
    return _Columns(
        trace_ids=[],
        labels=[],
        scores=[],
        clusters=[],
        stage2=[] if "stage2_score" in present else None,
        weights=[] if "weight" in present else None,
        synthetic=[] if "synthetic" in present else None,
        first_line={},
    )


def _text(value: Any) -> str:
    """A cell or JSON value as stripped text (None and missing as empty)."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return str(value).strip()


def _number(raw: Any, name: str, where: str) -> float:
    if isinstance(raw, bool):
        raise _fail(where, f"{name} must be a number, got {raw!r}")
    if isinstance(raw, int | float):
        value = float(raw)
    else:
        text = _text(raw)
        if not text:
            raise _fail(where, f"{name} is empty")
        try:
            value = float(text)
        except ValueError:
            raise _fail(where, f"{name} is not a number: {text!r}") from None
    if not math.isfinite(value):
        raise _fail(where, f"{name} must be finite, got {_text(raw)!r}")
    return value


def _boolean(raw: Any, name: str, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = _text(raw).lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise _fail(where, f"{name} must be true or false, got {_text(raw)!r}")


def _add_row(cols: _Columns, row: dict[str, Any], source: str, line: int) -> None:
    """Validate one record (keys are normalized column names) and append it."""
    where = f"{source}:{line}"
    trace_id = _text(row.get("trace_id"))
    if not trace_id:
        raise _fail(where, "trace_id is empty")
    if trace_id in cols.first_line:
        first = cols.first_line[trace_id]
        raise _fail(where, f"trace_id {trace_id!r} also appears on line {first}")
    raw_label = _text(row.get("label"))
    label = raw_label.lower()
    if label not in LABELS:
        raise _fail(where, f"label must be 'benign' or 'violation', got {raw_label!r}")
    score = _number(row.get("score"), "score", where)
    cluster = _text(row.get("cluster"))
    if not cluster:
        raise _fail(
            where, "cluster is empty; use the trace_id if the trace is its own independent run"
        )
    if cols.stage2 is not None:
        raw2 = row.get("stage2_score")
        if _text(raw2) == "":
            cols.stage2.append(math.nan)
        else:
            try:
                cols.stage2.append(_number(raw2, "stage2_score", where))
            except ScoreFileError as exc:
                raise _fail(
                    where,
                    f"{str(exc).removeprefix(where + ': ')}; leave it empty (CSV) or null "
                    "(JSON Lines) where stage 2 did not score the trace",
                ) from None
    if cols.weights is not None:
        weight = _number(row.get("weight"), "weight", where)
        if weight <= 0:
            raise _fail(where, f"weight must be positive, got {_text(row.get('weight'))!r}")
        cols.weights.append(weight)
    if cols.synthetic is not None:
        cols.synthetic.append(_boolean(row.get("synthetic"), "synthetic", where))
    cols.first_line[trace_id] = line
    cols.trace_ids.append(trace_id)
    cols.labels.append(label)
    cols.scores.append(score)
    cols.clusters.append(cluster)


def _build(
    cols: _Columns,
    source: str,
    sha256: str | None,
    comments: Sequence[str],
    ignored: Sequence[str],
) -> ScoreSet:
    if not cols.trace_ids:
        raise _fail(source, "no data rows")
    return ScoreSet(
        trace_ids=tuple(cols.trace_ids),
        labels=tuple(cols.labels),
        scores=np.array(cols.scores, dtype=np.float64),
        clusters=tuple(cols.clusters),
        stage2_scores=None if cols.stage2 is None else np.array(cols.stage2, dtype=np.float64),
        weights=None if cols.weights is None else np.array(cols.weights, dtype=np.float64),
        synthetic=None if cols.synthetic is None else np.array(cols.synthetic, dtype=np.bool_),
        source=source,
        sha256=sha256,
        comments=tuple(comments),
        ignored_columns=tuple(ignored),
    )


def _split_comments(text: str) -> tuple[str, list[str]]:
    """Blank out comment lines (keeping line numbers) and collect their text."""
    lines = text.split("\n")
    comments: list[str] = []
    kept: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            comments.append(line.strip()[1:].strip())
            kept.append("")
        else:
            kept.append(line)
    return "\n".join(kept), comments


def _check_header(names: Sequence[str], source: str) -> tuple[list[str], list[str]]:
    """Normalized column names and the ignored ones; raises on a bad header."""
    normalized = [n.strip().lower() for n in names]
    for i, name in enumerate(normalized, start=1):
        if not name:
            raise _fail(source, f"column {i} of the header has no name")
    dup = sorted({n for n in normalized if normalized.count(n) > 1})
    if dup:
        raise _fail(source, f"column {dup[0]!r} appears more than once in the header")
    missing = [c for c in REQUIRED_COLUMNS if c not in normalized]
    if missing:
        hint = ""
        if len(normalized) == 1:
            sep = next((s for s in (";", "\t", "|") if s in normalized[0]), None)
            if sep is not None:
                hint = f" (the header seems to use {sep!r} as the separator; use commas)"
        raise _fail(
            source,
            f"missing required column(s) {', '.join(missing)}; found {', '.join(normalized)}; "
            f"need {', '.join(REQUIRED_COLUMNS)}{hint}",
        )
    known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    return normalized, [n for n in normalized if n not in known]


def parse_csv(text: str, source: str = "<csv>", sha256: str | None = None) -> ScoreSet:
    """Parse CSV score data (with a header row).

    Args:
        text: The file's text.
        source: Name used in error messages (usually the path).
        sha256: Digest of the file's bytes, recorded on the result.

    Returns:
        The validated scores.

    Raises:
        ScoreFileError: On the first problem, naming the line.
    """
    body, comments = _split_comments(text)
    reader = csv.reader(io.StringIO(body, newline=""))
    header: list[str] = []
    cols: _Columns | None = None
    ignored: list[str] = []
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            line = reader.line_num
            if cols is None:
                header, ignored = _check_header(row, source)
                cols = _new_columns(set(header))
                continue
            if len(row) != len(header):
                raise _fail(f"{source}:{line}", f"{len(row)} values for {len(header)} columns")
            _add_row(cols, dict(zip(header, row, strict=True)), source, line)
    except csv.Error as exc:
        raise _fail(f"{source}:{reader.line_num}", f"malformed CSV: {exc}") from None
    if cols is None:
        raise _fail(source, "empty file: no header row")
    return _build(cols, source, sha256, comments, ignored)


def parse_jsonl(text: str, source: str = "<jsonl>", sha256: str | None = None) -> ScoreSet:
    """Parse JSON Lines score data: one JSON object per line.

    Keys are the column names. A column is present when any object has its
    key; ``stage2_score`` may then be missing or ``null`` on some lines, while
    ``weight`` and ``synthetic`` must appear on every line.

    Args:
        text: The file's text.
        source: Name used in error messages (usually the path).
        sha256: Digest of the file's bytes, recorded on the result.

    Returns:
        The validated scores.

    Raises:
        ScoreFileError: On the first problem, naming the line.
    """
    body, comments = _split_comments(text)
    records: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(body.split("\n"), start=1):
        if not line.strip():
            continue
        where = f"{source}:{number}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _fail(where, f"not valid JSON: {exc.msg}") from None
        if not isinstance(obj, dict):
            raise _fail(where, "each line must be a JSON object")
        records.append((number, {str(k).strip().lower(): v for k, v in obj.items()}))
    if not records:
        raise _fail(source, "no data rows")
    keys: set[str] = set().union(*(rec.keys() for _, rec in records))
    known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    cols = _new_columns(keys)
    for number, rec in records:
        missing = [c for c in REQUIRED_COLUMNS if c not in rec]
        missing += [c for c in ("weight", "synthetic") if c in keys and c not in rec]
        if missing:
            raise _fail(f"{source}:{number}", f"missing key(s) {', '.join(missing)}")
        _add_row(cols, rec, source, number)
    ignored = sorted(keys - known)
    return _build(cols, source, sha256, comments, ignored)


def format_of(path: str | Path, fmt: str | None = None) -> Format:
    """The format of a score file: ``fmt`` if given, else from the file suffix.

    Raises:
        ScoreFileError: For an unknown format or suffix.
    """
    if fmt is not None:
        if fmt not in FORMATS:
            raise ScoreFileError(f"unknown score file format {fmt!r}; use csv or jsonl")
        return "csv" if fmt == "csv" else "jsonl"
    suffix = Path(path).suffix.lower()
    if suffix not in _SUFFIX_FORMATS:
        raise ScoreFileError(
            f"{path}: cannot tell the format from the suffix {suffix or '(none)'!r}; "
            "name the file .csv or .jsonl, or pass the format explicitly"
        )
    return _SUFFIX_FORMATS[suffix]


def read_scores(path: str | Path, fmt: str | None = None) -> ScoreSet:
    """Read and validate a score file.

    Args:
        path: A ``.csv``, ``.jsonl``, or ``.ndjson`` file.
        fmt: ``"csv"`` or ``"jsonl"`` to override the suffix.

    Returns:
        The validated scores, with the file's SHA-256 and comments.

    Raises:
        ScoreFileError: If the file is missing, unreadable, or invalid.
    """
    p = Path(path)
    kind = format_of(p, fmt)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise ScoreFileError(f"{p}: file not found") from None
    except IsADirectoryError:
        raise ScoreFileError(f"{p}: is a directory, not a score file") from None
    except OSError as exc:
        raise ScoreFileError(f"{p}: cannot read: {exc.strerror}") from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ScoreFileError(f"{p}: not UTF-8 text") from None
    digest = hashlib.sha256(data).hexdigest()
    if kind == "csv":
        return parse_csv(text, str(p), digest)
    return parse_jsonl(text, str(p), digest)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def dumps_scores(scores: ScoreSet, fmt: str = "csv", comments: Sequence[str] = ()) -> str:
    """Score data as CSV or JSON Lines text (deterministic).

    Floats are written in their shortest round-trip form, so reading the text
    back gives identical values.

    Args:
        scores: The scores.
        fmt: ``"csv"`` or ``"jsonl"``.
        comments: Comment lines written first, each prefixed with ``# ``.

    Returns:
        The text, ending with a newline.
    """
    kind = format_of("", fmt)
    out = io.StringIO()
    for line in comments:
        for part in str(line).splitlines() or [""]:
            out.write(f"# {part}".rstrip() + "\n")
    columns = scores.columns
    if kind == "csv":
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        for rec in scores.records():
            writer.writerow([_cell(rec[c]) for c in columns])
    else:
        for rec in scores.records():
            out.write(json.dumps(rec, allow_nan=False) + "\n")
    return out.getvalue()


def write_scores(
    scores: ScoreSet, path: str | Path, fmt: str | None = None, comments: Sequence[str] = ()
) -> None:
    """Write a score file that :func:`read_scores` reads back unchanged.

    Args:
        scores: The scores.
        path: Destination; the suffix picks the format unless ``fmt`` is given.
        fmt: ``"csv"`` or ``"jsonl"``.
        comments: Comment lines written at the top.
    """
    p = Path(path)
    text = dumps_scores(scores, format_of(p, fmt), comments)
    p.write_text(text, encoding="utf-8", newline="")
