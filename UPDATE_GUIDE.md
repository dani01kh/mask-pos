# Mask POS Updates

This version adds GitHub-based updates.

## One-time setup

Install this updater-enabled version manually once on each POS computer.
After that, future packaged releases can be installed from inside Mask POS:

Settings -> App Updates -> Check for Updates

## Release workflow

1. Increase `APP_VERSION` in `app_update.py`.
2. Build the packaged app with PyInstaller.
3. Create the update ZIP:

   ```powershell
   .\scripts\make_update_zip.ps1 -Version 1.1.0
   ```

4. On GitHub, create a new Release tag like `v1.1.0`.
5. Upload `release\MaskPOS-v1.1.0.zip` as a release asset.

The app checks GitHub Releases and downloads the ZIP asset from the newest release.

## Protected Local Files

The updater does not replace local store data or settings:

- `pos.db`
- `pos_config.json`
- `cloudflare_pos_config.json`
- `cloud_sync_device.json`
- `config.json`
- `backups/`
- `data/`
- `receipts/`
- `reports/`

Do not upload live database files or private config files to GitHub Releases.
