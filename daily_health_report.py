
import io
import os
import json
import sqlite3
import zipfile
import urllib.parse
import urllib.request
import tempfile
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ZIP_FILE = Path(
    "/Users/jurek/Library/CloudStorage/GoogleDrive-answer261@gmail.com/"
    "My Drive/Vitals/Health Connect.zip"
)
ZIP_NAME = "Health Connect.zip"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
CHAT_ID = "5343130731"
TZ = ZoneInfo("Europe/Warsaw")

def local_time(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(ms) / 1000, timezone.utc
        ).astimezone(TZ)
    except (ValueError, TypeError, OSError, OverflowError):
        return None

def table_exists(db, table):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone() is not None

def get_columns(db, table):
    return {r[1] for r in db.execute(
        f'PRAGMA table_info("{table}")'
    )}

def first_column(cols, *options):
    return next((c for c in options if c in cols), None)

def drive_get(access_token, url):
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()

def download_zip_from_drive():
    # W chmurze: ZIP pobierany z Google Drive kontem serwisowym,
    # któremu udostępniono folder Vitals.
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    credentials.refresh(Request())
    query = urllib.parse.urlencode({
        "q": f"name = '{ZIP_NAME}' and trashed = false",
        "orderBy": "modifiedTime desc",
        "fields": "files(id, modifiedTime)",
        "pageSize": 1,
    })
    files = json.loads(drive_get(
        credentials.token,
        f"https://www.googleapis.com/drive/v3/files?{query}",
    ))["files"]
    if not files:
        raise SystemExit(f"Konto serwisowe nie widzi pliku {ZIP_NAME}.")
    return drive_get(
        credentials.token,
        f"https://www.googleapis.com/drive/v3/files/{files[0]['id']}"
        "?alt=media",
    )

if not TOKEN:
    raise SystemExit("Brak TELEGRAM_BOT_TOKEN w środowisku.")
if SERVICE_ACCOUNT_JSON:
    zip_source = io.BytesIO(download_zip_from_drive())
elif ZIP_FILE.exists():
    zip_source = ZIP_FILE
else:
    raise SystemExit(f"Nie znaleziono pliku ZIP: {ZIP_FILE}")

with zipfile.ZipFile(zip_source) as z:
    db_name = next(
        (n for n in z.namelist()
         if n.endswith("health_connect_export.db")), None
    )
    if not db_name:
        raise SystemExit("W ZIP-ie nie ma health_connect_export.db.")
    db_bytes = z.read(db_name)

report_day = datetime.now(TZ).date() - timedelta(days=1)
lines = [
    "📊 RAPORT DZIENNY",
    f"📅 {report_day:%d.%m.%Y}",
    "Zakres: wczoraj, czas lokalny",
    "",
]

