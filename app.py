from pathlib import Path
import threading
import traceback
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import json
import os
import shutil
import tempfile
import zipfile
import re
from pathlib import Path

from gee_worker import process, get_available_dates
from s2_worker import process_s2, get_s2_dates, S2_COLLECTION
import ee
from gee_worker import initialize_gee, _select_district, set_service_account_key
from local_processor import list_geotiffs, run_local_processing

app = FastAPI(title="Sentinel-1 VH GEE → Direct Local Download Automation")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

JOBS = {}
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "user_config.json"

def _credential_path_status():
    env_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY")
    if env_path:
        path = Path(env_path).expanduser()
    elif CONFIG_PATH.is_file():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("credential_path", "")
            path = Path(saved).expanduser() if saved else BASE_DIR / "credentials.json"
        except Exception:
            path = BASE_DIR / "credentials.json"
    else:
        path = BASE_DIR / "credentials.json"
    project_id = ""
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project_id = str(data.get("project_id", "")).strip()
        except Exception:
            pass
    return {
        "configured": path.is_file() and bool(project_id),
        "path": str(path.resolve()) if path.is_file() else "",
        "project_id": project_id,
        "default_path": str(BASE_DIR / "credentials.json"),
    }


# Uploaded study-area inputs for the online satellite workflow.
LOCAL_DATA_ROOT = Path(os.environ.get("GEE_LOCAL_DATA", str(BASE_DIR / "local_data"))).expanduser().resolve()
STUDY_UPLOAD_ROOT = LOCAL_DATA_ROOT / "study_uploads"
STUDY_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

@app.post("/study/upload-district")
async def upload_study_district(file: UploadFile = File(...)):
    folder = STUDY_UPLOAD_ROOT / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path = await _save_upload(file, folder, {".zip"})
        # Validate ZIP structure and require a shapefile component.
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            shp_names = [n for n in names if n.lower().endswith(".shp")]
            if not shp_names:
                raise ValueError("The ZIP must contain a .shp file and its companion files (.shx, .dbf, preferably .prj).")
            bad = zf.testzip()
            if bad:
                raise ValueError(f"The ZIP archive is corrupted near {bad}.")
        return {"path": str(path), "filename": file.filename, "message": "District ZIP uploaded."}
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse(status_code=400, content={"error": f"District upload failed: {type(exc).__name__}: {exc}"})

@app.post("/study/upload-mask")
async def upload_study_mask(file: UploadFile = File(...)):
    folder = STUDY_UPLOAD_ROOT / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path = await _save_upload(file, folder, {".tif", ".tiff"})
        return {"path": str(path), "filename": file.filename, "message": "Agriculture mask uploaded."}
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse(status_code=400, content={"error": f"Mask upload failed: {type(exc).__name__}: {exc}"})

