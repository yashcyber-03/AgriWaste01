import os, sqlite3
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature

BASE = os.path.dirname(os.path.abspath(__file__))
# Vercel's filesystem is read-only except /tmp (demo data resets there; use Postgres for production)
DB = "/tmp/agri.db" if os.environ.get("VERCEL") else os.path.join(BASE, "agri.db")
app = Flask(__name__, static_folder=None)
signer = URLSafeTimedSerializer(os.environ.get("SECRET_KEY", "change-me-in-production"))

CATEGORIES = [
    ("Crop residue", "Mulching", "Chop and spread over soil to retain moisture and suppress weeds."),
    ("Straw", "Biogas / Bedding", "Bale for animal bedding or feed it to a biogas digester."),
    ("Husk", "Biofuel briquettes", "Press into briquettes or use as clean-burning fuel."),
    ("Leaves", "Composting", "Layer with green waste and turn weekly; ready in 8-10 weeks."),
    ("Fruit & vegetable waste", "Biogas / Composting", "High moisture: ideal for biogas or vermicompost."),
    ("Animal-feed residue", "Recycling", "Dry and repurpose as feed supplement or pellets."),
]

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  phone TEXT,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('farmer','worker','admin')),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  recycling_method TEXT NOT NULL,   -- composting, mulching, biogas...
  recycling_tip TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farmer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  worker_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  quantity_kg REAL NOT NULL CHECK (quantity_kg > 0),
  address TEXT NOT NULL,
  latitude REAL, longitude REAL,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','accepted','scheduled','collected','recycled')),
  scheduled_date TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  is_read INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL UNIQUE REFERENCES requests(id) ON DELETE CASCADE,
  farmer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_req_farmer ON requests(farmer_id);
