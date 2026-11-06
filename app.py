import os
import sys
import calendar
import json
import re
import logging
import secrets
import hmac
from typing import TypedDict, Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
import pytz

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import firestore as google_firestore

# ========== 環境変数の読み込み ==========
load_dotenv()

# Groq APIキーの存在確認
if not os.getenv("GROQ_API_KEY"):
    print("❌ エラー: GROQ_API_KEY が設定されていません。")
    print("   .env ファイルを作成して 'GROQ_API_KEY=your_key' を記述するか、")
    print("   環境変数（set GROQ_API_KEY=your_key）を設定してください。")
    sys.exit(1)

from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, END

# ========== ロギング設定 ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD', ''),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '').lower() == 'true',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)


def resolve_student(value):
    for mode, info in STUDENT_MAP.items():
        if value in (mode, info['name']):
            return mode, info
    abort(404)


@app.before_request
def protect_admin():
    if request.endpoint not in {'admin_page', 'update_log', 'delete_log', 'admin_logout'}:
        return
    if not session.get('admin_authenticated'):
        student = (request.view_args or {}).get('student', 'ann')
        _, info = resolve_student(student)
        return redirect(url_for('admin_login', student=info['name']))
    if request.method == 'POST':
        token = session.get('admin_csrf', '')
        if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400)


@app.after_request
def private_admin_response(response):
    if request.endpoint in {'admin_login', 'admin_page', 'update_log', 'delete_log', 'admin_logout'}:
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    _, info = resolve_student(request.args.get('student', 'ann'))
    configured = bool(app.config['ADMIN_PASSWORD'])
    error = ''
    if request.method == 'POST' and configured:
        token = session.get('login_csrf', '')
        if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400)
        if hmac.compare_digest(request.form.get('password', '').encode(), app.config['ADMIN_PASSWORD'].encode()):
            session.clear()
            session.permanent = True
            session['admin_authenticated'] = True
            session['admin_csrf'] = secrets.token_hex(32)
            return redirect(url_for('admin_page', student=info['name']))
        error = 'パスワードが違います。もう一度入力してください。'
    session.setdefault('login_csrf', secrets.token_hex(32))
    return render_template('admin_login.html', error=error, configured=configured,
                           student=info['name'], mode=resolve_student(info['name'])[0]), (200 if configured else 503)


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

# --- Firestore 初期化（プロジェクトID: my-stamp-app-487411 に固定） ---
if not firebase_admin._apps:
    firebase_admin.initialize_app()
db = google_firestore.Client(project='my-stamp-app-487411', database='stamp-db')

# ========== 定数・マスターデータ設定 ==========
VALID_WORDS: set[str] = {
    "れんしゅう",
    "ぎゅうにゅう",
    "きっぷ",
    "しゃしん",
    "びょういん",
    "べんきょう",
    "じゅぎょう",
    "いっしょ",
    "りょこう",
}

STUDENT_MAP: Dict[str, Dict[str, str]] = {
    's01': {
        'name': "ann",
        'display': "Ann's Log",
        'template': 'index_cookie.html'
    },
    's02': {
        'name': "taisei",
        'display': "Taisei's Log",
        'template': 'index_studio.html'
    }
}

JST = pytz.timezone('Asia/Tokyo')
PST = pytz.timezone('America/Los_Angeles')
FRISCO = pytz.timezone('America/Chicago')


def student_timezone(student_name):
    return FRISCO if student_name == 'ann' else PST

# ========== LLM クライアント共有化 ==========
llm_analyzer = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.2)
llm_generator = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.25)

# ========== 型定義 ==========
class QuizState(TypedDict):
    errors_text: str
    analysis: Dict[str, Any]
    quiz_data: Dict[str, Any]
    review_result: str
    retry_count: int

