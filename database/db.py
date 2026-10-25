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
            event_date TEXT
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            event_time TEXT NOT NULL,
            title TEXT NOT NULL,
            event_type TEXT,
            participant TEXT,
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)

    connection.commit()
    connection.close()