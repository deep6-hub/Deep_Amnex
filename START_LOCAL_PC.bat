@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Local environment not found. Run INSTALL_LOCAL_PC.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
if not exist local_output mkdir local_output
if not exist local_data mkdir local_data
set GEE_LOCAL_OUTPUT=%CD%\local_output
set GEE_LOCAL_DATA=%CD%\local_data
echo ============================================================
echo Deep_Amnex running on THIS PC
echo Heavy GEE downloads, tile merging and raster processing run locally.
echo Keep this window open while a job is running.
echo Open http://127.0.0.1:8000 in your browser.
echo Output folder on this PC: %GEE_LOCAL_OUTPUT%
echo Uploaded input files on this PC: %GEE_LOCAL_DATA%
echo ============================================================
start "" http://127.0.0.1:8000
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
