"""伝票の写真 → 1行のレコード（AIの読み取り部分）。

運転手がLINEで送った「現場の受領伝票」「処分場の受入票」の写真を、
pipeline.py が読める slips.csv の1行に変換する。

使い方:
    python3 extract_slip.py photos/2026-10-01_V2_3_receipt.jpg [more.jpg ...]
    → 標準出力に slips.csv 形式の行を追記できるCSVを出す

API キーが無い環境では pipeline.py は data/slips.csv をそのまま使う。
"""
import base64
import csv
import sys
from pathlib import Path

MODEL = "claude-opus-5-5"

SLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "slip_type": {"type": "string", "enum": ["site_receipt", "disposal_intake", "unknown"]},
        "date": {"type": "string", "description": "YYYY-MM-DD。読めなければ空文字"},
        "time": {"type": "string", "description": "HH:MM。読めなければ空文字"},
        "place_name": {"type": "string", "description": "現場名または処分場名。印字・手書き・印影から"},
        "company_name": {"type": "string", "description": "元請け・得意先名。読めなければ空文字"},
        "vehicle_plate": {"type": "string", "description": "車番。読めなければ空文字"},
        "soil_type": {"type": "string", "enum": ["soil1", "soil2", "soil3", "soil4", "sludge", "mixed_waste", "unknown"],
                      "description": "土質区分。がれき等の混入があれば mixed_waste"},
        "volume_m3": {"type": "number", "description": "立米。記載が無ければ 0"},
        "stamp_present": {"type": "boolean", "description": "受領印・担当印があるか"},
        "confidence": {"type": "number", "description": "0〜1。読み取り全体の自信"},
        "needs_human": {"type": "boolean", "description": "ピンボケ・欠け・金額や土質が読めない等、人が見るべきか"},
        "note": {"type": "string", "description": "人への申し送り。短く"},
    },
    "required": ["slip_type", "date", "time", "place_name", "company_name", "vehicle_plate",
                 "soil_type", "volume_m3", "stamp_present", "confidence", "needs_human", "note"],
    "additionalProperties": False,
}

PROMPT = (
    "これは建設現場の残土運搬で使う伝票の写真です。"
    "「現場の受領伝票（元請け・基礎屋の受領印があるもの）」か"
    "「処分場の受入票（処分場名・土質・立米が書かれたもの）」のどちらかです。"
    "読み取れる項目だけを埋め、読めない項目は空文字か0にしてください。推測で埋めないでください。"
    "土質区分は、第1種〜第4種建設発生土を soil1〜soil4、泥土を sludge、がれき等の混入があれば mixed_waste。"
    "写真がぼけている、端が切れている、金額や土質が読めない場合は needs_human を true にしてください。"
)


def extract(image_path: str) -> dict:
    import anthropic  # 遅延import。SDKが無い環境でも pipeline.py は動く
    import json

    client = anthropic.Anthropic()
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode("utf-8")
    suffix = Path(image_path).suffix.lower()
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[suffix.lstrip(".")]

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2048,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SLIP_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        return {"slip_type": "unknown", "needs_human": True, "confidence": 0.0, "note": "モデルが処理を拒否。人が見る"}
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


FIELDS = ["slip_id", "slip_type", "date", "vehicle_id", "driver_id", "place_id", "timestamp",
          "soil_type", "volume_m3", "photo", "confidence"]


def main(paths):
    w = csv.DictWriter(sys.stdout, fieldnames=FIELDS + ["place_name", "needs_human", "note"])
    w.writeheader()
    for i, p in enumerate(paths, 1):
        r = extract(p)
        # 現場名・車番 → ID の突合は pipeline 側のマスタで行う。ここでは読んだままを出す
        w.writerow({
            "slip_id": f"X{i:04d}", "slip_type": r.get("slip_type", "unknown"), "date": r.get("date", ""),
            "vehicle_id": r.get("vehicle_plate", ""), "driver_id": "", "place_id": "",
            "timestamp": f"{r.get('date','')} {r.get('time','')}".strip(), "soil_type": r.get("soil_type", "unknown"),
            "volume_m3": r.get("volume_m3", 0), "photo": p, "confidence": r.get("confidence", 0),
            "place_name": r.get("place_name", ""), "needs_human": r.get("needs_human", True), "note": r.get("note", ""),
        })


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
