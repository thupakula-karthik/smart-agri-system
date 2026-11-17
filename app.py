import os
import io
import time
import json
import base64
import sqlite3
import datetime
from xml.sax.saxutils import escape
import requests
from PIL import Image
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kucet-smart-agri-secret-2026")

DB_PATH = os.environ.get("DB_PATH", "agri.db")

system_state = {
    "last_seen": 0.0,
    "network": "Offline",
    "pump_mode": "AUTO",
    "pump_state": 0,
    "farmer_whatsapp": os.environ.get("FARMER_WHATSAPP_TO", ""),
    "latest": {
        "temp": 28.0,
        "soil_temp": 26.5,
        "hum": 60.0,
        "soil": 45,
        "gas": 120,
        "flame": 0,
        "rain": 0,
        "pump": 0,
        "network": "Offline"
    },
    "alert_cooldowns": {}
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temp REAL,
            soil_temp REAL,
            hum REAL,
            soil REAL,
            gas INTEGER DEFAULT 120,
            flame INTEGER DEFAULT 0,
            rain INTEGER DEFAULT 0,
            pump_state INTEGER DEFAULT 0,
            network TEXT DEFAULT 'WiFi',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    for col, col_def in [
        ("gas", "INTEGER DEFAULT 120"),
        ("flame", "INTEGER DEFAULT 0"),
        ("rain", "INTEGER DEFAULT 0"),
        ("pump_state", "INTEGER DEFAULT 0"),
        ("network", "TEXT DEFAULT 'WiFi'")
    ]:
        try:
            cur.execute(f"ALTER TABLE telemetry ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            security_pin TEXT NOT NULL
        )
    ''')
    cur.execute("SELECT id FROM users WHERE username = ?", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password_hash, email, security_pin) VALUES (?, ?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "karthik.kucet@gmail.com", "1234")
        )
    conn.commit()
    conn.close()

init_db()

# ---------------- GEMINI VISION + NORMALIZED PILLOW FALLBACK ----------------
def identify_crop_and_pathology(image_bytes, crop_hint="Auto"):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            fast_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            fast_img.thumbnail((640, 640))
            buf = io.BytesIO()
            fast_img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            prompt = (
                f"You are an expert agricultural plant pathologist helping Indian farmers. "
                f"Analyze this crop leaf image (user hint: {crop_hint}). "
                f"Return ONLY valid JSON with keys: "
                f"crop (exact plant name e.g. Paddy/Rice, Cotton, Chilli, Tomato), "
                f"status (exact disease name e.g. Healthy, Rice Blast, Brown Spot, Bacterial Leaf Blight), "
                f"healthy (boolean true/false), "
                f"lesion_pct (number 0-100 estimated affected area), "
                f"advice (2 clear sentences of practical, safe treatment advice for the farmer)."
            )
            for model_id in ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash"):
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}",
                    json={
                        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
                        "generationConfig": {"response_mime_type": "application/json"}
                    },
                    timeout=12
                )
                if r.status_code == 200:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    res = json.loads(txt)
                    if isinstance(res, list):
                        res = res[0]
                    return {
                        "crop": str(res.get("crop", "Crop")),
                        "status": str(res.get("status", "Analyzed")),
                        "healthy": bool(res.get("healthy", True)),
                        "lesion_pct": round(float(res.get("lesion_pct", 0)), 1),
                        "advice": str(res.get("advice", ""))
                    }
                else:
                    print(f"Gemini {model_id} error:", r.status_code, r.text[:200], flush=True)
        except Exception as e:
            print("Gemini Vision exception:", str(e), flush=True)
    else:
        print("GEMINI_API_KEY is empty in Render env!", flush=True)

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((320, 320))
    w, h = img.size
    px = img.load()

    green_count = lesion_count = total_leaf = crossings = 0
    for y in range(h):
        in_green = False
        for x in range(w):
            r, g, b = px[x, y]
            br = (r + g + b) / 3.0
            is_green = (g > r * 1.04 and g > b * 1.04 and g > 42)
            is_lesion = (
                br >= 55 and not is_green and
                ((r > g * 1.08 and r > b * 1.15) or (r > 130 and g > 110 and b < 95))
            )
            if is_green:
                green_count += 1
                total_leaf += 1
                if not in_green:
                    crossings += 1
                    in_green = True
            else:
                in_green = False
                if is_lesion:
                    lesion_count += 1
                    total_leaf += 1

    norm_cross = (crossings / max(1, h)) * (320.0 / max(1, w))
    detected_crop = crop_hint if crop_hint != "Auto" else ("Paddy" if norm_cross >= 5.5 else ("Cotton" if green_count > (w * h * 0.18) else "Chilli"))
    lesion_ratio = (lesion_count / max(1, total_leaf)) * 100.0

    if total_leaf < (w * h * 0.04):
        return {"crop": detected_crop, "status": "Unclear Image", "healthy": True, "lesion_pct": 0.0, "advice": "Please upload a clearer, well-lit close-up of the crop leaves."}
    if lesion_ratio < 8.0:
        return {"crop": detected_crop, "status": "Healthy", "healthy": True, "lesion_pct": round(lesion_ratio, 2), "advice": f"{detected_crop} foliage looks healthy with minimal stress ({lesion_ratio:.1f}%)."}
    elif lesion_ratio < 22.0:
        return {"crop": detected_crop, "status": "Early Leaf Spot / Nutrient Stress", "healthy": False, "lesion_pct": round(lesion_ratio, 2), "advice": f"Early spotting detected on {detected_crop} ({lesion_ratio:.1f}%). Avoid overhead watering and inspect undersides for pests."}
    else:
        return {"crop": detected_crop, "status": "Severe Blight / Fungal Infection", "healthy": False, "lesion_pct": round(lesion_ratio, 2), "advice": f"High lesion coverage on {detected_crop} ({lesion_ratio:.1f}%). Isolate affected plants and apply recommended copper/neem fungicide."}

# ---------------- WHATSAPP SENDER (TWILIO) ----------------
def format_wa(num):
    num = (num or "").strip().replace(" ", "")
    if not num:
        return ""
    if not num.startswith("whatsapp:"):
        if not num.startswith("+"):
            num = "+91" + num.lstrip("0")
        num = "whatsapp:" + num
    return num

def send_auto_whatsapp(message, to_number=None, alert_key=None, cooldown=300):
    now = time.time()
    if alert_key:
        last_sent = system_state["alert_cooldowns"].get(alert_key, 0)
        if now - last_sent < cooldown:
            return False, "Cooldown active"

    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    wa_from = format_wa(os.environ.get("TWILIO_WHATSAPP_FROM", "+14155238886"))
    wa_to = format_wa(to_number or system_state["farmer_whatsapp"])

    if not sid or not token or not wa_to:
        return False, "Missing Twilio credentials or recipient number"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        resp = requests.post(
            url,
            auth=(sid, token),
            data={"From": wa_from, "To": wa_to, "Body": message},
            timeout=10
        )
        if resp.status_code in (200, 201):
            if alert_key:
                system_state["alert_cooldowns"][alert_key] = now
            return True, "Sent"
        return False, f"Twilio {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, str(e)

def build_status_text():
    d = system_state["latest"]
    online = (time.time() - system_state["last_seen"]) <= 15
    conn_label = f"ONLINE ({system_state['network']})" if online else "OFFLINE (AVG)"
    return (
        f"🌾 *KUCET Smart Agriculture*\n"
        f"📡 Status: *{conn_label}*\n"
        f"🌡️ Air Temp: *{d['temp']}°C* | 💧 Humidity: *{d['hum']}%*\n"
        f"🌱 Soil Temp: *{d['soil_temp']}°C* | 💦 Moisture: *{d['soil']}%*\n"
        f"💨 Gas: *{d['gas']} PPM* | 🔥 Flame: *{'ALERT!' if d['flame'] else 'SAFE'}*\n"
        f"🌧️ Rain: *{'YES' if d['rain'] else 'NO'}* | 🚰 Pump: *{'ON' if d['pump'] else 'OFF'}* ({system_state['pump_mode']})"
    )

# ---------------- AUTH ROUTES ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cur.fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user"] = username
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    msg, error = None, None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pin = request.form.get("security_pin", "").strip()
        new_pw = request.form.get("new_password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ? AND security_pin = ?", (username, pin))
        user = cur.fetchone()
        if user and new_pw:
            cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_pw), user["id"]))
            conn.commit()
            msg = "Password updated! You can now log in."
        else:
            error = "Invalid username or security PIN."
        conn.close()
    return render_template("forgot_password.html", msg=msg, error=error)

# ---------------- DASHBOARD & API ROUTES ----------------
@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", farmer_whatsapp=system_state["farmer_whatsapp"])

@app.route("/api/telemetry", methods=["POST"])
def receive_telemetry():
    try:
        data = request.get_json(force=True)
        temp = float(data.get("temp", 0.0))
        soil_temp = float(data.get("soil_temp", 0.0))
        hum = float(data.get("hum", 0.0))
        soil = float(data.get("soil", 0.0))
        gas = int(data.get("gas", 120))
        flame = int(data.get("flame", data.get("fire", 0)))
        rain = int(data.get("rain", 0))
        pump = int(data.get("pump", 0))
        network = str(data.get("network", "WiFi"))

        system_state["last_seen"] = time.time()
        system_state["network"] = network
        system_state["pump_state"] = pump
        system_state["latest"] = {
            "temp": round(temp, 1),
            "soil_temp": round(soil_temp, 1),
            "hum": round(hum, 1),
            "soil": int(soil),
            "gas": gas,
            "flame": flame,
            "rain": rain,
            "pump": pump,
            "network": network
        }

        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO telemetry (temp, soil_temp, hum, soil, gas, flame, rain, pump_state, network)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (temp, soil_temp, hum, soil, gas, flame, rain, pump, network))
        conn.commit()
        conn.close()

        if flame == 1:
            send_auto_whatsapp("🔥 *FIRE ALERT!* Flame detected in field! Emergency pump check active.", alert_key="flame")
        if gas > 1800:
            send_auto_whatsapp(f"💨 *GAS WARNING!* High smoke/gas detected: {gas} PPM.", alert_key="gas")
        if soil < 30:
            send_auto_whatsapp(f"🏜️ *DRY SOIL ALERT!* Soil moisture dropped to {int(soil)}%.", alert_key="soil")
        if rain == 1:
            send_auto_whatsapp("🌧️ *RAIN DETECTED!* Irrigation paused automatically.", alert_key="rain")

        return jsonify({
            "status": "success",
            "pump_mode": system_state["pump_mode"],
            "network": network
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/api/latest", methods=["GET"])
def get_latest():
    online = (time.time() - system_state["last_seen"]) <= 15
    if online:
        d = dict(system_state["latest"])
        d["online"] = True
        d["pump_mode"] = system_state["pump_mode"]
        return jsonify(d)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT AVG(temp) t, AVG(soil_temp) st, AVG(hum) h, AVG(soil) s, AVG(gas) g FROM telemetry")
    row = cur.fetchone()
    conn.close()
    return jsonify({
        "online": False,
        "network": "Offline",
        "temp": round(row["t"], 1) if row and row["t"] is not None else system_state["latest"]["temp"],
        "soil_temp": round(row["st"], 1) if row and row["st"] is not None else system_state["latest"]["soil_temp"],
        "hum": round(row["h"], 1) if row and row["h"] is not None else system_state["latest"]["hum"],
        "soil": int(row["s"]) if row and row["s"] is not None else system_state["latest"]["soil"],
        "gas": int(row["g"]) if row and row["g"] is not None else 120,
        "flame": 0,
        "rain": 0,
        "pump": 0,
        "pump_mode": system_state["pump_mode"]
    })

@app.route("/api/pump", methods=["POST"])
def set_pump():
    data = request.get_json(force=True)
    mode = str(data.get("mode", "AUTO")).upper()
    if mode in ("AUTO", "ON", "OFF"):
        system_state["pump_mode"] = mode
        return jsonify({"status": "success", "pump_mode": mode})
    return jsonify({"status": "error", "message": "Invalid mode"}), 400

@app.route("/api/weather", methods=["GET"])
def get_weather():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast?latitude=17.9784&longitude=79.5941&current_weather=true&daily=precipitation_probability_max&timezone=Asia/Kolkata",
            timeout=6
        ).json()
        cw = r.get("current_weather", {})
        rain_prob = r.get("daily", {}).get("precipitation_probability_max", [0])[0]
        return jsonify({"temp": cw.get("temperature", "--"), "wind": cw.get("windspeed", "--"), "rain_prob": rain_prob})
    except Exception:
        return jsonify({"temp": "--", "wind": "--", "rain_prob": 0})

@app.route("/api/crop-check", methods=["POST"])
def crop_check():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    crop_hint = request.form.get("crop", "Auto")
    img_bytes = request.files["image"].read()
    res = identify_crop_and_pathology(img_bytes, crop_hint)
    if not res["healthy"]:
        send_auto_whatsapp(
            f"🌿 *CROP DISEASE ALERT ({res['crop']})*\nStatus: {res['status']} ({res['lesion_pct']}%)\nAdvice: {res['advice']}",
            alert_key="disease"
        )
    return jsonify(res)

@app.route("/api/chat", methods=["POST"])
def chat():
    msg = (request.get_json(force=True).get("message") or "").strip().lower()
    d = system_state["latest"]
    online = (time.time() - system_state["last_seen"]) <= 15
    if any(k in msg for k in ("status", "live", "sensor", "temp", "soil", "moisture")):
        return jsonify({"reply": f"Node is {'ONLINE via ' + system_state['network'] if online else 'OFFLINE (showing AVG)'}. Air: {d['temp']}°C, Soil Temp: {d['soil_temp']}°C, Humidity: {d['hum']}%, Moisture: {d['soil']}%, Gas: {d['gas']} PPM, Pump: {'ON' if d['pump'] else 'OFF'} ({system_state['pump_mode']})."})
    if "pump on" in msg:
        system_state["pump_mode"] = "ON"
        return jsonify({"reply": "Pump mode set to MANUAL ON. The ESP32 will activate the pump on its next heartbeat."})
    if "pump off" in msg:
        system_state["pump_mode"] = "OFF"
        return jsonify({"reply": "Pump mode set to MANUAL OFF."})
    if "auto" in msg:
        system_state["pump_mode"] = "AUTO"
        return jsonify({"reply": "Pump mode restored to AUTO (irrigates below 30% moisture unless raining)."})
    if "weather" in msg:
        w = get_weather().get_json()
        return jsonify({"reply": f"Hanamkonda/Warangal Weather: {w['temp']}°C, Wind {w['wind']} km/h, Rain probability {w['rain_prob']}%."})
    return jsonify({"reply": "Ask me about live status, weather, pump control (PUMP ON/OFF/AUTO), or click the 📷 icon to scan a crop leaf!"})

@app.route("/api/whatsapp/test", methods=["POST"])
def whatsapp_test():
    data = request.get_json(force=True)
    phone = data.get("phone", "").strip()
    if phone:
        system_state["farmer_whatsapp"] = phone
    ok, info = send_auto_whatsapp(build_status_text(), to_number=phone)
    return jsonify({"status": "success" if ok else "error", "message": "Sent to WhatsApp!" if ok else info})

@app.route("/api/whatsapp", methods=["POST"])
def whatsapp_webhook():
    body = (request.form.get("Body") or "").strip()
    lower = body.lower()
    num_media = int(request.form.get("NumMedia") or 0)
    sender = request.form.get("From", "")
    if sender:
        system_state["farmer_whatsapp"] = sender

    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        try:
            r = requests.get(media_url, auth=(sid, token), timeout=10)
            res = identify_crop_and_pathology(r.content, "Auto")
            reply = f"🌿 *Leaf Scan ({res['crop']})*\nResult: *{res['status']}* ({res['lesion_pct']}%)\nAdvice: {res['advice']}"
        except Exception as e:
            reply = f"Could not analyze leaf photo: {e}"
    elif lower in ("hi", "hello", "hey", "menu", "start"):
        reply = build_status_text() + "\n\n*Commands:* STATUS | PUMP ON | PUMP OFF | PUMP AUTO | WEATHER | or send a leaf photo!"
    elif "status" in lower:
        reply = build_status_text()
    elif "pump on" in lower:
        system_state["pump_mode"] = "ON"
        reply = "🚰 Pump set to *MANUAL ON*."
    elif "pump off" in lower:
        system_state["pump_mode"] = "OFF"
        reply = "🛑 Pump set to *MANUAL OFF*."
    elif "pump auto" in lower or lower == "auto":
        system_state["pump_mode"] = "AUTO"
        reply = "⚙️ Pump set to *AUTO* mode."
    elif "weather" in lower:
        w = get_weather().get_json()
        reply = f"🌦️ *Warangal Weather:* {w['temp']}°C | Wind: {w['wind']} km/h | Rain Chance: {w['rain_prob']}%"
    else:
        reply = "Send *Hi* or *STATUS* for live field data, *PUMP ON/OFF/AUTO*, *WEATHER*, or a crop leaf photo."

    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'
    return Response(twiml, mimetype="application/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)