import os, time, hmac, hashlib, json, re
from datetime import datetime, timedelta
from urllib.parse import urlencode
import urllib.request
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # 브라우저에서 호출 허용

# ── 환경변수 (Railway에서 설정) ──────────────────────────────
ACCESS_KEY = os.environ.get("COUPANG_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("COUPANG_SECRET_KEY", "")
VENDOR_ID  = os.environ.get("COUPANG_VENDOR_ID",  "")

BASE_URL = "https://api-gateway.coupang.com"

# ── 발주 이력 저장 (메모리 + JSON 파일) ────────────────────────
HISTORY_FILE = "order_history.json"

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}  # { orderItemId: { downloadedAt, supplier, ... } }

def save_history(data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

order_history = load_history()

# ── HMAC 서명 생성 ───────────────────────────────────────────
def make_auth(method, path, query=""):
    dt = datetime.utcnow().strftime("%y%m%dT%H%M%SZ")
    message = dt + method + path + query
    sig = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    auth = f"CEA algorithm=HmacSHA256, access-key={ACCESS_KEY}, signed-date={dt}, signature={sig}"
    return auth, dt

def coupang_get(path, params=None):
    query = urlencode(sorted(params.items())) if params else ""
    auth, dt = make_auth("GET", path, query)
    url = BASE_URL + path + ("?" + query if query else "")
    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Content-Type": "application/json;charset=UTF-8",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

# ── 날짜 헬퍼 ────────────────────────────────────────────────
def today(): return datetime.now().strftime("%Y-%m-%d")
def days_ago(n): return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

# ════════════════════════════════════════════════════════════
#  API 엔드포인트
# ════════════════════════════════════════════════════════════

@app.route("/")
def health():
    return jsonify({"status": "ok", "vendor": VENDOR_ID})

# ── 설정 확인 ─────────────────────────────────────────────────
@app.route("/api/config")
def config():
    return jsonify({
        "configured": bool(ACCESS_KEY and SECRET_KEY and VENDOR_ID),
        "vendorId": VENDOR_ID,
        "hasAccessKey": bool(ACCESS_KEY),
        "hasSecretKey": bool(SECRET_KEY),
    })

# ── 주문 조회 (상태별) ─────────────────────────────────────────
# status: ACCEPT(신규주문), INSTRUCT(상품준비중), DEPARTURE(배송중), DELIVERING(배송중2), FINAL_DELIVERY(배송완료)
@app.route("/api/orders")
def get_orders():
    status     = request.args.get("status", "INSTRUCT")
    date_from  = request.args.get("from",   days_ago(3))
    date_to    = request.args.get("to",     today())

    path = f"/v2/providers/openapi/apis/api/v4/vendors/{VENDOR_ID}/ordersheets"
    params = {
        "createdAtFrom": date_from,
        "createdAtTo":   date_to,
        "status":        status,
        "maxPerPage":    100,
    }
    try:
        data = coupang_get(path, params)
        orders = data.get("data", [])

        # 각 주문에 발주 이력 정보 붙이기
        result = []
        for order in orders:
            items = order.get("orderItems", [])
            for item in items:
                oid = str(item.get("orderItemId", ""))
                hist = order_history.get(oid)
                result.append({
                    # 쿠팡 주문 원본 필드
                    "orderId":         str(order.get("orderId", "")),
                    "orderItemId":     oid,
                    "orderAt":         order.get("orderedAt", ""),
                    "status":          order.get("status", ""),
                    "productName":     item.get("productName", ""),
                    "sellerProductItemName": item.get("sellerProductItemName", ""),
                    "vendorItemName":  item.get("vendorItemName", ""),
                    "shippingCount":   item.get("shippingCount", 1),
                    # 수취인 정보
                    "receiverName":    order.get("receiver", {}).get("name", ""),
                    "receiverPhone":   order.get("receiver", {}).get("safeNumber") or order.get("receiver", {}).get("phone", ""),
                    "receiverZipCode": order.get("receiver", {}).get("postCode", ""),
                    "receiverAddr":    (order.get("receiver", {}).get("addr1", "") + " " + order.get("receiver", {}).get("addr2", "")).strip(),
                    "shippingMsg":     order.get("shippingPriceType", ""),  # 배송메시지는 별도 필드
                    "parcelPrintMessage": order.get("parcelPrintMessage", ""),
                    # 업체상품코드
                    "vendorItemId":    str(item.get("vendorItemId", "")),
                    "sellerProductId": str(item.get("sellerProductId", "")),
                    "externalVendorSkuCode": item.get("externalVendorSkuCode", ""),  # 업체상품코드
                    # 발주 이력
                    "downloaded":      hist is not None,
                    "downloadedAt":    hist.get("downloadedAt") if hist else None,
                    "downloadedSupplier": hist.get("supplier") if hist else None,
                })
        return jsonify({"ok": True, "count": len(result), "orders": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── 발주 완료 기록 ────────────────────────────────────────────
@app.route("/api/orders/mark-downloaded", methods=["POST"])
def mark_downloaded():
    body = request.get_json()
    order_item_ids = body.get("orderItemIds", [])
    supplier       = body.get("supplier", "")
    downloaded_at  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for oid in order_item_ids:
        order_history[str(oid)] = {
            "supplier":    supplier,
            "downloadedAt": downloaded_at,
        }
    save_history(order_history)
    return jsonify({"ok": True, "marked": len(order_item_ids), "at": downloaded_at})

# ── 발주 이력 조회 ────────────────────────────────────────────
@app.route("/api/orders/history")
def get_history():
    supplier = request.args.get("supplier", "")
    result = []
    for oid, h in order_history.items():
        if supplier and h.get("supplier") != supplier:
            continue
        result.append({"orderItemId": oid, **h})
    result.sort(key=lambda x: x.get("downloadedAt", ""), reverse=True)
    return jsonify({"ok": True, "count": len(result), "history": result})

# ── 발주 이력 삭제 (재발주 허용) ─────────────────────────────────
@app.route("/api/orders/unmark", methods=["POST"])
def unmark():
    body = request.get_json()
    oid = str(body.get("orderItemId", ""))
    if oid in order_history:
        del order_history[oid]
        save_history(order_history)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404
@app.route("/ip")
def get_ip():
    import urllib.request
    ip = urllib.request.urlopen("https://api.ipify.org").read().decode()
    return jsonify({"ip": ip})
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
