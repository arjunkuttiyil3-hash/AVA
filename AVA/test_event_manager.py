<<<<<<< HEAD
import unittest

from app import app
from services.event_manager import EventManager
from services.script_generator import _event_details
=======
from services.event_manager import EventManager
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2


schedule = [
    {
        "event_time": "10:00",
        "title": "Welcome"
    },
    {
        "event_time": "10:05",
        "title": "Prayer"
    },
    {
        "event_time": "10:10",
        "title": "Welcome Speech"
    },
    {
        "event_time": "10:20",
        "title": "Dance Performance"
    }
]


manager = EventManager(schedule)

<<<<<<< HEAD

class EventDetailsTest(unittest.TestCase):
    def test_includes_description_in_current_event_details(self):
        item = {
            "title": "Opening Ceremony",
            "participant": "Principal",
            "description": "Welcome the students and introduce the event."
        }

        details = _event_details(item)

        self.assertIn("Title: Opening Ceremony", details)
        self.assertIn("Participant: Principal", details)
        self.assertIn("Description: Welcome the students and introduce the event.", details)

    def test_live_template_handles_missing_description(self):
        with app.test_request_context():
            html = app.jinja_env.get_template("live.html").render(
                event={"name": "Test Event", "id": 1},
                previous=None,
                current={
                    "title": "Opening",
                    "event_time": "10:00",
                    "event_type": "opening",
                    "participant": "Host"
                },
                next_event=None,
                script=None,
                event_id=1
            )

        self.assertNotIn("Description:", html)

    def test_live_template_handles_sqlite_row_without_description(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT 'Opening' AS title, '10:00' AS event_time, "
            "'opening' AS event_type, 'Host' AS participant"
        )
        row = cursor.fetchone()

        with app.test_request_context():
            html = app.jinja_env.get_template("live.html").render(
                event={"name": "Test Event", "id": 1},
                previous=None,
                current=row,
                next_event=None,
                script=None,
                event_id=1
            )

        self.assertNotIn("Description:", html)

        connection.close()

    def test_generator_handles_sqlite_row_without_description(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT 'Opening' AS title, 'Host' AS participant, "
            "NULL AS description"
        )
        row = cursor.fetchone()

        details = _event_details(row)

        self.assertIn("Title: Opening", details)
        self.assertIn("Participant: Host", details)
        self.assertNotIn("Description:", details)

        connection.close()

    def test_event_description_is_included_for_no_speaker_program(self):
        from services.script_generator import _build_script_prompt

        prompt = _build_script_prompt(
            event_name="College Tech Fest",
            event_description="A two-day program featuring coding, robotics, and exhibitions.",
            previous=None,
            current={
                "title": "Opening Ceremony",
                "participant": None,
                "description": "Welcome the participants to the program."
            },
            next_event=None,
            style="Energetic"
        )

        self.assertIn("College Tech Fest", prompt)
        self.assertIn("A two-day program featuring coding, robotics, and exhibitions.", prompt)
        self.assertIn("Opening Ceremony", prompt)
        self.assertIn("No speaker is provided", prompt)
        self.assertIn("Introduce the program", prompt)
        self.assertIn("the virtual anchor", prompt)
        self.assertIn("deliver the introduction and announcement yourself", prompt)
        self.assertIn("Do not assign the introduction to another person", prompt)
        self.assertIn("Do not use the phrase \"welcome back\"", prompt)
        self.assertIn("Use only factual, grammatically correct sentences", prompt)
        self.assertIn("warm, encouraging audience engagement", prompt)
        self.assertIn("audience engagement", prompt)

    def test_no_speaker_detection_handles_sqlite_row(self):
        import sqlite3

        from services.script_generator import _build_script_prompt

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT 'Opening Ceremony' AS title, NULL AS participant, "
            "'Welcome the participants' AS description"
        ).fetchone()

        prompt = _build_script_prompt(
            event_name="College Tech Fest",
            event_description="A test program",
            previous=None,
            current=row,
            next_event=None,
            style="Energetic"
        )

        self.assertIn("No speaker is provided", prompt)
        connection.close()

    def test_event_description_is_optional_for_older_event_rows(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT 'College Tech Fest' AS name, 'Main Hall' AS venue, "
            "'2026-01-01' AS event_date"
        ).fetchone()

        description = row["description"] if "description" in row.keys() else ""

        self.assertEqual("", description)
        connection.close()


if __name__ == "__main__":
    unittest.main()
=======
print("CURRENT:")
print(manager.get_current())

print("\nNEXT:")
print(manager.get_next())

print("\nPREVIOUS:")
print(manager.get_previous())

print("\nMoving to next event...")

manager.next_event()

print("\nCURRENT:")
print(manager.get_current())

print("\nNEXT:")
print(manager.get_next())

print("\nPREVIOUS:")
print(manager.get_previous())
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
