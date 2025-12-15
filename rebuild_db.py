"""Utility to rebuild the records.db used by the server.
Deletes existing records.db and creates tables + indexes.
Run from the workspace: python rebuild_db.py
"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(__file__), 'records.db')

if os.path.exists(DB):
    print(f"Removing existing DB: {DB}")
    os.remove(DB)
else:
    print(f"DB does not exist, will create: {DB}")

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    filename TEXT,
    uploaded_at TEXT
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS lap_records (
    id INTEGER PRIMARY KEY,
    upload_id INTEGER,
    car_type INTEGER,
    track_idx INTEGER,
    time REAL,
    driver_name TEXT,
    FOREIGN KEY(upload_id) REFERENCES uploads(id)
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS finish_records (
    id INTEGER PRIMARY KEY,
    upload_id INTEGER,
    name TEXT,
    races INTEGER,
    difficulty TEXT,
    FOREIGN KEY(upload_id) REFERENCES uploads(id)
)
""")

# Indexes
cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_track_idx ON lap_records(car_type, track_idx)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_time ON lap_records(car_type, time)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_track_time ON lap_records(track_idx, time)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_driver_name ON lap_records(driver_name)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_upload_id ON lap_records(upload_id)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_finish_upload_id ON finish_records(upload_id)')

conn.commit()
conn.close()
print('Rebuilt DB and created indexes.')
