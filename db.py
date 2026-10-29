"""Storage layer: one interface, two backends.

LocalStore    - JSON file, used when USE_AWS=false (development / demo)
DynamoStore   - Amazon DynamoDB, used when USE_AWS=true (production on EC2)
"""
import json
import os
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from werkzeug.security import generate_password_hash

USE_AWS = os.getenv("USE_AWS", "false").lower() == "true"
REGION = os.getenv("AWS_REGION", "ap-south-1")

ROWS = "ABCDEFGH"
COLS = 12
TOTAL_SEATS = len(ROWS) * COLS
PREMIUM_ROWS = "GH"
PREMIUM_EXTRA = 80
MAX_SEATS = 8

CITIES = ["Pune", "Mumbai", "Bengaluru", "Delhi"]
THEATRES = {
    "Pune": ["PVR Phoenix Marketcity", "INOX Bund Garden"],
    "Mumbai": ["PVR Juhu", "Cinepolis Andheri"],
    "Bengaluru": ["PVR Orion Mall", "INOX Garuda"],
    "Delhi": ["PVR Select Citywalk", "INOX Nehru Place"],
}
TIMES = {"movie": ["11:00", "15:30", "20:15"], "event": ["19:30"]}

TABLES = {
    "MovieMagic_Users": "email",
    "MovieMagic_Movies": "movie_id",
    "MovieMagic_Shows": "show_id",
    "MovieMagic_Bookings": "booking_id",
}


class SeatTaken(Exception):
    def __init__(self, seats):
        super().__init__("Seats already taken: " + ", ".join(seats))
        self.seats = seats


def seat_sort_key(seat):
    return (seat[0], int(seat[1:]))


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------
def _m(i, title, kind, genre, lang, dur, rating, emoji, c1, c2, price, cities, desc):
    return dict(movie_id=f"m{i}", title=title, kind=kind, genre=genre, language=lang,
                duration=dur, rating=rating, emoji=emoji, c1=c1, c2=c2,
                base_price=price, cities=cities, description=desc)


SEED_MOVIES = [
    _m(1, "Midnight Monsoon", "movie", "Thriller", "Hindi", "2h 08m", 8.1, "🌧️", "#1d3557", "#457b9d", 220,
       ["Pune", "Mumbai", "Delhi"], "A journalist races a flooding city to expose a dam cover-up before dawn."),
    _m(2, "The Last Lighthouse", "movie", "Drama", "English", "1h 54m", 7.8, "🗼", "#264653", "#2a9d8f", 200,
       ["Pune", "Bengaluru"], "A retiring keeper and a stranded sailor share one final storm."),
    _m(3, "Quantum Heist", "movie", "Sci-Fi", "English", "2h 21m", 8.4, "🛰️", "#3a0ca3", "#4cc9f0", 280,
       ["Mumbai", "Bengaluru", "Delhi"], "A crew steals a single minute from the future, and the future wants it back."),
    _m(4, "Dhol & Dreams", "movie", "Comedy", "Hindi", "2h 02m", 7.2, "🥁", "#e76f51", "#f4a261", 180,
       ["Pune", "Mumbai"], "A wedding band with no bookings crashes the biggest wedding of the year."),
    _m(5, "Skyline Racers", "movie", "Action", "Hindi", "2h 17m", 7.6, "🏎️", "#9d0208", "#f48c06", 250,
       ["Pune", "Mumbai", "Bengaluru"], "Rooftop street races turn deadly when a rival crew plays for keeps."),
    _m(6, "Paper Moons", "movie", "Romance", "English", "1h 49m", 7.5, "🌙", "#6d597a", "#e56b6f", 190,
       ["Bengaluru", "Delhi"], "Two letter-writers fall for each other without ever meeting."),
    _m(7, "Hollow Forest", "movie", "Horror", "Hindi", "1h 47m", 6.9, "🌲", "#1b4332", "#52796f", 210,
       ["Mumbai", "Delhi"], "A trekking group learns why the villagers never enter after sunset."),
    _m(8, "Gully Kings", "movie", "Sports", "Hindi", "2h 11m", 8.0, "🏏", "#005f73", "#ee9b00", 200,
       ["Pune", "Delhi", "Bengaluru"], "A lane-cricket team takes on the city league with one real bat."),
    _m(9, "Indie Nights Live", "event", "Music", "Multi", "3h 00m", 8.6, "🎸", "#7209b7", "#f72585", 799,
       ["Pune", "Bengaluru"], "Five independent bands, one open-air stage."),
    _m(10, "Laugh Riot Stand-Up", "event", "Comedy", "English", "1h 45m", 8.2, "🎤", "#bb3e03", "#ee9b00", 499,
       ["Mumbai", "Delhi", "Pune"], "Four comics, one mic, zero filters."),
]


