# Suno Bulk Downloader

A simple command-line Python script to bulk download all of your private songs from [Suno AI](https://suno.com/).

This tool iterates through your library pages, downloads each song, and can optionally embed the cover art directly into the MP3 file's metadata.


## Features

- **Bulk Download:** Downloads all songs from your private library.
- **Automatic Retry:** Failed downloads are automatically retried up to 3 times with exponential backoff.
- **Chronological Order:** Songs are downloaded from oldest to newest for consistent version numbering.
- **Parallel Downloads:** Download multiple songs simultaneously with configurable worker threads.
- **Resume Support:** Skip already downloaded songs and only fetch new ones (like rsync).
- **Persistent State:** Tracks downloaded songs in a JSON file to enable resume functionality.
- **Failure Placeholders:** Creates placeholder files when downloads fail after all retries.
- **Metadata Embedding:** Automatically embeds the title, artist, and cover art (thumbnail) into the MP3 file.
- **File Sanitization:** Cleans up song titles to create valid filenames for any operating system.
- **Duplicate Handling:** If a file with the same name already exists, it saves the new file with a version suffix (e.g., `My Song v2.mp3`) to avoid overwriting.
- **Proxy Support:** Allows routing traffic through an HTTP/S proxy.
- **User-Friendly Output:** Uses colored console output for clear and readable progress updates.


https://imgur.com/a/Ox9goh7


## Requirements

- [Python 3.6+](https://www.python.org/downloads/)
- `pip` (Python's package installer, usually comes with Python)

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/your-repo-name.git
    cd your-repo-name
    ```
    *(Alternatively, you can download the repository as a ZIP file and extract it.)*

2.  **Install the required Python packages:**
    ```bash
    pip install -r requirements.txt
    ```

## How to Use

The script requires a **Suno Authorization Token** to access your private library. Here’s how to find it:

### Step 1: Find Your Authorization Token

1.  Open your web browser and go to [suno.com](https://suno.com/) and log in.
2.  Open your browser's **Developer Tools**. You can usually do this by pressing `F12` or `Ctrl+Shift+I` (Windows/Linux) or `Cmd+Option+I` (Mac).
3.  Go to the **Network** tab in the Developer Tools.
4.  In the filter box, type `feed` to easily find the right request.
5.  Refresh the Suno page or click around your library. You should see a new request appear in the list.
6.  Click on that request (it might be named something like `v2?hide_disliked=...`).
7.  In the new panel that appears, go to the **Headers** tab.
8.  Scroll down to the **Request Headers** section.
9.  Find the `Authorization` header. The value will look like `Bearer [long_string_of_characters]`.
10. **Copy only the long string of characters** (the token itself), *without* the word `Bearer `.

Example (Copy the whole string)
https://i.imgur.com/PQtOIM5.jpeg


**Important:** Your token is like a password. **Do not share it with anyone.**

### Step 2: Run the Script

Open your terminal or command prompt, navigate to the script's directory, and run it using the following command structure.

**Basic Usage (downloads audio only):**
```bash
python suno_downloader.py --token "your_token_here"
```
This will download all songs into a new folder named `suno-downloads`.

**Full-Featured Usage (with thumbnails and a custom directory):**
```bash
python suno_downloader.py --token "your_token_here" --directory "My Suno Music" --with-thumbnail
```
This will download all songs and their thumbnails into a folder named `My Suno Music`.

**Parallel Downloads (faster for large libraries):**
```bash
python suno_downloader.py --token "your_token_here" --max-workers 5 --with-thumbnail
```
This will download 5 songs simultaneously, significantly speeding up the process.

**Resume Downloads (skip already downloaded songs):**
```bash
python suno_downloader.py --token "your_token_here" --resume
```
This will skip songs that have already been downloaded, allowing you to run the script multiple times to only fetch new songs.

### Command-Line Arguments

- `--token` **(Required)**: Your Suno authorization token.
- `--directory` (Optional): The local directory where files will be saved. Defaults to `suno-downloads`.
- `--with-thumbnail` (Optional): A flag to download and embed the song's cover art.
- `--max-workers` (Optional): Number of parallel downloads (default: 1). Higher values download faster but use more bandwidth.
- `--resume` (Optional): Skip songs that have already been downloaded based on the state file.
- `--proxy` (Optional): A proxy server URL (e.g., `http://user:pass@127.0.0.1:8080`). You can provide multiple proxies separated by commas.

### New Features Explained

#### Automatic Retry
If a download fails due to network issues (like timeouts), the script will automatically retry up to 3 times with increasing delays between attempts. This ensures that temporary network issues don't cause missing files.

#### Chronological Order
Songs are now downloaded from oldest to newest. This means your first song will be named without a version number, and newer songs with the same name will get version numbers (v2, v3, etc.). This is more intuitive than the previous behavior.

#### Parallel Downloads
Use `--max-workers` to download multiple songs at the same time. For example, `--max-workers 5` will download 5 songs simultaneously. This is especially useful for large libraries (like 9000+ songs).

#### Resume Support
The script now maintains a state file (`suno_download_state.json`) that tracks which songs have been successfully downloaded. Use `--resume` to skip already downloaded songs and only fetch new ones. This is perfect for regularly updating your library without re-downloading everything.

#### Failure Placeholders
If a download fails after all retry attempts, the script creates a placeholder text file (e.g., `Song Name_FAILED.txt`) that contains the error message. This ensures you can identify which downloads failed and why.

## Disclaimer

This is an unofficial tool and is not affiliated with Suno, Inc. It is intended for personal use only to back up your own creations. Please respect Suno's Terms of Service. The developers of this script are not responsible for any misuse.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