# ========== JSON 安全抽出 ==========
def safe_json_extract(text: str, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """LLMの出力からJSONを安全に抽出"""
    if fallback is None:
        fallback = {}

    text = text.strip()
    text = re.sub(r'^```(?:json)?\n?|\n?```$', '', text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except Exception as e:
        logger.debug(f"JSON direct parse failed: {e}")

    try:
        matches = re.findall(r'(\{[\s\S]*?\})', text, re.DOTALL)
        if matches:
            return json.loads(matches[-1])
    except Exception as e:
        logger.warning(f"JSON regex extraction failed: {e}")

    return fallback

# ========== ノード1：エラー分析 ==========
def analyze_errors(state: QuizState) -> QuizState:
    errors_text: str = state.get('errors_text', '')

    if not errors_text or not errors_text.strip():
        return {**state, "analysis": {
            "target_patterns": ["拗音", "促音", "撥音"],
            "key_errors": [],
            "theme": "日常",
            "priority_words": ["れんしゅう", "ぎゅうにゅう", "きっぷ", "しゃしん"]
        }}

    prompt = f"""
以下の生徒の間違い記録を分析してください：

{errors_text}

特に**撥音（ん）、促音（っ）、拗音（ゃ・ゅ・ょ）**に関する誤りを重点的に分析してください。

**必ずこのJSONフォーマットで返してください。他の文字は一切書かないでください。**
{{
  "target_patterns": ["拗音", "促音", "撥音"],
  "key_errors": ["誤りの例"],
  "theme": "推定される学習テーマ",
  "priority_words": ["れんしゅう", "ぎゅうにゅう", "きっぷ"]
}}
"""
    response = llm_analyzer.invoke(prompt)
    analysis = safe_json_extract(response.content, {
        "target_patterns": ["拗音", "促音", "撥音"],
        "key_errors": [],
        "theme": "日常",
        "priority_words": ["れんしゅう", "ぎゅうにゅう", "きっぷ", "しゃしん"]
    })

    return {**state, "analysis": analysis}

# ========== ノード2：問題生成 ==========
def generate_quiz(state: QuizState) -> QuizState:
    analysis: Dict[str, Any] = state.get("analysis", {})
    retry_count: int = state.get("retry_count", 0)

    if not analysis.get("target_patterns"):
        return {**state, "quiz_data": {}, "retry_count": retry_count + 1}

    prompt = f"""
あなたはN5ひらがな（撥音・促音・拗音）練習問題の専門教師です。

分析結果：
{json.dumps(analysis, ensure_ascii=False, indent=2)}

**ルール（厳守）**:
- 撥音（ん）、促音（っ）、拗音（ゃゅょ）を中心に練習
- N5基本単語のみ使用（れんしゅう、ぎゅうにゅう、きっぷ、しゃしん、びょういん、べんきょう、じゅぎょう、いっしょ、りょこうなど）
- 問題形式は「次のうち正しいのはどれ？」

**JSONのみ**で出力。他の文字は一切禁止。
{{
  "question": "次のうち正しいのはどれですか？",
  "answer": "れんしゅう",
  "choices": ["れんしゅ", "れんしょう", "れんしゅう", "れんしょ"]
}}
"""
    response = llm_generator.invoke(prompt)
    quiz_data = safe_json_extract(response.content, {})

    return {**state, "quiz_data": quiz_data, "retry_count": retry_count + 1}

# ========== ノード3：品質確認 ==========
def review_quiz(state: QuizState) -> QuizState:
    quiz: Dict[str, Any] = state.get('quiz_data', {})

    if not quiz or not quiz.get("question"):
        logger.info("Quiz review failed: Empty question or quiz data.")
        return {**state, "review_result": "NG"}

    answer: str = quiz.get('answer', '')
    choices: List[str] = quiz.get('choices', [])

    if answer not in choices:
        logger.info(f"Quiz review failed: Answer '{answer}' not in choices {choices}.")
        return {**state, "review_result": "NG"}

    if len(choices) != 4 or len(set(choices)) != 4:
        logger.info(f"Quiz review failed: Invalid choices length/duplicates {choices}.")
        return {**state, "review_result": "NG"}

    if answer not in VALID_WORDS:
        logger.info(f"Quiz review failed: Answer '{answer}' not in VALID_WORDS master.")
        return {**state, "review_result": "NG"}

    return {**state, "review_result": "OK"}

# ========== retry 判定（ログ強化） ==========
def should_retry(state: QuizState) -> str:
    review_result: str = state.get("review_result", "")
    retry_count: int = state.get("retry_count", 0)

    if review_result == "OK":
        logger.info(f"Quiz generated successfully on attempt {retry_count}.")
        return END

    if retry_count >= 3:
        logger.warning(f"Quiz generation reached max retries ({retry_count}). Returning last output.")
        return END

    logger.info(f"Quiz review NG. Triggering retry attempt {retry_count + 1}...")
    return "generator"

# ========== LangGraph 構築 ==========
quiz_graph = StateGraph(QuizState)
quiz_graph.add_node("analyzer", analyze_errors)
quiz_graph.add_node("generator", generate_quiz)
quiz_graph.add_node("reviewer", review_quiz)
quiz_graph.set_entry_point("analyzer")
quiz_graph.add_edge("analyzer", "generator")
quiz_graph.add_edge("generator", "reviewer")
quiz_graph.add_conditional_edges("reviewer", should_retry)
quiz_app = quiz_graph.compile()


# --- Firestore 書き込み ---
def add_or_update_stamp(mode: str, form_data: Dict[str, Any]) -> None:
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return

    student_name: str = student_info['name']
    target_date: str = form_data.get('date', '')

    data = {
        'date': target_date,
        'stamp': form_data.get('stamp'),
        'public_comment': form_data.get('public_comment'),
        'student_comment': form_data.get('student_comment'),
        'lesson_point': form_data.get('lesson_point'),
        'homework': form_data.get('homework'),
        'ai_memo': form_data.get('ai_memo'),
        'student_name': student_name,
        'category': form_data.get('category', '日常'),
        'timestamp': google_firestore.SERVER_TIMESTAMP
    }

    doc_ref = db.collection('students').document(student_name).collection('logs').document(target_date)
    doc_ref.set(data, merge=True)
    logger.info(f"Firestore save success: {student_name} / {target_date}")


# --- Firestore 読み込み ---
def get_logs(student_name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    query = (
        db.collection("students")
        .document(student_name)
        .collection("logs")
        .order_by("date", direction=google_firestore.Query.DESCENDING)
    )

    if limit:
        query = query.limit(limit)

    docs = query.stream()

    logs: List[Dict[str, Any]] = []
    for doc in docs:
        data = doc.to_dict()
        if 'date' not in data:
            data['date'] = doc.id
        logs.append(data)

    return logs


# --- 直近N か月のログを抽出 ---
def filter_recent_logs(logs: List[Dict[str, Any]], months_back: int = 2, student_name: str = 'taisei') -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today = datetime.now(student_timezone(student_name)).date()
    month_index = today.year * 12 + today.month - 1 - months_back
    cutoff_year, cutoff_month = divmod(month_index, 12)
    cutoff_date = today.replace(year=cutoff_year, month=cutoff_month + 1,
                                day=min(today.day, calendar.monthrange(cutoff_year, cutoff_month + 1)[1]))
    cutoff_date_str = cutoff_date.strftime('%Y-%m-%d')

    recent, older = [], []
    for log in logs:
        if log.get('date', '') >= cutoff_date_str:
            recent.append(log)
        else:
            older.append(log)

    return recent, older


# --- キャンディ関連 ---
def get_candy_count(student_name: str) -> int:
    ref = db.collection("students").document(student_name)
    doc = ref.get()
    return doc.to_dict().get("candy_count", 0) if doc.exists else 0


def add_candy(student_name: str) -> Dict[str, Any]:
    ref = db.collection("students").document(student_name)
    doc = ref.get()
    if not doc.exists:
        return {"candy": 0, "cookie_added": False}

    current = doc.to_dict().get("candy_count", 0)
    new_count = current + 1
    cookie_added = False

    if new_count >= 3:
        new_count = 0
        cookie_added = True
        today = datetime.now(student_timezone(student_name)).strftime('%Y-%m-%d')
        cookie_ref = db.collection("students").document(student_name).collection("logs").document(today)
        cookie_doc = cookie_ref.get()
        if cookie_doc.exists:
            current_cookies = cookie_doc.to_dict().get("quiz_cookie", 0)
            cookie_ref.update({"quiz_cookie": current_cookies + 1})
        else:
            cookie_ref.set({
                "date": today,
                "stamp": "🍪",
                "quiz_cookie": 1,
                "student_name": student_name,
                "category": "報酬"
            }, merge=True)

    ref.update({"candy_count": new_count})
    return {"candy": new_count, "cookie_added": cookie_added}


# --- ルート設定 ---

@app.route('/')
def home():
    return redirect(url_for('index', mode='s01'))

@app.route('/<mode>')
def index(mode: str):
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return redirect(url_for('index', mode='s01'))

    template_name = student_info['template']
    display_name = student_info['display']
    student_name = student_info['name']

    jst_time = datetime.now(JST).strftime('%H:%M')
    now_pst_dt = datetime.now(student_timezone(student_name))
    pst_time = now_pst_dt.strftime('%H:%M')
    year = int(request.args.get('year', now_pst_dt.year))
    month = int(request.args.get('month', now_pst_dt.month))

    all_logs = get_logs(student_name)
    log_dict = {d["date"]: d.get("stamp") for d in all_logs if d.get("date")}

    recent_logs, older_logs = filter_recent_logs(all_logs, months_back=2, student_name=student_name)

    latest_homework = ""
    if all_logs:
        sorted_logs = sorted(all_logs, key=lambda x: x.get("date", ""), reverse=True)
        for log in sorted_logs:
            if log.get("homework"):
                latest_homework = log.get("homework")
                break

    candy_count = get_candy_count(student_name)
    total_count = len(all_logs)
    first_weekday, last_date = calendar.monthrange(year, month)

    return render_template(template_name,
                           mode=mode,
                           display_name=display_name,
                           log_dict=log_dict,
                           recent_logs=recent_logs,
                           older_logs=older_logs,
                           all_logs=all_logs,
                           latest_homework=latest_homework,
                           candy_count=candy_count,
                           year=year,
                           month=month,
                           first_day_index=(first_weekday + 1) % 7,
                           last_date=last_date,
                           jst_time=jst_time,
                           pst_time=pst_time,
                           total=total_count,
                           sheet=0)

@app.route('/edit/<mode>/<date>')
def edit(mode: str, date: str):
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return redirect(url_for('home'))

    display_name = student_info['display']
    student_name = student_info['name']

    logs_ref = db.collection('students').document(student_name).collection('logs')
    doc = logs_ref.document(date).get()
    record = doc.to_dict() if doc.exists else None

    return render_template('edit.html',
                           mode=mode,
                           date=date,
                           display_name=display_name,
                           record=record)

@app.route('/save/<mode>', methods=['POST'])
def save(mode: str):
    form_data = request.form.to_dict()
    add_or_update_stamp(mode, form_data)
    return redirect(url_for('index', mode=mode))

@app.route("/admin/<student>")
def admin_page(student: str):
    student_mode, info = resolve_student(student)
    target_name = info['name']

    logs_ref = db.collection("students").document(target_name).collection("logs")
    docs = logs_ref.stream()

    logs = []
    for doc in docs:
        data = doc.to_dict()
        if 'date' not in data:
            data['date'] = doc.id
        logs.append(data)

    logs.sort(key=lambda x: x.get("date", ""), reverse=True)
    return render_template("admin.html", logs=logs, mode=target_name, student_mode=student_mode)

@app.route("/update/<student>/<date>", methods=["POST"])
def update_log(student: str, date: str):
    _, info = resolve_student(student)
    student = info['name']
    data = {
        "stamp": request.form.get("stamp"),
        "public_comment": request.form.get("public_comment"),
        "student_comment": request.form.get("student_comment"),
        "lesson_point": request.form.get("lesson_point"),
        "homework": request.form.get("homework"),
        "ai_memo": request.form.get("ai_memo"),
        "category": request.form.get("category", "日常"),
        "date": date
    }
    db.collection("students").document(student).collection("logs").document(date).set(data, merge=True)
    return redirect(f"/admin/{student}")

@app.route("/delete/<student>/<date>", methods=["POST"])
def delete_log(student: str, date: str):
    _, info = resolve_student(student)
    student = info['name']
    db.collection("students").document(student).collection("logs").document(date).delete()
    return redirect(f"/admin/{student}")

@app.route("/api/logs/older/<student>", methods=["GET"])
def get_older_logs(student: str):
    student_info = STUDENT_MAP.get(student)
    if not student_info:
        for m, info in STUDENT_MAP.items():
            if info['name'] == student.lower():
                student_info = info
                break

    if not student_info:
        return jsonify({"error": "Student not found"}), 404

    student_name = student_info['name']
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = min(50, max(1, int(request.args.get('limit', 10))))
    except ValueError:
        return jsonify({'error': 'Invalid pagination'}), 400

    all_logs = get_logs(student_name)
    _, older_logs = filter_recent_logs(all_logs, months_back=2, student_name=student_name)

    older_logs_sorted = sorted(older_logs, key=lambda x: x.get("date", ""), reverse=True)
    paginated = older_logs_sorted[offset:offset + limit]

    return jsonify({
        "logs": paginated,
        "total_older": len(older_logs_sorted),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < len(older_logs_sorted)
    })

@app.route("/api/candy/<student>", methods=["GET"])
def candy_status(student: str):
    count = get_candy_count(student.lower())
    return jsonify({"student": student, "candy_count": count})

@app.route("/api/candy/<student>/add", methods=["POST"])
def candy_add(student: str):
    result = add_candy(student.lower())
    return jsonify(result)

@app.route("/quiz/<mode>", methods=["GET", "POST"])
def quiz_page(mode: str):
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return redirect(url_for('home'))

    student_name = student_info['name']

    if request.method == "POST":
        user_answer = request.form.get("answer")
        correct_answer = request.form.get("correct_answer")
        question = request.form.get("question")
        choices = request.form.getlist("choices")

        is_correct = (user_answer == correct_answer)
        result = {
            "is_correct": is_correct,
            "user_answer": user_answer,
            "correct_answer": correct_answer
        }

        quiz_data = {
            "question": question,
            "answer": correct_answer,
            "choices": choices
        }

        return render_template("quiz.html",
                               mode=mode,
                               display_name=student_info['display'],
                               quiz=quiz_data,
                               result=result)

    else:
        logs = get_logs(student_name, limit=50)

        errors = []
        for l in logs:
            ai_memo = l.get('ai_memo', '')
            if not ai_memo:
                continue
            try:
                memo_data = json.loads(ai_memo)
                for activity in memo_data.get('activities', []):
                    for err in activity.get('errors_noted', []):
                        errors.append(err)
            except Exception as e:
                logger.debug(f"Non-JSON or invalid ai_memo skipped: {e}")

        errors_text = "\n".join(errors[-20:])
        logger.info(f"Extracted errors_text count: {len(errors)}")

        result = quiz_app.invoke({
            "errors_text": errors_text,
            "analysis": {},
            "quiz_data": {},
            "review_result": "",
            "retry_count": 0
        })
        quiz_data = result.get("quiz_data", {})

        return render_template("quiz.html",
                               mode=mode,
                               display_name=student_info['display'],
                               quiz=quiz_data,
                               result=None)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port)
