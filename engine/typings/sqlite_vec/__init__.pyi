from sqlite3 import Connection

# sqlite-vec 0.1.9 public API used by the engine.
def load(conn: Connection) -> None: ...
