from flask import Flask, render_template, request, redirect, url_for
from database.db import init_database, get_connection
from services.event_manager import EventManager
from services.script_generator import generate_script

app = Flask(__name__)

init_database()

# Store the current position of each live event
live_positions = {}


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/create-event", methods=["GET", "POST"])
def create_event():

    if request.method == "POST":

        event_name = request.form["event_name"]
        venue = request.form["venue"]
        event_date = request.form["event_date"]

        connection = get_connection()

        cursor = connection.execute(
            """
            INSERT INTO events (name, venue, event_date)
            VALUES (?, ?, ?)
            """,
            (event_name, venue, event_date)
        )

        connection.commit()

        event_id = cursor.lastrowid

        connection.close()

        return redirect(url_for("event", event_id=event_id))

    return render_template("create_event.html")


@app.route("/event/<int:event_id>")
def event(event_id):

    connection = get_connection()

    event_data = connection.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    schedule = connection.execute(
        """
        SELECT * FROM schedule
        WHERE event_id = ?
        ORDER BY event_time
        """,
        (event_id,)
    ).fetchall()

    connection.close()

    return render_template(
        "event.html",
        event=event_data,
        schedule=schedule
    )


@app.route("/event/<int:event_id>/add-schedule", methods=["POST"])
def add_schedule(event_id):

    event_time = request.form["event_time"]
    title = request.form["title"]
    event_type = request.form["event_type"]
    participant = request.form["participant"]

    connection = get_connection()

    connection.execute(
        """
        INSERT INTO schedule
        (event_id, event_time, title, event_type, participant)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_time,
            title,
            event_type,
            participant
        )
    )

    connection.commit()
    connection.close()

    return redirect(url_for("event", event_id=event_id))


@app.route("/event/<int:event_id>/live")
def live_event(event_id):

    connection = get_connection()

    event_data = connection.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    schedule = connection.execute(
        """
        SELECT * FROM schedule
        WHERE event_id = ?
        ORDER BY event_time
        """,
        (event_id,)
    ).fetchall()

    connection.close()

    if not event_data:
        return "Event not found", 404

    if not schedule:
        return "No schedule available", 400

    # Start from first event
    if event_id not in live_positions:
        live_positions[event_id] = 0

    current_index = live_positions[event_id]

    manager = EventManager(schedule)

    # Move manager to stored position
    manager.current_index = current_index

    flow = manager.get_flow_context()

    return render_template(
        "live.html",
        event=event_data,
        previous=flow["previous"],
        current=flow["current"],
        next_event=flow["next"],
        script=None,
        event_id=event_id
    )


@app.route("/event/<int:event_id>/next", methods=["POST"])
def next_event(event_id):

    connection = get_connection()

    schedule = connection.execute(
        """
        SELECT * FROM schedule
        WHERE event_id = ?
        ORDER BY event_time
        """,
        (event_id,)
    ).fetchall()

    connection.close()

    if not schedule:
        return redirect(url_for("live_event", event_id=event_id))

    current_index = live_positions.get(event_id, 0)

    if current_index < len(schedule) - 1:
        live_positions[event_id] = current_index + 1

    return redirect(url_for("live_event", event_id=event_id))


@app.route("/event/<int:event_id>/generate-script", methods=["POST"])
def generate_event_script(event_id):

    connection = get_connection()

    event_data = connection.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    schedule = connection.execute(
        """
        SELECT * FROM schedule
        WHERE event_id = ?
        ORDER BY event_time
        """,
        (event_id,)
    ).fetchall()

    connection.close()

    if not event_data:
        return "Event not found", 404

    if not schedule:
        return "No schedule available", 400

    current_index = live_positions.get(event_id, 0)

    manager = EventManager(schedule)
    manager.current_index = current_index

    flow = manager.get_flow_context()

    script = generate_script(
        event_data["name"],
        flow["previous"],
        flow["current"],
        flow["next"]
    )

    return render_template(
        "live.html",
        event=event_data,
        previous=flow["previous"],
        current=flow["current"],
        next_event=flow["next"],
        script=script,
        event_id=event_id
    )


if __name__ == "__main__":
    app.run(debug=True)