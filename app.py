import os
from flask import Flask, render_template, request, jsonify
from html.parser import HTMLParser
import re
import requests
import threading
from supabase import create_client, Client

# ============================================================
# FLASK WEB SERVER SETUP (Look for templates in current root folder)
# ============================================================
app = Flask(__name__, template_folder='.')

# ============================================================
# SUPABASE CONFIGURATION
# ============================================================
SUPABASE_URL = "https://psfxcihqsoouetblpyvq.supabase.co"
SUPABASE_KEY = "sb_publishable_3MPpwBaNrhaJ5fVfiTE0eQ_2a6_NT8x"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE_NAME = "Xylem_database"

# ============================================================
# HTML TEXT EXTRACTION & PARSING HELPERS
# ============================================================
class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self.ignore_tags = {"script", "style", "head", "title", "meta"}
        self.current_tag = ""

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag.lower()

    def handle_data(self, data):
        if self.current_tag not in self.ignore_tags:
            text = data.strip()
            if text:
                self.result.append(text)

    def get_text(self):
        return "\n".join(self.result)

def extract_plain_text(html_content):
    parser = HTMLTextExtractor()
    parser.feed(html_content)
    text = parser.get_text()
    return re.sub(r"\n\s*\n+", "\n", text)

def parse_student_info(clean_text, default_roll):
    name_match = re.search(r"(?:Name|Student Name)\s*[:\-]?\s*(.+)", clean_text, re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else "N/A"

    roll_match = re.search(r"(?:Roll\s*(?:No|Number)?)\s*[:\-]?\s*(\d+)", clean_text, re.IGNORECASE)
    try:
        roll = int(roll_match.group(1).strip()) if roll_match else int(default_roll)
    except ValueError:
        roll = int(default_roll)

    score_match = re.search(r"(?:Score|Total\s*Marks?|Marks)\s*[:\-]?\s*([\d\.]+)", clean_text, re.IGNORECASE)
    try:
        score = int(float(score_match.group(1).strip())) if score_match else None
    except ValueError:
        score = None

    rank_match = re.search(r"(?:Rank|State\s*Rank|AIR)\s*[:\-]?\s*(\d+)", clean_text, re.IGNORECASE)
    try:
        rank = int(rank_match.group(1).strip()) if rank_match else None
    except ValueError:
        rank = None

    return {
        "name": name,
        "number": roll,
        "score": score,
        "rank": rank
    }

API_URL = "https://results.xylemlearning.com/wp-admin/admin-ajax.php"
EXAM_CLASS = "JEE"
EXAM_YEAR = "GIB-27SEP2026"
REQUEST_TIMEOUT = 100

thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "AuthorizedResultClient/1.0",
            "Accept": "text/html,application/json,*/*",
        })
        thread_local.session = session
    return thread_local.session

def check_roll(roll_number):
    session = get_session()
    data = {
        "action": "jsrmsp_student_result_view",
        "examclass": EXAM_CLASS,
        "examyear": EXAM_YEAR,
        "examroll": str(roll_number),
    }

    try:
        response = session.post(API_URL, data=data, timeout=REQUEST_TIMEOUT)
        status = response.status_code
        text = response.text.strip()

        if status != 200:
            return {"roll": roll_number, "status": "error", "message": f"HTTP {status}"}

        if not text:
            return {"roll": roll_number, "status": "error", "message": "Empty response"}

        if "Checking your browser before redirecting" in text or "anubis_challenge" in text:
            return {"roll": roll_number, "status": "blocked", "message": "Backend request was blocked by Anubis"}

        if "Result not found or not published yet" in text:
            return {"roll": roll_number, "status": "not_found", "message": None}

        clean_text = extract_plain_text(text)
        extracted_data = parse_student_info(clean_text, roll_number)

        return {
            "roll": roll_number,
            "status": "found",
            "message": None,
            "data": extracted_data
        }
    except requests.Timeout:
        return {"roll": roll_number, "status": "error", "message": "Request timeout"}
    except requests.RequestException as e:
        return {"roll": roll_number, "status": "error", "message": str(e)}

def mask_roll(roll_str):
    roll_str = str(roll_str)
    if len(roll_str) <= 3:
        return roll_str + "XXXXX"
    return roll_str[:3] + "X" * (len(roll_str) - 3)

@app.route('/')
def index():
    try:
        response = supabase.table(TABLE_NAME).select("*").order("rank", desc=False).execute()
        students = response.data if response.data else []
    except Exception as e:
        students = []

    rendered_students = []
    for s in students:
        rendered_students.append({
            "rank": s.get("rank", "N/A"),
            "name": s.get("name", "N/A"),
            "masked_roll": mask_roll(s.get("number", "")),
            "score": s.get("score", "N/A")
        })

    return render_template('index.html', students=rendered_students)

@app.route('/fetch-result', methods=['POST'])
def fetch_result_endpoint():
    data = request.get_json()
    roll_number = data.get('roll')

    if not roll_number:
        return jsonify({"status": "error", "message": "Roll number is required."}), 400

    try:
        roll_int = int(roll_number)
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid roll number format."}), 400

    # Check if the roll number already exists in the database
    try:
        existing = supabase.table(TABLE_NAME).select("*").eq("number", roll_int).execute()
        if existing.data and len(existing.data) > 0:
            return jsonify({"status": "exists", "message": "This roll number already exists in the list!"})
    except Exception as db_check_err:
        pass

    result = check_roll(roll_int)
    status = result["status"]

    if status == "found":
        info = result["data"]
        try:
            supabase.table(TABLE_NAME).insert({
                "name": info["name"],
                "number": info["number"],
                "score": info["score"],
                "rank": info["rank"]
            }).execute()
            return jsonify({"status": "success", "message": "Result found and added successfully!"})
        except Exception as db_err:
            return jsonify({"status": "exists", "message": "This roll number already exists in the list!"})
    elif status == "not_found":
        return jsonify({"status": "error", "message": "Result not found or not published yet."})
    elif status == "blocked":
        return jsonify({"status": "error", "message": "Request blocked by anti-bot protection."})
    else:
        return jsonify({"status": "error", "message": result.get("message", "Error fetching result.")})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)