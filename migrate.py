import csv
from google.cloud import firestore

db = firestore.Client(database='stamp-db')

CSV_FILE = "20260512My Stamp DB - シート1.csv"

def migrate():
    # studentsドキュメントの作成（candy_count初期化）
    for student in ["ann", "taisei"]:
        db.collection("students").document(student).set({"candy_count": 0})
        print(f"🍬 初期化: {student} / candy_count = 0")

    # logsサブコレクションにCSVデータ投入
    with open(CSV_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            student = row["生徒名"].strip().lower()
            date = row["日付"].strip()
            doc_ref = db.collection("students").document(student).collection("logs").document(date)
            doc_ref.set({
                "date": date,
                "stamp": row["スタンプ"].strip(),
                "public_comment": row["公開コメント"].strip(),
                "student_comment": row["生徒の感想"].strip(),
                "lesson_point": row["レッスンのポイント"].strip(),
                "homework": row["宿題"].strip(),
                "ai_memo": row["AIメモ"].strip(),
                "student_name": student
            })
            print(f"✅ Saved: {student} / {date}")

if __name__ == "__main__":
    migrate()