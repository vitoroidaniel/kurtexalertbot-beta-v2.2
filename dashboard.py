"""
dashboard.py — Kurtex Alert Bot Web Dashboard
Clean rewrite with all features working.
"""
import csv, hashlib, hmac, io, json, logging, os, re, time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Thread

from flask import Flask, jsonify, render_template_string, request, session, redirect, Response

logger = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET", "kurtex-dashboard-secret-change-me")

DATA_DIR       = Path(os.getenv("DATA_DIR", "/app/data"))
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))


def verify_telegram_login(data):
    check_hash = data.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    computed   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    data["hash"] = check_hash
    if abs(time.time() - int(data.get("auth_date", 0))) > 86400:
        return False
    return hmac.compare_digest(computed, check_hash)


def get_bot_username():
    return os.getenv("BOT_USERNAME", "")


def load_cases():
    # Cases now live in SQLite (storage/case_store.py), not cases.json — the
    # bot stopped writing that file once it moved off whole-file JSON storage
    # (see storage/db.py for why). get_all_cases() returns dicts with the
    # same field names the old JSON had, so nothing else in this file needs
    # to change.
    try:
        from storage.case_store import get_all_cases
        return get_all_cases()
    except Exception as e:
        logger.error(f"load_cases (SQLite) error: {e}")
        return []


import zoneinfo
CHI_TZ = zoneinfo.ZoneInfo("America/Chicago")

def today_str():
    return datetime.now(CHI_TZ).date().isoformat()

def week_start_str():
    now = datetime.now(CHI_TZ)
    return (now - timedelta(days=now.weekday())).date().isoformat()

def month_start_str():
    return datetime.now(CHI_TZ).date().replace(day=1).isoformat()

def fmt_dt(iso):
    if not iso: return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(CHI_TZ).strftime("%b %d %H:%M")
    except: return str(iso)[:16]

def norm_uname(u):
    """Normalize a Telegram username for comparison: strip whitespace, leading '@', lowercase."""
    return (u or "").strip().lstrip("@").lower()

def case_local_date(c):
    """The case's opened_at date, converted to America/Chicago (naive timestamps are assumed UTC)."""
    iso = c.get("opened_at") if isinstance(c, dict) else c
    if not iso: return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CHI_TZ).date().isoformat()
    except Exception:
        return (iso or "")[:10]

def fmt_secs(secs):
    if secs is None: return "—"
    secs = int(secs)
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs//60}m {secs%60}s"
    return f"{secs//3600}h {(secs%3600)//60}m"

TESTING_GROUPS = {"testing", "test", "tests"}

def is_testing(c):
    # Testing tab/filter removed: cases from testing groups are no longer
    # excluded from stats — they now count as normal active cases.
    return False

def build_phrase_pattern(q):
    """Compile a case-insensitive, whole-word/phrase regex for exact keyword matching.
    'air' won't match inside 'repair'; 'oil leak' matches only as a contiguous phrase."""
    tokens = [re.escape(t) for t in re.split(r"\s+", (q or "").strip()) if t]
    if not tokens:
        return None
    return re.compile(r"\b" + r"\s+".join(tokens) + r"\b", re.IGNORECASE)

def serialize_case(c):
    try:
        return {
            "id":          (c.get("id") or "")[:8],
            "full_id":     c.get("id") or "",
            "driver":      c.get("driver_name") or "—",
            "group":       c.get("group_name") or "—",
            "agent":       c.get("agent_name") or "—",
            "status":      c.get("status") or "open",
            "opened":      fmt_dt(c.get("opened_at")),
            "closed":      fmt_dt(c.get("closed_at")),
            "opened_raw":  case_local_date(c),
            "response":    fmt_secs(c.get("response_secs")),
            "description": (c.get("description") or "")[:200],
            "notes":       c.get("notes") or "",
            "reassigned":  bool(c.get("reassigned")),
        }
    except Exception as e:
        logger.error(f"serialize_case error: {e}")
        return {"id":"?","full_id":"","driver":"—","group":"—","agent":"—",
                "status":"open","opened":"—","closed":"—","opened_raw":"",
                "response":"—","description":"","notes":"","reassigned":False}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/auth/telegram")
def telegram_auth():
    data = dict(request.args)
    if not data.get("hash"): return redirect("/login?error=missing")
    if verify_telegram_login(data):
        user_id = int(data.get("id", 0))
        role = "agent"
        try:
            from storage.user_store import get_user
            u = get_user(user_id)
            if u: role = u.get("role", "agent")
        except: pass
        session["user"] = {
            "id": user_id, "first_name": data.get("first_name",""),
            "username": data.get("username",""), "photo_url": data.get("photo_url",""),
            "role": role,
        }
        return redirect("/")
    return redirect("/login?error=invalid")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        cases = load_cases()
        today = today_str(); wk = week_start_str(); mo = month_start_str()
        real = [c for c in cases if not is_testing(c)]
        tc = [c for c in real if case_local_date(c) == today]
        wc = [c for c in real if case_local_date(c) >= wk]
        mc = [c for c in real if case_local_date(c) >= mo]
        st = Counter(c.get("status","open") for c in tc)

        def lb(lst):
            cnt = Counter(c["agent_name"] for c in lst if c.get("agent_name") and c.get("status") in ("assigned","reported","done"))
            return [{"name":n,"count":v} for n,v in cnt.most_common(10)]
        # Group resolution rates (all-time, min 3 cases to be meaningful)
        from collections import defaultdict
        grp_data = defaultdict(lambda: {"total":0,"done":0,"missed":0})
        for c in real:
            gn = c.get("group_name","Unknown") or "Unknown"
            grp_data[gn]["total"] += 1
            if c.get("status") == "done":   grp_data[gn]["done"] += 1
            if c.get("status") == "missed": grp_data[gn]["missed"] += 1
        group_stats = []
        for gn, d in grp_data.items():
            if d["total"] >= 1:
                rate = round(d["done"]/d["total"]*100) if d["total"] else 0
                group_stats.append({"name":gn,"total":d["total"],"done":d["done"],"missed":d["missed"],"rate":rate})
        group_stats.sort(key=lambda x: -x["total"])

        unit_counts = Counter()
        unit_vtype = {}
        for c in real:
            u = (c.get("unit_number") or "").strip()
            if not u: continue
            unit_counts[u] += 1
            unit_vtype[u] = c.get("vehicle_type","") or ""
        top_problem_units = [
            {"unit": u, "vtype": unit_vtype.get(u,""), "count": cnt}
            for u, cnt in unit_counts.most_common(6)
        ]
        hashtags = re.findall(r'#\w+', " ".join(c.get("description","") for c in real).lower())
        rt = [c["response_secs"] for c in real if c.get("response_secs")]
        avg = int(sum(rt)/len(rt)) if rt else 0
        return jsonify({
            "today": {"total":len(tc),"open":st.get("open",0),"assigned":st.get("assigned",0)+st.get("reported",0),"done":st.get("done",0),"missed":st.get("missed",0)},
            "week":  {"total":len(wc),"done":sum(1 for c in wc if c.get("status")=="done"),"missed":sum(1 for c in wc if c.get("status")=="missed")},
            "month": {"total":len(mc),"done":sum(1 for c in mc if c.get("status")=="done"),"missed":sum(1 for c in mc if c.get("status")=="missed")},
            "all_time": {"total":len(cases),"done":sum(1 for c in cases if c.get("status")=="done"),"avg_resp":fmt_secs(avg)},
            "leaderboard_day": lb(tc), "leaderboard_week": lb(wc), "leaderboard_month": lb(mc),
            "top_groups": group_stats[:6],
            "top_problem_units": top_problem_units,
            "top_words": [{"word":w,"count":v} for w,v in Counter(hashtags).most_common(15)],
            "reassigned_count": sum(1 for c in cases if c.get("reassigned")),
        })
    except Exception as e:
        logger.error(f"api_stats error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases")
def api_cases():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        f = request.args.get("filter","today")
        search = request.args.get("search","").lower().strip()
        date_filter = request.args.get("date","").strip()
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 500))
        except (TypeError, ValueError):
            limit = 100
        cases = load_cases()
        if f != "testing":
            cases = [c for c in cases if not is_testing(c)]
        if date_filter:
            cases = [c for c in cases if case_local_date(c) == date_filter]
        elif f == "today":    cases = [c for c in cases if case_local_date(c) == today_str()]
        elif f == "week":     cases = [c for c in cases if case_local_date(c) >= week_start_str()]
        elif f == "missed":   cases = [c for c in cases if c.get("status") == "missed"]
        elif f == "active":   cases = [c for c in cases if c.get("status") in ("open","assigned","reported")]
        elif f == "reassigned": cases = [c for c in cases if c.get("reassigned")]
        elif f == "testing":  cases = [c for c in cases if is_testing(c)]
        status_f = request.args.get("status","").strip().lower()
        if status_f:
            cases = [c for c in cases if (c.get("status") or "").lower() == status_f]
        if search:
            cases = [c for c in cases if
                     search in (c.get("driver_name") or "").lower() or
                     search in (c.get("group_name") or "").lower() or
                     search in (c.get("agent_name") or "").lower() or
                     search in (c.get("description") or "").lower()]
        cases = sorted(cases, key=lambda c: c.get("opened_at",""), reverse=True)
        total = len(cases)
        page = cases[offset:offset+limit]
        return jsonify({
            "cases": [serialize_case(c) for c in page],
            "total": total, "offset": offset, "limit": limit,
            "has_more": offset + limit < total,
        })
    except Exception as e:
        logger.error(f"api_cases error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/case")
def api_case_detail():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    case_id = request.args.get("id","").strip()
    if not case_id: return jsonify({"error":"no id"}), 400
    try:
        for c in load_cases():
            if (c.get("id") or "") == case_id or (c.get("id") or "").startswith(case_id):
                data = serialize_case(c)
                data.update({
                    "full_description": c.get("description",""),
                    "full_notes":       c.get("notes","") or "",
                    "agent_username":   c.get("agent_username",""),
                    "assigned_at":      fmt_dt(c.get("assigned_at")),
                    "resolution_secs":  fmt_secs(c.get("resolution_secs")),
                    "vehicle_type":     c.get("vehicle_type",""),
                    "unit_number":      c.get("unit_number",""),
                    "report_driver":    c.get("report_driver",""),
                    "issue_text":       c.get("issue_text",""),
                    "load_type":        c.get("load_type",""),
                    "priority":         c.get("priority",""),
                    "pickup":           c.get("pickup",""),
                    "delivery":         c.get("delivery",""),
                    "comments":         c.get("comments",""),
                    "setpoint":         c.get("setpoint",""),
                    "current_temp":     c.get("current_temp",""),
                    "temp_recorder":    c.get("temp_recorder",""),
                })
                return jsonify(data)
        return jsonify({"error":"not found"}), 404
    except Exception as e:
        logger.error(f"api_case error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent")
def api_agent():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    agent_name = request.args.get("name","").strip()
    agent_uname = norm_uname(request.args.get("username",""))
    if not agent_name: return jsonify({"error":"no name"}), 400
    period = request.args.get("period","all").strip().lower()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(int(request.args.get("limit", 15)), 100))
    except (TypeError, ValueError):
        limit = 15
    try:
        cases = [c for c in load_cases() if
                 (c.get("agent_name") or "").strip().lower() == agent_name.lower() or
                 (agent_uname and norm_uname(c.get("agent_username")) == agent_uname)]
        total  = len(cases)
        done   = sum(1 for c in cases if c.get("status") == "done")
        missed = sum(1 for c in cases if c.get("status") == "missed")
        rt     = [c["response_secs"] for c in cases if c.get("response_secs")]
        avg    = int(sum(rt)/len(rt)) if rt else 0

        if period == "today":
            bound = today_str()
            period_cases = [c for c in cases if case_local_date(c) == bound]
        elif period == "week":
            bound = week_start_str()
            period_cases = [c for c in cases if case_local_date(c) >= bound]
        elif period == "month":
            bound = month_start_str()
            period_cases = [c for c in cases if case_local_date(c) >= bound]
        else:
            period = "all"
            period_cases = cases

        period_cases.sort(key=lambda c: c.get("opened_at",""), reverse=True)
        period_total  = len(period_cases)
        period_done   = sum(1 for c in period_cases if c.get("status") == "done")
        period_missed = sum(1 for c in period_cases if c.get("status") == "missed")
        page = period_cases[offset:offset+limit]

        return jsonify({
            "name": agent_name, "total": total, "done": done, "missed": missed,
            "avg_resp": fmt_secs(avg), "rate": round(done/total*100) if total else 0,
            "period": period,
            "period_stats": {
                "total": period_total, "done": period_done, "missed": period_missed,
                "rate": round(period_done/period_total*100) if period_total else 0,
            },
            "cases": [serialize_case(c) for c in page],
            "offset": offset, "limit": limit,
            "has_more": offset + limit < period_total,
        })
    except Exception as e:
        logger.error(f"api_agent error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/agents")
def api_agents():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    if session["user"].get("role","agent") not in ("developer","super_admin"):
        return jsonify({"error":"forbidden"}), 403
    try:
        cases = load_cases()
        users = []
        try:
            from storage.user_store import get_all_user_dicts
            users = [u for u in get_all_user_dicts() if (u.get("role") or "") in ("agent","super_admin")]
        except Exception as e:
            logger.error(f"user_store error: {e}")
        if not users:
            seen = {}
            for c in cases:
                name = (c.get("agent_name") or "").strip()
                if name and name not in seen:
                    seen[name] = {"name": name, "username": c.get("agent_username",""), "role": "agent"}
            users = list(seen.values())
        result = []
        for u in users:
            name = (u.get("name") or "").strip()
            if not name: continue
            uname = norm_uname(u.get("username"))
            agent_cases = [c for c in cases if
                           (c.get("agent_name") or "").strip().lower() == name.lower() or
                           (uname and norm_uname(c.get("agent_username")) == uname)]
            total  = len(agent_cases)
            done   = sum(1 for c in agent_cases if c.get("status") == "done")
            missed = sum(1 for c in agent_cases if c.get("status") == "missed")
            rt     = [c["response_secs"] for c in agent_cases if c.get("response_secs")]
            avg    = int(sum(rt)/len(rt)) if rt else 0
            result.append({
                "name":     name,
                "username": u.get("username",""),
                "total":    total, "done": done, "missed": missed,
                "avg_resp": fmt_secs(avg),
                "rate":     round(done/total*100) if total else 0,
            })
        result.sort(key=lambda x: -x["total"])
        return jsonify(result)
    except Exception as e:
        logger.error(f"api_agents error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/my_profile")
def api_my_profile():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        user  = session["user"]
        name  = user.get("first_name","")
        uname = norm_uname(user.get("username",""))
        cases = load_cases()
        my_cases = [c for c in cases if
                    (c.get("agent_name") or "").strip().lower() == name.strip().lower() or
                    (uname and norm_uname(c.get("agent_username")) == uname)]
        today = today_str(); wk = week_start_str()
        tc = [c for c in my_cases if case_local_date(c) == today]
        wc = [c for c in my_cases if case_local_date(c) >= wk]
        total  = len(my_cases)
        done   = sum(1 for c in my_cases if c.get("status") == "done")
        missed = sum(1 for c in my_cases if c.get("status") == "missed")
        rt     = [c["response_secs"] for c in my_cases if c.get("response_secs")]
        avg    = int(sum(rt)/len(rt)) if rt else 0
        recent = sorted(my_cases, key=lambda c: c.get("opened_at",""), reverse=True)[:10]
        return jsonify({
            "name": name, "username": uname, "role": user.get("role","agent"),
            "total": total, "done": done, "missed": missed,
            "avg_resp": fmt_secs(avg), "rate": round(done/total*100) if total else 0,
            "today_total": len(tc), "today_done": sum(1 for c in tc if c.get("status")=="done"),
            "week_total":  len(wc), "week_done":  sum(1 for c in wc if c.get("status")=="done"),
            "recent": [serialize_case(c) for c in recent],
        })
    except Exception as e:
        logger.error(f"api_my_profile error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/unit")
def api_unit():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    unit_number = request.args.get("unit","").strip()
    vtype = request.args.get("vtype","").strip().lower()
    if not unit_number: return jsonify({"error":"no unit"}), 400
    try:
        all_cases = load_cases()
        unit_cases = [c for c in all_cases if (c.get("unit_number") or "").strip() == unit_number]
        if vtype:
            unit_cases = [c for c in unit_cases if (c.get("vehicle_type") or "").strip().lower() == vtype]
        unit_cases.sort(key=lambda c: c.get("opened_at",""), reverse=True)
        total  = len(unit_cases)
        active = sum(1 for c in unit_cases if c.get("status") in ("open","assigned","reported","missed"))
        done   = sum(1 for c in unit_cases if c.get("status") == "done")
        missed = sum(1 for c in unit_cases if c.get("status") == "missed")
        issue_counts = Counter((c.get("issue_text","").strip() or "")[:60] for c in unit_cases if c.get("issue_text","").strip())
        return jsonify({
            "unit": unit_number,
            "vtype": (unit_cases[0].get("vehicle_type","") if unit_cases else ""),
            "total": total, "active": active, "done": done, "missed": missed,
            "top_issues": [{"issue": iss, "count": cnt} for iss, cnt in issue_counts.most_common(5)],
            "cases": [serialize_case(c) for c in unit_cases[:50]],
        })
    except Exception as e:
        logger.error(f"api_unit error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/issue_search")
def api_issue_search():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    q = request.args.get("q","").strip()
    vtype = request.args.get("vtype","").strip().lower()
    if not q: return jsonify({"error":"no query"}), 400
    try:
        # Match the exact keyword/phrase as typed (whole-word boundaries on each
        # side, internal whitespace tolerant), case-insensitive. This is a literal
        # match of the phrase, not "any of these words anywhere".
        pattern = build_phrase_pattern(q)
        if not pattern: return jsonify({"error":"no query"}), 400
        all_cases = load_cases()
        matches = []
        for c in all_cases:
            if not c.get("unit_number"): continue
            if vtype and (c.get("vehicle_type") or "").strip().lower() != vtype: continue
            text = (c.get("issue_text") or "") + " " + (c.get("description") or "")
            if pattern.search(text):
                matches.append(c)
        from collections import defaultdict
        by_unit = defaultdict(lambda: {"cases": [], "vtype": ""})
        for c in matches:
            unit = (c.get("unit_number") or "").strip()
            by_unit[unit]["vtype"] = c.get("vehicle_type","")
            by_unit[unit]["cases"].append(c)
        results = []
        for unit, d in by_unit.items():
            cases = sorted(d["cases"], key=lambda c: c.get("opened_at",""), reverse=True)
            last = cases[0]
            results.append({
                "unit": unit,
                "vtype": d["vtype"],
                "count": len(cases),
                "last_seen": fmt_dt(last.get("opened_at")),
                "sample_issue": (last.get("issue_text") or last.get("description") or "")[:100],
            })
        results.sort(key=lambda x: -x["count"])
        return jsonify({"query": q, "results": results, "total_matches": len(matches)})
    except Exception as e:
        logger.error(f"api_issue_search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fleet")
