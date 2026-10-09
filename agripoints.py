"""AgriPoints rewards: self-contained add-on. Registered from app.py via agripoints.register(...).
Balance = SUM of the ledger (credits minus debits); there is no separate stored balance to drift."""
import os, re, sqlite3
from flask import jsonify, request, g, send_from_directory

FRONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agripoints_frontend")
DBPATH = None

# Additive and idempotent: safe to run on every start, never touches existing tables.
MIGRATION = """
CREATE TABLE IF NOT EXISTS points_rules (
  activity_type TEXT PRIMARY KEY,
  points INTEGER NOT NULL CHECK (points >= 0),
  description TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS points_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  entry_type TEXT NOT NULL CHECK (entry_type IN ('credit','debit')),
  activity_type TEXT NOT NULL,
  request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
  points INTEGER NOT NULL CHECK (points > 0),
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
  reference TEXT NOT NULL UNIQUE,   -- e.g. 'request_completed:12' -> one reward per activity, enforced by the DB
  description TEXT NOT NULL,
  balance_after INTEGER NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON points_ledger(user_id, id);
INSERT OR IGNORE INTO points_rules(activity_type,points,description) VALUES
 ('request_verified', 10, 'Waste request reviewed and accepted by the collection team'),
 ('request_completed', 50, 'Waste collection completed'),
 ('recycling_verified', 25, 'Recycling activity verified by an administrator');
"""
LABEL = {"request_verified": "Request verified", "request_completed": "Collection completed",
         "recycling_verified": "Recycling verified"}

def _con():
    c = sqlite3.connect(DBPATH, timeout=15, isolation_level=None)  # manual transactions
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def _balance(c, uid):
    return c.execute("SELECT COALESCE(SUM(CASE entry_type WHEN 'credit' THEN points ELSE -points END),0) FROM points_ledger "
                     "WHERE user_id=? AND status='posted'", (uid,)).fetchone()[0]

def award(request_id, activity):
    """Credit a rule's points for one request, at most once. Returns points awarded or 0."""
    c = _con()
    try:
        c.execute("BEGIN IMMEDIATE")  # serialises concurrent writers
        r = c.execute("SELECT r.farmer_id FROM requests r JOIN users u ON u.id=r.farmer_id AND u.role='farmer' WHERE r.id=?", (request_id,)).fetchone()
        rule = c.execute("SELECT points FROM points_rules WHERE activity_type=? AND active=1 AND points>0", (activity,)).fetchone()
        if not r or not rule:
            c.execute("ROLLBACK"); return 0
        ref = f"{activity}:{request_id}"
        if c.execute("SELECT 1 FROM points_ledger WHERE reference=?", (ref,)).fetchone():
            c.execute("ROLLBACK"); return 0
        pts, uid = rule["points"], r["farmer_id"]
        c.execute("INSERT INTO points_ledger(user_id,entry_type,activity_type,request_id,points,reference,description,balance_after) VALUES(?,?,?,?,?,?,?,?)",
                  (uid, "credit", activity, request_id, pts, ref, f"{LABEL[activity]} (request #{request_id})", _balance(c, uid) + pts))
        c.execute("INSERT INTO notifications(user_id,message) VALUES(?,?)", (uid, f"You earned {pts} AgriPoints: {LABEL[activity].lower()} for request #{request_id}."))
        c.execute("COMMIT"); return pts
    except sqlite3.IntegrityError:  # unique reference hit by a concurrent writer
        c.execute("ROLLBACK"); return 0
    except Exception:
        try: c.execute("ROLLBACK")
        except Exception: pass
        raise
    finally:
        c.close()

def debit(user_id, points, reference, description, activity="redemption"):
    """For the future marketplace (no route uses it yet). Atomic: cannot overspend or double-spend a reference."""
    c = _con()
    try:
        c.execute("BEGIN IMMEDIATE")
        bal = _balance(c, user_id)
        if points <= 0 or bal < points or c.execute("SELECT 1 FROM points_ledger WHERE reference=?", (reference,)).fetchone():
            c.execute("ROLLBACK"); return False
        c.execute("INSERT INTO points_ledger(user_id,entry_type,activity_type,points,reference,description,balance_after) VALUES(?,?,?,?,?,?,?)",
                  (user_id, "debit", activity, points, reference, description, bal - points))
        c.execute("COMMIT"); return True
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK"); return False
    finally:
        c.close()

