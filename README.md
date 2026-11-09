# EchoGrab

**EchoGrab** is a standalone Windows application that listens to your microphone or system audio, identifies playing songs, finds verified links across major music platforms, and lets you download and tag high-quality audio files with custom cover art.

Everything needed to build and run the application—including Python, FFmpeg, and the JavaScript runtime for stream extraction—is automatically set up and bundled into a single portable executable.

---

## Features

* **Real-Time Audio Recognition (Echo)**
  * Capture audio from any connected microphone or loopback/stereo-mix input device.
  * Choose between **Progressive Stream** mode (which checks for a match at intervals while listening) or **Fixed Duration** mode (which records for a set number of seconds before identifying).
  * View a live spectrogram of the incoming audio signal while recording.
  * Automatic silence detection and volume normalization help identify quiet audio sources without wasting requests.

* **Verified Streaming Links**
  * Resolves direct track links across **YouTube Music, YouTube, SoundCloud, Apple Music, Spotify, Tidal, Deezer, and Amazon Music**, plus a direct link to **Shazam**.
  * Filters out remixes, loops, slowed/sped-up edits, and mismatched durations so only official studio matches are shown.

* **Audio Downloader & Tagging Studio (Grab)**
  * Automatically loads the best available download source when a song is recognized, or accepts any direct track URL you paste in.
  * Lists all available audio streams with codec, bitrate, and file size details.
  * Converts audio to **MP3, M4A, FLAC, OPUS, or WAV** at your chosen bitrate (`320k`, `256k`, `192k`, `128k`, or `Lossless`).
  * Embeds track metadata (**Title, Artist, Album, Year**) from either the recognized song data, the stream source, or your own custom edits.

* **Interactive Cover Art Cropper**
  * Load cover art from Shazam, the stream source thumbnail, or a local image file.
  * Drag to select a custom crop area or apply a 1:1 center square crop before embedding the artwork directly into the audio file.

* **Integrated Console & Auto-Updater**
  * View live application, stream extraction, and conversion logs inside the **Console** tab, with an option to retain or clear logs on exit.
  * Launching via `run.bat` automatically checks if core recognition and extraction packages (`shazamio`, `yt-dlp`, `yt-dlp-ejs`) have updates available, rebuilding the executable only when needed.

---

## Directory Structure

    EchoGrab/
    ├── run.bat                      # Smart launcher: checks for updates, builds if needed, and runs EchoGrab.exe
    ├── apps/                        # Output directory for the compiled application
    │   ├── EchoGrab.exe             # Standalone portable executable (generated on first run)
    │   ├── app.ico                  # Application icon
    │   ├── config.json              # Saved user preferences (created automatically)
    │   └── app_debug.log            # Runtime log file
    ├── script/
    │   ├── shazam_ytdlp_studio.py   # Main application source code
    │   └── build_windows.ps1        # Automated zero-dependency Windows build script
    └── build_env/                   # Isolated build workspace (auto-generated during build)

---

## Getting Started

### Prerequisites
* **Windows 10 or Windows 11 (64-bit)**
* **Internet connection** (required on first launch to download the portable build tools and for song recognition/downloading)
* No pre-installed Python, FFmpeg, or third-party runtimes are required on your system.

### Building and Running
1. Clone or download this repository.
2. Double-click **`run.bat`** in the project root.
3. **First Launch:** Because `apps\EchoGrab.exe` does not exist yet, `run.bat` will automatically run `script\build_windows.ps1`. This script downloads an isolated portable Python environment, FFmpeg, Deno, and all required Python libraries into `build_env\`, compiles `EchoGrab.exe` into the `apps\` folder, and launches the app.
4. **Subsequent Launches (Recommended):** Using `run.bat` is the intended way to start the application. Each time it runs, it checks if updates are available for the core recognition and download packages (`shazamio`, `yt-dlp`, `yt-dlp-ejs`). If everything is up to date, `EchoGrab.exe` launches immediately; if an update is found, it automatically updates the packages, rebuilds `EchoGrab.exe`, and launches the fresh binary.
5. **Standalone Portable Use:** Once built, the `EchoGrab.exe` binary inside the `./apps` folder (alongside `app.ico`) is completely self-contained and portable. You can copy or move it to any folder or another Windows PC and run `EchoGrab.exe` directly without the rest of the project files (minus the automatic package update checks provided by `run.bat`).

---

## Usage Guide

### 1. Identifying a Song (Echo Tab)
1. Select your **Input Device** from the dropdown (choose a microphone or a loopback/stereo-mix device to capture system audio). Click **Refresh** if you recently connected a new device.
2. Choose a capture **Mode**:
   * **Progressive Stream:** Listens continuously and checks for matches at regular intervals until a song is identified.
   * **Fixed Duration:** Records for the selected duration (`4` to `14` seconds) and performs a single recognition check.
3. Click **Start Listening**. The live spectrogram will appear while audio is being captured.
4. Once a match is found, the track details, album art, and verified streaming platform buttons are displayed.
5. Click **Open in Shazam** or any of the platform buttons to open the song in your browser, or click **Download Track** to jump straight to the **Grab** tab.

### 2. Downloading & Tagging Audio (Grab Tab)
1. When you identify a song in the **Echo** tab, the best available download URL, metadata tags, and cover art are automatically loaded into the **Grab** tab. You can also paste any supported direct song URL into **Source URL** and click **Fetch Streams**.
2. Under **Stream & Output Format**, select your preferred source stream, output **Format** (`mp3`, `m4a`, `flac`, `opus`, `wav`), and **Bitrate**.
3. Under **Metadata**, review or edit the **Title**, **Artist**, **Album**, and **Year** fields. Use **Load Recognized Tags** or **Load yt-dlp Tags** to switch between Shazam metadata and source stream metadata.
4. Under **Cover Art**, choose your artwork source (**Shazam**, **Source**, or **Browse File**). Drag on the preview canvas and click **Apply Selection** for a custom crop, click **Square Center** for a 1:1 crop, or **Reset** to restore the original image.
5. Choose your destination folder under **Output** and click **Download and Convert**.

### 3. Viewing Logs & Theme Settings
* Click **Toggle Theme** in the top-right header to switch between Dark and Light mode.
* Open the **Console** tab to inspect live debug logs from audio capture, platform matching, `yt-dlp`, and `ffmpeg`. Toggle **Retain console log on exit** depending on whether you want `app_debug.log` kept after closing the app.

---

## Configuration

User preferences are saved automatically to `config.json` in the application folder whenever settings are changed:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `theme` | `"dark"` | Active UI theme (`"dark"` or `"light"`) |
| `microphone_name` | `""` | Last selected audio input device |
| `listen_mode` | `"stream"` | Audio capture mode (`"stream"` or `"fixed"`) |
| `fixed_duration` | `8` | Recording duration in seconds for Fixed Duration mode (`4`–`14`) |
| `cooldown_seconds` | `5` | Minimum delay in seconds between recognition requests |
| `retain_logs_on_exit` | `true` | Keep `app_debug.log` after closing the application |
| `target_ext` | `"mp3"` | Default output audio format (`mp3`, `m4a`, `flac`, `opus`, `wav`) |
| `bitrate` | `"320k"` | Default output conversion bitrate |
| `embed_meta` | `true` | Embed metadata tags into output audio files |
| `embed_thumb` | `true` | Embed cover art into output audio files |
| `out_dir` | `~/Downloads` | Default output folder for downloaded tracks |

---

## License

This project is licensed under the **MIT License**.