def gen_shows(movies, days=4):
    out, today = [], date.today()
    for m in movies:
        for city in m["cities"]:
            for d in range(days):
                dt = (today + timedelta(days=d)).isoformat()
                for i, t in enumerate(TIMES[m["kind"]]):
                    out.append({
                        "show_id": f"{m['movie_id']}-{city[:3].lower()}-{dt}-{t.replace(':', '')}",
                        "movie_id": m["movie_id"], "city": city,
                        "theatre": THEATRES[city][i % 2], "date": dt, "time": t,
                        "price": m["base_price"] + (30 if t == "20:15" else 0),
                        "booked_seats": [],
                    })
    return out


def is_upcoming(show):
    now = datetime.now()
    return (show["date"], show["time"]) >= (now.date().isoformat(), now.strftime("%H:%M"))


def ensure_seed(store):
    if not store.list_movies():
        for m in SEED_MOVIES:
            store.put_movie(m)
        store.put_user({
            "email": "admin@moviemagic.com", "name": "Admin", "phone": "",
            "password_hash": generate_password_hash("admin123"),
            "created_at": datetime.utcnow().isoformat(),
        })
    movies = store.list_movies()
    if not any(s["date"] >= date.today().isoformat() for s in store.list_shows()):
        store.put_shows(gen_shows(movies))


# --------------------------------------------------------------------------
# Local JSON backend
# --------------------------------------------------------------------------
class LocalStore:
    mode = "local"

    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(__file__), "data", "local_db.json")
        self.lock = threading.RLock()
        self.d = {"users": {}, "movies": {}, "shows": {}, "bookings": {}, "notifications": []}

    def init(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.d.update(json.load(f))

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.d, f, indent=1)

    # users
    def get_user(self, email):
        return self.d["users"].get(email)

    def put_user(self, u):
        with self.lock:
            self.d["users"][u["email"]] = u
            self._save()

    # movies
    def list_movies(self):
        return list(self.d["movies"].values())

    def get_movie(self, mid):
        return self.d["movies"].get(mid)

    def put_movie(self, m):
        with self.lock:
            self.d["movies"][m["movie_id"]] = m
            self._save()

    # shows
    def list_shows(self, movie_id=None, city=None):
        return [s for s in self.d["shows"].values()
                if (not movie_id or s["movie_id"] == movie_id) and (not city or s["city"] == city)]

    def get_show(self, sid):
        return self.d["shows"].get(sid)

    def put_shows(self, shows):
        with self.lock:
            for s in shows:
                self.d["shows"][s["show_id"]] = s
            self._save()

    def book_seats(self, sid, seats):
        with self.lock:
            show = self.d["shows"][sid]
            clash = sorted(set(seats) & set(show["booked_seats"]), key=seat_sort_key)
            if clash:
                raise SeatTaken(clash)
            show["booked_seats"] = sorted(set(show["booked_seats"]) | set(seats), key=seat_sort_key)
            self._save()

    # bookings
    def put_booking(self, b):
        with self.lock:
            self.d["bookings"][b["booking_id"]] = b
            self._save()

    def get_booking(self, bid):
        return self.d["bookings"].get(bid)

    def list_bookings(self, email=None):
        rows = [b for b in self.d["bookings"].values() if not email or b["email"] == email]
        return sorted(rows, key=lambda b: b["created_at"], reverse=True)

    # local "inbox" so e-mails can be previewed without SNS
    def add_notification(self, n):
        with self.lock:
            self.d["notifications"].append(n)
            self._save()

    def get_notification(self, booking_id):
        for n in reversed(self.d["notifications"]):
            if n.get("booking_id") == booking_id:
                return n


# --------------------------------------------------------------------------
# DynamoDB backend
# --------------------------------------------------------------------------
def _from_ddb(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=lambda x: seat_sort_key(x) if isinstance(x, str) and x[1:].isdigit() else x)
    if isinstance(o, list):
        return [_from_ddb(i) for i in o]
    if isinstance(o, dict):
        return {k: _from_ddb(v) for k, v in o.items()}
    return o


