from google.cloud import firestore

db = firestore.Client()

STUDENTS = ["ann", "taisei"]

def init_candy():
    for student in STUDENTS:
        ref = db.collection("students").document(student)
        doc = ref.get()

        if doc.exists:
            data = doc.to_dict()
            # すでにcandy_countがある場合は上書きしない
            if "candy_count" in data:
                print(f"⏭️  スキップ: {student} はすでに candy_count = {data['candy_count']}")
            else:
                ref.update({"candy_count": 0})
                print(f"🍬 初期化完了: {student} / candy_count = 0")
        else:
            # ドキュメント自体がない場合は新規作成
            ref.set({"candy_count": 0})
            print(f"🍬 新規作成: {student} / candy_count = 0")

if __name__ == "__main__":
    init_candy()