# Deep_Amnex — Local PC Workflow

This version preserves the existing application UI and lets you run GEE downloads and raster processing on your own Windows PC. This avoids depending on Render's temporary filesystem for large processing jobs.

See `LOCAL_PC_WORKFLOW.md` for setup, output locations, and limitations.

Quick start:
1. Run `INSTALL_LOCAL_PC.bat` once.
2. Run `START_LOCAL_PC.bat`.
3. Open `http://127.0.0.1:8000`.
4. Configure your own GEE credentials and run your workflow.

The public Render URL remains separate. Use the local URL for direct-to-PC downloads and processing until a secure Render-to-local-agent job bridge is built.