def api_fleet():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        cases = [c for c in load_cases() if c.get("vehicle_type")]
        total         = len(cases)
        truck_count   = sum(1 for c in cases if c.get("vehicle_type") == "truck")
        trailer_count = sum(1 for c in cases if c.get("vehicle_type") == "trailer")
        reefer_count  = sum(1 for c in cases if c.get("vehicle_type") == "reefer")
        unit_counts   = Counter((c.get("unit_number","").strip(), c.get("vehicle_type","")) for c in cases if c.get("unit_number","").strip())
        truck_breakdowns = Counter(c.get("unit_number","").strip() for c in cases if c.get("vehicle_type") == "truck" and c.get("unit_number","").strip())
        driver_counts = Counter(c.get("report_driver","").strip() for c in cases if c.get("report_driver","").strip())
        issue_counts  = Counter((c.get("issue_text","").strip() or "")[:40] for c in cases if c.get("issue_text","").strip())
        load_counts   = Counter(c.get("load_type","").strip() for c in cases if c.get("load_type","").strip())
        latest_by_unit = {}
        for c in sorted(cases, key=lambda x: x.get("opened_at","")):
            unit = c.get("unit_number","").strip()
            vtype = c.get("vehicle_type","").strip()
            if unit:
                latest_by_unit[(unit, vtype)] = c
        fleet_status = []
        active_statuses = {"open", "assigned", "reported", "missed"}
        for (unit, vtype), c in latest_by_unit.items():
            status = c.get("status") or "open"
            fleet_status.append({
                "unit": unit,
                "vtype": vtype,
                "case_id": c.get("id",""),
                "status": "active" if status in active_statuses else "repaired",
                "case_status": status,
                "issue": c.get("issue_text") or c.get("description") or "",
                "driver": c.get("report_driver") or c.get("driver_name") or "",
                "opened": fmt_dt(c.get("opened_at")),
            })
        active_units = sum(1 for x in fleet_status if x["status"] == "active")
        repaired_units = sum(1 for x in fleet_status if x["status"] == "repaired")
        fleet_status = sorted(
            fleet_status,
            key=lambda x: (x["status"] != "active", x["vtype"], x["unit"])
        )[:30]
        return jsonify({
            "total_reports": total, "truck_count": truck_count,
            "trailer_count": trailer_count, "reefer_count": reefer_count,
            "active_units": active_units,
            "repaired_units": repaired_units,
            "top_units":    [{"unit":u,"vtype":vt,"count":cnt} for (u,vt),cnt in unit_counts.most_common(10)],
            "top_broken_trucks": [{"unit":u,"vtype":"truck","count":cnt} for u,cnt in truck_breakdowns.most_common(10)],
            "top_drivers":  [{"unit":n,"vtype":"","count":cnt} for n,cnt in driver_counts.most_common(10)],
            "top_issues":   [{"unit":iss,"vtype":"","count":cnt} for iss,cnt in issue_counts.most_common(8)],
            "load_types":   [{"unit":lt,"vtype":"","count":cnt} for lt,cnt in load_counts.most_common(6)],
            "fleet_status": fleet_status,
        })
    except Exception as e:
        logger.error(f"api_fleet error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/report")
def api_report():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        period    = request.args.get("period","today")
        date_from = request.args.get("from","")
        date_to   = request.args.get("to","")
        cases     = load_cases()
        if period == "today":
            label = "Today — " + datetime.now(CHI_TZ).strftime("%B %d, %Y")
            cases = [c for c in cases if case_local_date(c) == today_str()]
        elif period == "week":
            label = "This Week"; cases = [c for c in cases if case_local_date(c) >= week_start_str()]
        elif period == "month":
            label = "This Month"; cases = [c for c in cases if case_local_date(c) >= month_start_str()]
        elif period == "custom" and date_from:
            dt = date_to or today_str(); label = f"{date_from} to {dt}"
            cases = [c for c in cases if date_from <= case_local_date(c) <= dt]
        else:
            label = "All Time"
        total  = len(cases)
        done   = sum(1 for c in cases if c.get("status") == "done")
        missed = [c for c in cases if c.get("status") == "missed"]
        rt     = [c["response_secs"] for c in cases if c.get("response_secs")]
        avg    = int(sum(rt)/len(rt)) if rt else 0
        agent_counts  = Counter(c["agent_name"] for c in cases if c.get("agent_name") and c.get("status") in ("assigned","reported","done"))
        group_counts  = Counter(c.get("group_name","Unknown") for c in cases)

        # Top issues broken down by vehicle type (truck/trailer/reefer)
        by_vtype = {}
        for vt in ("truck", "trailer", "reefer"):
            vt_cases = [c for c in cases if (c.get("vehicle_type") or "").lower() == vt]
            issue_counts = Counter((c.get("issue_text") or c.get("description") or "Unspecified issue").strip() for c in vt_cases if (c.get("issue_text") or c.get("description")))
            by_vtype[vt] = {
                "total": len(vt_cases),
                "top_issues": [{"issue": i, "count": n} for i, n in issue_counts.most_common(5)],
            }

        # Top problem units for this period (most reported units, any type)
        unit_counts = Counter()
        unit_vtype = {}
        for c in cases:
            u = (c.get("unit_number") or "").strip()
            if not u: continue
            unit_counts[u] += 1
            unit_vtype[u] = c.get("vehicle_type","") or ""
        top_units = [{"unit": u, "vtype": unit_vtype.get(u,""), "count": n} for u, n in unit_counts.most_common(10)]

        return jsonify({
            "label": label, "total": total, "done": done, "missed": len(missed),
            "assigned": sum(1 for c in cases if c.get("status") in ("assigned","reported","done")),
            "open": sum(1 for c in cases if c.get("status") == "open"),
            "avg_resp": fmt_secs(avg), "rate": round(done/total*100) if total else 0,
            "leaderboard": [{"name":n,"count":v} for n,v in agent_counts.most_common(10)],
            "top_groups":  [{"name":n,"count":v} for n,v in group_counts.most_common(5)],
            "missed_cases": [serialize_case(c) for c in missed[:20]],
            "by_vtype": by_vtype,
            "top_units": top_units,
        })
    except Exception as e:
        logger.error(f"api_report error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/export")
def api_export():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    cases = load_cases()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID","Reported By","Group","Assigned To","Status","Opened","Closed","Response","Description","Notes"])
    for c in sorted(cases, key=lambda x: x.get("opened_at",""), reverse=True):
        w.writerow([
            (c.get("id") or "")[:8], c.get("driver_name",""), c.get("group_name",""),
            c.get("agent_name",""), c.get("status",""),
            (c.get("opened_at") or "")[:16], (c.get("closed_at") or "")[:16],
            fmt_secs(c.get("response_secs")), c.get("description",""), c.get("notes",""),
        ])
    out.seek(0)
    today = datetime.now(CHI_TZ).strftime("%Y-%m-%d")
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=kurtex-{today}.csv"})


# ── HTML pages ────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Kurtex Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Plus Jakarta Sans',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#1a1208}

/* Background slides */
.bg-slide{position:fixed;inset:0;transition:opacity 2s ease-in-out;background-size:cover;background-position:center;opacity:0}
.bg-slide.active{opacity:1}

/* Dark gradient overlay - left side darker for card, right shows photo */
.overlay{position:fixed;inset:0;background:linear-gradient(105deg,rgba(20,14,6,.92) 0%,rgba(20,14,6,.75) 40%,rgba(20,14,6,.3) 70%,rgba(20,14,6,.1) 100%)}

/* Card on left */
.card{position:relative;z-index:1;width:100%;max-width:400px;margin:0 auto}
.card-inner{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);border-radius:24px;padding:44px 36px;backdrop-filter:blur(16px)}

.logo{width:60px;height:60px;border-radius:16px;background:linear-gradient(135deg,#C17B3F,#8B4A1A);display:flex;align-items:center;justify-content:center;margin-bottom:20px;font-size:28px;box-shadow:0 4px 24px rgba(193,123,63,.5)}
h1{color:#fff;font-size:26px;font-weight:800;margin-bottom:6px;letter-spacing:-.4px;line-height:1.2;text-align:center}
.tagline{color:rgba(255,255,255,.5);font-size:13px;margin-bottom:28px;text-align:center}

/* Stats strip */
.stats{display:flex;gap:20px;margin-bottom:28px;padding:14px 16px;background:rgba(255,255,255,.06);border-radius:12px;border:1px solid rgba(255,255,255,.08)}
.stat{text-align:center;flex:1}
.stat-num{font-size:20px;font-weight:800;color:#D4904E}
.stat-lbl{font-size:9px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.06em;margin-top:2px}

.divider{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.divider-line{flex:1;height:1px;background:rgba(255,255,255,.12)}
.divider span{font-size:10px;color:rgba(255,255,255,.35);text-transform:uppercase;letter-spacing:.1em;white-space:nowrap}
.tg-wrap{display:flex;justify-content:center}

.error{color:#F87171;font-size:12px;margin-bottom:14px;background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.25);border-radius:8px;padding:8px 12px}

/* Right side caption */
.caption{position:fixed;bottom:40px;right:40px;z-index:2;text-align:right}
.caption-title{font-size:22px;font-weight:700;color:rgba(255,255,255,.9);line-height:1.2}
.caption-sub{font-size:12px;color:rgba(255,255,255,.45);margin-top:4px}

/* Dots indicator */
.dots{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:2;display:flex;gap:6px}
.dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.3);transition:all .3s}
.dot.active{background:#D4904E;width:20px;border-radius:3px}

@media(max-width:768px){
  .card{margin:0 16px}
  .caption{display:none}
}
</style>
</head><body>
<div id="bg1" class="bg-slide active"></div>
<div id="bg2" class="bg-slide"></div>
<div class="overlay"></div>

<div class="card">
  <div class="card-inner">
    <img src="data:image/png;base64,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" style="width:100px;height:100px;border-radius:50%;object-fit:cover;margin:0 auto 20px;display:block;box-shadow:0 4px 24px rgba(0,0,0,.3)">
    <h1>Kurtex Dashboard</h1>
    <p class="tagline">Truck Maintenance Command Center</p>
    <div class="stats">
      <div class="stat"><div class="stat-num">24/7</div><div class="stat-lbl">Monitoring</div></div>
      <div class="stat"><div class="stat-num">Live</div><div class="stat-lbl">Updates</div></div>
      <div class="stat"><div class="stat-num">100%</div><div class="stat-lbl">Secure</div></div>
    </div>
    {% if error %}<div class="error">Authentication failed. Please try again.</div>{% endif %}
    <div class="divider"><div class="divider-line"></div><span>Sign in with Telegram</span><div class="divider-line"></div></div>
    <div class="tg-wrap">
      <script async src="https://telegram.org/js/telegram-widget.js?22"
        data-telegram-login="{{ bot_username }}"
        data-size="large" data-radius="10"
        data-auth-url="/auth/telegram"
        data-request-access="write"></script>
    </div>
  </div>
</div>

<div class="caption" id="caption">
  <div class="caption-title" id="caption-title">Fleet Management</div>
  <div class="caption-sub" id="caption-sub">Photo: Unsplash</div>
</div>
<div class="dots" id="dots"></div>

<script>
var photos = [
  {url:'https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?w=1920&q=80&auto=format&fit=crop',title:'Fleet Operations',sub:'Keep your trucks moving'},
  {url:'https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1920&q=80&auto=format&fit=crop',title:'Route Management',sub:'Every mile tracked'},
  {url:'https://images.unsplash.com/photo-1519003722824-194d4455a60c?w=1920&q=80&auto=format&fit=crop',title:'Open Road',sub:'24/7 driver support'},
  {url:'https://images.unsplash.com/photo-1494976388531-d1058494cdd8?w=1920&q=80&auto=format&fit=crop',title:'Highway Logistics',sub:'Nationwide coverage'},
  {url:'https://images.unsplash.com/photo-1615799998603-7c6270a45196?w=1920&q=80&auto=format&fit=crop',title:'Maintenance Ready',sub:'Zero downtime goal'},
];

var current = 0;
var bg1 = document.getElementById('bg1');
var bg2 = document.getElementById('bg2');
var activeBg = bg1, inactiveBg = bg2;
var dotsEl = document.getElementById('dots');

// Build dots
photos.forEach(function(_, i) {
  var d = document.createElement('div');
  d.className = 'dot' + (i===0?' active':'');
  d.id = 'dot-'+i;
  dotsEl.appendChild(d);
});

function updateCaption(p) {
  document.getElementById('caption-title').textContent = p.title;
  document.getElementById('caption-sub').textContent = p.sub;
}

function setDot(idx) {
  document.querySelectorAll('.dot').forEach(function(d,i){ d.className='dot'+(i===idx?' active':''); });
}

function loadPhoto(idx) {
  var p = photos[idx];
  inactiveBg.style.backgroundImage = 'url('+p.url+')';
  inactiveBg.style.opacity = '0';
  setTimeout(function() {
    inactiveBg.style.opacity = '1';
    activeBg.style.opacity = '0';
    var tmp = activeBg; activeBg = inactiveBg; inactiveBg = tmp;
    updateCaption(p);
    setDot(idx);
  }, 50);
}

bg1.style.backgroundImage = 'url('+photos[0].url+')';
updateCaption(photos[0]);

setInterval(function() {
  current = (current+1) % photos.length;
  loadPhoto(current);
}, 6000);
</script>
</body></html>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Kurtex Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/@phosphor-icons/web@2.1.1/src/index.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#E8ECEF;--surface:rgba(255,255,255,.91);--surface2:rgba(247,248,250,.86);--surface3:#E5E8EC;
  --border:rgba(30,41,59,.12);--text:#1D2430;--muted:#687384;--muted2:#9AA3AF;
  --accent:#111418;--accent-bg:rgba(17,20,24,.08);
  --green:#16A34A;--green-bg:rgba(22,163,74,.08);
  --red:#DC2626;--red-bg:rgba(220,38,38,.08);
  --yellow:#D97706;--yellow-bg:rgba(217,119,6,.09);
  --blue:#4B6F94;--blue-bg:rgba(75,111,148,.08);
  --purple:#746B8F;--purple-bg:rgba(116,107,143,.08);
  --shadow:0 1px 2px rgba(15,23,42,.04),0 8px 28px rgba(15,23,42,.08);
}
[data-theme="dark"]{
  --bg:#11161B;--surface:rgba(20,24,30,.9);--surface2:rgba(30,35,42,.86);--surface3:#272D36;
  --border:rgba(255,255,255,.08);--text:#E8EAF0;--muted:#9AA3B1;--muted2:#626B79;
  --accent:#8AA4B8;--accent-bg:rgba(138,164,184,.14);
  --green:#4ADE80;--green-bg:rgba(74,222,128,.08);
  --red:#F87171;--red-bg:rgba(248,113,113,.08);
  --yellow:#FBBF24;--yellow-bg:rgba(251,191,36,.1);
  --blue:#86A6C8;--blue-bg:rgba(134,166,200,.1);
  --purple:#B0A6C8;--purple-bg:rgba(176,166,200,.1);
  --shadow:0 1px 4px rgba(0,0,0,.28),0 10px 36px rgba(0,0,0,.28);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{font-family:"Plus Jakarta Sans",sans-serif;background:transparent;color:var(--text);min-height:100vh;transition:background .2s,color .2s;isolation:isolate}
body::before{content:"";position:fixed;inset:0;z-index:-1;background:
  linear-gradient(90deg,rgba(232,236,239,.94),rgba(232,236,239,.82)),
  url("https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?auto=format&fit=crop&w=1800&q=80") center/cover no-repeat}
[data-theme="dark"] body::before{background:
  linear-gradient(90deg,rgba(17,22,27,.96),rgba(17,22,27,.86)),
  url("https://images.unsplash.com/photo-1601584115197-04ecc0da31d7?auto=format&fit=crop&w=1800&q=80") center/cover no-repeat}
body.modal-open{overflow:hidden}
.layout{display:flex;min-height:100vh}

/* ── Sidebar ── */
.sidebar{width:230px;flex-shrink:0;background:var(--surface);backdrop-filter:blur(18px);border-right:1px solid var(--border);padding:18px 10px 16px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;z-index:50;transition:transform .25s,background .2s;overflow-y:auto}
.sidebar-logo{display:flex;align-items:center;gap:10px;margin-bottom:22px;padding:4px 8px 14px;border-bottom:1px solid var(--border)}
.logo-icon{width:32px;height:32px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;box-shadow:0 2px 8px rgba(15,23,42,.16)}
.logo-text h2{font-size:13px;font-weight:800;letter-spacing:-.2px}
.logo-text small{font-size:10px;color:var(--muted);font-weight:500}
nav{flex:1;display:flex;flex-direction:column;gap:1px}
.nav-section-label{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);padding:10px 10px 4px;margin-top:4px}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;color:var(--muted);font-size:12px;font-weight:500;cursor:pointer;transition:all .12s;position:relative}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:var(--accent-bg);color:var(--accent);font-weight:600}
.nav-item.active::before{content:"";position:absolute;left:0;top:20%;bottom:20%;width:3px;border-radius:0 3px 3px 0;background:var(--accent)}
.nav-item i{font-size:15px;width:18px;text-align:center;flex-shrink:0}
.nav-group{margin-top:2px}
.nav-group-header{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-radius:8px;color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;transition:all .12s}
.nav-group-header i{font-size:15px;width:18px;text-align:center;flex-shrink:0}
.nav-group-header:hover{background:var(--surface2);color:var(--text)}
.nav-group-header span{display:flex;align-items:center;gap:9px}
.nav-caret{font-size:11px;transition:transform .2s;flex-shrink:0;opacity:.6}
.nav-caret.open{transform:rotate(180deg)}
.nav-group-items{overflow:hidden;max-height:0;transition:max-height .25s ease}
.nav-group-items.open{max-height:200px}
.nav-sub{padding-left:30px!important;font-size:11px!important;color:var(--muted2)!important}
.nav-sub:hover{color:var(--text)!important}
.nav-sub.active{color:var(--accent)!important}
.nav-badge{margin-left:auto;background:var(--red);color:#fff;font-size:9px;font-weight:800;padding:1px 6px;border-radius:20px;line-height:1.6}
.sidebar-footer{padding-top:12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:6px}
.user-chip{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:9px;background:var(--surface2);border:1px solid var(--border)}
.user-avatar{width:28px;height:28px;border-radius:50%;border:2px solid var(--border);flex-shrink:0;object-fit:cover}
.user-avatar-init{width:28px;height:28px;border-radius:50%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:#fff;flex-shrink:0}
.user-name{font-size:12px;font-weight:700;line-height:1.2}
.user-role{font-size:10px;color:var(--muted);text-transform:capitalize}
.sidebar-actions{display:flex;gap:5px}
.theme-btn{flex:1;padding:6px 8px;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--muted);font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:5px;transition:all .12s}
.theme-btn:hover{background:var(--surface3);color:var(--text)}
.logout-btn{flex:1;padding:6px 8px;background:var(--red-bg);border:1px solid rgba(220,38,38,.2);color:var(--red);border-radius:7px;font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:5px;transition:all .12s}
.logout-btn:hover{background:var(--red);color:#fff}

/* ── Mobile ── */
.mobile-header{display:none;position:sticky;top:0;z-index:60;background:var(--surface);backdrop-filter:blur(18px);border-bottom:1px solid var(--border);padding:11px 16px;align-items:center;justify-content:space-between}
.mobile-logo{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:800}
.hamburger{background:var(--surface2);border:1px solid var(--border);border-radius:7px;width:32px;height:32px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--text)}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99}

/* ── Main ── */
.main{flex:1;padding:20px 24px;overflow-x:hidden;min-width:0}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px;gap:10px;flex-wrap:wrap}
.topbar h1{font-size:17px;font-weight:800;letter-spacing:-.3px}
.topbar-right{display:flex;align-items:center;gap:6px}

/* Topbar buttons — distinct styles */
.badge-btn{display:inline-flex;align-items:center;gap:5px;border-radius:8px;padding:6px 12px;font-size:11px;font-weight:700;cursor:pointer;text-decoration:none;transition:all .13s;font-family:inherit;border:none;letter-spacing:.02em}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border-radius:9px;padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;border:none;background:var(--accent);color:#fff;transition:filter .13s, transform .1s;white-space:nowrap}
.btn:hover{filter:brightness(1.08)}
.btn:active{transform:scale(.97)}
.badge-btn.btn-outline{background:var(--surface);border:1px solid var(--border);color:var(--text)}
.badge-btn.btn-outline:hover{background:var(--surface2);border-color:var(--muted2)}
.badge-btn.btn-primary{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(15,23,42,.12)}
.badge-btn.btn-primary:hover{filter:brightness(1.1)}
.badge-btn.btn-ghost{background:transparent;border:1px solid transparent;color:var(--muted)}
.badge-btn.btn-ghost:hover{background:var(--surface2);color:var(--text)}
.live-pill{display:inline-flex;align-items:center;gap:6px;background:var(--green-bg);border:1px solid rgba(22,163,74,.2);color:var(--green);border-radius:20px;padding:5px 12px;font-size:11px;font-weight:700}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ── Stat cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.mini-stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 14px 14px;box-shadow:var(--shadow);position:relative;overflow:hidden;transition:transform .15s ease, box-shadow .15s ease}
.stat-card:hover{transform:translateY(-3px);box-shadow:0 12px 28px rgba(15,23,42,.14)}
.stat-card::before{content:"";position:absolute;left:0;top:0;right:0;height:3px}
.stat-card.c-accent::before{background:var(--accent)}
.stat-card.c-green::before{background:var(--green)}
.stat-card.c-red::before{background:var(--red)}
.stat-card.c-yellow::before{background:var(--yellow)}
.stat-card.c-blue::before{background:var(--blue)}
.stat-card.c-purple::before{background:var(--purple)}
.stat-icon{position:absolute;top:12px;right:12px;width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:17px}
.stat-card.c-accent .stat-icon{background:var(--accent-bg);color:var(--accent)}
.stat-card.c-green .stat-icon{background:var(--green-bg);color:var(--green)}
.stat-card.c-red .stat-icon{background:var(--red-bg);color:var(--red)}
.stat-card.c-yellow .stat-icon{background:var(--yellow-bg);color:var(--yellow)}
.stat-card.c-blue .stat-icon{background:var(--blue-bg);color:var(--blue)}
.stat-card.c-purple .stat-icon{background:var(--purple-bg);color:var(--purple)}
.stat-label{font-size:10px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;padding-right:34px}
.stat-value{font-size:28px;font-weight:800;line-height:1;letter-spacing:-.5px}
.v-accent{color:var(--accent)}.v-green{color:var(--green)}.v-red{color:var(--red)}
.v-yellow{color:var(--yellow)}.v-blue{color:var(--blue)}.v-purple{color:var(--purple)}
.v-sm{font-size:17px!important;margin-top:4px;letter-spacing:-.2px}

