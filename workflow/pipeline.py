"""AI事務員の背骨: 配車表 → 日報 → 請求 → 入金 ／ 受入票 → 受入台帳 → 残容量 → 3枚の紙

入力 (data/):  clients, sites, vehicles, drivers, disposal_sites, dispatch(予定),
               slips(伝票=写真の読み取り結果), location_events(位置), intake(処分場受入)
出力 (out/):   trips.csv(1往復1行)  gaps.csv + line_gaps.txt(漏れと催促)
               daily_reports.csv + line_daily.txt(自動日報と夕方の確認)
               invoices.csv + invoice_*.txt(月末請求の下書き)
               owner_1_truckday.md  owner_2_intake.md  owner_3_drivers.md(社長に渡す3枚)
               summary.md
標準ライブラリのみ。python3 pipeline.py
"""
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA, OUT = ROOT / "data", ROOT / "out"
OUT.mkdir(exist_ok=True)

LOADING_MIN_SITE = 15      # 現場での標準積込時間。これを超えた滞在を待機とみなす
LOADING_MIN_DISPOSAL = 10  # 処分場での標準受入時間
YARD_MIN = 30              # 車庫〜初現場・最終〜車庫の加算（拘束時間用）
DRIVER_MONTHLY_LIMIT_H = 293  # 改善基準告示の月拘束時間の上限（目安）


