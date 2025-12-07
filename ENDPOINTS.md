Death Rally Records server — Endpoints and usage
===============================================

Overview
--------
This repository provides a small Flask server that parses Death Rally `dr.cfg` files, stores parsed records in a local SQLite database (`records.db`) and exposes HTTP endpoints to upload files, inspect leaderboards and browse top times.

Run the server
--------------
1. Create a Python virtualenv (recommended) and install Flask:

   pip install flask

2. Start the server from the project root:

   python server.py

By default the server binds to `127.0.0.1:8000`.

Files of interest
-----------------
- `server.py` — Flask application and endpoints.
- `records.py` — parser that reads `dr.cfg` and returns structured records.
- `records.db` — SQLite database created automatically when you upload a file.
- `test_upload.bat` — example Windows batch script that posts `dr.cfg` to the running server (uses `curl`).
- `ENDPOINTS.md` — this document.

Database
--------
The server saves uploads to a local SQLite DB (`records.db`) alongside `server.py`. Tables:
- `uploads` (id, filename, uploaded_at)
- `lap_records` (id, upload_id, rec_no, car_name, track_name, idx, time, driver_name)
- `finish_records` (id, upload_id, rec_no, name, races, difficulty)

Each upload creates an `uploads` row and inserts the parsed lap and finish records linked by `upload_id`. `uploaded_at` is stored as UTC ISO string (ending with `Z`).

Endpoints
---------
1) GET /
- Returns a small upload HTML form.

2) POST /upload
- Purpose: upload a `dr.cfg` file to be parsed and stored.
- Form field: `file` (multipart file upload).
- Example curl (from project folder):

  curl -F "file=@dr.cfg" http://127.0.0.1:8000/upload

- Response: JSON with fields `lap_records`, `finish_records`, and `upload_id`.
  - `lap_records` / `finish_records` are arrays of parsed dataclass objects serialized to JSON.

3) GET /leaderboards
- Purpose: return a JSON snapshot of leaderboards.
- Response: JSON containing `lap_leaders` (best lap per car+track) and `finish_leaders` (best races per finish record).

4) GET /leaderboards/view
- Purpose: simple HTML page rendering the leaderboards.
- Open in browser: http://127.0.0.1:8000/leaderboards/view

5) GET /api/top_times
- Purpose: query top lap times with optional filters.
- Query parameters (all optional):
  - `car` — exact car name
  - `track` — exact track name
  - `driver` — partial match (substring) on driver name
  - `limit` — integer; default 10
- Example:

  curl "http://127.0.0.1:8000/api/top_times?car=Vagabond&track=Suburbia&limit=5"

- Response: JSON `{ results: [ {car_name, track_name, driver_name, time}, ... ] }` ordered by ascending time.

6) GET /api/meta
- Purpose: supply distinct car/track/driver values used by the UI.
- Response: JSON `{ cars: [...], tracks: [...], drivers: [...] }`.

7) GET /browse
- Purpose: interactive HTML UI to filter and show best 10 drivers per car/track/driver filter. Uses `/api/meta` and `/api/top_times`.
- Open in browser: http://127.0.0.1:8000/browse

Testing
-------
- `test_upload.bat` (Windows) posts `dr.cfg` from the current folder to the local server. Run it while the server is running.
- You can also use the upload form at `/` or the `curl` example above.

Notes and suggestions
---------------------
- The server stores uploads and records in `records.db` in the same folder as `server.py`.
- The parser expects the `dr.cfg` layout implemented in `records.py` (lap records 24 bytes, finish records 20 bytes, known offsets for fields). If you modify the parser, the database schema may need adjustments.
- This server is intended for local use (binds to `127.0.0.1`). Do not expose it publicly without adding authentication and securing uploads.

Contact
-------
If you want changes to the API, additional views, CSV export, or pagination, I can add them.
