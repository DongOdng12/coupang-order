import os
import hmac
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

ACCESS_KEY = os.environ.get("COUPANG_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("COUPANG_SECRET_KEY", "")
VENDOR_ID  = os.environ.get("COUPANG_VENDOR_ID", "")
BASE_URL   = "https://api-gateway.coupang.com"

HISTORY_FILE = "order_history.json"

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

order_history = load_history()

def generate_hmac(method, path, query=""):
    import time
    gmtime = time.gmtime()
    dt = time.strftime('%y%m%d', gmtime) + 'T' + time.strftime('%H%M%S', gmtime) + 'Z'
    message = dt + method + path + query
    signature = hmac.new(
        SECRET_KEY.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    authorization = (
        "CEA algorithm=HmacSHA256, access-key=" + ACCESS_KEY +
        ", signed-date=" + dt +
        ", signature=" + signature
    )
    return authorization

def coupang_request(method, path, params=None, body=None):
    query = urllib.parse.urlencode(params) if params else ""
    authorization = generate_hmac(method, path, query)
    url = BASE_URL + path + ("?" + query if query else "")

    data = json.dumps(body).encode('utf-8') if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": authorization,
            "X-Requested-By": VENDOR_ID,
            "Content-Type": "application/json;charset=UTF-8",
        },
        method=method
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))

def today():
    return datetime.now().strftime("%Y-%m-%d")

def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

@app.route("/")
def health():
    return jsonify({"status": "ok", "vendor": VENDOR_ID})

@app.route("/api/config")
def config():
    return jsonify({
        "configured": bool(ACCESS_KEY and SECRET_KEY and VENDOR_ID),
        "vendorId": VENDOR_ID,
        "hasAccessKey": bool(ACCESS_KEY),
        "hasSecretKey": bool(SECRET_KEY),
    })

@app.route("/ip")
def get_ip():
    ip = urllib.request.urlopen("https://api.ipify.org").read().decode()
    return jsonify({"ip": ip})

@app.route("/api/orders")
def get_orders():
    status    = request.args.get("status", "INSTRUCT")
    date_from = request.args.get("from", days_ago(3))
    date_to   = request.args.get("to", today())

    path = f"/v2/providers/openapi/apis/api/v4/vendors/{VENDOR_ID}/ordersheets"
    params = {
        "createdAtFrom": date_from,
        "createdAtTo":   date_to,
        "status":        status,
        "maxPerPage":    100,
    }

    try:
        data = coupang_request("GET", path, params)
        orders_raw = data.get("data", [])

        result = []
        for order in orders_raw:
            items = order.get("orderItems", [])
            for item in items:
                oid = str(item.get("orderItemId", ""))
                hist = order_history.get(oid)
                receiver = order.get("receiver", {})
                addr = (receiver.get("addr1", "") + " " + receiver.get("addr2", "")).strip()

                result.append({
                    "orderId":               str(order.get("orderId", "")),
                    "orderItemId":           oid,
                    "orderAt":               order.get("orderedAt", ""),
                    "status":                order.get("status", ""),
                    "productName":           item.get("productName", ""),
                    "sellerProductItemName": item.get("sellerProductItemName", ""),
                    "vendorItemName":        item.get("vendorItemName", ""),
                    "shippingCount":         item.get("shippingCount", 1),
                    "receiverName":          receiver.get("name", ""),
                    "receiverPhone":         receiver.get("safeNumber") or receiver.get("phone", ""),
                    "receiverZipCode":       receiver.get("postCode", ""),
                    "receiverAddr":          addr,
                    "parcelPrintMessage":    order.get("parcelPrintMessage", ""),
                    "vendorItemId":          str(item.get("vendorItemId", "")),
                    "sellerProductId":       str(item.get("sellerProductId", "")),
                    "externalVendorSkuCode": item.get("externalVendorSkuCode", ""),
                    "downloaded":            hist is not None,
                    "downloadedAt":          hist.get("downloadedAt") if hist else None,
                    "downloadedSupplier":    hist.get("supplier") if hist else None,
                })

        return jsonify({"ok": True, "count": len(result), "orders": result})

    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "detail": traceback.format_exc()}), 500

@app.route("/api/orders/mark-downloaded", methods=["POST"])
def mark_downloaded():
    body = request.get_json()
    order_item_ids = body.get("orderItemIds", [])
    supplier       = body.get("supplier", "")
    downloaded_at  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for oid in order_item_ids:
        order_history[str(oid)] = {
            "supplier":     supplier,
            "downloadedAt": downloaded_at,
        }
    save_history(order_history)
    return jsonify({"ok": True, "marked": len(order_item_ids), "at": downloaded_at})

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

@app.route("/api/orders/unmark", methods=["POST"])
def unmark():
    body = request.get_json()
    oid = str(body.get("orderItemId", ""))
    if oid in order_history:
        del order_history[oid]
        save_history(order_history)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
