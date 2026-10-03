"""サンプルデータ生成（五幸建材工業型のダンプ＋処分場会社、5営業日分）。

実運用では、配車表は配車担当の紙を転記、伝票は運転手がLINEで送る写真を
extract_slip.py で読み取り、位置はデジタコ／会社スマホから取る。
ここでは同じ形のCSVを決定的に生成する。意図的に「漏れ」を仕込んである。
"""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
random.seed(7)

DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]


def write(name, rows, fields):
    with open(DATA / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ---------- マスタ ----------
clients = [
    dict(client_id="C1", name="大和ホーム", branch="横浜支店", invoice_format="yamato_xlsx", closing_day="末日", payment_terms_days=60),
    dict(client_id="C2", name="大和ホーム", branch="川崎支店", invoice_format="yamato_xlsx", closing_day="末日", payment_terms_days=60),
    dict(client_id="C3", name="鈴木基礎工業", branch="", invoice_format="plain", closing_day="末日", payment_terms_days=30),
    dict(client_id="C4", name="緑産業（解体）", branch="", invoice_format="plain", closing_day="末日", payment_terms_days=30),
]
write("clients.csv", clients, list(clients[0]))

sites = [
    # contract_type: per_truck_day（車建て） / per_m3（立米建て）
    dict(site_id="S1", client_id="C1", name="港北A現場", contract_type="per_truck_day", unit_price=70000, waiting_free_min=60, waiting_fee_per_hour=5000, default_disposal="DS1", note="車建て。回転が多いほど元請けが得"),
    dict(site_id="S2", client_id="C2", name="鶴見B現場", contract_type="per_m3", unit_price=4500, waiting_free_min=30, waiting_fee_per_hour=5000, default_disposal="DS1", note="立米建て。道が狭く積込待ちが出る"),
    dict(site_id="S3", client_id="C3", name="川崎C現場", contract_type="per_m3", unit_price=4200, waiting_free_min=30, waiting_fee_per_hour=5000, default_disposal="DS2", note="外部処分場。往復が長い"),
    dict(site_id="S4", client_id="C4", name="青葉D解体", contract_type="per_truck_day", unit_price=20000, waiting_free_min=60, waiting_fee_per_hour=3000, default_disposal="DS1", note="4t。がれき混入注意"),
]
write("sites.csv", sites, list(sites[0]))

vehicles = [
    dict(vehicle_id="V1", plate="横浜100あ1001", vclass="10t", capacity_m3=5.5, daily_cost=40000, digitacho="yes"),
    dict(vehicle_id="V2", plate="横浜100あ1002", vclass="10t", capacity_m3=5.5, daily_cost=40000, digitacho="yes"),
    dict(vehicle_id="V3", plate="横浜100あ1003", vclass="10t", capacity_m3=5.5, daily_cost=40000, digitacho="yes"),
    dict(vehicle_id="V4", plate="横浜100あ1004", vclass="10t", capacity_m3=5.5, daily_cost=40000, digitacho="yes"),
    dict(vehicle_id="V5", plate="横浜400あ2001", vclass="4t", capacity_m3=2.0, daily_cost=19000, digitacho="no"),
    dict(vehicle_id="V6", plate="横浜400あ2002", vclass="4t", capacity_m3=2.0, daily_cost=19000, digitacho="no"),
]
write("vehicles.csv", vehicles, list(vehicles[0]))

drivers = [
    dict(driver_id="D1", name="山田", day_rate=15000, line_id="line_yamada"),
    dict(driver_id="D2", name="佐藤", day_rate=15000, line_id="line_sato"),
    dict(driver_id="D3", name="鈴木", day_rate=15000, line_id="line_suzuki"),
    dict(driver_id="D4", name="高橋", day_rate=15000, line_id="line_takahashi"),
    dict(driver_id="D5", name="田中", day_rate=13000, line_id="line_tanaka"),
    dict(driver_id="D6", name="伊藤", day_rate=13000, line_id="line_ito"),
]
write("drivers.csv", drivers, list(drivers[0]))

disposal = [
    dict(disposal_id="DS1", name="西八朔処分場（自社）", own="yes", price_soil1=3000, price_soil2=3200, price_soil3=3800, price_soil4=5500, capacity_m3=60000, used_m3_before=41800, open_hour=7, close_hour=17),
    dict(disposal_id="DS2", name="外部処分場X", own="no", price_soil1=3500, price_soil2=3800, price_soil3=4200, price_soil4=6000, capacity_m3="", used_m3_before="", open_hour=8, close_hour=17),
]
write("disposal_sites.csv", disposal, list(disposal[0]))

# ---------- 配車表（予定） ----------
plan = [
    ("V1", "D1", "S1", 4, "DS1"),
    ("V2", "D2", "S2", 3, "DS1"),
    ("V3", "D3", "S3", 3, "DS2"),
    ("V4", "D4", "S2", 3, "DS1"),
    ("V5", "D5", "S4", 4, "DS1"),
    ("V6", "D6", "S4", 4, "DS1"),
]
dispatch = []
for d in DAYS:
    for v, dr, s, n, ds in plan:
        dispatch.append(dict(date=d, vehicle_id=v, driver_id=dr, site_id=s, planned_trips=n, disposal_id=ds, start_time="08:00"))
write("dispatch.csv", dispatch, list(dispatch[0]))

# ---------- 実績（位置イベント・伝票・受入） ----------
# 現場→処分場の片道時間（分）
leg = {("S1", "DS1"): 25, ("S2", "DS1"): 30, ("S3", "DS2"): 45, ("S4", "DS1"): 20}
soil_of_site = {"S1": "soil2", "S2": "soil1", "S3": "soil3", "S4": "soil2"}
cap = {v["vehicle_id"]: v["capacity_m3"] for v in vehicles}

location, slips, intake = [], [], []
slip_no = 0


def ts(d, minutes):
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


for d in DAYS:
    for v, dr, s, n, ds in plan:
        actual_trips = n
        # 仕込み: 10/02 V5 は5往復（予定超過）、09/29 V2 は待機で2往復しかできない
        if d == "2026-10-02" and v == "V5":
            actual_trips = 5
        if d == "2026-09-29" and v == "V2":
            actual_trips = 2
        t = 8 * 60  # 08:00 車庫出発
        for k in range(actual_trips):
            # 現場での滞在: 積込15分 + 待機
            wait_site = 0
            if s == "S2":
                wait_site = random.choice([0, 20, 45, 70])
            if d == "2026-09-30" and v == "V4":
                wait_site = 95  # 仕込み: 未申告の長い待機
            if d == "2026-09-29" and v == "V2":
                wait_site = 110
            arrive_site = t + leg[(s, ds)]
            depart_site = arrive_site + 15 + wait_site
            arrive_ds = depart_site + leg[(s, ds)]
            wait_ds = random.choice([0, 0, 10, 25]) if ds == "DS1" else random.choice([0, 15, 30])
            depart_ds = arrive_ds + 10 + wait_ds
            location += [
                dict(date=d, vehicle_id=v, place_type="site", place_id=s, arrive=ts(d, arrive_site), depart=ts(d, depart_site)),
                dict(date=d, vehicle_id=v, place_type="disposal", place_id=ds, arrive=ts(d, arrive_ds), depart=ts(d, depart_ds)),
            ]
            vol = cap[v]
            soil = soil_of_site[s]
            # 伝票（写真から読み取った体）。仕込み: 漏れ
            make_receipt = True
            make_intake = True
            if d == "2026-10-01" and v == "V2" and k == 2:
                make_receipt = False  # 現場の受領伝票が無い（受入票はある）
            if d == "2026-10-02" and v == "V3" and k == 2:
                make_receipt = False
                make_intake = False  # 両方無い。位置だけが往復を示す
            if make_receipt:
                slip_no += 1
                slips.append(dict(slip_id=f"R{slip_no:04d}", slip_type="site_receipt", date=d, vehicle_id=v, driver_id=dr, place_id=s, timestamp=ts(d, depart_site - 5), soil_type=soil, volume_m3=vol, photo=f"photos/{d}_{v}_{k+1}_receipt.jpg", confidence=0.97))
            if make_intake:
                slip_no += 1
                slips.append(dict(slip_id=f"I{slip_no:04d}", slip_type="disposal_intake", date=d, vehicle_id=v, driver_id=dr, place_id=ds, timestamp=ts(d, arrive_ds + 5), soil_type=soil, volume_m3=vol, photo=f"photos/{d}_{v}_{k+1}_intake.jpg", confidence=0.95))
            if ds == "DS1":
                intake.append(dict(date=d, time=ts(d, arrive_ds + 5)[-5:], hauler="自社", vehicle_id=v, vclass=next(x["vclass"] for x in vehicles if x["vehicle_id"] == v), soil_type=soil, volume_m3=vol, payment="internal"))
            t = depart_ds + leg[(s, ds)] // 2  # 次の現場へ

# 自社処分場への外部持込（他社ダンプ・工務店）
haulers = [("田村運輸", "credit"), ("横浜土木", "credit"), ("川崎解体", "cash"), ("青葉造園", "cash"), ("港北建設", "credit"), ("個人持込", "cash")]
for d in DAYS:
    n_ext = random.randint(28, 42)
    for _ in range(n_ext):
        h, pay = random.choice(haulers)
        vclass = random.choice(["4t", "4t", "10t", "10t", "10t", "2t"])
        vol = {"2t": 1.2, "4t": 2.0, "10t": 5.5}[vclass]
        soil = random.choices(["soil1", "soil2", "soil3", "soil4"], weights=[45, 30, 15, 10])[0]
        hour = random.choices(range(7, 17), weights=[2, 6, 9, 8, 5, 4, 7, 8, 5, 2])[0]
        minute = random.randint(0, 59)
        intake.append(dict(date=d, time=f"{hour:02d}:{minute:02d}", hauler=h, vehicle_id="", vclass=vclass, soil_type=soil, volume_m3=vol, payment=pay))
intake.sort(key=lambda r: (r["date"], r["time"]))

write("location_events.csv", location, list(location[0]))
write("slips.csv", slips, list(slips[0]))
write("intake.csv", intake, list(intake[0]))
print(f"dispatch={len(dispatch)} location_events={len(location)} slips={len(slips)} intake={len(intake)}")
