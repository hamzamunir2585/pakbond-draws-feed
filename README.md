# Pak Bond Tracker — Static Draw Feed Template (GitHub + CDN)

This folder is a ready-to-deploy static JSON repository template for **Pak Bond Tracker**.
It hosts official National Savings (CDNS) prize bond schedules and announced draw results using **GitHub Actions** and a **global CDN** (Cloudflare Pages or jsDelivr) at **$0 cost** and **no custom backend server**.

---

## Architecture & Why It Scales

1. **GitHub Actions (`update-draws.yml`)** runs on schedule (every 6 hours) to crawl `https://savings.gov.pk/download-draws/`, all 6 denomination hubs, pagination links, and passed schedule dates.
2. **Python Normalization, Delta Hashing & 6-Year Pruning (`scripts/fetch_cdns_draws.py`)** extracts valid 6-digit winning numbers, writes both yearly bootstrap shards (`draws_<year>.json`) and individual draw files (`draws/<id>.json`), prunes draws older than 6 years, and writes SHA-256 hashes (`scheduleHash`, `yearHashes`, `drawFiles`) into `manifest.json`.
3. **Zero-Waste Client Delta Sync (`DrawFeedSyncService.kt`)**:
   - **No Changes:** Checks `manifest.json` using HTTP `If-None-Match` (`ETag`) and `version` — `304 Not Modified` downloads **0 draw files**.
   - **First Install:** Downloads the 6 yearly `draws_<year>.json` bootstrap shards + `schedule.json` once into local Room SQLite.
   - **Single Draw Update:** Compares `drawFiles` SHA-256 hashes in `manifest.json` and downloads **only the newly announced/updated `draws/<id>.json` file** (ignoring unchanged historical years).

---

## Setup Instructions (2 Minutes — No Code Edits Needed)

The Android app (`DrawFeedSyncService.kt`) is already pre-configured for:
- Primary: `https://raw.githubusercontent.com/hamzamunir2585/pakbond-draws-feed/main/manifest.json`
- CDN Fallback: `https://cdn.jsdelivr.net/gh/hamzamunir2585/pakbond-draws-feed@main/manifest.json`

### Option 1: GitHub + jsDelivr CDN (Fastest — Zero Extra Accounts)
1. Sign in to GitHub (`hamzamunir2585`) and create a **Public** repository named **`pakbond-draws-feed`**.
2. Upload all files from `/feed-template/` into the root of `pakbond-draws-feed` (including `.github/workflows/update-draws.yml`, `scripts/fetch_cdns_draws.py`, `manifest.json`, `schedule.json`, and `draws_2026.json`).
3. In the repository on GitHub, open **Settings -> Actions -> General -> Workflow permissions**, select **Read and write permissions**, and click **Save**.
4. Open the **Actions** tab -> select **Update Prize Bond Draws Feed** -> click **Run workflow** once to populate the full 6-year archive.

### Option 2: Cloudflare Pages (Unlimited Free Bandwidth)
1. Push the `/feed-template/` contents to your GitHub repository (`pakbond-draws-feed`).
2. Connect that repository to **Cloudflare Pages** (Framework preset: `None`, Build command: leave empty, Build output directory: `/`).
3. Cloudflare gives you a global HTTPS edge URL such as `https://pakbond-draws.pages.dev/manifest.json`.
4. Update `DrawFeedSyncService.DEFAULT_FEED_MANIFEST_URL` in the Android app if switching to a custom Cloudflare Pages domain.

---

## Supported JSON Schema

### `manifest.json`
```json
{
  "version": "2026.11.25.1",
  "updatedAtUtc": "2026-11-25T10:20:00Z",
  "retentionYears": 6,
  "scheduleUrl": "schedule.json",
  "scheduleHash": "a1b2c3d4e5f60718",
  "yearFiles": {
    "2026": "draws_2026.json"
  },
  "yearHashes": {
    "2026": "1122334455667788"
  },
  "drawFiles": {
    "d_108_1500": {
      "url": "draws/d_108_1500.json",
      "hash": "abcdef0123456789",
      "year": 2026,
      "denomination": 1500,
      "drawNo": 108,
      "status": "Announced"
    }
  }
}
```

### Draw Entry Schema (`schedule.json` & `draws_<year>.json`)
```json
{
  "id": "d_105_1500",
  "drawNo": 105,
  "denomination": 1500,
  "date": "2026-11-16",
  "city": "Rawalpindi",
  "status": "Announced",
  "first": ["619482"],
  "second": ["203948", "518293", "839201"],
  "third": ["109283", "348192", "492817", "729103", "910293"]
}
```
- Valid `denomination`: `100`, `200`, `750`, `1500`, `25000`, `40000`
- Valid `status`: `"Upcoming"`, `"Announced"`, `"Past"`
- Supported `date` formats: `yyyy-MM-dd` (preferred) or `dd MMM yyyy`
