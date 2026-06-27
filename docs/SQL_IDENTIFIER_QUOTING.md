# SQL Identifier Quoting - Fix for Special Characters in Table Names

## Problem

Users reported that table names containing special characters (particularly dashes/hyphens like `-`) cause SQL execution failures because the LLM-generated queries don't properly quote these identifiers.

### Example Issue
```sql
-- ❌ Fails - table name has dash
SELECT * FROM table-name

-- ✅ Works - properly quoted
SELECT * FROM "table-name"  -- PostgreSQL
SELECT * FROM `table-name`  -- MySQL
```

## Solution

We've implemented a **two-layer approach** to ensure identifiers are properly quoted:

### Layer 1: Enhanced LLM Prompt
The Analysis Agent prompt now includes explicit instructions to quote table/column names with special characters:

```
CRITICAL: When table or column names contain special characters (especially dashes/hyphens like '-'), 
you MUST wrap them in double quotes for PostgreSQL (e.g., "table-name") or backticks for MySQL 
(e.g., `table-name`). This is NON-NEGOTIABLE.
```

### Layer 2: Automatic SQL Sanitization (Safety Net)
Even if the LLM forgets to quote identifiers, we automatically fix the SQL before execution using the `SQLIdentifierQuoter` utility.

## How It Works

### 1. SQL Identifier Quoting Utility (`api/utils/sql_sanitizer.py`)

The `SQLIdentifierQuoter` class provides:

- **Detection**: Identifies table names with special characters (`-`, spaces, etc.)
- **Extraction**: Parses SQL queries to find table references (FROM, JOIN, UPDATE, etc.)
- **Auto-quoting**: Automatically wraps identifiers with the correct quote character
- **Database-aware**: Uses `"` for PostgreSQL, `` ` `` for MySQL

### 2. Integration Points

The sanitizer is integrated at two critical points in the query execution pipeline:

#### A. Regular Query Execution (`api/core/text2sql.py`)
After the LLM generates SQL but before execution:

```python
# Auto-quote table names with special characters
original_sql = answer_an['sql_query']
if original_sql:
    # Extract known table names from schema
    known_tables = {table[0] for table in result}
    
    # Determine database-specific quote character
    db_type, _ = get_database_type_and_loader(db_url)
    quote_char = DatabaseSpecificQuoter.get_quote_char(db_type)
    
    # Auto-quote identifiers with special characters
    sanitized_sql, was_modified = SQLIdentifierQuoter.auto_quote_identifiers(
        original_sql, known_tables, quote_char
    )
    
    if was_modified:
        logging.info("SQL query auto-sanitized: quoted table names with special characters")
        answer_an['sql_query'] = sanitized_sql
```

#### B. Destructive Operations Confirmation
The same sanitization applies to confirmed destructive operations (INSERT, UPDATE, DELETE, etc.).

## Features

### Safety First
- **Only quotes known tables**: Won't quote arbitrary strings or aliases
- **Idempotent**: Already-quoted identifiers aren't double-quoted
- **SQL keyword aware**: Doesn't quote SQL keywords (SELECT, FROM, etc.)
- **Database-specific**: Uses correct quote character for each database type

### Comprehensive Coverage
- Handles multiple table references in complex queries
- Supports qualified column names (`table-name.column`)
- Works with JOINs, subqueries, INSERT, UPDATE, DELETE, etc.

### Special Characters Handled
- Dashes/hyphens: `-`
- Spaces: ` `
- Dots: `.`
- And many more: `@`, `#`, `$`, `%`, etc.

## Usage Examples

### Example 1: Simple SELECT
```python
query = "SELECT * FROM order-items"
known_tables = {"order-items"}
result, modified = SQLIdentifierQuoter.auto_quote_identifiers(query, known_tables, '"')
# Result: SELECT * FROM "order-items"
```

### Example 2: Complex JOIN
```python
query = "SELECT * FROM user-accounts JOIN order-history ON user-accounts.id = order-history.user_id"
known_tables = {"user-accounts", "order-history"}
result, modified = SQLIdentifierQuoter.auto_quote_identifiers(query, known_tables, '"')
# Result: SELECT * FROM "user-accounts" JOIN "order-history" ON "user-accounts".id = "order-history".user_id
```

### Example 3: INSERT with Dashes
```python
query = "INSERT INTO table-name (id, name) VALUES (1, 'test')"
known_tables = {"table-name"}
result, modified = SQLIdentifierQuoter.auto_quote_identifiers(query, known_tables, '`')
# Result: INSERT INTO `table-name` (id, name) VALUES (1, 'test')
```

## Testing

Comprehensive unit tests are available in `tests/test_sql_sanitizer.py`:

```bash
# Run SQL sanitizer tests
pytest tests/test_sql_sanitizer.py -v

# Run all tests
make test
```

Test coverage includes:
- ✅ Table name detection and quoting
- ✅ Special character handling
- ✅ Complex queries (JOINs, subqueries)
- ✅ Database-specific quote characters
- ✅ Edge cases (already quoted, SQL keywords, aliases)

## Performance

The sanitizer has minimal performance impact:
- **Fast regex-based parsing**: No full SQL parsing overhead
- **Only runs when needed**: Skipped if no special characters detected
- **Non-blocking**: Executes synchronously in milliseconds

## Limitations

### What It Handles
- ✅ Table names in FROM, JOIN, UPDATE, INSERT INTO clauses
- ✅ Qualified column references (`table.column`)
- ✅ Common special characters (dashes, spaces, etc.)

### What It Doesn't Handle (Yet)
- ❌ Column names with special characters (future enhancement)
- ❌ Complex subquery table aliases
- ❌ Dynamic SQL with string concatenation

These limitations are acceptable because:
1. Table names with special chars are the primary pain point (as per user feedback)
2. The LLM prompt layer can still handle column quoting
3. Complex scenarios are rare in typical Text2SQL use cases

## Configuration

No configuration required! The system automatically:
- Detects database type from connection URL
- Selects appropriate quote character
- Identifies tables from schema
- Sanitizes queries transparently

## Monitoring

When sanitization occurs, it's logged:

```
INFO: SQL query auto-sanitized: quoted table names with special characters
```

This helps track when the LLM missed quoting vs. when queries were already correct.

## Future Enhancements

Potential improvements:
1. **Column name quoting**: Extend to handle column names with special chars
2. **Full SQL parsing**: Use a proper SQL parser for more robust handling
3. **Configurable rules**: Allow users to customize quoting behavior
4. **Performance optimization**: Cache table names per session

## Contributing

When modifying the SQL sanitizer:
1. Add tests to `tests/test_sql_sanitizer.py`
2. Ensure backward compatibility
3. Update this documentation
4. Test with both PostgreSQL and MySQL

## Conclusion

This two-layer approach (LLM prompt + automatic sanitization) provides robust handling of special characters in SQL identifiers, addressing the user's concern while maintaining safety and reliability.
