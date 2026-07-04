import os
import calendar
from flask import Flask, render_template, request, redirect, url_for, jsonify
from datetime import datetime
import pytz

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import firestore as google_firestore

app = Flask(__name__)

# --- Firestore 初期化（プロジェクトID: my-stamp-app-487411 に固定） ---
if not firebase_admin._apps:
    firebase_admin.initialize_app()
# 正史データベース 'stamp-db' に繋ぎます
db = google_firestore.Client(project='my-stamp-app-487411', database='stamp-db')

# --- タイムゾーン設定 ---
JST = pytz.timezone('Asia/Tokyo')
PST = pytz.timezone('America/Los_Angeles')

# --- 生徒マッピング ---
STUDENT_MAP = {
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

# --- Firestore 書き込み（BigQuery用の category フィールドを追加） ---
def add_or_update_stamp(mode, form_data):
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return

    student_name = student_info['name']
    target_date = form_data.get('date')

    data = {
        'date': target_date,
        'stamp': form_data.get('stamp'),
        'public_comment': form_data.get('public_comment'),
        'student_comment': form_data.get('student_comment'),
        'lesson_point': form_data.get('lesson_point'),
        'homework': form_data.get('homework'),
        'ai_memo': form_data.get('ai_memo'),
        'student_name': student_name,
        'category': form_data.get('category', '日常'), # BigQuery分析用のタグ
        'timestamp': google_firestore.SERVER_TIMESTAMP
    }

    doc_ref = db.collection('students').document(student_name).collection('logs').document(target_date)
    # merge=True を使うことで、既存のフィールドを壊さずに更新・作成します
    doc_ref.set(data, merge=True)
    print(f"🔥 保存成功: {student_name} / {target_date}")


# --- Firestore 読み込み（エラー耐性強化版） ---
def get_logs(student_name):
    logs_ref = db.collection("students").document(student_name).collection("logs")
    docs = logs_ref.stream()

    logs = []
    for doc in docs:
        data = doc.to_dict()
        # セーフティ：もしデータ内に date が無ければドキュメントIDを使う
        if 'date' not in data:
            data['date'] = doc.id
        logs.append(data)

    # 日付順に並び替え
    logs.sort(key=lambda x: x.get("date", ""))
    return logs


# --- キャンディ取得 ---
def get_candy_count(student_name):
    ref = db.collection("students").document(student_name)
    doc = ref.get()
    if doc.exists:
        return doc.to_dict().get("candy_count", 0)
    return 0


# --- キャンディ加算（3個でクッキーに変換） ---
def add_candy(student_name):
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
        today = datetime.now(PST).strftime('%Y-%m-%d')
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
def index(mode):
    student_info = STUDENT_MAP.get(mode)
    if not student_info:
        return redirect(url_for('index', mode='s01'))

    template_name = student_info['template']
    display_name = student_info['display']
    student_name = student_info['name']

    jst_time = datetime.now(JST).strftime('%H:%M')
    pst_time = datetime.now(PST).strftime('%H:%M')

    now_pst_dt = datetime.now(PST)
    year = int(request.args.get('year', now_pst_dt.year))
    month = int(request.args.get('month', now_pst_dt.month))

    all_logs = get_logs(student_name)
    log_dict = {d["date"]: d.get("stamp") for d in all_logs if d.get("date")}
    
    # 宿題の表示（最新のログから取得）
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
def edit(mode, date):
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
def save(mode):
    form_data = request.form.to_dict()
    add_or_update_stamp(mode, form_data)
    return redirect(url_for('index', mode=mode))

@app.route("/admin/<student>")
def admin_page(student):
    # s01/s02 形式でも ann/taisei 形式でも対応できるように補正
    target_name = student
    for m, info in STUDENT_MAP.items():
        if m == student:
            target_name = info['name']
            break

    logs_ref = db.collection("students").document(target_name).collection("logs")
    docs = logs_ref.stream()

    logs = []
    for doc in docs:
        data = doc.to_dict()
        # ★ KeyError: 'date' 回避の徹底
        if 'date' not in data:
            data['date'] = doc.id
        logs.append(data)

    logs.sort(key=lambda x: x.get("date", ""), reverse=True)

    return render_template("admin.html", logs=logs, mode=student)

@app.route("/update/<student>/<date>", methods=["POST"])
def update_log(student, date):
    # admin画面からの直接更新用
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

@app.route("/delete/<student>/<date>")
def delete_log(student, date):
    db.collection("students").document(student).collection("logs").document(date).delete()
    return redirect(f"/admin/{student}")

# --- APIルート等（既存通り） ---
@app.route("/api/candy/<student>", methods=["GET"])
def candy_status(student):
    count = get_candy_count(student.lower())
    return jsonify({"student": student, "candy_count": count})

@app.route("/api/candy/<student>/add", methods=["POST"])
def candy_add(student):
    result = add_candy(student.lower())
    return jsonify(result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port)
