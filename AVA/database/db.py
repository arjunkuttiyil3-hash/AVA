import sqlite3
import os

DATABASE = os.path.join("database", "ava.db")


def get_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = get_connection()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            venue TEXT,
<<<<<<< HEAD
            event_date TEXT,
            description TEXT
        )
    """)

    event_columns = [
        row[1] for row in connection.execute(
            "PRAGMA table_info(events)"
        ).fetchall()
    ]

    if "description" not in event_columns:
        connection.execute(
            "ALTER TABLE events ADD COLUMN description TEXT"
        )

=======
            event_date TEXT
        )
    """)

>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            event_time TEXT NOT NULL,
            title TEXT NOT NULL,
            event_type TEXT,
            participant TEXT,
<<<<<<< HEAD
            description TEXT,
=======
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)

<<<<<<< HEAD
    columns = [
        row[1] for row in connection.execute(
            "PRAGMA table_info(schedule)"
        ).fetchall()
    ]

    if "description" not in columns:
        connection.execute(
            "ALTER TABLE schedule ADD COLUMN description TEXT"
        )

=======
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
    connection.commit()
    connection.close()