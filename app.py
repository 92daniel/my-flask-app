import os
from flask import Flask, request, jsonify, render_template
import json
from geopy.distance import geodesic
from datetime import datetime, timedelta
import logging
from collections import OrderedDict
from pymongo import MongoClient  # ← 新增：連接 MongoDB

app = Flask(__name__)

# 日誌設定
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ===============================
# 連接 MongoDB 並載入兩筆資料：全時段路線 + route_stops
# ===============================
try:
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
    db = client["大數據專題"]

    # 載入「全時段_高鐵出發路線」集合
    raw_data = {}
    for doc in db["路線班表字典"].find():
        for key, value in doc.items():
            if key != "_id":
                raw_data[key] = value
    logger.info(f"從 MongoDB 成功載入 {len(raw_data)} 條全時段路線。")

    # 載入 route_stops 集合
    route_stops_doc = db["route_stops"].find_one()
    if route_stops_doc and "_id" in route_stops_doc:
        del route_stops_doc["_id"]
    route_stops = route_stops_doc if route_stops_doc else {}
    logger.info(f"從 MongoDB 成功載入 route_stops，共 {len(route_stops)} 筆路線。")

except Exception as e:
    logger.error(f"MongoDB 載入資料失敗：{str(e)}")
    raw_data = {}
    route_stops = {}

# 預處理：建立 route_data 與所有目的地座標清單
route_data = OrderedDict()
all_end_coords = []

for key, value in raw_data.items():
    if "目的地座標" in value:
        end_lat, end_lng = value["目的地座標"]
        route_key = f"24.80818,121.0405→{end_lat},{end_lng}"
        route_data[route_key] = value
        all_end_coords.append((end_lat, end_lng, route_key))


@app.route("/")
def index():
    return render_template("index2.html")


@app.route("/get_route", methods=["POST"])
def get_route():
    try:
        data = request.get_json()
        logger.debug(f"接收到的請求資料: {data}")

        if not data or "lat" not in data or "lng" not in data:
            return jsonify({
                "success": False,
                "message": "缺少必要的座標參數 (需要 'lat' 和 'lng')",
                "received_data": data
            })

        user_lat = float(data["lat"])
        user_lng = float(data["lng"])
        user_coord = (user_lat, user_lng)

        sorted_end_coords = sorted(
            all_end_coords,
            key=lambda x: geodesic(user_coord, (x[0], x[1])).meters
        )

        candidate_routes = []
        checked_count = 0

        for end_lat, end_lng, route_key in sorted_end_coords:
            if checked_count >= 3:
                break
            dist = geodesic(user_coord, (end_lat, end_lng)).meters
            checked_count += 1
            if route_key in route_data and "路徑" in route_data[route_key]:
                candidate_routes.append({
                    "route_key": route_key,
                    "end_coord": (end_lat, end_lng),
                    "distance": dist,
                    "raw_path": route_data[route_key]["路徑"]
                })
                logger.debug(f"找到候選路徑 {len(candidate_routes)}: {route_key}, 距離: {dist:.2f} 公尺")

        if not candidate_routes:
            return jsonify({
                "success": False,
                "message": "找不到合適的路線",
                "checked_endpoints": checked_count
            })

        processed_routes = []
        for route in candidate_routes:
            try:
                trimmed_path = []
                trim_reason = "未截斷"
                for i, step in enumerate(route["raw_path"]):
                    if len(step) >= 3 and isinstance(step[2], str):
                        try:
                            datetime.strptime(step[2], "%H:%M")
                        except ValueError:
                            continue
                    is_last_point = (i == len(route["raw_path"]) - 1)
                    if len(step) == 4 and step[3]:
                        step_coord = step[3]
                        dist_to_path_end = geodesic(step_coord, route["end_coord"]).meters
                        dist_to_user = geodesic(step_coord, user_coord).meters
                        if not is_last_point and dist_to_path_end <= 200:
                            trim_reason = f"距離推薦路徑終點 {dist_to_path_end:.2f} 公尺內 (非終點)"
                            new_step = ("抵達", step[1], step[2], step[3])
                            trimmed_path.append(new_step)
                            break
                        if dist_to_user <= 50:
                            trim_reason = f"距離使用者點擊位置 {dist_to_user:.2f} 公尺內"
                            new_step = ("抵達", step[1], step[2], step[3])
                            trimmed_path.append(new_step)
                            break
                    trimmed_path.append(step)

                start_time = datetime.strptime(trimmed_path[0][2], "%H:%M")
                end_time = datetime.strptime(trimmed_path[-1][2], "%H:%M")
                total_time = (end_time - start_time).total_seconds()

                coordinates = []
                for step in trimmed_path:
                    if len(step) == 4 and step[3]:
                        coordinates.append({
                            "stop": step[1],
                            "mode": step[0],
                            "time": step[2],
                            "lat": step[3][0],
                            "lng": step[3][1]
                        })

                processed_routes.append({
                    "route_key": route["route_key"],
                    "trimmed_path": trimmed_path,
                    "total_time": total_time,
                    "coordinates": coordinates,
                    "debug_info": {
                        "原始路徑長度": len(route["raw_path"]),
                        "截斷後路徑長度": len(trimmed_path),
                        "截斷原因": trim_reason,
                        "距離使用者點": route["distance"]
                    }
                })

            except Exception as e:
                logger.error(f"處理路線 {route['route_key']} 時發生錯誤: {str(e)}")
                continue

        if not processed_routes:
            return jsonify({
                "success": False,
                "message": "無法處理任何候選路線",
                "checked_endpoints": checked_count
            })

        best_route = min(processed_routes, key=lambda x: x["total_time"])

        total_time_str = str(timedelta(seconds=best_route["total_time"]))
        if total_time_str.startswith("0:"):
            total_time_str = total_time_str[2:]

        simplified_path = []
        for step in best_route["trimmed_path"]:
            if len(step) < 2:
                continue
            if step[0] == "走路":
                simplified_path.append(["走路", step[1]])
            elif step[0] == "步行轉乘":
                simplified_path.append(["步行轉乘", step[1]])
            elif step[0] == "抵達":
                simplified_path.append(["抵達", step[1]])
            elif len(step) >= 3:
                simplified_path.append([step[0], step[1], step[2]])

        return jsonify({
            "success": True,
            "總耗時": total_time_str,
            "到達時間": best_route["trimmed_path"][-1][2],
            "簡略路線": simplified_path,
            "詳細路線": best_route["trimmed_path"],
            "經緯度路徑": best_route["coordinates"],
            "debug_info": {
                "檢查的終點數量": checked_count,
                "候選路線數量": len(candidate_routes),
                "處理後路線數量": len(processed_routes),
                "選擇的路線鍵": best_route["route_key"],
                "總時間(秒)": best_route["total_time"],
                **best_route["debug_info"]
            }
        })

    except Exception as e:
        logger.error(f"處理請求時發生錯誤: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"伺服器錯誤: {str(e)}",
            "error_type": type(e).__name__
        })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
