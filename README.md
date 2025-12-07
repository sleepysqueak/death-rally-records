# Death Rally Records

Small Flask server to parse Death Rally `dr.cfg`, store lap/finish records in SQLite and expose simple APIs and HTML UI to browse leaderboards.

Quickstart
----------
1. Create a Python virtualenv and install dependencies:

   python -m venv .venv
   .\.venv\Scripts\activate
   pip install flask

2. (Optional) Rebuild DB to clear existing data:

   python rebuild_db.py

3. Start server:

   python server.py

4. Open browser: http://127.0.0.1:8000/

Files
-----
- `server.py` — Flask app
- `records.py` — dr.cfg parser
- `rebuild_db.py` — deletes and recreates `records.db` with indexes
- `test_upload.bat` — example curl-based uploader (Windows)

Notes
-----
- `records.db` is in the project root. It's ignored by `.gitignore` to avoid committing data.
- This project is intended for local use on a developer machine.
