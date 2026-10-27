# Deep_Amnex — direct-to-PC processing

## What this package does

Run the existing FastAPI interface locally on Windows. Earth Engine downloads, tile merging, and raster processing then run on the same PC, and the study-area output folder is created under `local_output` rather than Render's temporary filesystem. Uploaded study-area inputs are stored under `local_data/study_uploads`.

## Setup

1. Extract the ZIP to a writable folder, e.g. `D:\Deep_Amnex`.
2. Install Python 3.11 or 3.12 (64-bit) and enable Add Python to PATH.
3. Run `INSTALL_LOCAL_PC.bat` once.
4. Run `START_LOCAL_PC.bat` and keep its console window open while processing.
5. Use `http://127.0.0.1:8000` in your browser.
6. Configure your GEE service-account JSON in this local app. Do not commit the key to GitHub.
7. Upload district ZIP and agriculture-mask GeoTIFF, load dates, and run the workflow.
8. Find outputs in the `local_output` folder beside the app. Uploaded study inputs are under `local_data`.

## Important architecture note

This local mode runs the app itself on your PC. It does **not** make a button on the public Render site execute arbitrary jobs on your PC. A public-site-to-local-agent bridge would require a separate authenticated, outbound-polling agent and server-side job queue. Until that bridge is implemented, use the local URL for large jobs. Do not expose the local server to the public internet.

## Storage

Allow at least 10–15 GB free for a 5.25–7 GB seasonal input set, tiles, intermediate rasters, and outputs. Actual requirements depend on tile overlap, compression, and output format. Keep the PC awake and connected while processing.
