"""Base loader module providing abstract base class for data loaders."""

import re
from abc import ABC, abstractmethod
from typing import AsyncGenerator, List, Any, TYPE_CHECKING


class BaseLoader(ABC):
    """Abstract base class for data loaders."""

    @staticmethod
    @abstractmethod
    async def load(_graph_id: str, _data) -> AsyncGenerator[tuple[bool, str], None]:
        """
        Load the graph data into the database.
        This method must be implemented by any subclass.
        """
        if TYPE_CHECKING:  # pragma: no cover - only for type checking
            yield True, ""

    @staticmethod
    @abstractmethod
    def _execute_sample_query(
        cursor, table_name: str, col_name: str, sample_size: int = 10
    ) -> List[Any]:
        """
        Execute query to get most-frequent sample values for a column.

        Args:
            cursor: Database cursor
            table_name: Name of the table
            col_name: Name of the column
            sample_size: Max number of samples to retrieve (default: 10);
                frequency-ordered queries return min(distinct, limit), so
                flags naturally yield 2-3 values and code lists up to 10

        Returns:
            List of sample values
        """

    # Identifier-like names: ids/keys/hashes carry no filter semantics, and a
    # DISTINCT scan over a high-cardinality identifier is wasted work on
    # production-size tables.
    _IDENTIFIER_NAME_RE = re.compile(r"(id|guid|uuid|hash)$", re.IGNORECASE)
    # Sample text/flag columns AND coded enums (int/bigint codes such as
    # type_repo, entity_type, *_cd) and dates, so the model SEES the actual
    # domain values / available reporting dates instead of guessing. Identifier
    # columns are still skipped by name. The GROUP BY ... ORDER BY COUNT(*) DESC
    # LIMIT query bounds cost and surfaces the dominant (real) values first even
    # when the table has noisy rows. "int" matches int/integer/bigint/smallint.
    _SAMPLABLE_TYPE_RE = re.compile(
        r"char|string|text|bool|bit|int|date|timestamp", re.IGNORECASE
    )

    @classmethod
    def is_sample_candidate_column(cls, col_name: str, data_type: str = "") -> bool:
        """Whether a column is worth a value-preview query.

        Filter-style columns only: short text / flag types, never identifier
        or key columns. Empty data_type (engine didn't report one) is treated
        as non-candidate — previews are an enrichment, not a requirement.
        """
        name = str(col_name or "").strip().lower()
        if not name or cls._IDENTIFIER_NAME_RE.search(name):
            return False
        return bool(cls._SAMPLABLE_TYPE_RE.search(str(data_type or "")))

    @classmethod
    def extract_sample_values_for_column(
        cls, cursor, table_name: str, col_name: str, sample_size: int = 10,
        data_type: str = "",
    ) -> List[Any]:
        """
        Extract sample values for a FILTER-LIKE column to provide compact
        examples. Identifier/key columns and non-text types are skipped: they
        are useless as examples and expensive to DISTINCT-scan on production
        tables.

        Args:
            cursor: Database cursor
            table_name: Name of the table
            col_name: Name of the column
            sample_size: Max number of samples to retrieve (default: 10)
            data_type: Column data type as reported by the engine

        Returns:
            List of sample values (converted to strings), or empty list
        """
        if not cls.is_sample_candidate_column(col_name, data_type):
            return []
        try:
            sample_values = cls._execute_sample_query(
                cursor, table_name, col_name, sample_size
            )
        except Exception:  # pylint: disable=broad-exception-caught
            return []

        if sample_values:
            first_val = sample_values[0]
            if isinstance(first_val, (str, int, float)):
                return [str(v) for v in sample_values]

        return []
