import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    url_for,
    flash,
    session,
)

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(APP_DIR, "trainer.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-this")  # Set SECRET_KEY on PythonAnywhere!


# -----------------------------
# Auth helpers (single trainer password)
# -----------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# -----------------------------
# Database helpers
# -----------------------------
def get_db():
    # One connection per request
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON;")

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS clients (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          first_name TEXT NOT NULL,
          last_name  TEXT NOT NULL,
          date_of_birth TEXT NOT NULL, -- ISO 'YYYY-MM-DD'
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS health_profiles (
          client_id INTEGER PRIMARY KEY,
          medication TEXT,
          health_problems TEXT,
          FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS measurements (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          client_id INTEGER NOT NULL,
          date TEXT NOT NULL, -- ISO 'YYYY-MM-DD'

          weight REAL,
          bmi REAL,
          fat_percentage REAL,

          grip_strength REAL,
          plank_seconds INTEGER,
          push_ups INTEGER,

          notes TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),

          FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_measurements_client_date
        ON measurements(client_id, date);
        """
    )

    db.commit()
    db.close()


# Run init once on startup
with app.app_context():
    init_db()


def parse_float(value):
    # Accept empty -> None
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return "INVALID"


def parse_int(value):
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return "INVALID"


def parse_date_iso(value):
    # Expect YYYY-MM-DD
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return "INVALID"


# -----------------------------
# Auth routes
# -----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")

        trainer_password = os.environ.get("TRAINER_PASSWORD", "")
        if trainer_password and password == trainer_password:
            session["logged_in"] = True
            return redirect(url_for("clients_list"))

        flash("Invalid password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
@login_required
def home():
    return redirect(url_for("clients_list"))


@app.route("/clients")
@login_required
def clients_list():
    db = get_db()
    q = request.args.get("q", "").strip()

    if q:
        like = f"%{q}%"
        clients = db.execute(
            """
            SELECT * FROM clients
            WHERE first_name LIKE ? OR last_name LIKE ?
            ORDER BY last_name, first_name
            """,
            (like, like),
        ).fetchall()
    else:
        clients = db.execute("SELECT * FROM clients ORDER BY last_name, first_name").fetchall()

    return render_template("clients_list.html", clients=clients, q=q)


@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def client_new():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        dob = parse_date_iso(request.form.get("date_of_birth", ""))

        errors = []
        if not first_name:
            errors.append("First name is required.")
        if not last_name:
            errors.append("Last name is required.")
        if dob in (None, "INVALID"):
            errors.append("Date of birth must be in YYYY-MM-DD format.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("client_new.html")

        db = get_db()
        cur = db.execute(
            """
            INSERT INTO clients (first_name, last_name, date_of_birth)
            VALUES (?, ?, ?)
            """,
            (first_name, last_name, dob),
        )
        client_id = cur.lastrowid

        # Ensure a health profile row exists (optional fields)
        db.execute(
            "INSERT OR IGNORE INTO health_profiles (client_id, medication, health_problems) VALUES (?, '', '')",
            (client_id,),
        )

        db.commit()
        flash("Client created.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    return render_template("client_new.html")


@app.route("/clients/<int:client_id>")
@login_required
def client_detail(client_id):
    db = get_db()

    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    health = db.execute(
        "SELECT * FROM health_profiles WHERE client_id = ?", (client_id,)
    ).fetchone()

    measurements = db.execute(
        """
        SELECT * FROM measurements
        WHERE client_id = ?
        ORDER BY date DESC, id DESC
        """,
        (client_id,),
    ).fetchall()

    latest = measurements[0] if measurements else None

    return render_template(
        "client_detail.html",
        client=client,
        health=health,
        measurements=measurements,
        latest=latest,
    )


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def client_edit(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        dob = parse_date_iso(request.form.get("date_of_birth", ""))

        errors = []
        if not first_name:
            errors.append("First name is required.")
        if not last_name:
            errors.append("Last name is required.")
        if dob in (None, "INVALID"):
            errors.append("Date of birth must be in YYYY-MM-DD format.")

        if errors:
            for e in errors:
                flash(e, "error")
            # Re-render with what they typed
            return render_template(
                "client_edit.html",
                client={
                    "id": client_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "date_of_birth": request.form.get("date_of_birth", ""),
                },
            )

        db.execute(
            """
            UPDATE clients
            SET first_name = ?, last_name = ?, date_of_birth = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (first_name, last_name, dob, client_id),
        )
        db.commit()

        flash("Client updated.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    return render_template("client_edit.html", client=client)


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def client_delete(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    # This will cascade delete health_profiles + measurements due to ON DELETE CASCADE
    db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    db.commit()

    flash("Client deleted.", "success")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>/health/edit", methods=["GET", "POST"])
@login_required
def health_edit(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    health = db.execute(
        "SELECT * FROM health_profiles WHERE client_id = ?", (client_id,)
    ).fetchone()

    if request.method == "POST":
        medication = request.form.get("medication", "").strip()
        health_problems = request.form.get("health_problems", "").strip()

        db.execute(
            """
            INSERT INTO health_profiles (client_id, medication, health_problems)
            VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
              medication=excluded.medication,
              health_problems=excluded.health_problems
            """,
            (client_id, medication, health_problems),
        )
        db.commit()
        flash("Health profile updated.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    return render_template("health_edit.html", client=client, health=health)


@app.route("/clients/<int:client_id>/measurements/new", methods=["GET", "POST"])
@login_required
def measurement_new(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    if request.method == "POST":
        date = parse_date_iso(request.form.get("date", ""))

        weight = parse_float(request.form.get("weight", ""))
        bmi = parse_float(request.form.get("bmi", ""))
        fat_percentage = parse_float(request.form.get("fat_percentage", ""))

        grip_strength = parse_float(request.form.get("grip_strength", ""))
        plank_seconds = parse_int(request.form.get("plank_seconds", ""))
        push_ups = parse_int(request.form.get("push_ups", ""))

        notes = request.form.get("notes", "").strip()

        errors = []
        if date in (None, "INVALID"):
            errors.append("Measurement date must be in YYYY-MM-DD format.")

        # Validate numeric fields
        for label, val in [
            ("Weight", weight),
            ("BMI", bmi),
            ("Fat %", fat_percentage),
            ("Grip strength", grip_strength),
            ("Plank seconds", plank_seconds),
            ("Push ups", push_ups),
        ]:
            if val == "INVALID":
                errors.append(f"{label} must be a number (or left blank).")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("measurement_new.html", client=client)

        db.execute(
            """
            INSERT INTO measurements
            (client_id, date, weight, bmi, fat_percentage, grip_strength, plank_seconds, push_ups, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                date,
                weight,
                bmi,
                fat_percentage,
                grip_strength,
                plank_seconds,
                push_ups,
                notes,
            ),
        )
        db.commit()
        flash("Measurement added.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    # Default date to today for convenience
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("measurement_new.html", client=client, today=today)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)