"""T2S SDK - Text2SQL without a server.

This package provides a Python SDK for T2S's text-to-SQL
functionality, allowing you to convert natural language questions
to SQL queries directly in your Python applications.

Example:
    ```python
    from t2s import T2SClient

    async def main():
        qw = T2SClient(falkordb_url="redis://localhost:6379")
        await qw.connect_database("postgresql://user:pass@host/mydb")

        result = await qw.query("mydb", "Show me all customers from NYC")
        print(result.sql_query)   # SELECT * FROM customers WHERE city = 'NYC'
        print(result.results)      # [{"id": 1, "name": "John", "city": "NYC"}, ...]
        print(result.ai_response)  # "Found 42 customers from New York City..."
    ```

Requirements:
    - FalkorDB instance (local or remote)
    - OpenAI or Azure OpenAI API key
    - Target SQL database (PostgreSQL or MySQL)
"""

from t2s.client import T2SClient
from t2s.models import (
    QueryResult,
    QueryMetadata,
    QueryAnalysis,
    SchemaResult,
    DatabaseConnection,
    RefreshResult,
    QueryRequest,
    ChatMessage,
)
from t2s.connection import FalkorDBConnection

__all__ = [
    "T2SClient",
    "QueryResult",
    "QueryMetadata",
    "QueryAnalysis",
    "SchemaResult",
    "DatabaseConnection",
    "RefreshResult",
    "QueryRequest",
    "ChatMessage",
    "FalkorDBConnection",
]

__version__ = "0.2.0"