def register(app, db_path, auth, db):
    global DBPATH
    DBPATH = db_path
    c = sqlite3.connect(db_path); c.executescript(MIGRATION); c.commit(); c.close()

    @app.after_request  # watches the EXISTING status route; its code is unchanged
    def _reward_on_status_change(resp):
        m = re.fullmatch(r"/api/requests/(\d+)/status", request.path)
        if m and request.method == "PUT" and resp.status_code == 200:
            try:
                rid = int(m.group(1)); k = _con()
                row = k.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone(); k.close()
                if row and row["status"] in ("accepted", "scheduled", "collected", "recycled"):
                    award(rid, "request_verified")
                if row and row["status"] in ("collected", "recycled"):
                    award(rid, "request_completed")
            except Exception:
                app.logger.exception("AgriPoints reward failed")  # never break the existing response
        return resp

    @app.get("/api/agripoints/summary")
    @auth("farmer")
    def ap_summary():
        k = db(); uid = g.user["id"]  # identity comes only from the signed token, never from the client
        s = k.execute("""SELECT COALESCE(SUM(CASE WHEN entry_type='credit' THEN points END),0) earned,
            COALESCE(SUM(CASE WHEN entry_type='credit' AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now') THEN points END),0) month,
            COALESCE(SUM(CASE entry_type WHEN 'credit' THEN points ELSE -points END),0) balance
            FROM points_ledger WHERE user_id=? AND status='posted'""", (uid,)).fetchone()
        rules = k.execute("SELECT activity_type,points,description FROM points_rules WHERE active=1 AND points>0").fetchall()
        return jsonify(balance=s["balance"], total_earned=s["earned"], month_earned=s["month"], rules=[dict(r) for r in rules])

    @app.get("/api/agripoints/transactions")
    @auth("farmer")
    def ap_tx():
        rows = db().execute("""SELECT id,created_at,activity_type,request_id,reference,description,entry_type,points,status,balance_after
            FROM points_ledger WHERE user_id=? ORDER BY id DESC LIMIT 200""", (g.user["id"],)).fetchall()
        return jsonify([dict(r, points=r["points"] if r["entry_type"] == "credit" else -r["points"]) for r in rows])

    @app.post("/api/agripoints/admin/verify-recycling")
    @auth("admin")
    def ap_verify_recycling():
        try: rid = int((request.get_json(force=True) or {}).get("request_id"))
        except (TypeError, ValueError): return jsonify(error="A valid request_id is required."), 400
        r = db().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        if not r: return jsonify(error="Request not found."), 404
        if r["status"] != "recycled": return jsonify(error="Only requests already marked Recycled can be verified."), 400
        pts = award(rid, "recycling_verified")
        return (jsonify(awarded=pts), 201) if pts else (jsonify(error="Already rewarded or rule inactive."), 409)

    @app.put("/api/agripoints/admin/rules/<activity>")
    @auth("admin")
    def ap_rule(activity):
        d = request.get_json(force=True) or {}
        try: pts = int(d.get("points")); assert 0 <= pts <= 10000
        except Exception: return jsonify(error="points must be a whole number from 0 to 10000."), 400
        cur = db().execute("UPDATE points_rules SET points=?, active=? WHERE activity_type=?", (pts, 0 if d.get("active") == 0 else 1, activity))
        db().commit()
        return (jsonify(ok=True) if cur.rowcount else (jsonify(error="Unknown rule."), 404))

    # pages + static files (absolute /agripoints_frontend/... URLs work locally and on Vercel)
    @app.get("/agripoints")
    def ap_page(): return send_from_directory(FRONT, "agripoints.html")
    @app.get("/agripoints/transactions")
    def ap_tx_page(): return send_from_directory(FRONT, "transactions.html")
    @app.get("/agripoints_frontend/<path:f>")
    def ap_static(f): return send_from_directory(FRONT, f)
