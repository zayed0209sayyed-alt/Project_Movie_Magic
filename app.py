"""Movie Magic - cloud-based movie ticket booking (Flask + DynamoDB + SNS on EC2)."""
import logging
import os
import re
import uuid
from datetime import date, datetime
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("moviemagic")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
store = db.get_store()
ADMINS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "admin@moviemagic.com").split(",")}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PALETTE = [("#1d3557", "#457b9d"), ("#6d597a", "#e56b6f"), ("#264653", "#e9c46a"), ("#3a0ca3", "#4cc9f0")]


# ---------------------------------------------------------------- helpers
def seat_price(show, seat):
    return show["price"] + (db.PREMIUM_EXTRA if seat[0] in db.PREMIUM_ROWS else 0)


def valid_seat(s):
    return len(s) >= 2 and s[0] in db.ROWS and s[1:].isdigit() and 1 <= int(s[1:]) <= db.COLS


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a, %d %b")


app.jinja_env.filters["fdate"] = fmt_date


@app.before_request
def load_user():
    g.user = store.get_user(session["email"]) if "email" in session else None
    if g.user:
        g.is_admin = g.user["email"].lower() in ADMINS


@app.context_processor
def inject():
    return {"cities": db.CITIES, "storage_mode": store.mode, "user": g.get("user"), "is_admin": g.get("is_admin", False)}


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not g.is_admin:
            abort(403)
        return f(*a, **k)
    return w


