@echo off
REM Test upload of dr.cfg to local server
set SERVER_URL=http://127.0.0.1:8000/upload
if not exist dr.cfg (
  echo dr.cfg not found in current folder. Copy the game's dr.cfg here and re-run.
  pause
  exit /b 1
)

echo Uploading dr.cfg to %SERVER_URL%
curl -v -F "file=@dr.cfg" %SERVER_URL%
pause