/* ── Cards ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:var(--shadow)}
.card-title{font-size:12px;font-weight:800;margin-bottom:14px;display:flex;align-items:center;gap:7px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.card-title i{font-size:14px;color:var(--accent)}

/* ── Period toggle ── */
.toggle-tabs{display:flex;background:var(--surface2);border-radius:8px;padding:3px;gap:2px;margin-bottom:14px}
.toggle-btn{flex:1;padding:5px 10px;border-radius:6px;border:none;background:transparent;font-size:11px;font-weight:600;color:var(--muted);cursor:pointer;font-family:inherit;transition:all .12s}
.toggle-btn.active{background:var(--accent);color:#fff;box-shadow:0 2px 6px rgba(15,23,42,.14)}

/* ── Filter tabs ── */
.filter-tabs{display:flex;gap:5px;flex-wrap:wrap}
.tab-btn{padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;font-family:inherit;transition:all .12s}
.tab-btn:hover{border-color:var(--accent);color:var(--accent)}
.tab-btn.active{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 2px 6px rgba(15,23,42,.12)}

/* ── List rows ── */
.list-row{display:flex;align-items:center;gap:7px;padding:7px 0;border-bottom:1px solid var(--border)}
.list-row:last-child{border-bottom:none}
.list-name{font-size:12px;font-weight:500;flex:1}
.list-count{font-size:11px;font-weight:800;color:var(--accent);background:var(--accent-bg);padding:2px 9px;border-radius:20px;flex-shrink:0}
.bar-wrap{flex:1.5;height:3px;background:var(--surface3);border-radius:2px;margin:0 6px}
.bar-fill{height:100%;border-radius:2px;background:var(--accent);transition:width .5s}
.medal{font-size:13px;flex-shrink:0;width:20px}

.section{margin-bottom:20px}
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px}
.section-title{font-size:13px;font-weight:700}

.search-wrap{position:relative;margin-bottom:14px}
.search-wrap input{width:100%;padding:9px 14px 9px 38px;background:var(--surface);border:1px solid var(--border);border-radius:9px;font-size:13px;color:var(--text);font-family:inherit;outline:none;transition:border .15s}
.search-wrap input:focus{border-color:var(--accent)}
.search-wrap i{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:15px}

.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:540px}
thead th{padding:9px 12px;text-align:left;color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);background:var(--surface2)}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s;cursor:pointer}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
td{padding:9px 12px;vertical-align:middle}
.status-badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;white-space:nowrap}
.s-open{background:var(--blue-bg);color:var(--blue)}.s-assigned{background:var(--yellow-bg);color:var(--yellow)}
.s-reported{background:var(--purple-bg);color:var(--purple)}.s-done{background:var(--green-bg);color:var(--green)}
.s-missed{background:var(--red-bg);color:var(--red)}
.reassign-badge{display:inline-flex;padding:2px 6px;border-radius:20px;font-size:10px;font-weight:700;background:var(--purple-bg);color:var(--purple);margin-left:4px}
.desc-cell{max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}

.word-grid{display:flex;flex-wrap:wrap;gap:7px}
.word-tag{padding:4px 11px;border-radius:20px;font-size:12px;font-weight:600;background:var(--accent-bg);color:var(--accent);border:1px solid rgba(99,102,241,.15)}

.stats-list .row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.stats-list .row:last-child{border-bottom:none}
.stats-list .val{font-weight:700}

.modal-overlay{display:none;position:fixed;inset:0;background:rgba(15,23,42,.42);z-index:300;align-items:center;justify-content:center;padding:16px;overscroll-behavior:contain}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);backdrop-filter:blur(18px);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:860px;width:100%;max-height:88vh;overflow-y:auto;overscroll-behavior:contain;position:relative;box-shadow:0 8px 40px rgba(0,0,0,.15)}
.modal-close{position:absolute;top:14px;right:14px;background:var(--surface2);border:1px solid var(--border);border-radius:7px;width:28px;height:28px;cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--muted)}
.modal h2{font-size:16px;font-weight:700;margin-bottom:16px;padding-right:40px}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
.detail-item{background:var(--surface2);border-radius:8px;padding:10px 12px}
.detail-label{font-size:10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
.detail-val{font-size:13px;font-weight:600}
.desc-box{background:var(--surface2);border-radius:8px;padding:12px;margin-bottom:10px}
.notes-box{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px;margin-bottom:10px}
[data-theme="dark"] .notes-box{background:rgba(251,191,36,.06);border-color:rgba(251,191,36,.2)}
.box-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;display:block;margin-bottom:6px;color:var(--muted)}
.box-text{font-size:13px;line-height:1.6}