# ---------------------------------------------------------------- auth
@app.route("/")
def home():
    if g.user:
        return redirect(url_for("movies"))
    featured = [m for m in store.list_movies()][:4]
    return render_template("landing.html", featured=featured)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        phone, pw = f.get("phone", "").strip(), f.get("password", "")
        if not name or not EMAIL_RE.match(email) or len(pw) < 6:
            flash("Enter your name, a valid e-mail and a password of at least 6 characters.", "error")
        elif store.get_user(email):
            flash("That e-mail is already registered. Log in instead.", "error")
        else:
            store.put_user({"email": email, "name": name, "phone": phone,
                            "password_hash": generate_password_hash(pw),
                            "created_at": datetime.utcnow().isoformat()})
            res = notify.subscribe_user(email)
            session["email"] = email
            flash(f"Welcome, {name}! {res['note']}", "success" if res["ok"] else "info")
            return redirect(url_for("movies"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw = request.form.get("email", "").strip().lower(), request.form.get("password", "")
        u = store.get_user(email)
        if u and check_password_hash(u["password_hash"], pw):
            session["email"] = email
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("movies"))
        flash("E-mail or password is incorrect.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------------------- browsing
@app.route("/movies")
@login_required
def movies():
    q = request.args.get("q", "").strip().lower()
    city = request.args.get("city", session.get("city", ""))
    genre, kind = request.args.get("genre", ""), request.args.get("kind", "")
    if "city" in request.args:
        session["city"] = city
    shows = [s for s in store.list_shows(city=city or None) if db.is_upcoming(s)]
    by_movie = {}
    for s in shows:
        by_movie.setdefault(s["movie_id"], []).append(s)
    all_movies = store.list_movies()
    items = []
    for m in all_movies:
        ms = by_movie.get(m["movie_id"])
        if not ms:
            continue
        if q and q not in m["title"].lower() and q not in m["genre"].lower() and q not in m["language"].lower():
            continue
        if genre and m["genre"] != genre:
            continue
        if kind and m["kind"] != kind:
            continue
        items.append({**m, "show_count": len(ms),
                      "seats_left": sum(db.TOTAL_SEATS - len(s["booked_seats"]) for s in ms),
                      "from_price": min(s["price"] for s in ms)})
    items.sort(key=lambda m: -m["rating"])
    genres = sorted({m["genre"] for m in all_movies})
    return render_template("movies.html", items=items, genres=genres, q=request.args.get("q", ""),
                           city=city, genre=genre, kind=kind)


@app.route("/movie/<movie_id>")
@login_required
def movie(movie_id):
    m = store.get_movie(movie_id) or abort(404)
    all_shows = [s for s in store.list_shows(movie_id=movie_id) if db.is_upcoming(s)]
    avail_cities = sorted({s["city"] for s in all_shows})
    city = request.args.get("city") or session.get("city") or ""
    if city not in avail_cities:
        city = avail_cities[0] if avail_cities else ""
    days = {}
    for s in sorted([s for s in all_shows if s["city"] == city], key=lambda s: (s["date"], s["time"])):
        s["seats_left"] = db.TOTAL_SEATS - len(s["booked_seats"])
        days.setdefault(s["date"], []).append(s)
    return render_template("movie.html", m=m, days=days, city=city, avail_cities=avail_cities)


@app.route("/show/<show_id>")
@login_required
def show(show_id):
    s = store.get_show(show_id) or abort(404)
    m = store.get_movie(s["movie_id"])
    return render_template("seats.html", s=s, m=m, rows=db.ROWS, cols=db.COLS, premium_rows=db.PREMIUM_ROWS,
                           extra=db.PREMIUM_EXTRA, max_seats=db.MAX_SEATS, total=db.TOTAL_SEATS)


@app.route("/api/show/<show_id>/seats")
@login_required
def api_seats(show_id):
    s = store.get_show(show_id) or abort(404)
    return jsonify(booked=s["booked_seats"], left=db.TOTAL_SEATS - len(s["booked_seats"]))


# ---------------------------------------------------------------- booking
@app.post("/book/<show_id>")
@login_required
def book(show_id):
    s = store.get_show(show_id) or abort(404)
    m = store.get_movie(s["movie_id"])
    seats = sorted({x.strip().upper() for x in request.form.get("seats", "").split(",") if x.strip()},
                   key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0))
    if not seats or len(seats) > db.MAX_SEATS or not all(valid_seat(x) for x in seats):
        flash(f"Pick between 1 and {db.MAX_SEATS} seats.", "error")
        return redirect(url_for("show", show_id=show_id))
    try:
        store.book_seats(show_id, seats)
    except db.SeatTaken as e:
        flash(f"Sorry, {', '.join(e.seats)} just got booked by someone else. Pick other seats.", "error")
        return redirect(url_for("show", show_id=show_id))
    b = {"booking_id": "MM" + uuid.uuid4().hex[:8].upper(), "email": g.user["email"], "name": g.user["name"],
         "show_id": show_id, "movie_id": m["movie_id"], "title": m["title"], "emoji": m["emoji"],
         "c1": m["c1"], "c2": m["c2"], "city": s["city"], "theatre": s["theatre"],
         "date": s["date"], "time": s["time"], "seats": seats,
         "total": sum(seat_price(s, x) for x in seats), "created_at": datetime.utcnow().isoformat()}
    res = notify.send_booking_email(b, store)
    b["email_status"] = "sent" if res["ok"] else "failed"
    store.put_booking(b)
    log.info("Booked %s %s seats=%s", b["booking_id"], b["email"], seats)
    return redirect(url_for("booking", booking_id=b["booking_id"]))


@app.route("/booking/<booking_id>")
@login_required
def booking(booking_id):
    b = store.get_booking(booking_id) or abort(404)
    if b["email"] != g.user["email"] and not g.is_admin:
        abort(403)
    return render_template("booking.html", b=b, mail=store.get_notification(booking_id))


@app.route("/bookings")
@login_required
def my_bookings():
    return render_template("bookings.html", rows=store.list_bookings(g.user["email"]))


# ---------------------------------------------------------------- admin
@app.route("/admin")
@admin_required
def admin():
    bookings = store.list_bookings()
    shows = store.list_shows()
    return render_template("admin.html", movies=store.list_movies(), bookings=bookings[:50],
                           stats={"bookings": len(bookings), "revenue": sum(b["total"] for b in bookings),
                                  "seats": sum(len(b["seats"]) for b in bookings), "shows": len(shows)})


@app.post("/admin/movie")
@admin_required
def admin_add_movie():
    f = request.form
    title = f.get("title", "").strip()
    cities = [c for c in f.getlist("cities") if c in db.CITIES]
    if not title or not cities:
        flash("A title and at least one city are required.", "error")
        return redirect(url_for("admin"))
    c1, c2 = PALETTE[len(title) % len(PALETTE)]
    mid = "m" + uuid.uuid4().hex[:6]
    try:
        price, rating = int(f.get("price") or 200), float(f.get("rating") or 7.5)
    except ValueError:
        flash("Price and rating must be numbers.", "error")
        return redirect(url_for("admin"))
    kind = "event" if f.get("kind") == "event" else "movie"
    m = {"movie_id": mid, "title": title, "kind": kind, "genre": f.get("genre", "Drama").strip() or "Drama",
         "language": f.get("language", "English").strip() or "English", "duration": f.get("duration", "2h 00m"),
         "rating": rating, "emoji": f.get("emoji") or "🎬", "c1": c1, "c2": c2, "base_price": price,
         "cities": cities, "description": f.get("description", "").strip()}
    store.put_movie(m)
    store.put_shows(db.gen_shows([m]))
    flash(f"Added “{title}” with shows for the next 4 days.", "success")
    return redirect(url_for("admin"))


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, msg="You don't have access to this page."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, msg="We couldn't find that page."), 404


@app.route("/health")
def health():
    return jsonify(status="ok", storage=store.mode)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=os.getenv("FLASK_DEBUG") == "1")