def _to_ddb(o):
    if isinstance(o, float):
        return Decimal(str(o))
    if isinstance(o, list):
        return [_to_ddb(i) for i in o]
    if isinstance(o, dict):
        return {k: _to_ddb(v) for k, v in o.items() if v not in (None, "")}
    return o


class DynamoStore:
    mode = "aws"

    def __init__(self):
        import boto3
        self.ddb = boto3.resource("dynamodb", region_name=REGION)
        self.t = {}

    def init(self):
        existing = {t.name for t in self.ddb.tables.all()}
        for name, key in TABLES.items():
            if name not in existing:
                self.ddb.create_table(
                    TableName=name,
                    KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                ).wait_until_exists()
            self.t[name] = self.ddb.Table(name)

    def _scan(self, name, **kw):
        items, resp = [], self.t[name].scan(**kw)
        items += resp["Items"]
        while "LastEvaluatedKey" in resp:
            resp = self.t[name].scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kw)
            items += resp["Items"]
        return [_from_ddb(i) for i in items]

    def _get(self, name, key, val):
        item = self.t[name].get_item(Key={key: val}).get("Item")
        return _from_ddb(item) if item else None

    # users
    def get_user(self, email):
        return self._get("MovieMagic_Users", "email", email)

    def put_user(self, u):
        self.t["MovieMagic_Users"].put_item(Item=_to_ddb(u))

    # movies
    def list_movies(self):
        return self._scan("MovieMagic_Movies")

    def get_movie(self, mid):
        return self._get("MovieMagic_Movies", "movie_id", mid)

    def put_movie(self, m):
        self.t["MovieMagic_Movies"].put_item(Item=_to_ddb(m))

    # shows
    def list_shows(self, movie_id=None, city=None):
        from boto3.dynamodb.conditions import Attr
        flt = None
        if movie_id:
            flt = Attr("movie_id").eq(movie_id)
        if city:
            flt = Attr("city").eq(city) if flt is None else flt & Attr("city").eq(city)
        shows = self._scan("MovieMagic_Shows", **({"FilterExpression": flt} if flt is not None else {}))
        for s in shows:
            s["booked_seats"] = s.get("booked_seats", [])
        return shows

    def get_show(self, sid):
        s = self._get("MovieMagic_Shows", "show_id", sid)
        if s:
            s["booked_seats"] = s.get("booked_seats", [])
        return s

    def put_shows(self, shows):
        with self.t["MovieMagic_Shows"].batch_writer() as bw:
            for s in shows:
                item = _to_ddb({k: v for k, v in s.items() if k != "booked_seats"})
                if s.get("booked_seats"):
                    item["booked_seats"] = set(s["booked_seats"])  # DynamoDB String Set
                bw.put_item(Item=item)

    def book_seats(self, sid, seats):
        """Atomic: succeeds only if NONE of the seats is already in the string set."""
        from botocore.exceptions import ClientError
        cond = " AND ".join(f"NOT contains(booked_seats, :s{i})" for i in range(len(seats)))
        vals = {f":s{i}": s for i, s in enumerate(seats)}
        vals[":add"] = set(seats)
        try:
            self.t["MovieMagic_Shows"].update_item(
                Key={"show_id": sid}, UpdateExpression="ADD booked_seats :add",
                ConditionExpression=cond, ExpressionAttributeValues=vals)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                taken = set(self.get_show(sid)["booked_seats"]) & set(seats)
                raise SeatTaken(sorted(taken, key=seat_sort_key) or seats)
            raise

    # bookings
    def put_booking(self, b):
        self.t["MovieMagic_Bookings"].put_item(Item=_to_ddb(b))

    def get_booking(self, bid):
        return self._get("MovieMagic_Bookings", "booking_id", bid)

    def list_bookings(self, email=None):
        from boto3.dynamodb.conditions import Attr
        kw = {"FilterExpression": Attr("email").eq(email)} if email else {}
        return sorted(self._scan("MovieMagic_Bookings", **kw), key=lambda b: b["created_at"], reverse=True)

    def add_notification(self, n):  # SNS is the real channel in AWS mode
        pass

    def get_notification(self, booking_id):
        return None


def get_store():
    store = DynamoStore() if USE_AWS else LocalStore()
    store.init()
    ensure_seed(store)
    return store