"""

def init_db():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM users").fetchone():
        con.executemany("INSERT INTO categories(name,recycling_method,recycling_tip) VALUES(?,?,?)", CATEGORIES)
        for n, e, r in [("Demo Farmer", "farmer@demo.com", "farmer"),
                        ("Demo Collector", "worker@demo.com", "worker"),
                        ("Demo Admin", "admin@demo.com", "admin")]:
            con.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                        (n, e, generate_password_hash("demo123"), r))
        seed = [(1, 1, 450, "Plot 12, Khadki Road, Daund", "collected"), (1, 2, 1200, "Plot 12, Khadki Road, Daund", "recycled"),
                (1, 4, 180, "Near Water Tank, Yavat", "scheduled"), (1, 5, 320, "Market Yard, Daund", "pending"),
                (1, 3, 600, "Village Patas", "accepted")]
        for f, c, q, a, s in seed:
            con.execute("INSERT INTO requests(farmer_id,category_id,quantity_kg,address,status,worker_id) VALUES(?,?,?,?,?,2)", (f, c, q, a, s))
        con.execute("UPDATE users SET phone='+91 98765 43210' WHERE email='farmer@demo.com'")
        con.commit()
    con.close()
init_db()

def auth(*roles):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **k):
            tok = request.headers.get("Authorization", "").replace("Bearer ", "")
            try:
                uid = signer.loads(tok, max_age=60 * 60 * 24 * 7)
            except BadSignature:
                return jsonify(error="Please sign in again."), 401
            u = db().execute("SELECT id,name,email,role FROM users WHERE id=?", (uid,)).fetchone()
            if not u: return jsonify(error="Account not found."), 401
            if roles and u["role"] not in roles: return jsonify(error="You don't have access to this."), 403
            g.user = dict(u)
            return fn(*a, **k)
        return wrap
    return deco

def notify(uid, msg):
    db().execute("INSERT INTO notifications(user_id,message) VALUES(?,?)", (uid, msg))

@app.post("/api/register")
def register():
    d = request.get_json(force=True)
    if not all(d.get(k) for k in ("name", "email", "password")) or len(d["password"]) < 6:
        return jsonify(error="Name, email and a password of 6+ characters are required."), 400
    role = d.get("role") if d.get("role") in ("farmer", "worker") else "farmer"
    try:
        db().execute("INSERT INTO users(name,email,phone,password_hash,role) VALUES(?,?,?,?,?)",
                     (d["name"].strip(), d["email"].lower().strip(), d.get("phone"), generate_password_hash(d["password"]), role))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That email is already registered."), 409
    return jsonify(ok=True), 201

@app.post("/api/login")
def login():
    d = request.get_json(force=True)
    u = db().execute("SELECT * FROM users WHERE email=?", ((d.get("email") or "").lower().strip(),)).fetchone()
    if not u or not check_password_hash(u["password_hash"], d.get("password", "")):
        return jsonify(error="Incorrect email or password."), 401
    return jsonify(token=signer.dumps(u["id"]), user={"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]})

@app.get("/api/categories")
@auth()
def categories():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM categories")])

@app.post("/api/requests")
@auth("farmer")
def add_request():
    d = request.get_json(force=True)
    try:
        qty = float(d["quantity_kg"]); cid = int(d["category_id"])
        assert qty > 0 and d["address"].strip()
    except Exception:
        return jsonify(error="Choose a waste type, enter a quantity and an address."), 400
    cur = db().execute("INSERT INTO requests(farmer_id,category_id,quantity_kg,address,latitude,longitude,notes) VALUES(?,?,?,?,?,?,?)",
                       (g.user["id"], cid, qty, d["address"].strip(), d.get("latitude"), d.get("longitude"), d.get("notes")))
    for w in db().execute("SELECT id FROM users WHERE role IN ('worker','admin')"):
        notify(w["id"], f"New collection request #{cur.lastrowid} ({qty:g} kg).")
    notify(g.user["id"], f"Request #{cur.lastrowid} submitted. We'll notify you when it's accepted.")
    db().commit()
    return jsonify(id=cur.lastrowid), 201

@app.get("/api/requests")
@auth()
def list_requests():
    q = """SELECT r.*, c.name AS category, u.name AS farmer, u.email AS farmer_email, u.phone AS farmer_phone, f.rating FROM requests r
           JOIN categories c ON c.id=r.category_id JOIN users u ON u.id=r.farmer_id
           LEFT JOIN feedback f ON f.request_id=r.id"""
    args = ()
    if g.user["role"] == "farmer": q += " WHERE r.farmer_id=?"; args = (g.user["id"],)
    return jsonify([dict(r) for r in db().execute(q + " ORDER BY r.id DESC", args)])

FLOW = {"accepted": "accepted", "scheduled": "scheduled", "collected": "collected", "recycled": "recycled"}

@app.put("/api/requests/<int:rid>/status")
@auth("worker", "admin")
def set_status(rid):
    d = request.get_json(force=True); st = d.get("status")
    r = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not r or st not in FLOW: return jsonify(error="Invalid request or status."), 400
    db().execute("UPDATE requests SET status=?, worker_id=?, scheduled_date=COALESCE(?,scheduled_date), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (st, g.user["id"], d.get("scheduled_date"), rid))
    msg = {"accepted": "was accepted", "scheduled": f"is scheduled for {d.get('scheduled_date') or 'soon'}",
           "collected": "has been collected", "recycled": "has been recycled"}[st]
    notify(r["farmer_id"], f"Your collection request #{rid} {msg}.")
    db().commit()
    return jsonify(ok=True)

@app.get("/api/dashboard")
@auth()
def dashboard():
    w, a = ("WHERE farmer_id=?", (g.user["id"],)) if g.user["role"] == "farmer" else ("", ())
    row = db().execute(f"""SELECT COALESCE(SUM(quantity_kg),0) reported,
        COALESCE(SUM(CASE WHEN status IN ('collected','recycled') THEN quantity_kg END),0) collected,
        COALESCE(SUM(CASE WHEN status='recycled' THEN quantity_kg END),0) recycled,
        COUNT(CASE WHEN status='pending' THEN 1 END) pending, COUNT(*) total FROM requests {w}""", a).fetchone()
    return jsonify(dict(row))

@app.get("/api/reports")
@auth("worker", "admin")
def reports():
    by_cat = db().execute("SELECT c.name, COALESCE(SUM(r.quantity_kg),0) kg FROM categories c LEFT JOIN requests r ON r.category_id=c.id GROUP BY c.id").fetchall()
    by_status = db().execute("SELECT status, COUNT(*) n FROM requests GROUP BY status").fetchall()
    rating = db().execute("SELECT ROUND(AVG(rating),1) avg, COUNT(*) n FROM feedback").fetchone()
    return jsonify(by_category=[dict(r) for r in by_cat], by_status=[dict(r) for r in by_status], rating=dict(rating))

@app.get("/api/notifications")
@auth()
def notifications():
    rows = db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20", (g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/notifications/read")
@auth()
def read_notes():
    db().execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],)); db().commit()
    return jsonify(ok=True)

@app.post("/api/feedback")
@auth("farmer")
def feedback():
    d = request.get_json(force=True)
    r = db().execute("SELECT id FROM requests WHERE id=? AND farmer_id=? AND status IN ('collected','recycled')", (d.get("request_id"), g.user["id"])).fetchone()
    if not r or not 1 <= int(d.get("rating", 0)) <= 5: return jsonify(error="You can rate completed collections only."), 400
    try:
        db().execute("INSERT INTO feedback(request_id,farmer_id,rating,comment) VALUES(?,?,?,?)", (r["id"], g.user["id"], int(d["rating"]), d.get("comment")))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="You already rated this collection."), 409
    return jsonify(ok=True), 201

@app.get("/api/admin/users")
@auth("admin")
def users():
    return jsonify([dict(r) for r in db().execute("SELECT id,name,email,role,created_at FROM users ORDER BY id")])

@app.delete("/api/admin/users/<int:uid>")
@auth("admin")
def del_user(uid):
    if uid == g.user["id"]: return jsonify(error="You can't delete your own account."), 400
    db().execute("DELETE FROM users WHERE id=?", (uid,)); db().commit()
    return jsonify(ok=True)

import agripoints  # AgriPoints add-on (separate module, own tables and pages)
agripoints.register(app, DB, auth, db)

@app.get("/")
def home():
    return send_from_directory(os.path.join(BASE, "public"), "index.html")

if __name__ == "__main__":
    app.run(debug=True)
