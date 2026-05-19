import oracledb
from src.config import DB_HOST, DB_PORT, DB_SERVICE, DB_USER, DB_PASSWORD, DB_MAX_ROWS


def get_connection():
    dsn = oracledb.makedsn(DB_HOST, DB_PORT, service_name=DB_SERVICE)
    return oracledb.connect(user=DB_USER, password=DB_PASSWORD, dsn=dsn)


def execute_query(sql):
    """
    Execute a SELECT query against Oracle DB.
    Returns (columns: list[str], rows: list[tuple], error: str|None)
    """
    try:
        conn = get_connection()
    except oracledb.DatabaseError as e:
        return [], [], f"Connection failed: {e}"

    try:
        cursor = conn.cursor()
        # Oracle driver does not accept a trailing semicolon
        cursor.execute(sql.rstrip().rstrip(";"))
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchmany(DB_MAX_ROWS)
        return columns, rows, None
    except oracledb.DatabaseError as e:

        return [], [], f"Query execution failed: {e}"
    finally:
        cursor.close()
        conn.close()


def print_results(columns, rows):
    if not rows:
        print("  (no rows returned)")
        return

    # Calculate column widths
    widths = [len(c) for c in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    sep   = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header = "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)) + " |"

    print(sep)
    print(header)
    print(sep)
    for row in rows:
        line = "| " + " | ".join(
            (str(v) if v is not None else "NULL").ljust(widths[i])
            for i, v in enumerate(row)
        ) + " |"
        print(line)
    print(sep)
    print(f"  {len(rows)} row(s) returned (limit: {DB_MAX_ROWS})")
