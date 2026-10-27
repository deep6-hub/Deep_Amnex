# Deep_Amnex — browser upload version

This version replaces user-entered local filesystem paths with browser file pickers and uploads.

## What to upload in the web app

- **Earth Engine credentials:** select your service-account `.json` file and click **UPLOAD & VALIDATE KEY**.
- **District boundary:** select a ZIP containing the complete shapefile set (`.shp`, `.shx`, `.dbf`, `.prj`, etc.) and click **BROWSE & UPLOAD DISTRICT ZIP**.
- **Agriculture mask:** select the `.tif`/`.tiff` file and click **BROWSE & UPLOAD MASK**.
- **Local Processing:** select one or more GeoTIFFs and click **UPLOAD SELECTED GEOTIFFS**. If masking is needed, upload the mask too.

Output folders are created on the server automatically. When a job completes, use the **Download result ZIP** link in the status area.

## Render settings

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Keep `app.py`, `gee_worker.py`, `s2_worker.py`, `local_processor.py`, `requirements.txt`, `templates/`, and `static/` at the repository root.

## Important notes

- This app uses upload controls rather than browsing the server's filesystem. Browser security does not allow a website to choose an arbitrary local path on the visitor's computer.
- Uploaded files and generated results are stored in temporary server storage. On Render, this storage may be cleared by a restart or redeploy; download results promptly.
- The credential upload currently configures the application's Earth Engine service-account key. This is not a multi-user identity or access-control system. Do not expose the URL publicly or use it for untrusted users. For separate concurrent credentials, add session-aware job isolation and authentication.
- Never commit a real service-account key to GitHub or include it in a project ZIP.
- Large uploads may exceed limits imposed by the hosting platform or your plan.
