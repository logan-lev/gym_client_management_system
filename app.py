import os
import sqlite3
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from flask import Flask, g, redirect, render_template, request, url_for, flash

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(APP_DIR, "trainer.db")

# If DATABASE_URL is set (Render), we'll use Postgres. Otherwise (local), SQLite.
DATABASE_URL = os.environ.get("DATABASE_URL")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-this")  # set SECRET_KEY on Render!


# -----------------------------
# SQL helpers (SQLite ? vs Postgres %s)
# -----------------------------
def sql(query: str) -> str:
    # Convert SQLite placeholders (?) to Postgres placeholders (%s) when needed
    return query.replace("?", "%s") if DATABASE_URL else query


# -----------------------------
# Database helpers
# -----------------------------
def get_db():
    # One connection per request
    if "db" not in g:
        if DATABASE_URL:
            # Postgres (Render)
            g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            # SQLite (local dev)
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
    if DATABASE_URL:
        # Postgres schema
        with psycopg.connect(DATABASE_URL) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                  id SERIAL PRIMARY KEY,
                  first_name TEXT NOT NULL,
                  last_name  TEXT NOT NULL,
                  date_of_birth DATE NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS health_profiles (
                  client_id INTEGER PRIMARY KEY,
                  medication TEXT,
                  health_problems TEXT,
                  FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
                );
                """
            )

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS measurements (
                  id SERIAL PRIMARY KEY,
                  client_id INTEGER NOT NULL,
                  date DATE NOT NULL,

                  weight DOUBLE PRECISION,
                  bmi DOUBLE PRECISION,
                  fat_percentage DOUBLE PRECISION,

                  grip_strength DOUBLE PRECISION,
                  plank_seconds INTEGER,
                  push_ups INTEGER,

                  notes TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                  FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
                );
                """
            )

            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_measurements_client_date
                ON measurements(client_id, date);
                """
            )
    else:
        # SQLite schema (local dev)
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


# Run init once on startup (safe due to IF NOT EXISTS)
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
# Routes
# -----------------------------
@app.route("/")
def home():
    return redirect(url_for("clients_list"))


@app.route("/clients")
def clients_list():
    db = get_db()
    q = request.args.get("q", "").strip()

    if q:
        like = f"%{q}%"
        clients = db.execute(
            sql(
                """
                SELECT * FROM clients
                WHERE first_name LIKE ? OR last_name LIKE ?
                ORDER BY last_name, first_name
                """
            ),
            (like, like),
        ).fetchall()
    else:
        clients = db.execute(
            sql("SELECT * FROM clients ORDER BY last_name, first_name")
        ).fetchall()

    return render_template("clients_list.html", clients=clients, q=q)


@app.route("/clients/new", methods=["GET", "POST"])
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
            sql(
                """
                INSERT INTO clients (first_name, last_name, date_of_birth)
                VALUES (?, ?, ?)
                """
            ),
            (first_name, last_name, dob),
        )

        # sqlite3 cursor has lastrowid; psycopg uses fetchone RETURNING
        if DATABASE_URL:
            # Re-run insert with RETURNING id for Postgres
            # (psycopg won't give lastrowid the same way)
            cur = db.execute(
                """
                INSERT INTO clients (first_name, last_name, date_of_birth)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (first_name, last_name, dob),
            )
            client_id = cur.fetchone()["id"]
        else:
            client_id = cur.lastrowid

        # Ensure a health profile row exists (optional fields)
        if DATABASE_URL:
            db.execute(
                """
                INSERT INTO health_profiles (client_id, medication, health_problems)
                VALUES (%s, %s, %s)
                ON CONFLICT (client_id) DO NOTHING
                """,
                (client_id, "", ""),
            )
        else:
            db.execute(
                "INSERT OR IGNORE INTO health_profiles (client_id, medication, health_problems) VALUES (?, '', '')",
                (client_id,),
            )

        db.commit()
        flash("Client created.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    return render_template("client_new.html")


@app.route("/clients/<int:client_id>")
def client_detail(client_id):
    db = get_db()

    client = db.execute(
        sql("SELECT * FROM clients WHERE id = ?"), (client_id,)
    ).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    health = db.execute(
        sql("SELECT * FROM health_profiles WHERE client_id = ?"), (client_id,)
    ).fetchone()

    measurements = db.execute(
        sql(
            """
            SELECT * FROM measurements
            WHERE client_id = ?
            ORDER BY date DESC, id DESC
            """
        ),
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


# -----------------------------
# Edit Client
# -----------------------------
@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_edit(client_id):
    db = get_db()
    client = db.execute(
        sql("SELECT * FROM clients WHERE id = ?"), (client_id,)
    ).fetchone()
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

        if DATABASE_URL:
            db.execute(
                """
                UPDATE clients
                SET first_name = %s, last_name = %s, date_of_birth = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (first_name, last_name, dob, client_id),
            )
        else:
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


# -----------------------------
# Delete Client
# -----------------------------
@app.route("/clients/<int:client_id>/delete", methods=["POST"])
def client_delete(client_id):
    db = get_db()
    client = db.execute(
        sql("SELECT * FROM clients WHERE id = ?"), (client_id,)
    ).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    # This will cascade delete health_profiles + measurements due to ON DELETE CASCADE
    db.execute(sql("DELETE FROM clients WHERE id = ?"), (client_id,))
    db.commit()

    flash("Client deleted.", "success")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>/health/edit", methods=["GET", "POST"])
def health_edit(client_id):
    db = get_db()
    client = db.execute(
        sql("SELECT * FROM clients WHERE id = ?"), (client_id,)
    ).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("clients_list"))

    health = db.execute(
        sql("SELECT * FROM health_profiles WHERE client_id = ?"), (client_id,)
    ).fetchone()

    if request.method == "POST":
        medication = request.form.get("medication", "").strip()
        health_problems = request.form.get("health_problems", "").strip()

        if DATABASE_URL:
            db.execute(
                """
                INSERT INTO health_profiles (client_id, medication, health_problems)
                VALUES (%s, %s, %s)
                ON CONFLICT (client_id) DO UPDATE SET
                  medication=excluded.medication,
                  health_problems=excluded.health_problems
                """,
                (client_id, medication, health_problems),
            )
        else:
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
def measurement_new(client_id):
    db = get_db()
    client = db.execute(
        sql("SELECT * FROM clients WHERE id = ?"), (client_id,)
    ).fetchone()
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
            sql(
                """
                INSERT INTO measurements
                (client_id, date, weight, bmi, fat_percentage, grip_strength, plank_seconds, push_ups, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
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