.timeline{display:flex;align-items:flex-start;gap:0;margin-bottom:16px;padding:14px;background:var(--surface2);border-radius:10px}
.tl-step{display:flex;flex-direction:column;align-items:center;flex:1;position:relative}
.tl-step:not(:last-child)::after{content:"";position:absolute;top:12px;left:calc(50% + 12px);width:calc(100% - 24px);height:2px;background:var(--border)}
.tl-step.done-step::after{background:var(--accent)}
.tl-dot{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;border:2px solid var(--border);background:var(--surface);z-index:1;position:relative;font-weight:700}
.tl-dot.active{border-color:var(--accent);background:var(--accent);color:#fff}
.tl-dot.done{border-color:var(--green);background:var(--green);color:#fff}
.tl-label{font-size:9px;color:var(--muted);margin-top:5px;font-weight:600;text-transform:uppercase}
.tl-time{font-size:9px;color:var(--muted2);margin-top:2px;text-align:center}

.agent-stat{background:var(--surface2);border-radius:8px;padding:10px;text-align:center}
.agent-stat-val{font-size:22px;font-weight:800}
.agent-stat-label{font-size:10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.agent-card{cursor:pointer;transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease;position:relative;overflow:hidden}
.agent-card:hover{transform:translateY(-3px);box-shadow:0 10px 24px rgba(0,0,0,.18);border-color:var(--accent)}
.agent-card-rank{position:absolute;top:14px;right:16px;font-size:11px;font-weight:800;color:var(--muted);background:var(--surface2);border-radius:20px;padding:3px 10px}
.agent-card-avatar{width:56px;height:56px;border-radius:50%;background:var(--accent-bg);display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800;color:var(--accent);flex-shrink:0}
.agent-card-statbox{background:var(--surface2);border-radius:9px;padding:10px 4px;text-align:center}
.agent-card-statval{font-size:19px;font-weight:800}
.agent-card-statlabel{font-size:9px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.agent-rate-track{background:var(--surface2);border-radius:20px;height:8px;overflow:hidden;margin-top:8px}
.agent-rate-fill{height:100%;border-radius:20px;background:linear-gradient(90deg,var(--accent),var(--green))}
.agent-card-footer{margin-top:14px;display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:12px}

.report-modal-overlay{display:none;position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:400;align-items:flex-start;justify-content:center;padding:20px;overflow-y:auto;overscroll-behavior:contain}
.report-modal-overlay.open{display:flex}
.report-modal{background:var(--surface);backdrop-filter:blur(18px);border:1px solid var(--border);border-radius:16px;width:100%;max-width:700px;margin:auto;overscroll-behavior:contain}
.report-header{padding:20px 24px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.report-header h2{font-size:17px;font-weight:700}
.report-tabs{display:flex;background:var(--surface2);border-radius:8px;padding:3px;gap:2px}
.report-tab{padding:5px 14px;border-radius:6px;border:none;background:transparent;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer;font-family:inherit;transition:all .15s}
.report-tab.active{background:var(--surface);color:var(--accent);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.report-close{background:var(--surface2);border:1px solid var(--border);border-radius:7px;width:28px;height:28px;cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--muted)}
.report-body{padding:20px 24px}
.report-period-bar{display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.report-period-bar select,.report-period-bar input{padding:7px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;font-size:12px;color:var(--text);font-family:inherit;outline:none}
.report-generate-btn{padding:7px 16px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
.report-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;margin-bottom:18px}
.report-stat{background:var(--surface2);border-radius:10px;padding:12px;text-align:center}
.report-stat-val{font-size:24px;font-weight:800;line-height:1}
.report-stat-label{font-size:10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-top:3px}
.report-section{margin-bottom:16px}
.report-section h3{font-size:11px;font-weight:700;margin-bottom:8px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.report-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.report-row:last-child{border-bottom:none}
.report-row .rname{flex:1;font-weight:500}
.report-row .rcount{font-weight:700;color:var(--accent);background:var(--accent-bg);padding:1px 8px;border-radius:20px}
.report-footer{padding:12px 24px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.report-footer .ts{font-size:11px;color:var(--muted)}
.print-report-btn{display:flex;align-items:center;gap:6px;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:7px 14px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}

.loading{text-align:center;padding:28px;color:var(--muted);font-size:13px}
.empty-state{text-align:center;padding:40px;color:var(--muted)}
.page{display:none}
.page.active{display:block}

@media(max-width:768px){
  /* Sidebar drawer */
  .sidebar{position:fixed;left:0;top:0;height:100%!important;transform:translateX(-100%);width:260px;z-index:100;box-shadow:4px 0 32px rgba(0,0,0,.2);overflow-y:auto}
  .sidebar.open{transform:translateX(0)!important}
  .sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99}
  .sidebar-overlay.open{display:block}
  .mobile-header{display:flex!important;z-index:101}

  /* Layout - sidebar hidden, full width content */
  .layout{display:block}
  .main{padding:12px 12px 100px;width:100%;min-width:0;margin-left:0!important}

  /* Topbar */
  .topbar{flex-wrap:nowrap;gap:6px}
  .topbar h1{font-size:16px;white-space:nowrap}
  .topbar-right{gap:4px}
  .topbar-right .badge-btn{padding:6px 8px;font-size:11px}
  .topbar-right .badge-btn span{display:none}
  .topbar-right .badge-btn i{font-size:16px}

  /* Stats - 2 cols */
  .stat-grid{grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
  .stat-value{font-size:22px}
  .stat-card{padding:12px}
  .mini-stat-grid{grid-template-columns:repeat(2,1fr)!important;gap:8px}
  .mini-stat-grid .agent-stat-val{font-size:18px}
  .mini-stat-grid .agent-stat-label{font-size:9px}
  .mini-stat-grid .agent-card-statval{font-size:16px}
  .mini-stat-grid .agent-card-statlabel{font-size:9px}
  .mini-stat-grid .stat-label{padding-right:0}

  /* Columns */
  .two-col{grid-template-columns:1fr;gap:10px}
  .detail-grid{grid-template-columns:1fr}
  .agent-stats{grid-template-columns:repeat(2,1fr)}

  /* Tables - horizontal scroll */
  .table-wrap{overflow:hidden}
  .table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%}
  table{min-width:480px}
  td,th{padding:8px 10px!important;font-size:11px!important}

  /* Filter tabs - scroll horizontal */
  .filter-tabs{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}
  .tab-btn{white-space:nowrap;padding:7px 12px;font-size:12px}

  /* Toggle tabs (period/type pills) - scroll instead of squeezing */
  .toggle-tabs{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .toggle-btn{flex:none;white-space:nowrap;padding:8px 14px;font-size:12px}

  /* Section header stacks */
  .section-header{flex-direction:column;align-items:flex-start;gap:8px}
  .section-header > div{width:100%}

  /* Search input bigger */
  .search-wrap input{font-size:15px;padding:11px 14px 11px 40px}
  .btn{padding:11px 18px;font-size:14px;width:100%}

  /* Modals - bottom sheet */
  .modal-overlay{padding:0;align-items:flex-end}
  .modal{border-radius:20px 20px 0 0;max-height:90vh;max-width:100%;border-bottom:none;padding:20px 16px}
  .report-modal-overlay{padding:0;align-items:flex-end}
  .report-modal{border-radius:20px 20px 0 0;max-width:100%}

  /* Nav items bigger touch targets */
  .nav-item{padding:13px 12px;font-size:14px}
  .nav-item i{font-size:17px}

  /* Cards */
  .card{padding:14px}

  /* Agents grid */
  #agents-content > div{grid-template-columns:1fr!important}

  /* Hide topbar on scroll trick - remove right overflow */
  body{overflow-x:hidden}
}
@media(max-width:480px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .topbar-right .badge-btn:first-child{display:none}
}
@media print{
  /* Generic page print (topbar Print button) — unchanged */
  body:not(.printing-report) .sidebar,
  body:not(.printing-report) .mobile-header,
  body:not(.printing-report) .topbar-right,
  body:not(.printing-report) .report-modal-overlay{display:none!important}
  body:not(.printing-report) .main{padding:0}

  /* Report print — show only the official report, nothing else */
  body.printing-report .sidebar,
  body.printing-report .mobile-header,
  body.printing-report .topbar-right,
  body.printing-report .main,
  body.printing-report .report-header,
  body.printing-report .report-period-bar,
  body.printing-report #report-sections-bar,
  body.printing-report .report-footer{display:none!important}
  body.printing-report .report-modal-overlay{display:block!important;position:static!important;padding:0!important;background:white!important;inset:auto!important}
  body.printing-report .report-modal{box-shadow:none!important;border:none!important;border-radius:0!important;max-width:100%!important;width:100%!important;margin:0!important;backdrop-filter:none!important}
  body.printing-report .report-body{padding:0!important}

  body{background:white;color:black}
}
</style>
</head>
<body>


<div class="mobile-header">
  <div class="mobile-logo"><img src="data:image/png;base64,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" style="width:32px;height:32px;border-radius:50%;object-fit:cover"> Kurtex</div>
  <div class="hamburger" onclick="toggleSidebar()"><i class="ph ph-list"></i></div>
</div>
<div class="sidebar-overlay" id="sidebar-overlay" onclick="closeSidebar()"></div>

<div class="layout">
<aside class="sidebar" id="sidebar">
  <div class="sidebar-logo">
    <img src="data:image/png;base64,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" style="width:44px;height:44px;border-radius:50%;object-fit:cover;flex-shrink:0">
    <div class="logo-text"><h2>Kurtex</h2><small>Alert Dashboard</small></div>
  </div>
  <nav>
    <div class="nav-item active" onclick="showPage('overview')"><i class="ph ph-squares-four"></i> Overview</div>
    <div class="nav-item" onclick="showPage('cases')"><i class="ph ph-clipboard-text"></i> Cases</div>
    <div class="nav-item" onclick="showPage('missed')"><i class="ph ph-warning"></i> Missed <span class="nav-badge" id="missed-badge" style="display:none"></span></div>
    <div class="nav-item" onclick="showPage('leaderboard')"><i class="ph ph-trophy"></i> Leaderboard</div>

    <!-- Analytics group -->
    <div class="nav-group">
      <div class="nav-group-header" onclick="toggleGroup('group-analytics')">
        <span><i class="ph ph-chart-bar"></i> Analytics</span>
        <i class="ph ph-caret-down nav-caret" id="caret-group-analytics"></i>
      </div>
      <div class="nav-group-items" id="group-analytics">
        <div class="nav-item nav-sub" onclick="showPage('trends')"><i class="ph ph-trend-up"></i> Trends</div>
        <div class="nav-item nav-sub" onclick="showPage('comparison')"><i class="ph ph-arrows-left-right"></i> Comparison</div>
      </div>
    </div>

    <!-- Fleet group -->
    <div class="nav-group">
      <div class="nav-group-header" onclick="toggleGroup('group-fleet')">
        <span><i class="ph ph-truck"></i> Fleet</span>
        <i class="ph ph-caret-down nav-caret" id="caret-group-fleet"></i>
      </div>
      <div class="nav-group-items" id="group-fleet">
        <div class="nav-item nav-sub" onclick="showPage('fleet')"><i class="ph ph-wrench"></i> Fleet Stats</div>
        <div class="nav-item nav-sub" onclick="showPage('fleet_intel')"><i class="ph ph-magnifying-glass"></i> Intelligence</div>
      </div>
    </div>

    <div class="nav-item" onclick="showPage('my_profile')"><i class="ph ph-user"></i> My Profile</div>
    {% if is_manager %}<div class="nav-item" onclick="showPage('agents')"><i class="ph ph-users"></i> Agents</div>{% endif %}
  </nav>
  <div class="sidebar-footer">
    <div class="user-chip">
      {% if user.photo_url %}<img class="user-avatar" src="{{ user.photo_url }}" alt="">
      {% else %}<div class="user-avatar-init">{{ user.first_name[0] }}</div>{% endif %}
      <div><div class="user-name">{{ user.first_name }}</div><div class="user-role">{{ user.role if user.role else "Manager" }}</div></div>
    </div>
    <div class="sidebar-actions">
      <button class="theme-btn" onclick="toggleTheme()"><i class="ph ph-sun" id="theme-icon"></i> <span id="theme-label">Light</span></button>
      <button class="logout-btn" onclick="window.location='/logout'"><i class="ph ph-sign-out"></i> Out</button>
    </div>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <h1 id="page-title">Overview</h1>
    <div class="topbar-right">
      <button class="badge-btn btn-outline" onclick="openReport()"><i class="ph ph-file-text"></i> <span>Report</span></button>
      <button class="badge-btn btn-ghost" onclick="window.print()"><i class="ph ph-printer"></i> <span>Print</span></button>
      <a class="badge-btn btn-primary" href="/api/export"><i class="ph ph-download-simple"></i> <span>Export</span></a>
      <div class="live-pill"><div class="dot"></div><span id="last-update">Loading...</span></div>
    </div>
  </div>

  <div class="page active" id="page-overview">
    <div class="stat-grid" id="stat-grid"><div class="loading">Loading...</div></div>
    <div class="two-col">
      <div class="card"><div class="card-title"><i class="ph ph-trophy"></i>Top Assigned Today</div><div id="lb-overview"></div></div>
      <div class="card"><div class="card-title"><i class="ph ph-wrench"></i>Top Problem Units</div><div id="units-overview"></div></div>
    </div>
    <div class="section">
      <div class="section-header"><div class="section-title">Recent Cases</div></div>
      <div class="table-wrap"><div class="table-scroll" id="recent-table"><div class="loading">Loading...</div></div></div>
    </div>
  </div>

  <div class="page" id="page-cases">
    <div class="search-wrap"><i class="ph ph-magnifying-glass"></i><input type="text" id="cases-search" placeholder="Search reported by, group, assigned to..." oninput="onSearch('cases')"></div>
    <div class="section">
      <div class="section-header">
        <div class="section-title">All Cases</div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <div class="filter-tabs">
            <button class="tab-btn active" onclick="setCaseFilter('today',this)">Today</button>
            <button class="tab-btn" onclick="setCaseFilter('week',this)">This Week</button>
            <button class="tab-btn" onclick="setCaseFilter('active',this)">Active</button>
            <button class="tab-btn" onclick="setCaseFilter('all',this)">All</button>
          </div>
          <select id="status-filter" onchange="loadCases()" style="padding:6px 10px;background:var(--surface);border:1px solid var(--border);border-radius:7px;font-size:12px;color:var(--text);font-family:inherit;outline:none;cursor:pointer">
            <option value="">All Statuses</option>
            <option value="open">🔵 Open</option>
            <option value="assigned">🟡 Assigned</option>
            <option value="reported">🟣 Reported</option>
            <option value="done">🟢 Done</option>
            <option value="missed">🔴 Missed</option>
          </select>
          <input type="date" id="cases-date-picker" style="padding:5px 10px;background:var(--surface);border:1px solid var(--border);border-radius:7px;font-size:12px;color:var(--text);font-family:inherit;outline:none" onchange="setCaseDateFilter(this.value)">
          <button class="tab-btn" id="cases-date-clear" onclick="clearDateFilter()" style="display:none">✕</button>
        </div>
      </div>
      <div class="table-wrap"><div class="table-scroll" id="cases-table"><div class="loading">Loading...</div></div></div>
    </div>
  </div>

  <div class="page" id="page-missed">
    <div class="search-wrap"><i class="ph ph-magnifying-glass"></i><input type="text" id="missed-search" placeholder="Search..." oninput="onSearch('missed')"></div>
    <div class="section">
      <div class="section-header"><div class="section-title">Missed Cases</div></div>
      <div class="table-wrap"><div class="table-scroll" id="missed-table"><div class="loading">Loading...</div></div></div>
    </div>
  </div>

  <div class="page" id="page-leaderboard">
    <div class="two-col">
      <div class="card">
        <div class="card-title"><i class="ph ph-trophy"></i>Agent Leaderboard</div>
        <div class="toggle-tabs">
          <button class="toggle-btn active" onclick="setLbPeriod('day',this)">Today</button>
          <button class="toggle-btn" onclick="setLbPeriod('week',this)">Week</button>
          <button class="toggle-btn" onclick="setLbPeriod('month',this)">Month</button>
        </div>
        <div id="leaderboard-full"></div>
      </div>
      <div class="card"><div class="card-title"><i class="ph ph-wrench"></i>Top Problem Units</div><div id="units-lb"></div></div>
    </div>
  </div>

  <div class="page" id="page-analytics">
    <div class="two-col">
      <div class="card">
        <div class="card-title"><i class="ph ph-chart-bar"></i>Period Summary</div>
        <div class="toggle-tabs">
          <button class="toggle-btn active" onclick="setAnalyticsPeriod('week',this)">Week</button>
          <button class="toggle-btn" onclick="setAnalyticsPeriod('month',this)">Month</button>
        </div>
        <div class="stats-list" id="analytics-stats"></div>
      </div>
      <div class="card"><div class="card-title"><i class="ph ph-hash"></i>Top Issue Keywords</div><div id="word-cloud"></div></div>
    </div>
  </div>

  <div class="page" id="page-fleet">
    <div id="fleet-content"><div class="loading">Loading fleet stats...</div></div>
  </div>

  <div class="page" id="page-my_profile">
    <div id="my-profile-content"><div class="loading">Loading...</div></div>
  </div>

  <!-- Trends -->
  <div class="page" id="page-trends">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap">
      <div class="toggle-tabs" style="margin-bottom:0">
        <button class="toggle-btn active" onclick="setTrendPeriod(7,this)">7 Days</button>
        <button class="toggle-btn" onclick="setTrendPeriod(30,this)">30 Days</button>
        <button class="toggle-btn" onclick="setTrendPeriod(90,this)">90 Days</button>
      </div>
    </div>
    <div class="two-col" style="margin-bottom:16px">
      <div class="card"><div class="card-title"><i class="ph ph-chart-line"></i> Cases Over Time</div><canvas id="trend-cases-chart" height="180"></canvas></div>
      <div class="card"><div class="card-title"><i class="ph ph-timer"></i> Avg Response Time</div><canvas id="trend-resp-chart" height="180"></canvas></div>
    </div>
    <div class="card"><div class="card-title"><i class="ph ph-chart-bar"></i> Daily Breakdown</div><canvas id="trend-bar-chart" height="120"></canvas></div>
  </div>

  <!-- Comparison -->
  <div class="page" id="page-comparison">
    <div id="comparison-content"><div class="loading">Loading...</div></div>
  </div>

  <!-- Fleet Intelligence -->
  <div class="page" id="page-fleet_intel">
    <div class="card" style="margin-bottom:16px">
      <div class="card-title"><i class="ph ph-magnifying-glass"></i> Search by Issue</div>
      <div class="toggle-tabs" style="margin-bottom:10px" id="issue-search-vtype-tabs">
        <button class="toggle-btn active" data-vtype="" onclick="setIssueSearchVtype('')">All</button>
        <button class="toggle-btn" data-vtype="truck" onclick="setIssueSearchVtype('truck')">Truck</button>
        <button class="toggle-btn" data-vtype="trailer" onclick="setIssueSearchVtype('trailer')">Trailer</button>
        <button class="toggle-btn" data-vtype="reefer" onclick="setIssueSearchVtype('reefer')">Reefer</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <input id="issue-search-input" type="text" placeholder="e.g. Box temp out of range" style="flex:1;min-width:200px;padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);color:var(--text)" onkeydown="if(event.key==='Enter')searchIssue()">
        <button class="btn" onclick="searchIssue()"><i class="ph ph-magnifying-glass"></i> Search</button>
      </div>
      <div id="issue-search-results"></div>
    </div>
    <div id="fleet-intel-content"><div class="loading">Loading...</div></div>
  </div>

  <!-- Agent Profiles (manager only) -->
  <div class="page" id="page-agents">
    <div id="agents-content"><div class="loading">Loading...</div></div>
  </div>
</main>
</div>

<!-- Case Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
<div class="modal">
  <button class="modal-close" onclick="closeModal()"><i class="ph ph-x"></i></button>
  <h2 id="modal-title">Case Detail</h2>
  <div id="modal-body"><div class="loading">Loading...</div></div>
</div>
</div>

<!-- Report Viewer Modal -->
<div class="modal-overlay" id="report-view-overlay" style="z-index:500" onclick="if(event.target===this)closeReportView()">
<div class="modal" style="max-width:640px">
  <button class="modal-close" onclick="closeReportView()"><i class="ph ph-x"></i></button>
  <h2 id="report-view-title">Case Report</h2>
  <div id="report-view-body"><div class="loading">Loading...</div></div>
  <div style="margin-top:16px;text-align:right">
    <button onclick="printReportView()" style="background:var(--accent);color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px"><i class="ph ph-printer"></i> Print</button>
  </div>
</div>
</div>

<!-- Agent Modal -->
<div class="modal-overlay" id="agent-modal-overlay" style="z-index:200" onclick="if(event.target===this)closeAgentModal()">
<div class="modal">
  <button class="modal-close" onclick="closeAgentModal()"><i class="ph ph-x"></i></button>
  <h2 id="agent-modal-title">Agent Profile</h2>
  <div id="agent-modal-body"><div class="loading">Loading...</div></div>
</div>
</div>

<!-- Unit Detail Modal -->
<div class="modal-overlay" id="unit-modal-overlay" style="z-index:300" onclick="if(event.target===this)closeUnitModal()">
<div class="modal">
  <button class="modal-close" onclick="closeUnitModal()"><i class="ph ph-x"></i></button>
  <h2 id="unit-modal-title">Unit Detail</h2>
  <div id="unit-modal-body"><div class="loading">Loading...</div></div>
</div>
</div>

<!-- Report Modal -->
<div class="report-modal-overlay" id="report-modal-overlay" onclick="if(event.target===this)closeReport()">
<div class="report-modal">
  <div class="report-header">
    <h2><i class="ph ph-file-text" style="color:var(--accent)"></i> Report</h2>
    <div style="display:flex;align-items:center;gap:8px">
      <div class="report-tabs">
        <button class="report-tab active" onclick="setReportTab('today',this)">Today</button>
        <button class="report-tab" onclick="setReportTab('custom',this)">Custom</button>
      </div>
      <button class="report-close" onclick="closeReport()"><i class="ph ph-x"></i></button>
    </div>
  </div>
  <div class="report-body">
    <div id="report-period-bar" class="report-period-bar" style="display:none">
      <select id="report-period-select" onchange="toggleCustomDates()">
        <option value="week">This Week</option>
        <option value="month">This Month</option>
        <option value="custom">Custom Range</option>
      </select>
      <div id="custom-date-inputs" style="display:none;align-items:center;gap:6px">
        <input type="date" id="report-date-from">
        <span style="color:var(--muted);font-size:12px">to</span>
        <input type="date" id="report-date-to">
      </div>
      <button class="report-generate-btn" onclick="generateReport()">Generate</button>
    </div>
    <div id="report-sections-bar" style="display:none;flex-wrap:wrap;gap:4px 16px;padding:10px 12px;margin-bottom:14px;background:var(--surface2);border-radius:10px;font-size:12px">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer;font-weight:700;color:var(--muted);text-transform:uppercase;font-size:10px;letter-spacing:.04em;width:100%;margin-bottom:2px">Include in report:</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="summary" checked onchange="renderReportContent()"> Summary</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="agents" checked onchange="renderReportContent()"> Agent Performance</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="groups" checked onchange="renderReportContent()"> Most Active Groups</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="vtype" checked onchange="renderReportContent()"> Top Issues by Vehicle Type</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="units" checked onchange="renderReportContent()"> Top Problem Units</label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" class="report-section-cb" data-section="missed" checked onchange="renderReportContent()"> Unresolved Alerts</label>
    </div>
    <div id="report-content"><div class="loading">Loading report...</div></div>
  </div>
  <div class="report-footer">
    <span class="ts" id="report-ts"></span>
    <button class="print-report-btn" onclick="printReport()"><i class="ph ph-printer"></i> Print Report</button>
  </div>
</div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
var stats = {};
var currentFilter = 'today';
var currentPage = 'overview';
var lbPeriod = 'day';
var analyticsPeriod = 'week';
var reportTab = 'today';
var currentDateFilter = '';
var searchTimers = {};
var isDark = localStorage.getItem('kurtex-theme') === 'dark';
var bodyLockCount = 0;
var bodyScrollY = 0;
var pages = ['overview','cases','missed','leaderboard','trends','comparison','fleet','fleet_intel','my_profile','agents'];
var titles = {overview:'Overview',cases:'Cases',missed:'Missed Cases',leaderboard:'Leaderboard',trends:'Trends',comparison:'Week Comparison',fleet:'Fleet Stats',fleet_intel:'Fleet Intelligence',my_profile:'My Profile',agents:'Agent Profiles'};
var medals = ['🥇','🥈','🥉'];

// ── Theme ──────────────────────────────────────────────────────────────────
function applyTheme() {
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  var icon = document.getElementById('theme-icon');
  var label = document.getElementById('theme-label');
  if (icon) icon.className = isDark ? 'ph ph-moon' : 'ph ph-sun';
  if (label) label.textContent = isDark ? 'Dark Mode' : 'Light Mode';
}
function toggleTheme() { isDark = !isDark; localStorage.setItem('kurtex-theme', isDark?'dark':'light'); applyTheme(); }
applyTheme();

// ── Sidebar ────────────────────────────────────────────────────────────────
function toggleSidebar() {
  var sb = document.getElementById('sidebar');
  var ov = document.getElementById('sidebar-overlay');
  var isOpen = sb.classList.contains('open');
  if (isOpen) {
    sb.classList.remove('open');
    ov.classList.remove('open');
    document.body.style.overflow = '';
  } else {
    sb.classList.add('open');
    ov.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
  document.body.style.overflow = '';
}

function lockBodyScroll() {
  bodyLockCount += 1;
  if (bodyLockCount === 1) {
    bodyScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.classList.add('modal-open');
    document.body.style.position = 'fixed';
    document.body.style.top = '-' + bodyScrollY + 'px';
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
  }
}

function unlockBodyScroll() {
  bodyLockCount = Math.max(0, bodyLockCount - 1);
  if (bodyLockCount === 0) {
    document.body.classList.remove('modal-open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    window.scrollTo(0, bodyScrollY);
  }
}

function anyModalOpen() {
  return !!document.querySelector('.modal-overlay.open,.report-modal-overlay.open');
}

// ── Navigation ─────────────────────────────────────────────────────────────
function showPage(page) {
  // Always close sidebar first on mobile
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
  document.body.style.overflow = '';
  
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active');});
  document.querySelectorAll('.nav-item').forEach(function(a){a.classList.remove('active');});
  var pg = document.getElementById('page-'+page);
  if (pg) pg.classList.add('active');
  var idx = pages.indexOf(page);
  var navItems = document.querySelectorAll('.nav-item');
  if (navItems[idx]) navItems[idx].classList.add('active');
  var titleEl = document.getElementById('page-title');
  if (titleEl) titleEl.textContent = titles[page] || page;
  currentPage = page;
  localStorage.setItem('kurtex-page', page);
  setTimeout(function(){ refresh(true); }, 50);
}

function setCaseFilter(f, btn) {
  currentFilter = f; currentDateFilter = '';
  document.querySelectorAll('#page-cases .tab-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  document.getElementById('cases-date-clear').style.display = 'none';
  document.getElementById('cases-date-picker').value = '';
  loadCases();
}

function setCaseDateFilter(date) {
  if (!date) return;
  document.querySelectorAll('#page-cases .tab-btn').forEach(function(b){b.classList.remove('active');});
  document.getElementById('cases-date-clear').style.display = '';
  currentFilter = '__date__'; currentDateFilter = date;
  loadCases();
}

function clearDateFilter() {
  document.getElementById('cases-date-picker').value = '';
  document.getElementById('cases-date-clear').style.display = 'none';
  currentDateFilter = ''; currentFilter = 'today';
  var firstTab = document.querySelector('#page-cases .tab-btn');
  if (firstTab) firstTab.classList.add('active');
  loadCases();
}

function setLbPeriod(p, btn) {
  lbPeriod = p;
  document.querySelectorAll('#page-leaderboard .toggle-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  renderLeaderboard();
}

function setAnalyticsPeriod(p, btn) {
  analyticsPeriod = p;
  document.querySelectorAll('#page-analytics .toggle-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  renderAnalytics();
}

function onSearch(type) {
  clearTimeout(searchTimers[type]);
  searchTimers[type] = setTimeout(function(){
    if (type === 'cases') loadCases();
    else if (type === 'missed') loadMissed();
  }, 300);
}

// ── Helpers ────────────────────────────────────────────────────────────────
function statusBadge(s) {
  var map = {open:'s-open',assigned:'s-assigned',reported:'s-reported',done:'s-done',missed:'s-missed'};
  return '<span class="status-badge ' + (map[s]||'s-open') + '">' + h(s) + '</span>';
}

function h(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(ch) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
  });
}

function attr(v) {
  return h(v);
}

function caseTable(cases) {
  if (!cases || !cases.length) return '<div class="empty-state">No cases found</div>';
  var rows = cases.map(function(c) {
    var cid = (c.full_id || '');
    return '<tr onclick="openCase(this.dataset.id)" data-id="' + attr(cid) + '">'
      + '<td><b>' + h(c.driver||'—') + '</b></td>'
      + '<td style="color:var(--muted)">' + h(c.group||'—') + '</td>'
      + '<td>' + h(c.agent||'—') + '</td>'
      + '<td>' + statusBadge(c.status) + (c.reassigned ? '<span class="reassign-badge">reassigned</span>' : '') + '</td>'
      + '<td style="color:var(--muted);font-size:11px">' + h(c.opened||'—') + '</td>'
      + '<td style="font-size:11px">' + h(c.response||'—') + '</td>'
      + '<td class="desc-cell">' + h(c.description||'') + '</td>'
      + '</tr>';
  }).join('');
  return '<table><thead><tr>'
    + '<th>Reported By</th><th>Group</th><th>Assigned To</th><th>Status</th><th>Opened</th><th>Response</th><th>Description</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}
function groupRateRows(groups) {
  if (!groups || !groups.length) return '<div style="color:var(--muted);font-size:13px;padding:8px 0">No data yet</div>';
  var maxTotal = groups[0].total || 1;
  return groups.map(function(g, i) {
    var rateColor = g.rate >= 80 ? 'var(--green)' : g.rate >= 50 ? 'var(--yellow)' : 'var(--red)';
    return '<div class="list-row" style="flex-direction:column;align-items:stretch;gap:4px;padding:8px 0">'
      + '<div style="display:flex;align-items:center;gap:7px">'
      + '<span class="medal">' + (medals[i]||(i+1)+'.') + '</span>'
      + '<span class="list-name" style="font-size:12px;font-weight:600">' + h(g.name) + '</span>'
      + '<span style="margin-left:auto;font-size:11px;font-weight:700;color:'+rateColor+'">' + g.rate + '% ✓</span>'
      + '<span style="font-size:11px;color:var(--muted)">' + g.total + ' cases</span>'
      + '</div>'
      + '<div style="display:flex;gap:3px;height:4px;border-radius:3px;overflow:hidden;background:var(--surface3)">'
      + '<div style="width:'+Math.round(g.done/Math.max(g.total,1)*100)+'%;background:var(--green);transition:width .4s"></div>'
      + '<div style="width:'+Math.round(g.missed/Math.max(g.total,1)*100)+'%;background:var(--red);transition:width .4s"></div>'
      + '</div>'
      + '</div>';
  }).join('');
}

function unitProblemRows(units) {
  if (!units || !units.length) return '<div style="color:var(--muted);font-size:13px;padding:8px 0">No data yet</div>';
  var maxCount = units[0].count || 1;
  var vtypeStyles = {
    truck:   'color:#fff;background:#000',
    trailer: 'color:var(--purple);background:var(--surface2)',
    reefer:  'color:var(--accent);background:var(--surface2)',
  };
  return units.map(function(u, i) {
    var vs = vtypeStyles[(u.vtype||'').toLowerCase()] || 'color:var(--muted);background:var(--surface2)';
    return '<div class="list-row" style="flex-direction:column;align-items:stretch;gap:4px;padding:8px 0;cursor:pointer" data-unit="'+attr(u.unit)+'" data-vtype="'+attr(u.vtype||'')+'" onclick="openUnitModal(this.dataset.unit,this.dataset.vtype)">'
      + '<div style="display:flex;align-items:center;gap:7px">'
      + '<span class="medal">' + (medals[i]||(i+1)+'.') + '</span>'
      + '<span class="list-name" style="font-size:12px;font-weight:600">' + h(u.unit) + '</span>'
      + (u.vtype ? '<span style="font-size:10px;font-weight:700;'+vs+';padding:2px 7px;border-radius:20px;text-transform:capitalize">'+h(u.vtype)+'</span>' : '')
      + '<span style="margin-left:auto;font-size:11px;font-weight:700;color:var(--red)">' + u.count + ' cases</span>'
      + '</div>'
      + '<div style="height:4px;border-radius:3px;overflow:hidden;background:var(--surface3)">'
      + '<div style="width:'+Math.round(u.count/maxCount*100)+'%;background:var(--red);transition:width .4s"></div>'
      + '</div>'
      + '</div>';
  }).join('');
}

function listRows(items, maxCount) {
  if (!items || !items.length) return '<div style="color:var(--muted);font-size:13px;padding:8px 0">No data yet</div>';
  return items.map(function(item, i) {
    return '<div class="list-row">'
      + '<span class="medal">' + (medals[i]||(i+1)+'.') + '</span>'
      + '<span class="list-name">' + h(item.name) + '</span>'
      + '<div class="bar-wrap"><div class="bar-fill" style="width:' + Math.round(item.count/(maxCount||1)*100) + '%"></div></div>'
      + '<span class="list-count">' + item.count + '</span>'
      + '</div>';
  }).join('');
}

function buildTimeline(c) {
  var steps = [
    {label:'Open',     time: c.opened||''},
    {label:'Assigned', time: c.assigned_at||''},
    {label:'Reported', time: ''},
    {label:'Resolved', time: c.closed||''},
  ];
  var order = ['open','assigned','reported','done','missed'];
  var si = Math.max(0, order.indexOf(c.status));
  var html = '<div class="timeline">';
  steps.forEach(function(s, i) {
    var isDone   = i < si;
    var isActive = i === si || (c.status === 'missed' && i === 0);
    var dotClass = isDone ? 'done' : isActive ? 'active' : '';
    html += '<div class="tl-step' + (isDone?' done-step':'') + '">'
      + '<div class="tl-dot ' + dotClass + '">' + (isDone?'✓':(i+1)) + '</div>'
      + '<div class="tl-label">' + s.label + '</div>'
      + '<div class="tl-time">' + (s.time&&s.time!=='—'?s.time:'') + '</div>'
      + '</div>';
  });
  html += '</div>';
  return html;
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadStats() {
  try {
    var r = await fetch('/api/stats');
    if (r.status === 401) { window.location='/login'; return; }
    if (!r.ok) return;
    stats = await r.json();
    var t = stats.today || {};
    var sg = document.getElementById('stat-grid');
    if (sg) sg.innerHTML =
      '<div class="stat-card c-accent"><div class="stat-icon"><i class="ph ph-chart-bar"></i></div><div class="stat-label">Today Total</div><div class="stat-value v-accent">' + (t.total||0) + '</div></div>'
      + '<div class="stat-card c-blue"><div class="stat-icon"><i class="ph ph-user-check"></i></div><div class="stat-label">Assigned</div><div class="stat-value v-blue">' + (t.assigned||0) + '</div></div>'
      + '<div class="stat-card c-green"><div class="stat-icon"><i class="ph ph-check-circle"></i></div><div class="stat-label">Resolved</div><div class="stat-value v-green">' + (t.done||0) + '</div></div>'
      + '<div class="stat-card c-red"><div class="stat-icon"><i class="ph ph-warning-circle"></i></div><div class="stat-label">Missed</div><div class="stat-value v-red">' + (t.missed||0) + '</div></div>'
      + '<div class="stat-card c-purple"><div class="stat-icon"><i class="ph ph-arrows-clockwise"></i></div><div class="stat-label">Reassigned</div><div class="stat-value v-purple">' + (stats.reassigned_count||0) + '</div></div>'
      + '<div class="stat-card c-yellow"><div class="stat-icon"><i class="ph ph-timer"></i></div><div class="stat-label">Avg Response</div><div class="stat-value v-sm v-yellow">' + ((stats.all_time||{}).avg_resp||'—') + '</div></div>';

    var badge = document.getElementById('missed-badge');
    if (badge) { if (t.missed > 0) { badge.textContent = t.missed; badge.style.display=''; } else badge.style.display='none'; }

    var lb = (stats.leaderboard_day||[]).slice(0,5);
    var lbo = document.getElementById('lb-overview');
    if (lbo) lbo.innerHTML = listRows(lb, lb[0]?lb[0].count:1);

    var grps = stats.top_groups||[];
    var units = stats.top_problem_units||[];
    var uo = document.getElementById('units-overview');
    if (uo) uo.innerHTML = unitProblemRows(units);

    renderLeaderboard();
    renderAnalytics();

    var wc = document.getElementById('word-cloud');
    if (wc) wc.innerHTML = (stats.top_words||[]).length
    ? '<div class="word-grid">' + (stats.top_words||[]).map(function(w){return '<span class="word-tag">'+h(w.word)+' <b>'+h(w.count)+'</b></span>';}).join('') + '</div>'
      : '<div style="color:var(--muted);font-size:13px">No hashtag keywords yet</div>';

    var ulb = document.getElementById('units-lb');
    if (ulb) ulb.innerHTML = unitProblemRows(units);
  } catch(e) { console.error('loadStats error:', e); }
}

function renderLeaderboard() {
  if (!stats.leaderboard_day) return;
  var lb = stats['leaderboard_'+lbPeriod] || [];
  var el = document.getElementById('leaderboard-full');
  if (!el) return;
  el.innerHTML = lb.length
    ? lb.map(function(a,i){return '<div class="list-row"><span class="medal">'+(medals[i]||(i+1)+'.')+'</span><span class="list-name">'+h(a.name)+'</span><span class="list-count">'+h(a.count)+' cases</span></div>';}).join('')
    : '<div style="color:var(--muted);font-size:13px;padding:8px 0">No data</div>';
}

function renderAnalytics() {
  if (!stats.week) return;
  var d = analyticsPeriod==='week' ? stats.week : stats.month;
  var rate = d.total ? Math.round(d.done/d.total*100) : 0;
  var el = document.getElementById('analytics-stats');
  if (!el) return;
  el.innerHTML =
    '<div class="row"><span>Total Cases</span><span class="val">'+d.total+'</span></div>'
    + '<div class="row"><span>Resolved</span><span class="val" style="color:var(--green)">'+d.done+'</span></div>'
    + '<div class="row"><span>Missed</span><span class="val" style="color:var(--red)">'+d.missed+'</span></div>'
    + '<div class="row"><span>Resolution Rate</span><span class="val">'+rate+'%</span></div>'
    + '<div class="row"><span>All Time Total</span><span class="val">'+((stats.all_time||{}).total||0)+'</span></div>';
}

var casesOffset = 0;
var casesLimit = 100;
var casesAccum = [];

async function loadCases(append) {
  var el = document.getElementById('cases-table');
  if (!el) return;
  if (!append) { casesOffset = 0; casesAccum = []; el.innerHTML = '<div class="loading">Loading...</div>'; }
  try {
    var search = (document.getElementById('cases-search')||{}).value||'';
    var statusF = (document.getElementById('status-filter')||{}).value||'';
    var base = currentFilter === '__date__'
      ? '/api/cases?date='+currentDateFilter
      : '/api/cases?filter='+currentFilter;
    var url = base + '&search='+encodeURIComponent(search)+'&status='+statusF+'&offset='+casesOffset+'&limit='+casesLimit;
    var r = await fetch(url);
    if (!r.ok) return;
    var d = await r.json();
    casesAccum = append ? casesAccum.concat(d.cases||[]) : (d.cases||[]);
    var countLabel = '<div style="font-size:11px;color:var(--muted);padding:6px 2px">Showing ' + casesAccum.length + ' of ' + d.total + ' cases</div>';
    el.innerHTML = countLabel + caseTable(casesAccum)
      + (d.has_more ? '<div style="text-align:center;margin:12px 0"><button class="btn" style="background:var(--surface2);color:var(--text)" onclick="casesOffset+='+casesLimit+';loadCases(true)"><i class="ph ph-arrow-down"></i> Load More</button></div>' : '');
  } catch(e) { console.error(e); el.innerHTML = '<div class="loading">Error loading cases.</div>'; }
}

var missedOffset = 0;
var missedAccum = [];

async function loadMissed(append) {
  var el = document.getElementById('missed-table');
  if (!el) return;
  if (!append) { missedOffset = 0; missedAccum = []; }
  try {
    var search = (document.getElementById('missed-search')||{}).value||'';
    var r = await fetch('/api/cases?filter=missed&search='+encodeURIComponent(search)+'&offset='+missedOffset+'&limit=100');
    if (!r.ok) return;
    var d = await r.json();
    missedAccum = append ? missedAccum.concat(d.cases||[]) : (d.cases||[]);
    var countLabel = '<div style="font-size:11px;color:var(--muted);padding:6px 2px">Showing ' + missedAccum.length + ' of ' + d.total + ' cases</div>';
    el.innerHTML = countLabel + caseTable(missedAccum)
      + (d.has_more ? '<div style="text-align:center;margin:12px 0"><button class="btn" style="background:var(--surface2);color:var(--text)" onclick="missedOffset+=100;loadMissed(true)"><i class="ph ph-arrow-down"></i> Load More</button></div>' : '');
  } catch(e) { console.error(e); }
}

async function loadFleet() {
  var el = document.getElementById('fleet-content');
  if (!el) return;
  el.innerHTML = '<div class="loading">Loading fleet stats...</div>';
  try {
    var r = await fetch('/api/fleet');
    if (!r.ok) { el.innerHTML = '<div class="loading">Error loading fleet stats.</div>'; return; }
    var d = await r.json();
    window._fleetData = d;
    function unitCard(title, items) {
      if (!items||!items.length) return '<div class="card"><div class="card-title">'+title+'</div><div style="color:var(--muted);font-size:13px">No data yet</div></div>';
      var max = items[0].count||1;
      return '<div class="card"><div class="card-title">'+title+'</div>'
        + items.map(function(item,i){
          return '<div class="list-row"><span class="medal">'+(medals[i]||(i+1)+'.')+'</span>'
            + '<span class="list-name">'+h(item.unit)+(item.vtype?' <span style="font-size:10px;color:var(--muted)">'+h(item.vtype)+'</span>':'')+'</span>'
            + '<div class="bar-wrap"><div class="bar-fill" style="width:'+Math.round(item.count/max*100)+'%"></div></div>'
            + '<span class="list-count">'+item.count+'</span></div>';
        }).join('')
        + '</div>';
    }
    el.innerHTML =
      '<div class="stat-grid" style="margin-bottom:20px">'
      + '<div class="stat-card c-accent" style="cursor:pointer" onclick="setFleetStatusFilter(\\'all\\')" title="Show all cases"><div class="stat-icon"><i class="ph ph-chart-bar"></i></div><div class="stat-label">Total Reports</div><div class="stat-value v-accent">'+d.total_reports+'</div></div>'
      + '<div class="stat-card c-blue" style="cursor:pointer" onclick="setFleetStatusFilter(\\'truck\\')" title="Show truck cases"><div class="stat-icon"><i class="ph ph-truck"></i></div><div class="stat-label">Trucks</div><div class="stat-value v-blue">'+d.truck_count+'</div></div>'
      + '<div class="stat-card c-yellow"><div class="stat-icon"><i class="ph ph-lightning"></i></div><div class="stat-label">Active Units</div><div class="stat-value v-yellow">'+d.active_units+'</div></div>'
      + '<div class="stat-card c-green"><div class="stat-icon"><i class="ph ph-check-circle"></i></div><div class="stat-label">Repaired Units</div><div class="stat-value v-green">'+d.repaired_units+'</div></div>'
      + '</div>'
      + '<div class="stat-grid" style="margin-bottom:20px">'
      + '<div class="stat-card c-yellow" style="cursor:pointer" onclick="setFleetStatusFilter(\\'trailer\\')" title="Show trailer cases"><div class="stat-icon"><i class="ph ph-package"></i></div><div class="stat-label">Trailers</div><div class="stat-value v-yellow">'+d.trailer_count+'</div></div>'
      + '<div class="stat-card c-purple" style="cursor:pointer" onclick="setFleetStatusFilter(\\'reefer\\')" title="Show reefer cases"><div class="stat-icon"><i class="ph ph-snowflake"></i></div><div class="stat-label">Reefers</div><div class="stat-value v-purple">'+d.reefer_count+'</div></div>'
      + '</div>'
      + '<div id="fleet-status-wrap"></div>'
      + '<div class="two-col" style="margin-bottom:16px">'
      + unitCard('<i class="ph ph-truck"></i> Trucks Breaking Down Most', d.top_broken_trucks)
      + unitCard('<i class="ph ph-user"></i> Most Reported Drivers', d.top_drivers)
      + '</div>'
      + '<div class="two-col">'
      + unitCard('<i class="ph ph-wrench"></i> Most Reported Units', d.top_units)
      + unitCard('<i class="ph ph-warning"></i> Top Issues', d.top_issues)
      + '</div>';
    fleetStatusState = { vtype: 'all', search: '' };
    renderFleetStatus();
  } catch(e) { console.error(e); el.innerHTML = '<div class="loading">Error.</div>'; }
}

var fleetStatusState = { vtype: 'all', search: '' };

function setFleetStatusFilter(vtype) {
  fleetStatusState.vtype = vtype;
  renderFleetStatus();
  var wrap = document.getElementById('fleet-status-wrap');
  if (wrap) wrap.scrollIntoView({behavior:'smooth', block:'start'});
}

function fleetStatusFilteredHtml(s) {
  var items = (window._fleetData && window._fleetData.fleet_status) || [];
  var q = (s.search||'').toLowerCase().trim();
  var filtered = items.filter(function(item) {
    if (s.vtype !== 'all' && (item.vtype||'').toLowerCase() !== s.vtype) return false;
    if (q && (item.unit||'').toLowerCase().indexOf(q)===-1 && (item.issue||'').toLowerCase().indexOf(q)===-1 && (item.driver||'').toLowerCase().indexOf(q)===-1) return false;
    return true;
  });
  var rows = filtered.map(function(item) {
    var badge = item.status === 'active'
      ? '<span class="status-badge s-reported">active</span>'
      : '<span class="status-badge s-done">repaired</span>';
    return '<tr style="cursor:pointer" data-unit="'+attr(item.unit)+'" data-vtype="'+attr(item.vtype||'')+'" onclick="openUnitModal(this.dataset.unit, this.dataset.vtype)" title="Click to see all cases for unit '+attr(item.unit)+'">'
      + '<td><b>'+h(item.unit)+'</b><div style="font-size:10px;color:var(--muted);text-transform:uppercase">'+h(item.vtype)+'</div></td>'
      + '<td>'+badge+'</td>'
      + '<td>'+h(item.issue)+'</td>'
      + '<td>'+h(item.driver)+'</td>'
      + '<td>'+h(item.opened)+'</td>'
      + '</tr>';
  }).join('');
  return filtered.length
    ? '<div class="table-wrap"><div class="table-scroll"><table><thead><tr><th>Unit</th><th>Status</th><th>Issue</th><th>Driver</th><th>Opened</th></tr></thead><tbody>' + rows + '</tbody></table></div></div>'
    : '<div style="color:var(--muted);font-size:13px;padding:20px 0;text-align:center">No units match this filter.</div>';
}

// Only refreshes the results area (used while typing) so the search input
// itself is never destroyed/recreated and keeps focus + cursor position.
function updateFleetStatusResults() {
  var results = document.getElementById('fleet-status-results');
  if (!results || !window._fleetData) return;
  results.innerHTML = fleetStatusFilteredHtml(fleetStatusState);
}

function renderFleetStatus() {
  var wrap = document.getElementById('fleet-status-wrap');
  if (!wrap || !window._fleetData) return;
  var s = fleetStatusState;
  function vbtn(v, label) {
    return '<button class="toggle-btn'+(s.vtype===v?' active':'')+'" onclick="fleetStatusState.vtype=\\''+v+'\\';renderFleetStatus()">'+label+'</button>';
  }
  wrap.innerHTML =
    '<div class="card" style="margin-bottom:16px">'
    + '<div class="card-title"><i class="ph ph-activity"></i> Fleet Status <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:4px">— click a unit to view history</span></div>'
    + '<div class="toggle-tabs" style="margin-bottom:10px">' + vbtn('all','All') + vbtn('truck','Truck') + vbtn('trailer','Trailer') + vbtn('reefer','Reefer') + '</div>'
    + '<div class="search-wrap" style="margin-bottom:12px"><i class="ph ph-magnifying-glass"></i><input type="text" id="fleet-status-search" placeholder="Search unit, issue, or driver..." value="'+attr(s.search||'')+'" oninput="fleetStatusState.search=this.value;updateFleetStatusResults()"></div>'
    + '<div id="fleet-status-results">' + fleetStatusFilteredHtml(s) + '</div>'
    + '</div>';
}


async function loadMyProfile() {
  var el = document.getElementById('my-profile-content');
  if (!el) return;
  el.innerHTML = '<div class="loading">Loading...</div>';
  try {
    var r = await fetch('/api/my_profile');
    if (!r.ok) { el.innerHTML = '<div class="loading">Error loading profile.</div>'; return; }
    var p = await r.json();
    el.innerHTML =
      '<div class="two-col" style="margin-bottom:16px">'
      + '<div class="card">'
      + '<div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">'
      + '<div style="width:52px;height:52px;border-radius:50%;background:var(--accent-bg);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:var(--accent);flex-shrink:0">'+h((p.name||'?')[0])+'</div>'
      + '<div><div style="font-size:17px;font-weight:700">'+h(p.name)+'</div><div style="font-size:12px;color:var(--muted)">'+(p.username?'@'+h(p.username)+' · ':'')+h(p.role)+'</div></div>'
      + '</div>'
      + '<div class="mini-stat-grid">'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--green)">'+p.done+'</div><div class="agent-stat-label">Resolved</div></div>'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--red)">'+p.missed+'</div><div class="agent-stat-label">Missed</div></div>'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--accent)">'+p.rate+'%</div><div class="agent-stat-label">Rate</div></div>'
      + '</div></div>'
      + '<div class="card"><div class="card-title">Period Breakdown</div><div class="stats-list">'
      + '<div class="row"><span>Today assigned</span><span class="val">'+p.today_total+'</span></div>'
      + '<div class="row"><span>Today resolved</span><span class="val" style="color:var(--green)">'+p.today_done+'</span></div>'
      + '<div class="row"><span>This week assigned</span><span class="val">'+p.week_total+'</span></div>'
      + '<div class="row"><span>This week resolved</span><span class="val" style="color:var(--green)">'+p.week_done+'</span></div>'
      + '<div class="row"><span>Avg response</span><span class="val">'+p.avg_resp+'</span></div>'
      + '</div></div></div>'
      + '<div class="section-title" style="margin-bottom:10px">Recent Cases</div>'
      + '<div class="table-wrap"><div class="table-scroll">'+caseTable(p.recent)+'</div></div>';
  } catch(e) { console.error(e); el.innerHTML = '<div class="loading">Error.</div>'; }
}

async function loadAgents() {
  var el = document.getElementById('agents-content');
  if (!el) return;
  el.innerHTML = '<div class="loading">Loading...</div>';
  try {
    var r = await fetch('/api/agents');
    if (r.status === 403) { el.innerHTML = '<div class="loading">Access denied.</div>'; return; }
    if (!r.ok) { el.innerHTML = '<div class="loading">Error loading agents.</div>'; return; }
    var agents = await r.json();
    if (!agents.length) { el.innerHTML = '<div class="empty-state">No agents found.</div>'; return; }
    var cards = agents.map(function(a, i) {
      var init = (a.name||'?')[0].toUpperCase();
      var rate = a.rate || 0;
      var rateColor = rate >= 80 ? 'var(--green)' : rate >= 50 ? 'var(--accent)' : 'var(--red)';
      return '<div class="card agent-card" data-agent="' + attr(a.name||'') + '" data-username="' + attr(a.username||'') + '" onclick="openAgentModal(this.dataset.agent, this.dataset.username)">'
        + '<div class="agent-card-rank">#' + (i+1) + '</div>'
        + '<div style="display:flex;align-items:center;gap:14px;margin-bottom:18px;padding-right:36px">'
        + '<div class="agent-card-avatar">' + init + '</div>'
        + '<div style="min-width:0"><div style="font-size:17px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + h(a.name||'') + '</div>'
        + '<div style="font-size:12px;color:var(--muted)">' + (a.username?'@'+h(a.username):'No username') + '</div></div>'
        + '</div>'
        + '<div class="mini-stat-grid">'
        + '<div class="agent-card-statbox"><div class="agent-card-statval" style="color:var(--accent)">' + (a.total||0) + '</div><div class="agent-card-statlabel">Total</div></div>'
        + '<div class="agent-card-statbox"><div class="agent-card-statval" style="color:var(--green)">' + (a.done||0) + '</div><div class="agent-card-statlabel">Done</div></div>'
        + '<div class="agent-card-statbox"><div class="agent-card-statval" style="color:var(--red)">' + (a.missed||0) + '</div><div class="agent-card-statlabel">Missed</div></div>'
        + '<div class="agent-card-statbox"><div class="agent-card-statval" style="color:' + rateColor + '">' + rate + '%</div><div class="agent-card-statlabel">Rate</div></div>'
        + '</div>'
        + '<div class="agent-rate-track"><div class="agent-rate-fill" style="width:' + Math.min(rate,100) + '%"></div></div>'
        + '<div class="agent-card-footer" style="justify-content:flex-end">'
        + '<span style="color:var(--accent);font-weight:700;display:flex;align-items:center;gap:4px">View Cases <i class="ph ph-arrow-right"></i></span>'
        + '</div>'
        + '</div>';
    }).join('')
    el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px">' + cards + '</div>';
  } catch(e) { console.error(e); el.innerHTML = '<div class="loading">Error: '+h(e.message)+'</div>'; }
}

// ── Modals ─────────────────────────────────────────────────────────────────
async function openCase(el) {
  var caseId = (typeof el === 'string') ? el : el.dataset.id;
  document.getElementById('modal-overlay').classList.add('open');
  lockBodyScroll();
  document.getElementById('modal-body').innerHTML = '<div class="loading">Loading...</div>';
  document.getElementById('modal-title').textContent = 'Loading...';
  try {
    var r = await fetch('/api/case?id='+encodeURIComponent(caseId));
    if (!r.ok) { document.getElementById('modal-body').innerHTML = '<div class="loading">Case not found.</div>'; return; }
    var c = await r.json();
    document.getElementById('modal-title').textContent = (c.driver||'—') + ' — ' + (c.group||'—');
    var extra = '';
    if (c.vehicle_type) {
      extra += '<div class="detail-grid" style="margin-bottom:14px">'
      + '<div class="detail-item"><div class="detail-label">Vehicle Type</div><div class="detail-val">'+h(c.vehicle_type||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Unit Number</div><div class="detail-val">'+h(c.unit_number||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Priority</div><div class="detail-val">'+h(c.priority||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Load Type</div><div class="detail-val">'+h(c.load_type||'—')+'</div></div>'
        + '</div>';
    }
    document.getElementById('modal-body').innerHTML =
      buildTimeline(c)
      + '<div class="detail-grid">'
      + '<div class="detail-item"><div class="detail-label">Status</div><div class="detail-val">'+statusBadge(c.status)+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Assigned To</div><div class="detail-val">'+h(c.agent||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Reported By</div><div class="detail-val">'+h(c.driver||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Group</div><div class="detail-val">'+h(c.group||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Opened</div><div class="detail-val">'+h(c.opened||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Assigned At</div><div class="detail-val">'+h(c.assigned_at||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Response Time</div><div class="detail-val">'+h(c.response||'—')+'</div></div>'
      + '<div class="detail-item"><div class="detail-label">Resolution Time</div><div class="detail-val">'+h(c.resolution_secs||'—')+'</div></div>'
      + '</div>'
      + extra
      + (c.full_description ? '<div class="desc-box"><span class="box-label">Issue Description</span><p class="box-text">'+h(c.full_description)+'</p></div>' : '')
      + (c.full_notes ? '<div class="notes-box"><span class="box-label">Report / Notes</span><p class="box-text">'+h(c.full_notes)+'</p></div>' : '')
      + ((c.status === 'reported' || c.status === 'done')
        ? '<div style="margin-top:14px;text-align:center"><button data-id="' + attr(c.full_id) + '" onclick="viewFullReport(this.dataset.id)" style="background:var(--accent);color:#fff;border:none;border-radius:10px;padding:10px 24px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:8px"> View Full Report</button></div>'
        : '');
  } catch(e) {
    console.error('openCase error:', e);
    document.getElementById('modal-body').innerHTML = '<div class="loading">Error loading case.</div>';
  }
}
function closeModal() {
  var overlay = document.getElementById('modal-overlay');
  if (overlay.classList.contains('open')) {
    overlay.classList.remove('open');
    unlockBodyScroll();
  }
}

async function viewFullReport(caseIdOrEl) {
  var caseId = (typeof caseIdOrEl === "string") ? caseIdOrEl : caseIdOrEl.dataset.id;
  document.getElementById('report-view-overlay').classList.add('open');
  lockBodyScroll();
  document.getElementById('report-view-body').innerHTML = '<div class="loading">Loading report...</div>';
  try {
    var r = await fetch('/api/case?id='+encodeURIComponent(caseId));
    if (!r.ok) { document.getElementById('report-view-body').innerHTML = '<div class="loading">Error loading report.</div>'; return; }
    var c = await r.json();
    document.getElementById('report-view-title').textContent = 'Report — ' + (c.driver||'—') + ' / ' + (c.group||'—');

    // case data loaded

    // Parse the notes field which contains the bot report text
    var notes = c.full_notes || '';
    var isRealReport = notes && notes !== 'case reported';

    // Build report rows from case data
    var vtype = c.vehicle_type || '';
    var unitLabel = vtype === 'truck' ? 'Truck' : vtype === 'trailer' ? 'Trailer' : vtype === 'reefer' ? 'Reefer' : 'Unit';

    function row(label, val) {
      if (!val || val === '—') return '';
      return '<div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">'
        + '<div style="min-width:160px;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;flex-shrink:0">'+h(label)+'</div>'
        + '<div style="font-size:14px;font-weight:500;color:var(--text)">'+h(val)+'</div>'
        + '</div>';
    }

    // Priority icon
    var priorityIcon = c.priority === 'critical' ? '🔴' : c.priority === 'high' ? '🟠' : c.priority === 'medium' ? '🟡' : '🟢';
    var priorityLabel = c.priority ? (priorityIcon + ' ' + c.priority.charAt(0).toUpperCase() + c.priority.slice(1)) : '';

    // Priority
    var pIcons = {critical:'🔴',high:'🟠',medium:'🟡',low:'🟢'};
    var pIcon = pIcons[c.priority] || '🟢';
    var pText = c.priority ? c.priority.charAt(0).toUpperCase()+c.priority.slice(1) : 'Low';

    // Unit label
    var vtypeLabel = vtype === 'truck' ? 'Truck' : vtype === 'trailer' ? 'Trailer' : vtype === 'reefer' ? 'Reefer' : '';

    // Load section
    var loadType = c.load_type || '';
    var loadIsEmpty = loadType.toLowerCase() === 'empty';

    function line(label, val) {
      if (!val || val === '—') return '';
      return '<div style="font-size:13px;margin-bottom:6px"><b>' + h(label) + ':</b> ' + h(val) + '</div>';
    }

    // Build report exactly like Telegram bot
    var s = '<div style="font-size:15px;font-weight:700;margin-bottom:2px">'
      + pIcon + ' Case Report';
    if (vtypeLabel) s += ' — ' + vtypeLabel;
    s += '</div>';
    s += '<div style="font-size:13px;margin-bottom:16px">Priority: <b>' + pText + '</b></div>';

    s += '<div style="margin-bottom:16px">';
    if (vtypeLabel && c.unit_number) s += line(vtypeLabel, c.unit_number);
    s += line('Reported by', c.report_driver || c.driver);
    s += line('Issue', c.issue_text || c.full_description);
    s += '</div>';

    if (loadType) {
      s += '<div style="margin-bottom:16px">';
      s += line('JBS/Broker Load', loadType);
      if (!loadIsEmpty) {
        s += line('Pick up Location/Time', c.pickup);
        s += line('Delivery Location/Time', c.delivery);
      }
      s += line('Current Location', c.location);
      s += '</div>';
    }

    if (vtype === 'reefer') {
      s += '<div style="margin-bottom:16px">';
      s += line('Setpoint', c.setpoint);
      s += line('Current temp', c.current_temp);
      s += line('Temp recorder', c.temp_recorder);
      s += '</div>';
    }

    if (c.comments) {
      s += '<div style="margin-bottom:16px">' + line('Comments', c.comments) + '</div>';
    }

    s += line('Handled by', c.agent);

    document.getElementById('report-view-body').innerHTML =
      '<div style="background:var(--surface2);border-radius:12px;padding:18px 20px;font-family:inherit;line-height:1">' + s + '</div>';
  } catch(e) {
    document.getElementById('report-view-body').innerHTML = '<div class="loading">Error loading report.</div>';
  }
}

function closeReportView() {
  var overlay = document.getElementById('report-view-overlay');
  if (overlay.classList.contains('open')) {
    overlay.classList.remove('open');
    unlockBodyScroll();
  }
}

function printReportView() {
  var orig = document.title;
  document.title = document.getElementById('report-view-title').textContent;
  window.print();
  document.title = orig;
}

var agentModalState = { name: '', username: '', period: 'all', offset: 0, limit: 15, rows: '' };

function agentCaseRow(c) {
  var cid = c.full_id || '';
  return '<tr style="border-bottom:1px solid var(--border);cursor:pointer" data-id="' + attr(cid) + '" onclick="closeAgentModal();var id=this.dataset.id;setTimeout(function(){openCase(id);},200)">'
    + '<td style="padding:8px 10px;font-weight:500">' + h(c.driver||'—') + '</td>'
    + '<td style="padding:8px 10px;color:var(--muted)">' + h(c.group||'—') + '</td>'
    + '<td style="padding:8px 10px">' + statusBadge(c.status) + '</td>'
    + '<td style="padding:8px 10px;color:var(--muted);font-size:11px">' + h(c.opened||'—') + '</td>'
    + '</tr>';
}

async function openAgentModal(nameOrEl, username) {
  var name = (typeof nameOrEl === 'string') ? nameOrEl : nameOrEl.dataset.agent;
  var uname = (typeof nameOrEl === 'string') ? (username||'') : (nameOrEl.dataset.username||'');
  agentModalState = { name: name, username: uname, period: 'all', offset: 0, limit: 15, rows: '' };
  document.getElementById('agent-modal-overlay').classList.add('open');
  lockBodyScroll();
  document.getElementById('agent-modal-body').innerHTML = '<div class="loading">Loading profile...</div>';
  document.getElementById('agent-modal-title').textContent = name;
  await loadAgentProfileData(true);
}

function setAgentPeriod(period) {
  if (agentModalState.period === period) return;
  agentModalState.period = period;
  agentModalState.offset = 0;
  agentModalState.rows = '';
  loadAgentProfileData(true);
}

async function loadAgentModalMore() {
  agentModalState.offset += agentModalState.limit;
  await loadAgentProfileData(false);
}

async function loadAgentProfileData(resetHeader) {
  var body = document.getElementById('agent-modal-body');
  var s = agentModalState;
  if (resetHeader) body.innerHTML = '<div class="loading">Loading profile...</div>';
  try {
    var url = '/api/agent?name=' + encodeURIComponent(s.name)
      + '&username=' + encodeURIComponent(s.username||'')
      + '&period=' + encodeURIComponent(s.period)
      + '&offset=' + s.offset + '&limit=' + s.limit;
    var r = await fetch(url);
    if (!r.ok) { body.innerHTML = '<div class="loading">Agent not found.</div>'; return; }
    var a = await r.json();
    var ps = a.period_stats || {total:0,done:0,missed:0,rate:0};
    var newRows = (a.cases || []).map(agentCaseRow).join('');
    s.rows = s.offset > 0 ? s.rows + newRows : newRows;
    var periodLabels = {today:'Today', week:'This Week', month:'This Month', all:'All Time'};
    function tab(p) {
      return '<button class="toggle-btn' + (s.period===p?' active':'') + '" onclick="setAgentPeriod(\\'' + p + '\\')">' + periodLabels[p] + '</button>';
    }
    body.innerHTML =
      '<div class="mini-stat-grid" style="margin-bottom:16px">'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--accent)">'+a.total+'</div><div class="agent-stat-label">Total (all-time)</div></div>'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--green)">'+a.done+'</div><div class="agent-stat-label">Resolved</div></div>'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--red)">'+a.missed+'</div><div class="agent-stat-label">Missed</div></div>'
      + '<div class="agent-stat"><div class="agent-stat-val" style="color:var(--accent)">'+a.rate+'%</div><div class="agent-stat-label">Rate</div></div>'
      + '</div>'
      + '<div class="toggle-tabs" style="margin-bottom:10px">' + tab('today') + tab('week') + tab('month') + tab('all') + '</div>'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:4px">'
      + '<div style="font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Cases — ' + periodLabels[s.period] + '</div>'
      + '<div style="font-size:11px;color:var(--muted)">' + ps.total + ' total &middot; ' + ps.done + ' done &middot; ' + ps.missed + ' missed</div>'
      + '</div>'
      + (s.rows
        ? '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px;min-width:400px">'
          + '<thead><tr style="background:var(--surface2);border-bottom:1px solid var(--border)">'
          + '<th style="padding:8px 10px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Reported By</th>'
          + '<th style="padding:8px 10px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Group</th>'
          + '<th style="padding:8px 10px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Status</th>'
          + '<th style="padding:8px 10px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Date</th>'
          + '</tr></thead><tbody id="agent-case-rows">' + s.rows + '</tbody></table></div>'
          + (a.has_more ? '<div style="text-align:center;margin-top:12px"><button class="btn" style="background:var(--surface2);color:var(--text)" onclick="loadAgentModalMore()"><i class="ph ph-arrow-down"></i> Load More</button></div>' : '')
        : '<div style="color:var(--muted);font-size:13px;padding:20px 0;text-align:center">No cases in this period.</div>');
  } catch(e) {
    console.error('agent modal error:', e);
    body.innerHTML = '<div class="loading">Error loading profile.</div>';
  }
}
function closeAgentModal() {
  var overlay = document.getElementById('agent-modal-overlay');
  if (overlay.classList.contains('open')) {
    overlay.classList.remove('open');
    unlockBodyScroll();
  }
}

// ── Unit Modal ─────────────────────────────────────────────────────────────
async function openUnitModal(unitNumber, vtype) {
  var overlay = document.getElementById('unit-modal-overlay');
  var body = document.getElementById('unit-modal-body');
  var title = document.getElementById('unit-modal-title');
  overlay.classList.add('open');
  lockBodyScroll();
  body.innerHTML = '<div class="loading">Loading unit history...</div>';
  title.textContent = 'Unit ' + unitNumber;
  try {
    var url = '/api/unit?unit=' + encodeURIComponent(unitNumber);
    if (vtype) url += '&vtype=' + encodeURIComponent(vtype);
    var r = await fetch(url);
    if (!r.ok) { body.innerHTML = '<div class="loading">Error loading unit data.</div>'; return; }
    var d = await r.json();
    var vtypeLabel = d.vtype ? ' <span style="font-size:11px;color:var(--muted);text-transform:uppercase;background:var(--surface2);padding:2px 7px;border-radius:5px">'+h(d.vtype)+'</span>' : '';
    title.innerHTML = 'Unit ' + h(unitNumber) + vtypeLabel;
    var issuesHtml = '';
    if (d.top_issues && d.top_issues.length) {
      issuesHtml = '<div style="margin-bottom:16px"><div style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--muted);margin-bottom:8px;letter-spacing:.05em">Top Issues</div>'
        + d.top_issues.map(function(x){
          return '<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px">'
            + '<span style="flex:1">' + h(x.issue||'—') + '</span>'
            + '<span style="font-weight:700;color:var(--accent);background:var(--accent-bg);padding:1px 8px;border-radius:20px;font-size:11px">' + h(x.count) + 'x</span>'
            + '</div>';
        }).join('')
        + '</div>';
    }
    var statsHtml = '<div class="mini-stat-grid" style="margin-bottom:16px">'
      + '<div class="stat-card"><div class="stat-label">Total</div><div class="stat-value v-accent" style="font-size:20px">' + d.total + '</div></div>'
      + '<div class="stat-card"><div class="stat-label">Active</div><div class="stat-value v-yellow" style="font-size:20px">' + d.active + '</div></div>'
      + '<div class="stat-card"><div class="stat-label">Resolved</div><div class="stat-value v-green" style="font-size:20px">' + d.done + '</div></div>'
      + '<div class="stat-card"><div class="stat-label">Missed</div><div class="stat-value v-red" style="font-size:20px">' + d.missed + '</div></div>'
      + '</div>';
    var rows = '';
    if (d.cases && d.cases.length) {
      rows = '<div style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--muted);margin-bottom:8px;letter-spacing:.05em">All Cases</div>'
        + '<div class="table-wrap"><div class="table-scroll">'
        + '<table><thead><tr><th>Reported By</th><th>Status</th><th>Opened</th><th>Response</th><th>Description</th></tr></thead><tbody>'
        + d.cases.map(function(c){
            var cid = c.full_id || '';
            return '<tr style="cursor:pointer" onclick="closeUnitModal();var id=this.dataset.id;setTimeout(function(){openCase(id);},200)" data-id="'+attr(cid)+'">'
              + '<td><b>'+h(c.driver||'—')+'</b></td>'
              + '<td>'+statusBadge(c.status)+'</td>'
              + '<td style="font-size:11px;color:var(--muted)">'+h(c.opened||'—')+'</td>'
              + '<td style="font-size:11px">'+h(c.response||'—')+'</td>'
              + '<td class="desc-cell">'+h(c.description||'')+'</td>'
              + '</tr>';
          }).join('')
        + '</tbody></table></div></div>';
    } else {
      rows = '<div class="empty-state">No cases found for this unit.</div>';
    }
    body.innerHTML = statsHtml + issuesHtml + rows;
  } catch(e) {
    body.innerHTML = '<div class="loading">Error: ' + h(e.message) + '</div>';
  }
}
function closeUnitModal() {
  var overlay = document.getElementById('unit-modal-overlay');
  if (overlay.classList.contains('open')) {
    overlay.classList.remove('open');
    unlockBodyScroll();
  }
}

// ── Report ─────────────────────────────────────────────────────────────────
function openReport() {
  document.getElementById('report-modal-overlay').classList.add('open');
  lockBodyScroll();
  reportTab = 'today';
  document.querySelectorAll('.report-tab').forEach(function(b,i){b.classList.toggle('active',i===0);});
  document.getElementById('report-period-bar').style.display = 'none';
  document.getElementById('report-sections-bar').style.display = 'none';
  generateReport();
}
function closeReport() {
  var overlay = document.getElementById('report-modal-overlay');
  if (overlay.classList.contains('open')) {
    overlay.classList.remove('open');
    unlockBodyScroll();
  }
}
function setReportTab(tab, btn) {
  reportTab = tab;
  document.querySelectorAll('.report-tab').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  document.getElementById('report-period-bar').style.display = tab==='custom' ? 'flex' : 'none';
  if (tab==='today') generateReport();
}
function toggleCustomDates() {
  var v = document.getElementById('report-period-select').value;
  document.getElementById('custom-date-inputs').style.display = v==='custom' ? 'flex' : 'none';
}
async function generateReport() {
  document.getElementById('report-content').innerHTML = '<div class="loading">Generating report...</div>';
  document.getElementById('report-sections-bar').style.display = 'none';
  var url = '/api/report?period=today';
  if (reportTab === 'custom') {
    var period = document.getElementById('report-period-select').value;
    if (period === 'custom') {
      var from = document.getElementById('report-date-from').value;
      var to   = document.getElementById('report-date-to').value;
      if (!from) { document.getElementById('report-content').innerHTML = '<div class="loading">Please select a start date.</div>'; return; }
      url = '/api/report?period=custom&from='+from+'&to='+(to||from);
    } else {
      url = '/api/report?period='+period;
    }
  }
  try {
    var r = await fetch(url);
    if (!r.ok) { document.getElementById('report-content').innerHTML = '<div class="loading">Error generating report.</div>'; return; }
    window._reportData = await r.json();
    document.getElementById('report-sections-bar').style.display = 'flex';
    renderReportContent();
  } catch(e) { document.getElementById('report-content').innerHTML = '<div class="loading">Error.</div>'; }
}

function reportSectionEnabled(name) {
  var cb = document.querySelector('.report-section-cb[data-section="'+name+'"]');
  return !cb || cb.checked;
}

function renderReportContent() {
  var d = window._reportData;
  if (!d) return;
  document.getElementById('report-ts').textContent = 'Generated ' + new Date().toLocaleString('en-US',{timeZone:'America/Chicago'}) + ' CT';
  var now = new Date();
  var dateStr = now.toLocaleDateString('en-US',{timeZone:'America/Chicago',weekday:'long',year:'numeric',month:'long',day:'numeric'});
  var timeStr = now.toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour:'2-digit',minute:'2-digit'}) + ' CT';
  var resRate = d.total ? Math.round((d.total-d.missed)/d.total*100) : 0;
  var vtypeLabels = {truck:'Trucks', trailer:'Trailers', reefer:'Reefers'};
  var vtypeIcons = {truck:'🚚', trailer:'📦', reefer:'❄️'};
  var html = '';

  // Official letterhead — always shown
  html += '<div style="text-align:center;border-bottom:3px double var(--accent);padding-bottom:16px;margin-bottom:22px">'
    + '<div style="font-size:25px;font-weight:900;letter-spacing:.06em;color:var(--text)">KURTEX MAINTENANCE</div>'
    + '<div style="font-size:12px;font-weight:600;letter-spacing:.1em;color:var(--accent);text-transform:uppercase;margin-top:3px">Official Fleet Operations Report</div>'
    + '<div style="display:flex;justify-content:center;gap:28px;margin-top:16px;flex-wrap:wrap">'
    + '<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Report Period</div><div style="font-size:14px;font-weight:700">'+h(d.label)+'</div></div>'
    + '<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Generated</div><div style="font-size:14px;font-weight:700">'+dateStr+'</div><div style="font-size:11px;color:var(--muted)">'+timeStr+'</div></div>'
    + '</div></div>';

  if (reportSectionEnabled('summary')) {
    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:20px">'
      + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">'
      + '<div style="font-size:28px;font-weight:800;color:var(--accent)">'+d.total+'</div>'
      + '<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:3px">Total Alerts</div>'
      + '</div>'
      + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">'
      + '<div style="font-size:28px;font-weight:800;color:var(--green)">'+d.done+'</div>'
      + '<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:3px">Resolved</div>'
      + '</div>'
      + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">'
      + '<div style="font-size:28px;font-weight:800;color:var(--red)">'+d.missed+'</div>'
      + '<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:3px">Missed</div>'
      + '</div>'
      + '</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px">'
      + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 16px;display:flex;align-items:center;justify-content:space-between">'
      + '<span style="font-size:12px;color:var(--muted);font-weight:500">Resolution Rate</span>'
      + '<span style="font-size:18px;font-weight:800;color:'+(resRate>=80?'var(--green)':resRate>=60?'var(--yellow)':'var(--red)')+'">'+resRate+'%</span>'
      + '</div>'
      + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 16px;display:flex;align-items:center;justify-content:space-between">'
      + '<span style="font-size:12px;color:var(--muted);font-weight:500">Avg Response Time</span>'
      + '<span style="font-size:18px;font-weight:800;color:var(--text)">'+d.avg_resp+'</span>'
      + '</div>'
      + '</div>';
  }

  if (reportSectionEnabled('agents') && d.leaderboard.length) {
    html += '<div style="margin-bottom:20px">'
      + '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">Agent Performance</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:13px">'
      + '<thead><tr>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">#</th>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Agent</th>'
      + '<th style="text-align:right;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Cases</th>'
      + '</tr></thead><tbody>'
      + d.leaderboard.map(function(a,i){
          return '<tr style="border-top:1px solid var(--border)">'
            + '<td style="padding:8px;font-weight:700;color:var(--muted);width:30px">'+(i+1)+'.</td>'
            + '<td style="padding:8px;font-weight:500">'+(medals[i]?medals[i]+' ':'')+h(a.name)+'</td>'
            + '<td style="padding:8px;text-align:right;font-weight:700;color:var(--accent)">'+h(a.count)+'</td>'
            + '</tr>';
        }).join('')
      + '</tbody></table></div>';
  }

  if (reportSectionEnabled('groups') && d.top_groups.length) {
    html += '<div style="margin-bottom:20px">'
      + '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">Most Active Groups</div>'
      + d.top_groups.map(function(g,i){
          var maxCount = d.top_groups[0].count;
          var pct = Math.round(g.count/maxCount*100);
          return '<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--border)">'
            + '<span style="font-size:12px;font-weight:500;width:180px;flex-shrink:0">'+h(g.name)+'</span>'
            + '<div style="flex:1;height:5px;background:var(--surface3);border-radius:3px">'
            + '<div style="height:100%;border-radius:3px;background:var(--accent);width:'+pct+'%"></div></div>'
            + '<span style="font-size:12px;font-weight:700;color:var(--accent);width:30px;text-align:right">'+h(g.count)+'</span>'
            + '</div>';
        }).join('')
      + '</div>';
  }

  if (reportSectionEnabled('vtype') && d.by_vtype) {
    var vtypeBlocks = ['truck','trailer','reefer'].map(function(vt) {
      var vd = d.by_vtype[vt];
      if (!vd || !vd.total) return '';
      return '<div style="margin-bottom:14px">'
        + '<div style="font-size:12px;font-weight:700;margin-bottom:6px">'+vtypeIcons[vt]+' '+vtypeLabels[vt]+' <span style="color:var(--muted);font-weight:500">('+vd.total+' reports)</span></div>'
        + (vd.top_issues.length
          ? vd.top_issues.map(function(x){
              return '<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0 4px 20px;border-top:1px solid var(--border);font-size:12px">'
                + '<span>'+h(x.issue)+'</span><span style="font-weight:700;color:var(--accent);flex-shrink:0">'+x.count+'x</span></div>';
            }).join('')
          : '<div style="padding-left:20px;color:var(--muted);font-size:12px">No issues logged</div>')
        + '</div>';
    }).join('');
    html += '<div style="margin-bottom:20px">'
      + '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">Top Issues by Vehicle Type</div>'
      + (vtypeBlocks || '<div style="color:var(--muted);font-size:12px">No vehicle-type data for this period</div>')
      + '</div>';
  }

  if (reportSectionEnabled('units') && d.top_units && d.top_units.length) {
    html += '<div style="margin-bottom:20px">'
      + '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">Top Problem Units</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
      + '<thead><tr>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Unit</th>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Type</th>'
      + '<th style="text-align:right;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Reports</th>'
      + '</tr></thead><tbody>'
      + d.top_units.map(function(u){
          return '<tr style="border-top:1px solid var(--border)">'
            + '<td style="padding:7px 8px;font-weight:700">'+h(u.unit)+'</td>'
            + '<td style="padding:7px 8px;color:var(--muted);text-transform:capitalize">'+h(u.vtype||'—')+'</td>'
            + '<td style="padding:7px 8px;text-align:right;font-weight:700;color:var(--accent)">'+h(u.count)+'</td>'
            + '</tr>';
        }).join('')
      + '</tbody></table></div>';
  }

  if (reportSectionEnabled('missed') && d.missed_cases.length) {
    html += '<div style="margin-bottom:8px">'
      + '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--red);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">Unresolved Alerts ('+d.missed+')</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
      + '<thead><tr>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Driver</th>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Group</th>'
      + '<th style="text-align:left;padding:6px 8px;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase">Time</th>'
      + '</tr></thead><tbody>'
      + d.missed_cases.map(function(c){
          return '<tr style="border-top:1px solid var(--border)">'
            + '<td style="padding:7px 8px;font-weight:500">'+h(c.driver)+'</td>'
            + '<td style="padding:7px 8px;color:var(--muted)">'+h(c.group)+'</td>'
            + '<td style="padding:7px 8px;color:var(--muted);font-size:11px">'+h(c.opened)+'</td>'
            + '</tr>';
        }).join('')
      + '</tbody></table></div>';
  }

  html += '<div style="margin-top:24px;padding-top:12px;border-top:1px solid var(--border);text-align:center;font-size:10px;color:var(--muted)">This is an official Kurtex Maintenance report, generated automatically from live fleet data.</div>';

  document.getElementById('report-content').innerHTML = html;
}
function printReport() {
  var orig = document.title;
  document.title = 'Kurtex Maintenance Report — ' + new Date().toLocaleDateString('en-US',{timeZone:'America/Chicago'});
  document.body.classList.add('printing-report');
  function cleanup() {
    document.title = orig;
    document.body.classList.remove('printing-report');
    window.removeEventListener('afterprint', cleanup);
  }
  window.addEventListener('afterprint', cleanup);
  window.print();
  // Fallback in case afterprint doesn't fire (some mobile browsers)
  setTimeout(cleanup, 2000);
}

// ── Refresh ────────────────────────────────────────────────────────────────

// ── Nav Groups ──────────────────────────────────────────────────────────────
function toggleGroup(id) {
  var el = document.getElementById(id);
  var caret = document.getElementById('caret-' + id);
  var open = el.classList.contains('open');
  el.classList.toggle('open', !open);
  if (caret) caret.classList.toggle('open', !open);
}

// ── Trends ───────────────────────────────────────────────────────────────────
var trendPeriod = 30;
var trendCharts = {};

function setTrendPeriod(days, btn) {
  trendPeriod = days;
  document.querySelectorAll('#page-trends .toggle-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  loadTrends();
}

async function loadTrends() {
  try {
    var r = await fetch('/api/trends?period=' + trendPeriod);
    if (!r.ok) return;
    var d = await r.json();

    // Destroy existing charts
    Object.values(trendCharts).forEach(function(c){ if(c) c.destroy(); });
    trendCharts = {};

    var accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    var green  = getComputedStyle(document.documentElement).getPropertyValue('--green').trim();
    var red    = getComputedStyle(document.documentElement).getPropertyValue('--red').trim();
    var muted  = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();

    // Cases over time
    var ctx1 = document.getElementById('trend-cases-chart').getContext('2d');
    trendCharts.cases = new Chart(ctx1, {
      type: 'line',
      data: {
        labels: d.labels,
        datasets: [
          {label:'Total', data:d.totals, borderColor:accent, backgroundColor:accent+'22', fill:true, tension:.4, pointRadius:2},
          {label:'Resolved', data:d.resolved, borderColor:green, backgroundColor:'transparent', tension:.4, pointRadius:2},
          {label:'Missed', data:d.missed, borderColor:red, backgroundColor:'transparent', tension:.4, pointRadius:2},
        ]
      },
      options: {responsive:true, plugins:{legend:{labels:{color:muted,font:{size:11}}}}, scales:{x:{ticks:{color:muted,font:{size:10},maxTicksLimit:10}},y:{ticks:{color:muted,font:{size:10}},beginAtZero:true}}}
    });

    // Avg response time
    var ctx2 = document.getElementById('trend-resp-chart').getContext('2d');
    trendCharts.resp = new Chart(ctx2, {
      type: 'line',
      data: {labels: d.labels, datasets: [{label:'Avg Resp (secs)', data:d.avg_resp, borderColor:accent, backgroundColor:accent+'22', fill:true, tension:.4, pointRadius:2}]},
      options: {responsive:true, plugins:{legend:{labels:{color:muted,font:{size:11}}}}, scales:{x:{ticks:{color:muted,font:{size:10},maxTicksLimit:10}},y:{ticks:{color:muted,font:{size:10}},beginAtZero:true}}}
    });

    // Daily bar
    var ctx3 = document.getElementById('trend-bar-chart').getContext('2d');
    trendCharts.bar = new Chart(ctx3, {
      type: 'bar',
      data: {labels: d.labels, datasets: [
        {label:'Total', data:d.totals, backgroundColor:accent+'88'},
        {label:'Resolved', data:d.resolved, backgroundColor:green+'88'},
        {label:'Missed', data:d.missed, backgroundColor:red+'88'},
      ]},
      options: {responsive:true, plugins:{legend:{labels:{color:muted,font:{size:11}}}}, scales:{x:{stacked:false,ticks:{color:muted,font:{size:10},maxTicksLimit:15}},y:{beginAtZero:true,ticks:{color:muted,font:{size:10}}}}}
    });
  } catch(e) { console.error('trends error:', e); }
}

// ── Comparison ───────────────────────────────────────────────────────────────
async function loadComparison() {
  var el = document.getElementById('comparison-content');
  el.innerHTML = '<div class="loading">Loading comparison...</div>';
  try {
    var r = await fetch('/api/comparison');
    var d = await r.json();
    function deltaHtml(delta, label) {
      if (!delta || delta.pct === 0) return '<span style="color:var(--muted);font-size:11px">—</span>';
      var color = delta.up ? 'var(--green)' : 'var(--red)';
      var arrow = delta.up ? '↑' : '↓';
      return '<span style="color:'+color+';font-size:11px;font-weight:600">'+arrow+' '+delta.pct+'%</span>';
    }
    function compRow(label, thisVal, lastVal, delta, unit) {
      unit = unit || '';
      return '<tr style="border-bottom:1px solid var(--border)">'
        + '<td style="padding:12px 14px;font-size:13px;font-weight:500;color:var(--muted)">'+label+'</td>'
        + '<td style="padding:12px 14px;font-size:18px;font-weight:800;color:var(--text);text-align:center">'+thisVal+unit+'</td>'
        + '<td style="padding:12px 14px;font-size:16px;font-weight:600;color:var(--muted2);text-align:center">'+lastVal+unit+'</td>'
        + '<td style="padding:12px 14px;text-align:center">'+deltaHtml(delta)+'</td>'
        + '</tr>';
    }
    el.innerHTML =
      '<div class="card">'
      + '<table style="width:100%;border-collapse:collapse">'
      + '<thead><tr style="background:var(--surface2)">'
      + '<th style="padding:10px 14px;text-align:left;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase">Metric</th>'
      + '<th style="padding:10px 14px;text-align:center;font-size:11px;color:var(--accent);font-weight:700;text-transform:uppercase">This Week</th>'
      + '<th style="padding:10px 14px;text-align:center;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase">Last Week</th>'
      + '<th style="padding:10px 14px;text-align:center;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase">Change</th>'
      + '</tr></thead><tbody>'
      + compRow('Total Cases', d.this_week.total, d.last_week.total, d.delta_total)
      + compRow('Resolved', d.this_week.done, d.last_week.done, d.delta_done)
      + compRow('Missed', d.this_week.missed, d.last_week.missed, d.delta_missed)
      + compRow('Resolution Rate', d.this_week.rate, d.last_week.rate, d.delta_rate, '%')
      + compRow('Avg Response', d.this_week.avg_resp, d.last_week.avg_resp, d.delta_resp)
      + '</tbody></table></div>';
  } catch(e) { el.innerHTML = '<div class="loading">Error.</div>'; }
}

// ── Issue Search ──────────────────────────────────────────────────────────────
var issueSearchVtype = '';
function setIssueSearchVtype(vtype) {
  issueSearchVtype = vtype;
  document.querySelectorAll('#issue-search-vtype-tabs .toggle-btn').forEach(function(b){
    b.classList.toggle('active', b.dataset.vtype === vtype);
  });
  if (document.getElementById('issue-search-input').value.trim()) searchIssue();
}

async function searchIssue() {
  var q = document.getElementById('issue-search-input').value.trim();
  var vtype = issueSearchVtype;
  var el = document.getElementById('issue-search-results');
  if (!q) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="loading">Searching...</div>';
  try {
    var r = await fetch('/api/issue_search?q=' + encodeURIComponent(q) + (vtype ? '&vtype=' + encodeURIComponent(vtype) : ''));
    var d = await r.json();
    if (!r.ok) { el.innerHTML = '<div class="empty-state">' + h(d.error||'Error') + '</div>'; return; }
    if (!d.results.length) { el.innerHTML = '<div class="empty-state">No units found for "' + h(q) + '".</div>'; return; }
    el.innerHTML = '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">' + d.total_matches + ' matching case(s) across ' + d.results.length + ' unit(s)</div>'
      + '<div class="table-wrap"><div class="table-scroll"><table>'
      + '<thead><tr><th>Unit #</th><th>Type</th><th>Matches</th><th>Sample Issue</th><th>Last Seen</th></tr></thead><tbody>'
      + d.results.map(function(u){
          return '<tr style="cursor:pointer" data-unit="'+attr(u.unit)+'" data-vtype="'+attr(u.vtype||'')+'" onclick="openUnitModal(this.dataset.unit, this.dataset.vtype)" title="View all cases for unit '+attr(u.unit)+'">'
            + '<td><b>'+h(u.unit)+'</b></td>'
            + '<td><span style="background:var(--accent-bg);color:var(--accent);padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600">'+h(u.vtype)+'</span></td>'
            + '<td><b style="color:var(--accent)">'+h(u.count)+'</b></td>'
            + '<td style="color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+h(u.sample_issue)+'</td>'
            + '<td style="color:var(--muted);font-size:11px">'+h(u.last_seen)+'</td>'
            + '</tr>';
        }).join('')
      + '</tbody></table></div></div>';
  } catch(e) { el.innerHTML = '<div class="loading">Error: '+h(e.message)+'</div>'; }
}

// ── Fleet Intelligence ────────────────────────────────────────────────────────
async function loadFleetIntel() {
  var el = document.getElementById('fleet-intel-content');
  el.innerHTML = '<div class="loading">Loading fleet intelligence...</div>';
  try {
    var r = await fetch('/api/fleet_intelligence');
    var d = await r.json();
    window._intelData = d;

    var driversHtml = '<div class="table-wrap"><div class="table-scroll"><table>'
      + '<thead><tr><th>Driver</th><th>Reports</th><th>Most Common Issue</th></tr></thead><tbody>'
      + (d.top_drivers.length ? d.top_drivers.map(function(dr, i){
          return '<tr>'
            + '<td><span style="margin-right:6px">'+(i<3?['🥇','🥈','🥉'][i]:(i+1)+'.')+'</span><b>'+h(dr.name)+'</b></td>'
            + '<td><b style="color:var(--accent)">'+h(dr.total)+'</b></td>'
            + '<td style="color:var(--muted)">'+h(dr.top_issue)+'</td>'
            + '</tr>';
        }).join('') : '<tr><td colspan="3" style="text-align:center;color:var(--muted);padding:20px">No data yet</td></tr>')
      + '</tbody></table></div></div>';

    el.innerHTML =
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">'
      + '<div class="stat-card c-accent"><div class="stat-icon"><i class="ph ph-chart-bar"></i></div><div class="stat-label">Total Reports</div><div class="stat-value v-accent">'+d.total_reports+'</div></div>'
      + '<div class="stat-card c-blue"><div class="stat-icon"><i class="ph ph-hash"></i></div><div class="stat-label">Unique Units Tracked</div><div class="stat-value v-blue">'+d.top_units.length+'</div></div>'
      + '</div>'
      + '<div id="intel-units-wrap"></div>'
      + '<div class="section-title" style="margin:16px 0 10px">Most Reported Drivers</div>'
      + driversHtml;
    renderIntelUnits('all', '');
  } catch(e) { el.innerHTML = '<div class="loading">Error: '+h(e.message)+'</div>'; }
}

function intelUnitsRowsHtml(vtype, search) {
  var items = (window._intelData && window._intelData.top_units) || [];
  var q = (search||'').toLowerCase().trim();
  var filtered = items.filter(function(u) {
    if (vtype !== 'all' && (u.vtype||'').toLowerCase() !== vtype) return false;
    if (q && (u.unit||'').toLowerCase().indexOf(q)===-1 && (u.top_issue||'').toLowerCase().indexOf(q)===-1) return false;
    return true;
  });
  var rows = filtered.map(function(u){
    return '<tr style="cursor:pointer" data-unit="'+attr(u.unit)+'" data-vtype="'+attr(u.vtype||'')+'" onclick="openUnitModal(this.dataset.unit, this.dataset.vtype)" title="View all cases for unit '+attr(u.unit)+'">'
      + '<td><b>'+h(u.unit)+'</b></td>'
      + '<td><span style="background:var(--accent-bg);color:var(--accent);padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600">'+h(u.vtype)+'</span></td>'
      + '<td><b style="color:var(--accent)">'+h(u.total)+'</b></td>'
      + '<td style="color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+h(u.top_issue)+'</td>'
      + '<td style="color:var(--muted);font-size:11px">'+h(u.last_seen)+'</td>'
      + '</tr>';
  }).join('');
  return filtered.length ? rows : '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">No units match this filter</td></tr>';
}

// Only refreshes the table rows (used while typing) so the search input
// itself is never destroyed/recreated and keeps focus + cursor position.
function updateIntelUnitsTable(vtype, search) {
  var tbody = document.getElementById('intel-units-tbody');
  if (!tbody || !window._intelData) return;
  window._intelUnitsVtype = vtype;
  tbody.innerHTML = intelUnitsRowsHtml(vtype, search);
}

function renderIntelUnits(vtype, search) {
  var wrap = document.getElementById('intel-units-wrap');
  if (!wrap || !window._intelData) return;
  window._intelUnitsVtype = vtype;
  function vbtn(v, label) {
    return '<button class="toggle-btn'+(vtype===v?' active':'')+'" onclick="renderIntelUnits(\\''+v+'\\', document.getElementById(\\'intel-units-search\\').value)">'+label+'</button>';
  }
  wrap.innerHTML =
    '<div class="section-title" style="margin-bottom:10px">Most Reported Units</div>'
    + '<div class="toggle-tabs" style="margin-bottom:10px">' + vbtn('all','All') + vbtn('truck','Truck') + vbtn('trailer','Trailer') + vbtn('reefer','Reefer') + '</div>'
    + '<div class="search-wrap" style="margin-bottom:12px"><i class="ph ph-magnifying-glass"></i><input type="text" id="intel-units-search" placeholder="Search unit or issue..." value="'+attr(search||'')+'" oninput="updateIntelUnitsTable(window._intelUnitsVtype, this.value)"></div>'
    + '<div class="table-wrap"><div class="table-scroll"><table>'
    + '<thead><tr><th>Unit #</th><th>Type</th><th>Reports</th><th>Top Issue</th><th>Last Seen</th></tr></thead>'
    + '<tbody id="intel-units-tbody">' + intelUnitsRowsHtml(vtype, search) + '</tbody></table></div></div>';
}

async function refresh(force) {
  force = force !== false;
  if (!force && anyModalOpen()) return;
  await loadStats();
  if (!force) {
    var luQuiet = document.getElementById('last-update');
    if (luQuiet) luQuiet.textContent = 'Updated ' + new Date().toLocaleTimeString('en-US',{timeZone:'America/Chicago'}) + ' CT';
    return;
  }
  if (currentPage==='overview') {
    try {
      var r = await fetch('/api/cases?filter=today&limit=10');
      if (r.ok) {
        var d = await r.json();
        var el = document.getElementById('recent-table');
        if (el) el.innerHTML = caseTable((d.cases||[]).slice(0,10));
      }
    } catch(e) { console.error(e); }
  } else if (currentPage==='cases') loadCases();
  else if (currentPage==='missed') loadMissed();
  else if (currentPage==='fleet') loadFleet();
  else if (currentPage==='trends') loadTrends();
  else if (currentPage==='comparison') loadComparison();
  else if (currentPage==='fleet_intel') loadFleetIntel();
  else if (currentPage==='my_profile') loadMyProfile();
  else if (currentPage==='agents') loadAgents();
  var lu = document.getElementById('last-update');
  if (lu) lu.textContent = 'Updated ' + new Date().toLocaleTimeString('en-US',{timeZone:'America/Chicago'}) + ' CT';
}

function autoRefresh() {
  refresh(false);
}

// Restore last visited page
try {
  var savedPage = localStorage.getItem('kurtex-page');
  if (savedPage && pages.indexOf(savedPage) >= 0) {
    showPage(savedPage);
  } else {
    refresh(true);
  }
} catch(e) { refresh(true); }
setInterval(autoRefresh, 30000);
</script>
</body>
</html>"""



@app.route("/api/trends")
def api_trends():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        cases = [c for c in load_cases() if not is_testing(c)]
        period = request.args.get("period","30")
        days = int(period)
        from datetime import timedelta
        today = datetime.now(CHI_TZ).date()
        labels, totals, resolved, missed_arr, avg_resp_arr = [], [], [], [], []
        for i in range(days-1, -1, -1):
            d = today - timedelta(days=i)
            ds = d.isoformat()
            day_cases = [c for c in cases if case_local_date(c) == ds]
            rt = [c["response_secs"] for c in day_cases if c.get("response_secs")]
            labels.append(d.strftime("%b %d"))
            totals.append(len(day_cases))
            resolved.append(sum(1 for c in day_cases if c.get("status")=="done"))
            missed_arr.append(sum(1 for c in day_cases if c.get("status")=="missed"))
            avg_resp_arr.append(int(sum(rt)/len(rt)) if rt else 0)
        return jsonify({"labels":labels,"totals":totals,"resolved":resolved,"missed":missed_arr,"avg_resp":avg_resp_arr})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comparison")
def api_comparison():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        from datetime import timedelta
        cases = [c for c in load_cases() if not is_testing(c)]
        today = datetime.now(CHI_TZ).date()
        # This week vs last week
        this_mon = today - timedelta(days=today.weekday())
        last_mon = this_mon - timedelta(days=7)
        last_sun = this_mon - timedelta(days=1)
        def week_stats(start, end):
            wc = [c for c in cases if start.isoformat() <= case_local_date(c) <= end.isoformat()]
            total = len(wc); done = sum(1 for c in wc if c.get("status")=="done")
            missed = sum(1 for c in wc if c.get("status")=="missed")
            rt = [c["response_secs"] for c in wc if c.get("response_secs")]
            avg = int(sum(rt)/len(rt)) if rt else 0
            rate = round(done/total*100) if total else 0
            return {"total":total,"done":done,"missed":missed,"avg_resp":fmt_secs(avg),"avg_secs":avg,"rate":rate}
        this_sun = today
        tw = week_stats(this_mon, this_sun)
        lw = week_stats(last_mon, last_sun)
        def delta(a, b, reverse=False):
            if b == 0: return {"pct": 0, "up": True}
            pct = round((a-b)/b*100)
            up = pct > 0 if not reverse else pct < 0
            return {"pct": abs(pct), "up": up}
        return jsonify({
            "this_week": tw, "last_week": lw,
            "delta_total":  delta(tw["total"], lw["total"]),
            "delta_done":   delta(tw["done"], lw["done"]),
            "delta_missed": delta(tw["missed"], lw["missed"], reverse=True),
            "delta_rate":   delta(tw["rate"], lw["rate"]),
            "delta_resp":   delta(tw["avg_secs"], lw["avg_secs"], reverse=True),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/fleet_intelligence")
def api_fleet_intelligence():
    if not session.get("user"): return jsonify({"error":"unauthorized"}), 401
    try:
        all_cases = [c for c in load_cases() if not is_testing(c)]
        reported = [c for c in all_cases if c.get("vehicle_type")]
        # Top units with history
        from collections import defaultdict
        unit_data = defaultdict(lambda: {"cases":[], "vtype":""})
        for c in reported:
            unit = (c.get("unit_number") or "").strip()
            if not unit: continue
            unit_data[unit]["vtype"] = c.get("vehicle_type","")
            unit_data[unit]["cases"].append(c)
        top_units = []
        for unit, data in unit_data.items():
            cases = data["cases"]
            total = len(cases)
            issues = Counter((c.get("issue_text") or "")[:50] for c in cases if c.get("issue_text"))
            top_issue = issues.most_common(1)[0][0] if issues else "—"
            last_case = max(cases, key=lambda c: c.get("opened_at",""))
            top_units.append({
                "unit": unit, "vtype": data["vtype"], "total": total,
                "top_issue": top_issue, "last_seen": fmt_dt(last_case.get("opened_at")),
            })
        top_units.sort(key=lambda x: -x["total"])
        # Top drivers
        driver_data = defaultdict(list)
        for c in reported:
            d = (c.get("report_driver") or "").strip()
            if d: driver_data[d].append(c)
        top_drivers = []
        for name, cases in driver_data.items():
            total = len(cases)
            issues = Counter((c.get("issue_text") or "")[:50] for c in cases if c.get("issue_text"))
            top_issue = issues.most_common(1)[0][0] if issues else "—"
            top_drivers.append({"name": name, "total": total, "top_issue": top_issue})
        top_drivers.sort(key=lambda x: -x["total"])
        return jsonify({
            "top_units": top_units[:20],
            "top_drivers": top_drivers[:20],
            "total_reports": len(reported),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/login")
def login():
    return render_template_string(LOGIN_HTML, bot_username=get_bot_username(), error=request.args.get("error"))

@app.route("/")
def index():
    if not session.get("user"): return redirect("/login")
    user = session["user"]
    is_manager = user.get("role","agent") in ("developer","super_admin")
    return render_template_string(DASHBOARD_HTML, user=user, is_manager=is_manager)

def run_dashboard():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)

def start_dashboard_thread():
    Thread(target=run_dashboard, daemon=True).start()
    logger.info(f"Dashboard started on port {DASHBOARD_PORT}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_dashboard()