def load(name):
    with open(DATA / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def minutes(a, b):
    return int((dt(b) - dt(a)).total_seconds() // 60)


def write_csv(name, rows, fields):
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_text(name, text):
    (OUT / name).write_text(text, encoding="utf-8")


# ---------------- マスタ ----------------
clients = {c["client_id"]: c for c in load("clients.csv")}
sites = {s["site_id"]: s for s in load("sites.csv")}
vehicles = {v["vehicle_id"]: v for v in load("vehicles.csv")}
drivers = {d["driver_id"]: d for d in load("drivers.csv")}
disposals = {d["disposal_id"]: d for d in load("disposal_sites.csv")}
for s in sites.values():
    s["unit_price"] = float(s["unit_price"]); s["waiting_free_min"] = int(s["waiting_free_min"]); s["waiting_fee_per_hour"] = float(s["waiting_fee_per_hour"])
for v in vehicles.values():
    v["capacity_m3"] = float(v["capacity_m3"]); v["daily_cost"] = float(v["daily_cost"])


def client_label(cid):
    c = clients[cid]
    return f"{c['name']} {c['branch']}".strip()


def disposal_price(ds_id, soil):
    return float(disposals[ds_id].get(f"price_{soil}", 0) or 0)


# ---------------- 1. 往復（1往復1行）を組み立てる ----------------
# 位置イベント（現場→処分場の対）を往復の骨格にし、伝票を貼り付ける。
dispatch = load("dispatch.csv")
plan = {(r["date"], r["vehicle_id"]): r for r in dispatch}
slips = load("slips.csv")
loc = load("location_events.csv")

by_vd = defaultdict(list)
for e in loc:
    by_vd[(e["date"], e["vehicle_id"])].append(e)

slip_pool = defaultdict(list)
for s in slips:
    slip_pool[(s["date"], s["vehicle_id"], s["slip_type"])].append(s)
for k in slip_pool:
    slip_pool[k].sort(key=lambda s: s["timestamp"])

trips = []
for (date, vid), events in sorted(by_vd.items()):
    events.sort(key=lambda e: e["arrive"])
    p = plan.get((date, vid), {})
    n = 0
    i = 0
    while i < len(events) - 1:
        a, b = events[i], events[i + 1]
        if a["place_type"] != "site" or b["place_type"] != "disposal":
            i += 1
            continue
        n += 1
        site, ds = sites[a["place_id"]], disposals[b["place_id"]]
        wait_site = max(0, minutes(a["arrive"], a["depart"]) - LOADING_MIN_SITE)
        wait_ds = max(0, minutes(b["arrive"], b["depart"]) - LOADING_MIN_DISPOSAL)
        # 伝票を時刻の近さで貼る
        def take(kind, lo, hi):
            pool = slip_pool[(date, vid, kind)]
            for s in pool:
                if lo <= s["timestamp"] <= hi:
                    pool.remove(s); return s
            return None
        receipt = take("site_receipt", a["arrive"], a["depart"])
        intake = take("disposal_intake", b["arrive"], b["depart"])
        vol = float((intake or receipt or {}).get("volume_m3") or vehicles[vid]["capacity_m3"])
        soil = (intake or receipt or {}).get("soil_type") or "unknown"
        trips.append(dict(
            trip_id=f"{date}_{vid}_{n}", date=date, vehicle_id=vid, driver_id=p.get("driver_id", ""),
            client_id=site["client_id"], site_id=site["site_id"], disposal_id=ds["disposal_id"],
            contract_type=site["contract_type"], trip_no=n,
            arrive_site=a["arrive"], depart_site=a["depart"], arrive_disposal=b["arrive"], depart_disposal=b["depart"],
            wait_site_min=wait_site, wait_disposal_min=wait_ds, volume_m3=vol, soil_type=soil,
            receipt_slip=receipt["slip_id"] if receipt else "", intake_slip=intake["slip_id"] if intake else "",
            evidence="slips" if (receipt and intake) else ("partial" if (receipt or intake) else "location_only"),
            disposal_cost=0.0 if ds["own"] == "yes" else vol * disposal_price(ds["disposal_id"], soil),
        ))
        i += 2

write_csv("trips.csv", trips, list(trips[0]))

# ---------------- 2. 漏れの検知と催促（その日のうちに） ----------------
gaps, line_gaps = [], []
trips_by_vd = defaultdict(list)
for t in trips:
    trips_by_vd[(t["date"], t["vehicle_id"])].append(t)

for (date, vid), p in sorted(plan.items()):
    ts = trips_by_vd.get((date, vid), [])
    drv = drivers[p["driver_id"]]["name"]
    site_name = sites[p["site_id"]]["name"]
    planned, actual = int(p["planned_trips"]), len(ts)
    if actual != planned:
        kind = "予定超過" if actual > planned else "予定未達"
        gaps.append(dict(date=date, vehicle_id=vid, driver=drv, kind=kind, detail=f"{site_name} 予定{planned}往復 / 実績{actual}往復", action="配車担当に確認。超過なら請求に反映、未達なら理由を日報に"))
    for t in ts:
        if not t["receipt_slip"]:
            gaps.append(dict(date=date, vehicle_id=vid, driver=drv, kind="受領伝票なし", detail=f"{site_name} {t['trip_no']}往復目（現場 {t['arrive_site'][-5:]}着）", action="本人にLINEで催促"))
            line_gaps.append(f"[{date} 16:00 → {drv}さん] {site_name}の{t['trip_no']}回目の受領伝票、写真まだ来てないです。夕方までにお願いします🙏")
        if not t["intake_slip"]:
            gaps.append(dict(date=date, vehicle_id=vid, driver=drv, kind="受入票なし", detail=f"{disposals[t['disposal_id']]['name']} {t['trip_no']}往復目", action="自社処分場なら受付の控えで補完。外部なら本人に催促"))
            if disposals[t["disposal_id"]]["own"] != "yes":
                line_gaps.append(f"[{date} 16:00 → {drv}さん] {disposals[t['disposal_id']]['name']}の{t['trip_no']}回目の受入票は？ 無ければ処分場に再発行を頼みます。")
        free = sites[t["site_id"]]["waiting_free_min"]
        if t["wait_site_min"] > free:
            gaps.append(dict(date=date, vehicle_id=vid, driver=drv, kind="待機（請求対象）", detail=f"{site_name} {t['trip_no']}往復目 現場待機 {t['wait_site_min']}分（無料枠{free}分）", action="日報に自動記載。待機料を請求一覧へ"))
    waits = [(t["trip_no"], t["wait_site_min"]) for t in ts if t["wait_site_min"] > sites[t["site_id"]]["waiting_free_min"]]
    if waits:
        detail = "、".join(f"{n}回目{w}分" for n, w in waits)
        line_gaps.append(f"[{date} 17:00 → {drv}さん] 今日の{site_name}、現場での待ちが {detail} になってますが合ってます？ 合ってれば待機料を請求に入れます。")

write_csv("gaps.csv", gaps, ["date", "vehicle_id", "driver", "kind", "detail", "action"])
write_text("line_gaps.txt", "\n".join(line_gaps) + "\n")

# ---------------- 3. 自動日報と夕方の確認 ----------------
daily, line_daily = [], []
for (date, vid), ts in sorted(trips_by_vd.items()):
    p = plan[(date, vid)]
    drv = drivers[p["driver_id"]]
    first, last = min(t["arrive_site"] for t in ts), max(t["depart_disposal"] for t in ts)
    bound_min = minutes(first, last) + YARD_MIN * 2
    wait_s, wait_d = sum(t["wait_site_min"] for t in ts), sum(t["wait_disposal_min"] for t in ts)
    m3 = sum(t["volume_m3"] for t in ts)
    sitelist = "・".join(sorted({sites[t["site_id"]]["name"] for t in ts}))
    dslist = "・".join(sorted({disposals[t["disposal_id"]]["name"] for t in ts}))
    missing = sum(1 for t in ts if t["evidence"] != "slips")
    daily.append(dict(date=date, driver_id=drv["driver_id"], driver=drv["name"], vehicle_id=vid, vclass=vehicles[vid]["vclass"],
                      sites=sitelist, disposals=dslist, trips=len(ts), planned=p["planned_trips"], volume_m3=round(m3, 1),
                      wait_site_min=wait_s, wait_disposal_min=wait_d, bound_hours=round(bound_min / 60, 1),
                      slips_missing=missing, status="要確認" if missing or len(ts) != int(p["planned_trips"]) else "OK待ち"))
    line_daily.append(
        f"[{date} 17:00 → {drv['name']}さん]\n"
        f"今日の日報です。合ってたら「OK」、違ったら直す箇所を送ってください。\n"
        f"　車両: {vid}（{vehicles[vid]['vclass']}）\n"
        f"　現場: {sitelist} → {dslist}\n"
        f"　往復: {len(ts)}回（予定{p['planned_trips']}）　{round(m3,1)}m3\n"
        f"　待機: 現場{wait_s}分 ／ 処分場{wait_d}分\n"
        f"　拘束: {round(bound_min/60,1)}時間（{first[-5:]}着〜{last[-5:]}発、車庫前後込み）\n"
        + (f"　※伝票が{missing}枚足りません。写真お願いします\n" if missing else "")
    )
write_csv("daily_reports.csv", daily, list(daily[0]))
write_text("line_daily.txt", "\n".join(line_daily))

# ---------------- 4. 月末請求の下書き（得意先×現場） ----------------
month = f"{min(t['date'] for t in trips)}〜{max(t['date'] for t in trips)}"
inv = defaultdict(lambda: dict(truck_days=set(), m3=0.0, trips=0, wait_fee=0.0, wait_hours=0.0, disposal_passthrough=0.0))
for t in trips:
    key = (t["client_id"], t["site_id"])
    r = inv[key]
    r["trips"] += 1
    r["disposal_passthrough"] += t["disposal_cost"]
    r["m3"] += t["volume_m3"]
    r["truck_days"].add((t["date"], t["vehicle_id"]))
    s = sites[t["site_id"]]
    over = max(0, t["wait_site_min"] - s["waiting_free_min"])
    if over:
        h = (over + 29) // 30 * 0.5  # 30分単位切り上げ
        r["wait_hours"] += h
        r["wait_fee"] += h * s["waiting_fee_per_hour"]

invoices = []
for (cid, sid), r in sorted(inv.items()):
    s = sites[sid]
    if s["contract_type"] == "per_truck_day":
        base = len(r["truck_days"]) * s["unit_price"]
        basis = f"車建て {len(r['truck_days'])}台日 × {int(s['unit_price']):,}円"
    else:
        base = r["m3"] * s["unit_price"]
        basis = f"立米建て {r['m3']:.1f}m3 × {int(s['unit_price']):,}円"
    invoices.append(dict(month=month, client=client_label(cid), invoice_format=clients[cid]["invoice_format"], payment_terms_days=clients[cid]["payment_terms_days"],
                         site=s["name"], contract=s["contract_type"], basis=basis, trips=r["trips"], truck_days=len(r["truck_days"]), m3=round(r["m3"], 1),
                         base_amount=int(base), waiting_hours=r["wait_hours"], waiting_fee=int(r["wait_fee"]), disposal_passthrough=int(r["disposal_passthrough"]), total=int(base + r["wait_fee"] + r["disposal_passthrough"]),
                         needs_human_check="yes"))
write_csv("invoices.csv", invoices, list(invoices[0]))
for cid in clients:
    rows = [x for x in invoices if x["client"] == client_label(cid)]
    if not rows:
        continue
    lines = [f"請求書（下書き・要確認）  {client_label(cid)} 御中  対象: {month}  書式: {clients[cid]['invoice_format']}  支払: 末締め{clients[cid]['payment_terms_days']}日後", ""]
    tot = 0
    for x in rows:
        lines.append(f"  {x['site']:　<10} {x['basis']:<32} {x['base_amount']:>10,}円")
        if x["waiting_fee"]:
            lines.append(f"  {'':　<10} 待機料 {x['waiting_hours']}h × {int(sites[next(k for k,v in sites.items() if v['name']==x['site'])]['waiting_fee_per_hour']):,}円 {x['waiting_fee']:>10,}円")
        if x["disposal_passthrough"]:
            lines.append(f"  {'':　<10} {'処分費（外部処分場・実費）':<32} {x['disposal_passthrough']:>10,}円")
        tot += x["total"]
    lines += ["", f"  合計（税抜）{tot:>44,}円", "", "  ※ 待機料は日報の位置記録に基づく。元請け書式へ転記前に人が確認する。"]
    write_text(f"invoice_{cid}.txt", "\n".join(lines) + "\n")

# ---------------- 5. 社長に渡す3枚 ----------------
# 5-1 車両別・得意先別・現場別の「1台1日の利益」
td = defaultdict(lambda: dict(trips=0, m3=0.0, wait=0, revenue=0.0, disposal_cost=0.0))
for t in trips:
    k = (t["date"], t["vehicle_id"], t["site_id"])
    r = td[k]
    r["trips"] += 1; r["m3"] += t["volume_m3"]; r["wait"] += t["wait_site_min"]; r["disposal_cost"] += t["disposal_cost"]
    s = sites[t["site_id"]]
    if s["contract_type"] == "per_m3":
        r["revenue"] += t["volume_m3"] * s["unit_price"]
    over = max(0, t["wait_site_min"] - s["waiting_free_min"])
    if over:
        r["revenue"] += ((over + 29) // 30 * 0.5) * s["waiting_fee_per_hour"]
for (date, vid, sid), r in td.items():
    s = sites[sid]
    if s["contract_type"] == "per_truck_day":
        r["revenue"] += s["unit_price"]
    r["revenue"] += r["disposal_cost"]  # 外部処分費は実費請求（通過）。利益はゼロ
    r["cost"] = vehicles[vid]["daily_cost"] + r["disposal_cost"]
    r["profit"] = r["revenue"] - r["cost"]

by_site = defaultdict(lambda: dict(td=0, trips=0, wait=0, revenue=0.0, cost=0.0))
for (date, vid, sid), r in td.items():
    b = by_site[sid]
    b["td"] += 1; b["trips"] += r["trips"]; b["wait"] += r["wait"]; b["revenue"] += r["revenue"]; b["cost"] += r["cost"]

lines = [f"# 1枚目: 1台1日の利益（{month}、{len(set(t['date'] for t in trips))}営業日）", "", "売上には外部処分場の処分費（実費請求）を含む。原価は車両の標準原価＋外部処分費。", "",
         "| 得意先 | 現場 | 契約 | 台日 | 平均回転 | 平均現場待機 | 1台日の売上 | 1台日の原価 | 1台日の利益 | 見立て |",
         "|---|---|---|---|---|---|---|---|---|---|"]
for sid, b in sorted(by_site.items(), key=lambda kv: (kv[1]["revenue"] - kv[1]["cost"]) / kv[1]["td"]):
    s = sites[sid]
    rev, cost, n = b["revenue"] / b["td"], b["cost"] / b["td"], b["td"]
    turns, wait = b["trips"] / n, b["wait"] / b["trips"]
    if s["contract_type"] == "per_truck_day" and turns >= 3.5:
        view = "車建てで回転が多い。元請けが得。立米建てか単価交渉の根拠"
    elif s["contract_type"] == "per_m3" and wait > s["waiting_free_min"]:
        view = "立米建てで待機が長い。待機料を取るか、積込体制を元請けに相談"
    elif rev - cost < 10000:
        view = "利益が薄い。単価か処分先を見直す"
    else:
        view = "良い現場。増やしたい"
    lines.append(f"| {client_label(s['client_id'])} | {s['name']} | {'車建て' if s['contract_type']=='per_truck_day' else '立米建て'} | {n} | {turns:.1f} | {wait:.0f}分 | {rev:,.0f}円 | {cost:,.0f}円 | **{rev-cost:,.0f}円** | {view} |")
tot_rev = sum(b["revenue"] for b in by_site.values()); tot_cost = sum(b["cost"] for b in by_site.values())
wait_fee_total = sum(x["waiting_fee"] for x in invoices)
lines += ["", f"- 期間の運搬売上 {tot_rev:,.0f}円、原価 {tot_cost:,.0f}円、利益 {tot_rev-tot_cost:,.0f}円（外部処分場の処分費は実費請求として売上と原価の両方に計上）",
          f"- うち待機料として請求できる額 **{wait_fee_total:,}円**（日報の位置記録から自動集計。今までは請求していなかった分）",
          "- 原価は車両の1日あたり標準原価（運転手・燃料・償却・保険）。実額は月次で置き換える", ""]
write_text("owner_1_truckday.md", "\n".join(lines) + "\n")

# 5-2 処分場（受入）
intake = load("intake.csv")
ds1 = disposals["DS1"]
ext = [r for r in intake if r["hauler"] != "自社"]
soil_agg = defaultdict(lambda: dict(n=0, m3=0.0, rev=0.0))
hauler_agg = defaultdict(lambda: dict(n=0, m3=0.0, rev=0.0, cash=0))
hour_agg = defaultdict(int)
own_m3 = 0.0
for r in intake:
    vol = float(r["volume_m3"]); price = disposal_price("DS1", r["soil_type"])
    hour_agg[int(r["time"][:2])] += 1
    if r["hauler"] == "自社":
        own_m3 += vol
        continue
    a = soil_agg[r["soil_type"]]; a["n"] += 1; a["m3"] += vol; a["rev"] += vol * price
    h = hauler_agg[r["hauler"]]; h["n"] += 1; h["m3"] += vol; h["rev"] += vol * price; h["cash"] += (r["payment"] == "cash")
ext_rev = sum(a["rev"] for a in soil_agg.values()); ext_m3 = sum(a["m3"] for a in soil_agg.values())
days = len(set(r["date"] for r in intake))
used = float(ds1["used_m3_before"]) + ext_m3 + own_m3
remaining = float(ds1["capacity_m3"]) - used
monthly_rate = (ext_m3 + own_m3) / days * 22
soil_name = {"soil1": "第1種（良い土）", "soil2": "第2種", "soil3": "第3種", "soil4": "第4種（悪い土）"}
lines = [f"# 2枚目: 処分場（{ds1['name']}）の受入（{month}、{days}営業日）", "",
         f"- 外部持込 {len(ext)}台 ／ {ext_m3:.0f}m3 ／ 受入売上 **{ext_rev:,.0f}円**（1日平均 {len(ext)/days:.0f}台、{ext_rev/days:,.0f}円）",
         f"- 自社車両の持込 {own_m3:.0f}m3（社内振替。外部に出せば {own_m3*3200:,.0f}円相当）",
         f"- 残容量 **{remaining:,.0f}m3**（{ds1['capacity_m3']}m3中 {used:,.0f}m3使用）。今の月間ペース {monthly_rate:,.0f}m3 → **残り約 {remaining/monthly_rate:.1f}ヶ月**", "",
         "## 土質別（外部持込）", "", "| 土質 | 台数 | m3 | 単価 | 売上 | 売上シェア |", "|---|---|---|---|---|---|"]
for soil in ["soil1", "soil2", "soil3", "soil4"]:
    a = soil_agg[soil]
    lines.append(f"| {soil_name[soil]} | {a['n']} | {a['m3']:.0f} | {disposal_price('DS1', soil):,.0f}円 | {a['rev']:,.0f}円 | {a['rev']/ext_rev*100:.0f}% |")
lines += ["", "## 持込業者別", "", "| 業者 | 台数 | m3 | 売上 | 現金比率 |", "|---|---|---|---|---|"]
for h, a in sorted(hauler_agg.items(), key=lambda kv: -kv[1]["rev"]):
    lines.append(f"| {h} | {a['n']} | {a['m3']:.0f} | {a['rev']:,.0f}円 | {a['cash']/a['n']*100:.0f}% |")
peak = max(hour_agg.items(), key=lambda kv: kv[1])
lines += ["", "## 時間帯別の台数（自社含む）", "", "| 時 | " + " | ".join(f"{h}" for h in range(7, 17)) + " |", "|---|" + "---|" * 10,
          "| 台 | " + " | ".join(str(hour_agg.get(h, 0)) for h in range(7, 17)) + " |", "",
          "## 社長に出す二択（判断は社長）", "",
          f"1. 第1種（単価{disposal_price('DS1','soil1'):,.0f}円）が台数の過半で残容量を使っている。第1種の受入を絞って第3・4種を優先するか、第1種の単価を上げるか。",
          f"2. {peak[0]}時台に受入が集中し待ちが出ている。早朝（7時前）と夕方（17時以降）の枠を開けて他社ダンプを取り込むか、ピークの単価を上げるか。",
          f"3. 残り約{remaining/monthly_rate:.0f}ヶ月。次の土地の目処を立て始めるか、受入単価を段階的に上げて残容量を延ばすか。", ""]
write_text("owner_2_intake.md", "\n".join(lines) + "\n")

# 5-3 運転手別
drv_agg = defaultdict(lambda: dict(days=0, trips=0, m3=0.0, wait=0, bound=0.0, rev=0.0))
for d in daily:
    a = drv_agg[d["driver_id"]]
    a["days"] += 1; a["trips"] += d["trips"]; a["m3"] += d["volume_m3"]; a["wait"] += d["wait_site_min"] + d["wait_disposal_min"]; a["bound"] += d["bound_hours"]
for (date, vid, sid), r in td.items():
    drv_agg[plan[(date, vid)]["driver_id"]]["rev"] += r["revenue"]
lines = [f"# 3枚目: 運転手別の稼働（{month}、{days}営業日）", "",
         "| 運転手 | 車格 | 稼働日 | 往復 | 1日平均回転 | 待機合計 | 拘束時間合計 | 1日平均拘束 | 月換算拘束 | 売上 | 見立て |", "|---|---|---|---|---|---|---|---|---|---|---|"]
for did, a in sorted(drv_agg.items(), key=lambda kv: -kv[1]["rev"]):
    d = drivers[did]; vcl = next(x["vclass"] for x in daily if x["driver_id"] == did)
    avg_b = a["bound"] / a["days"]; monthly = avg_b * 22
    if monthly > DRIVER_MONTHLY_LIMIT_H * 0.9:
        view = f"月{DRIVER_MONTHLY_LIMIT_H}hの上限に近い。配車を軽くするか休日を足す"
    elif a["wait"] / a["days"] > 60:
        view = "待機が多い。本人の責任ではない。待機手当の対象"
    else:
        view = "安定"
    lines.append(f"| {d['name']} | {vcl} | {a['days']} | {a['trips']} | {a['trips']/a['days']:.1f} | {a['wait']}分 | {a['bound']:.1f}h | {avg_b:.1f}h | {monthly:.0f}h | {a['rev']:,.0f}円 | {view} |")
lines += ["", f"- 月換算拘束は「1日平均 × 22日」。改善基準告示の目安 {DRIVER_MONTHLY_LIMIT_H}h（年3,400h枠）に対して見る",
          "- 待機は位置記録から自動。本人申告に頼らないので、公平な手当の根拠になる",
          "- 売上は配車された現場の売上を按分したもの。給与の根拠にはせず、配車の偏りを見るために使う", ""]
write_text("owner_3_drivers.md", "\n".join(lines) + "\n")

# ---------------- 6. サマリ ----------------
n_missing = sum(1 for t in trips if t["evidence"] != "slips")
summary = f"""# 実行サマリ（{month}、{days}営業日、{len(vehicles)}台）

| 項目 | 値 |
|---|---|
| 往復（1往復1行） | {len(trips)}行 |
| 伝票が揃っている往復 | {len(trips)-n_missing} / {len(trips)}（{(len(trips)-n_missing)/len(trips)*100:.0f}%） |
| 検知した漏れ・確認事項 | {len(gaps)}件 → `gaps.csv`、催促文 `line_gaps.txt` |
| 自動日報 | {len(daily)}枚 → `daily_reports.csv`、夕方の確認文 `line_daily.txt` |
| 請求書の下書き | {len(invoices)}行（得意先{len({x['client'] for x in invoices})}社）→ `invoices.csv`、`invoice_*.txt` |
| 今回新たに請求できる待機料 | {wait_fee_total:,}円 |
| 運搬の売上 / 利益 | {tot_rev:,.0f}円 / {tot_rev-tot_cost:,.0f}円 |
| 処分場の外部受入売上 | {ext_rev:,.0f}円（残容量 約{remaining/monthly_rate:.1f}ヶ月） |
| 社長に渡す3枚 | `owner_1_truckday.md` `owner_2_intake.md` `owner_3_drivers.md` |

運転手が新しくやったことは「伝票の写真を送る」だけ。配車表は配車担当の紙を転記。位置はデジタコ。
金に関わる出力（請求書・待機料）は `needs_human_check=yes`。人が見てから出す。
"""
write_text("summary.md", summary)
print(summary)