@app.post("/local/browse-folder")
def browse_local_folder():
    """Open a native folder picker on the computer running this app.

    This is intended for the Windows local-PC version. A browser hosted on Render
    cannot open a folder picker on the user's own computer.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        selected = filedialog.askdirectory(title="Choose output folder", mustexist=False)
        root.destroy()
        if not selected:
            return {"cancelled": True, "path": ""}
        path = Path(selected).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return {"cancelled": False, "path": str(path)}
    except Exception as exc:
        return JSONResponse(status_code=501, content={"error": "Native folder browsing is available when Deep_Amnex is running locally on your PC. " + str(exc)})

@app.post("/study/create-output-folder")
def create_study_output_folder():
    """Create a persistent output folder on the machine running this app.

    When run locally, this is the user's PC. On Render, it is still the server.
    """
    root = Path(os.environ.get("GEE_LOCAL_OUTPUT", str(BASE_DIR / "local_output"))).expanduser().resolve()
    path = root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return {"path": str(path), "display": f"Local output folder: {path}"}

class JobRequest(BaseModel):
    district_path: str
    district_field: str = "district"
    district_name: str
    mask_path: str
    output_dir: str
    year: int
    start_month: int
    end_month: int
    polarization: str = "VH"
    orbit: str = "DESCENDING"
    crs: str = "EPSG:32642"
    selected_dates: list[str] = []
    mode: str = "s1"
    selected_bands: list[str] = []
    selected_indices: list[str] = []
    selected_outputs: list[dict] = []


class LocalFilesRequest(BaseModel):
    folder: str
    recursive: bool = False


class LocalProcessingRequest(BaseModel):
    operation: str = "stack"
    input_paths: list[str] = []
    output_dir: str
    output_name: str = "Local_Processed_Output.tif"
    mask_path: str = ""


@app.get("/credentials/status")
def credentials_status():
    return _credential_path_status()

@app.post("/credentials/configure")
async def configure_credentials(file: UploadFile = File(...)):
    """Upload, validate, and store a local GEE service-account key on this PC."""
    if not file.filename or Path(file.filename).suffix.lower() != ".json":
        return JSONResponse(status_code=400, content={"error": "Choose a service-account .json key file."})

    raw = await file.read(1_048_577)
    await file.close()
    if not raw:
        return JSONResponse(status_code=400, content={"error": "The selected JSON file is empty."})
    if len(raw) > 1_048_576:
        return JSONResponse(status_code=400, content={"error": "The JSON key file is too large (maximum 1 MB)."})

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "The selected file is not valid UTF-8 JSON."})

    required = ("client_email", "private_key", "project_id")
    missing = [key for key in required if not data.get(key)]
    if missing:
        return JSONResponse(status_code=400, content={"error": "This does not look like a Google service-account key. Missing: " + ", ".join(missing)})

    # Keep uploaded secrets in a dedicated local-only folder, not in the ZIP or UI.
    key_dir = BASE_DIR / "private_credentials"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / "gee-service-account.json"
    temp_path = key_dir / ("upload-" + uuid.uuid4().hex + ".tmp")
    try:
        temp_path.write_text(json.dumps(data), encoding="utf-8")
        # Validate the uploaded key with Earth Engine before recording it as active.
        set_service_account_key(temp_path)
        initialize_gee()
        temp_path.replace(key_path)
        set_service_account_key(key_path)
        os.environ["GEE_SERVICE_ACCOUNT_KEY"] = str(key_path.resolve())
        initialize_gee()
        CONFIG_PATH.write_text(json.dumps({"credential_path": str(key_path.resolve())}, indent=2), encoding="utf-8")
        return {"configured": True, "filename": Path(file.filename).name, "project_id": data["project_id"], "message": "Credentials validated and saved on this PC."}
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return JSONResponse(status_code=400, content={"error": f"Credential validation failed: {type(exc).__name__}: {exc}"})

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/dates")
def get_dates(job: JobRequest):
    try:
        if job.mode == "s2":
            dates = get_s2_dates(job.district_path, job.district_field, job.district_name, job.year, job.start_month, job.end_month)
            return {"dates": dates}
        dates = get_available_dates(
            job.district_path,
            job.district_field,
            job.district_name,
            job.year,
            job.start_month,
            job.end_month,
            job.polarization,
            job.orbit,
        )
        return {"dates": dates}
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"error": traceback.format_exc()},
        )


def run_job(job_id, payload):
    JOBS[job_id] = {"status": "running", "logs": []}

    def log(message, level=1):
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        JOBS[job_id]["logs"].append(f"[{timestamp}] {message}")

    job_started = time.perf_counter()
    log("[JOB START] Full workflow started")
    try:
        if payload.get("mode") == "s2":
            process_s2(payload, log)
        else:
            process(payload, log)
        elapsed = time.perf_counter() - job_started
        log(f"[JOB END] Workflow completed | total elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")
        JOBS[job_id]["status"] = "completed"
    except Exception:
        elapsed = time.perf_counter() - job_started
        log(f"[JOB FAILED] Workflow stopped | elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["logs"].append("ERROR:")
        JOBS[job_id]["logs"].append(traceback.format_exc())



@app.post("/preview/{preview_date}")
def preview_image(preview_date: str, job: JobRequest):
    """Return a temporary Earth Engine tile URL for S1 VH/VV or S2 FCC preview."""
    try:
        initialize_gee()
        geom_wgs84, _ = _select_district(job.district_path, job.district_field, job.district_name)
        region = ee.Geometry(geom_wgs84.__geo_interface__)
        start = ee.Date(preview_date)
        end = start.advance(1, "day")
        bounds = list(geom_wgs84.bounds)  # minx, miny, maxx, maxy in EPSG:4326

        if job.mode == "s2":
            collection = (ee.ImageCollection(S2_COLLECTION)
                          .filterDate(start, end)
                          .filterBounds(region)
                          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 100)))
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-2 scenes found for {preview_date}."})
            mosaic = collection.select(["B2", "B3", "B4", "B8"]).mosaic().clip(region)
            visual = mosaic.visualize(bands=["B8", "B4", "B3"], min=0, max=3000, gamma=1.1)
            visualization = "FCC: B8 (NIR), B4 (Red), B3 (Green)"
        else:
            polarization = job.polarization.upper()
            if polarization not in ("VH", "VV"):
                return JSONResponse(status_code=400, content={"error": "Preview polarization must be VH or VV."})
            collection = (ee.ImageCollection("COPERNICUS/S1_GRD")
                          .filterDate(start, end)
                          .filterBounds(region)
                          .filter(ee.Filter.eq("instrumentMode", "IW"))
                          .filter(ee.Filter.eq("orbitProperties_pass", job.orbit))
                          .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarization))
                          .select(polarization))
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-1 {polarization} scenes found for {preview_date} with {job.orbit} orbit."})
            mosaic = collection.mosaic().clip(region)
            # Sentinel-1 GRD values are in dB. Grayscale stretch emphasizes typical crop-land backscatter.
            visual = mosaic.visualize(min=-25, max=-5, palette=["000000", "FFFFFF"])
            visualization = f"Sentinel-1 {polarization} backscatter (dB, grayscale; {job.orbit})"

        map_info = visual.getMapId()
        return {"date": preview_date,
                "tile_url": map_info["tile_fetcher"].url_format,
                "bounds": [[bounds[1], bounds[0]], [bounds[3], bounds[2]]],
                "scene_count": count,
                "visualization": visualization}
    except Exception:
        return JSONResponse(status_code=500, content={"error": traceback.format_exc()})

@app.post("/preview-mosaic")
def preview_mosaic(job: JobRequest):
    """Preview a native Earth Engine ImageCollection.mosaic() across selected dates."""
    try:
        if len(job.selected_dates) < 2:
            return JSONResponse(status_code=400, content={"error": "Select at least two acquisition dates using the date checkboxes to create a mosaic."})
        initialize_gee()
        geom_wgs84, _ = _select_district(job.district_path, job.district_field, job.district_name)
        region = ee.Geometry(geom_wgs84.__geo_interface__)
        bounds = list(geom_wgs84.bounds)

        # Merge the selected one-day collections, then sort chronologically.
        # GEE mosaic() gives priority to later images in collection order.
        if job.mode == "s2":
            collection = ee.ImageCollection([])
            for day in sorted(set(job.selected_dates)):
                start = ee.Date(day)
                daily = (ee.ImageCollection(S2_COLLECTION)
                         .filterDate(start, start.advance(1, "day"))
                         .filterBounds(region)
                         .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 100))
                         .select(["B2", "B3", "B4", "B8"]))
                collection = collection.merge(daily)
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": "No Sentinel-2 scenes found on the selected dates."})
            collection = collection.sort("system:time_start")
            image = collection.mosaic().clip(region)
            visual = image.visualize(bands=["B8", "B4", "B3"], min=0, max=3000, gamma=1.1)
            visualization = "Sentinel-2 FCC GEE mosaic(): B8 (NIR), B4 (Red), B3 (Green)"
        else:
            polarization = job.polarization.upper()
            if polarization not in ("VH", "VV"):
                return JSONResponse(status_code=400, content={"error": "Preview polarization must be VH or VV."})
            collection = ee.ImageCollection([])
            for day in sorted(set(job.selected_dates)):
                start = ee.Date(day)
                daily = (ee.ImageCollection("COPERNICUS/S1_GRD")
                         .filterDate(start, start.advance(1, "day"))
                         .filterBounds(region)
                         .filter(ee.Filter.eq("instrumentMode", "IW"))
                         .filter(ee.Filter.eq("orbitProperties_pass", job.orbit))
                         .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarization))
                         .select(polarization))
                collection = collection.merge(daily)
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-1 {polarization} scenes found on the selected dates with {job.orbit} orbit."})
            collection = collection.sort("system:time_start")
            image = collection.mosaic().clip(region)
            visual = image.visualize(min=-25, max=-5, palette=["000000", "FFFFFF"])
            visualization = f"Sentinel-1 {polarization} GEE mosaic() (dB, grayscale; {job.orbit})"

        map_info = visual.getMapId()
        return {"date": ", ".join(sorted(set(job.selected_dates))), 
                "tile_url": map_info["tile_fetcher"].url_format,
                "bounds": [[bounds[1], bounds[0]], [bounds[3], bounds[2]]],
                "scene_count": count,
                "visualization": visualization,
                "mosaic_dates": sorted(set(job.selected_dates)),
                "mosaic_method": "ee.ImageCollection.mosaic()"}
    except Exception:
        return JSONResponse(status_code=500, content={"error": traceback.format_exc()})


@app.post("/run")
def start_job(job: JobRequest):
    if job.crs != "EPSG:32642":
        return JSONResponse(
            status_code=400,
            content={"error": "CRS is locked to EPSG:32642."},
        )

    if not job.selected_outputs:
        return JSONResponse(
            status_code=400,
            content={"error": "Select at least one previewed date or mosaic in the output list."},
        )
    if any(not item.get("dates") or item.get("kind") not in ("date", "mosaic") for item in job.selected_outputs):
        return JSONResponse(status_code=400, content={"error": "Invalid output selection."})
    job.selected_dates = sorted({d for item in job.selected_outputs for d in item.get("dates", [])})

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "logs": []}

    thread = threading.Thread(
        target=run_job,
        args=(job_id, job.model_dump()),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.post("/local/files")
def scan_local_files(req: LocalFilesRequest):
    try:
        files = list_geotiffs(req.folder, req.recursive)
        return {"files": files}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


def run_local_job(job_id: str, payload: dict):
    JOBS[job_id] = {"status": "running", "logs": []}
    def log(message, level=1):
        JOBS[job_id]["logs"].append(message)
    try:
        output_path = run_local_processing(payload, log)
        JOBS[job_id]["output_path"] = output_path
        JOBS[job_id]["status"] = "completed"
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        import traceback as _traceback
        log(_traceback.format_exc())
        JOBS[job_id]["status"] = "error"


@app.post("/local/run")
def start_local_job(req: LocalProcessingRequest):
    if not req.input_paths:
        return JSONResponse(status_code=400, content={"error": "Select at least one GeoTIFF."})
    if req.operation not in ("stack", "mosaic", "mask"):
        return JSONResponse(status_code=400, content={"error": "Choose Stack, Mosaic, or Mask."})
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "logs": []}
    threading.Thread(target=run_local_job, args=(job_id, req.model_dump()), daemon=True).start()
    return {"job_id": job_id}



# Online file-based local tools. Files are kept in temporary server storage and packaged for download.
TOOLS_ROOT = Path(tempfile.gettempdir()) / "deep_amnex_tools"
TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
TOOL_OUTPUTS = {}

def _safe_name(name: str) -> str:
    name = Path(name or "upload.bin").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:140] or "upload.bin"

async def _save_upload(upload: UploadFile, folder: Path, allowed=None) -> Path:
    name = _safe_name(upload.filename or "upload.bin")
    if allowed and Path(name).suffix.lower() not in allowed:
        raise ValueError(f"Unsupported file type for {name}. Allowed: {', '.join(sorted(allowed))}")
    dest = folder / name
    # avoid duplicate filenames within one request
    if dest.exists():
        dest = folder / f"{uuid.uuid4().hex[:8]}_{name}"
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > 1024 * 1024 * 1024:
                out.close(); dest.unlink(missing_ok=True)
                raise ValueError("Each uploaded file must be smaller than 1 GB.")
            out.write(chunk)
    return dest

@app.get("/tools/outputs")
def tools_outputs():
    return {"outputs": [{"id": k, "name": v["name"], "kind": v["kind"]} for k,v in TOOL_OUTPUTS.items()]}

@app.get("/tools/download/{output_id}")
def tools_download(output_id: str):
    item = TOOL_OUTPUTS.get(output_id)
    if not item or not Path(item["path"]).is_file():
        return JSONResponse(status_code=404, content={"error":"Output not found. It may have expired after a server restart."})
    return FileResponse(item["path"], filename=item["name"], media_type="application/zip")

@app.post("/tools/vector-columns")
async def tools_vector_columns(file: UploadFile = File(...)):
    folder = TOOLS_ROOT / ("columns_" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path = await _save_upload(file, folder, {".zip", ".shp", ".gpkg", ".geojson", ".json"})
        import geopandas as gpd
        gdf = gpd.read_file(path)
        return {"columns": [str(c) for c in gdf.columns if str(c).lower() not in {"geometry"}], "count": int(len(gdf))}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"{type(exc).__name__}: {exc}"})
    finally:
        shutil.rmtree(folder, ignore_errors=True)

@app.post("/tools/process")
async def tools_process(
    operation: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    mask: UploadFile | None = File(default=None),
    vector: UploadFile | None = File(default=None),
    gt: UploadFile | None = File(default=None),
    field: str = Form(default=""),
    output_name: str = Form(default="Processed_Output"),
):
    operation = operation.strip().lower()
    if operation not in {"stack", "mask", "mosaic", "clip", "split_gt"}:
        return JSONResponse(status_code=400, content={"error":"Unknown operation."})
    job_dir = TOOLS_ROOT / uuid.uuid4().hex
    input_dir = job_dir / "inputs"; result_dir = job_dir / "results"
    input_dir.mkdir(parents=True); result_dir.mkdir(parents=True)
    try:
        saved_files = []
        for f in files:
            if f and f.filename:
                saved_files.append(await _save_upload(f, input_dir, {".tif", ".tiff"}))
        mask_path = await _save_upload(mask, input_dir, {".tif", ".tiff"}) if mask and mask.filename else None
        vector_path = await _save_upload(vector, input_dir, {".zip", ".shp", ".gpkg", ".geojson", ".json"}) if vector and vector.filename else None
        gt_path = await _save_upload(gt, input_dir, {".zip", ".shp", ".gpkg", ".geojson", ".json"}) if gt and gt.filename else None
        out_base = _safe_name(output_name).rsplit('.',1)[0]
        if operation in {"stack", "mask", "mosaic"}:
            if not saved_files: raise ValueError("Upload one or more input GeoTIFFs.")
            if operation == "mask" and (len(saved_files) != 1 or not mask_path): raise ValueError("Masking requires exactly one input GeoTIFF and an agriculture-mask GeoTIFF.")
            if operation == "stack" and mask_path:
                # Step 1 stays stack-only; use its output as input to step 2 if desired.
                pass
            if operation == "mosaic" and len(saved_files) < 2: raise ValueError("Upload/select at least two GeoTIFFs to mosaic.")
            tif_out = result_dir / f"{out_base}.tif"
            def _logger(msg): pass
            payload = {"operation": operation, "input_paths": [str(x) for x in saved_files], "output_dir": str(result_dir), "output_name": tif_out.name, "mask_path": str(mask_path) if mask_path and operation == "mask" else ""}
            result = Path(run_local_processing(payload, _logger))
            package = job_dir / f"{out_base}.zip"
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf: zf.write(result, result.name)
            kind = operation
        elif operation == "clip":
            if not saved_files: raise ValueError("Upload at least one raster GeoTIFF to clip.")
            if not vector_path: raise ValueError("Upload a tehsil/district boundary vector (ZIP shapefile, SHP, GPKG or GeoJSON).")
            if not field.strip(): raise ValueError("Enter the vector column used to split/clip features, such as district or tehsil.")
            import geopandas as gpd
            import rasterio
            from rasterio.mask import mask as raster_mask
            gdf = gpd.read_file(vector_path)
            if field not in gdf.columns: raise ValueError(f"Column '{field}' not found. Available columns: {', '.join(map(str,gdf.columns))}")
            if gdf.crs is None: raise ValueError("The boundary vector has no CRS. Define its CRS before uploading.")
            for raster_path in saved_files:
                with rasterio.open(raster_path) as src:
                    shapes = gdf.to_crs(src.crs)
                    for value, group in shapes.groupby(field, dropna=True):
                        geom = [g.__geo_interface__ for g in group.geometry if g is not None and not g.is_empty]
                        if not geom: continue
                        arr, transform = raster_mask(src, geom, crop=True, filled=True)
                        prof = src.profile.copy(); prof.update(height=arr.shape[1], width=arr.shape[2], transform=transform, compress="deflate")
                        safeval = _safe_name(str(value))
                        out = result_dir / f"{raster_path.stem}_{safeval}_clip.tif"
                        with rasterio.open(out,"w",**prof) as dst: dst.write(arr)
            if not list(result_dir.glob("*.tif")): raise ValueError("No clipped outputs were created; check geometries and overlap.")
            package = job_dir / f"{out_base}_clipped.zip"
            with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as zf:
                for f in result_dir.glob("*.tif"): zf.write(f,f.name)
            kind = "clip"
        else: # split_gt
            if not gt_path: raise ValueError("Upload the ground-truth vector file.")
            if not field.strip(): raise ValueError("Choose a GT attribute column, such as crop name, tehsil or district.")
            import geopandas as gpd
            gdf = gpd.read_file(gt_path)
            if field not in gdf.columns: raise ValueError(f"Column '{field}' not found. Available columns: {', '.join(map(str,gdf.columns))}")
            for value, group in gdf.groupby(field, dropna=True):
                name = _safe_name(str(value))
                out = result_dir / f"{name}.gpkg"
                group.to_file(out, driver="GPKG")
            if not list(result_dir.glob("*.gpkg")): raise ValueError("No GT outputs were created; selected column may contain only empty values.")
            package = job_dir / f"{out_base}_split_gt.zip"
            with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as zf:
                for f in result_dir.glob("*.gpkg"): zf.write(f,f.name)
            kind = "split_gt"
        output_id = uuid.uuid4().hex
        TOOL_OUTPUTS[output_id] = {"path":str(package), "name":package.name, "kind":kind, "created":time.time()}
        return {"ok":True, "output_id":output_id, "output_name":package.name, "download_url":f"/tools/download/{output_id}", "message":f"{operation.replace('_',' ').title()} completed. Download the ZIP output."}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error":f"{type(exc).__name__}: {exc}"})
    finally:
        # Keep result package, remove uploaded intermediates; output zip is outside result directory.
        shutil.rmtree(input_dir, ignore_errors=True)
        shutil.rmtree(result_dir, ignore_errors=True)
        # Remove the request folder only if it no longer contains the packaged output.
        for f in job_dir.iterdir() if job_dir.exists() else []:
            if f.is_dir(): shutil.rmtree(f, ignore_errors=True)


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in JOBS:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return JOBS[job_id]