with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
    tmp.write(db_bytes)
    tmp.flush()
    db = sqlite3.connect(tmp.name)

    # KROKI
    table = "steps_record_table"
    if table_exists(db, table):
        cols = get_columns(db, table)
        start_col = first_column(cols, "start_time")
        count_col = first_column(cols, "count", "steps")
        records = []
        if start_col and count_col:
            for start, count in db.execute(
                f'SELECT "{start_col}", "{count_col}" FROM "{table}"'
            ):
                dt = local_time(start)
                if dt and dt.date() == report_day and count is not None:
                    records.append(int(count))
        if records:
            lines.append(f"👣 Kroki: {sum(records):,}".replace(",", " "))
            lines.append(
                "   Uwaga: eksport może zawierać tylko część danych dnia."
            )
        else:
            lines.append("👣 Kroki: brak rekordu z tego dnia.")
    else:
        lines.append("👣 Kroki: brak tabeli w eksporcie.")

    # DYSTANS: baza zapisuje metry.
    table = "distance_record_table"
    if table_exists(db, table):
        cols = get_columns(db, table)
        start_col = first_column(cols, "start_time")
        value_col = first_column(cols, "distance", "meters", "value")
        entries = []
        if start_col and value_col:
            for start, value in db.execute(
                f'SELECT "{start_col}", "{value_col}" FROM "{table}"'
            ):
                dt = local_time(start)
                if dt and dt.date() == report_day and value is not None:
                    entries.append(float(value))
        if entries:
            lines.append(f"📍 Dystans: {sum(entries) / 1000:.2f} km")
        else:
            lines.append("📍 Dystans: brak rekordów z tego dnia.")

    # TĘTNO: seria z rzeczywistymi pomiarami BPM
    lines += ["", "❤️ TĘTNO"]
    table = "heart_rate_record_series_table"
    all_readings = []
    if table_exists(db, table):
        cols = get_columns(db, table)
        time_col = first_column(cols, "epoch_millis")
        bpm_col = first_column(cols, "beats_per_minute")
        if time_col and bpm_col:
            for ts, bpm in db.execute(
                f'SELECT "{time_col}", "{bpm_col}" FROM "{table}"'
            ):
                dt = local_time(ts)
                if dt and bpm is not None:
                    bpm = int(bpm)
                    if 25 <= bpm <= 240:
                        all_readings.append((dt, bpm))
    all_readings.sort()
    readings = [(dt, b) for dt, b in all_readings if dt.date() == report_day]
    if readings:
        # Średnia ważona czasem: każdy pomiar obowiązuje do następnego,
        # inaczej gęste pomiary z treningu zawyżają średnią dnia.
        day_start = datetime.combine(report_day, time(), TZ)
        day_end = min(day_start + timedelta(days=1), all_readings[-1][0])
        weighted = 0.0
        covered = 0.0
        for i, (dt, bpm) in enumerate(all_readings):
            next_dt = (
                all_readings[i + 1][0] if i + 1 < len(all_readings)
                else day_end
            )
            seconds = (
                min(next_dt, day_end) - max(dt, day_start)
            ).total_seconds()
            if seconds > 0:
                weighted += bpm * seconds
                covered += seconds
        values = [b for _, b in readings]
        average = weighted / covered if covered else sum(values) / len(values)
        lines.append(f"Zapisane pomiary: {len(values)}")
        lines.append(f"Średnia dnia (ważona czasem): {average:.0f} bpm")
        lines.append(f"Zakres: {min(values)}–{max(values)} bpm")

        # Pokazujemy tylko poważne zmiany: serie gęstych pomiarów
        # (np. trening) jako jedną linię i pojedyncze odchylenia
        # od średniej dnia o co najmniej 20 bpm.
        bursts = []
        for dt, bpm in readings:
            if bursts and dt - bursts[-1][-1][0] <= timedelta(minutes=10):
                bursts[-1].append((dt, bpm))
            else:
                bursts.append([(dt, bpm)])
        changes = []
        for burst in bursts:
            bpms = [b for _, b in burst]
            if len(burst) >= 3:
                changes.append(
                    f"• {burst[0][0]:%H:%M}–{burst[-1][0]:%H:%M}: "
                    f"{min(bpms)}–{max(bpms)} bpm "
                    f"(śr. {sum(bpms) / len(bpms):.0f})"
                )
            else:
                changes += [
                    f"• {dt:%H:%M}: {bpm} bpm"
                    for dt, bpm in burst if abs(bpm - average) >= 20
                ]
        if changes:
            lines.append("Poważne zmiany:")
            lines += changes
        else:
            lines.append("Brak poważnych zmian w ciągu dnia.")
    else:
        lines.append("Brak zapisanych pomiarów z tego dnia.")

    # KALORIE AKTYWNE: baza zapisuje kalorie, nie kilokalorie.
    table = "active_calories_burned_record_table"
    if table_exists(db, table):
        cols = get_columns(db, table)
        start_col = first_column(cols, "start_time")
        energy_col = first_column(cols, "energy")
        energies = []
        if start_col and energy_col:
            for start, energy in db.execute(
                f'SELECT "{start_col}", "{energy_col}" FROM "{table}"'
            ):
                dt = local_time(start)
                if dt and dt.date() == report_day and energy is not None:
                    energies.append(float(energy))
        if energies:
            lines.append("")
            lines.append(
                f"🔥 Kalorie aktywne: {sum(energies) / 1000:.0f} kcal"
            )
            lines.append(
                "Sprawdź w Samsung Health, czy eksport obejmuje cały dzień."
            )
        else:
            lines.append("🔥 Kalorie aktywne: brak rekordów z tego dnia.")

    # TRENINGI rozpoczęte tego dnia; odrzucamy błędne zakresy czasu.
    lines += ["", "🏋️ TRENINGI"]
    table = "exercise_session_record_table"
    sessions = []
    if table_exists(db, table):
        cols = get_columns(db, table)
        start_col = first_column(cols, "start_time")
        end_col = first_column(cols, "end_time")
        title_col = first_column(cols, "title")
        if start_col:
            select_cols = [start_col]
            if end_col:
                select_cols.append(end_col)
            if title_col:
                select_cols.append(title_col)
            sql = (
                "SELECT " + ", ".join(f'"{c}"' for c in select_cols) +
                f' FROM "{table}"'
            )
            for row in db.execute(sql):
                start = local_time(row[0])
                if not start or start.date() != report_day:
                    continue
                idx = 1
                end = local_time(row[idx]) if end_col else None
                idx += 1 if end_col else 0
                title = row[idx] if title_col else None
                duration = None
                if end and end >= start:
                    mins = (end - start).total_seconds() / 60
                    if 1 <= mins <= 360:
                        duration = round(mins)
                sessions.append((start, title, duration))
    if sessions:
        for start, title, duration in sorted(sessions):
            name = str(title).strip() if title else "Trening"
            suffix = f", {duration} min" if duration is not None else ""
            lines.append(f"• {start:%H:%M} — {name}{suffix}")
    else:
        lines.append("Brak sesji treningowej rozpoczętej tego dnia w eksporcie.")

    lines += [
        "",
        "ℹ️ Dane pochodzą z Health Connect ZIP.",
        "Serie, powtórzenia i ciężary nie są dostępne w tabelach sesji.",
        "Brak rekordu nie oznacza, że aktywności nie było.",
    ]
    db.close()

message = "\n".join(lines)

# Telegram przyjmuje do 4096 znaków na wiadomość.
if len(message) > 4000:
    message = message[:3950] + "\n… (raport skrócony)"

payload = json.dumps({
    "chat_id": CHAT_ID,
    "text": message,
}).encode("utf-8")

request = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(request, timeout=20) as response:
    result = json.loads(response.read().decode("utf-8"))

if not result.get("ok"):
    raise SystemExit("Telegram nie potwierdził wysłania raportu.")

print("Raport wysłany na Telegram.")
