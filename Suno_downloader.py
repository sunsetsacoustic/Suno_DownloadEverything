import argparse
from datetime import datetime
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from queue import Queue, Empty
from threading import Lock

import requests
from colorama import Fore, Style, init
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error
from mutagen.mp3 import MP3

init(autoreset=True)

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'
STATE_FILE = "suno_download_state.json"

# Global lock for thread-safe file operations and printing
state_lock = Lock()
print_lock = Lock()

def log_with_timestamp(message, color=Fore.WHITE):
    """Thread-safe logging with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with print_lock:
        print(f"{Fore.CYAN}[{timestamp}]{Style.RESET_ALL} {color}{message}{Style.RESET_ALL}")

def prompt_for_new_token():
    """Prompt user for a new token when the current one expires."""
    log_with_timestamp("=" * 60, Fore.YELLOW)
    log_with_timestamp("⚠️  TOKEN EXPIRED - PLEASE PROVIDE NEW TOKEN", Fore.YELLOW)
    log_with_timestamp("=" * 60, Fore.YELLOW)
    print()
    try:
        new_token = input(f"{Fore.CYAN}Enter new Bearer token (or press Ctrl+C to abort): {Style.RESET_ALL}").strip()
        if new_token:
            log_with_timestamp("✅ New token received, resuming...", Fore.GREEN)
            return new_token
        else:
            log_with_timestamp("❌ No token provided", Fore.RED)
            return None
    except (KeyboardInterrupt, EOFError):
        log_with_timestamp("❌ Aborted by user", Fore.RED)
        return None

def sanitize_filename(name, maxlen=200):
    safe = re.sub(FILENAME_BAD_CHARS, "_", name)
    safe = safe.strip(" .")
    return safe[:maxlen] if len(safe) > maxlen else safe

def retry_with_backoff(max_retries=10, initial_delay=1, backoff_factor=2):
    """Decorator to retry a function with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        print(f"{Fore.YELLOW}  -> Attempt {attempt + 1} failed: {e}")
                        print(f"{Fore.YELLOW}  -> Retrying in {delay} seconds...")
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        print(f"{Fore.RED}  -> All {max_retries} attempts failed")
            raise last_exception
        return wrapper
    return decorator

def load_state(directory):
    """Load the download state from JSON file."""
    state_path = os.path.join(directory, STATE_FILE)
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"{Fore.YELLOW}Warning: Could not load state file: {e}")
            return {}
    return {}

def save_state(directory, state):
    """Save the download state to JSON file."""
    state_path = os.path.join(directory, STATE_FILE)
    with state_lock:
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"{Fore.RED}Warning: Could not save state file: {e}")

def pick_proxy_dict(proxies_list):
    if not proxies_list: return None
    proxy = random.choice(proxies_list)
    return {"http": proxy, "https": proxy}

@retry_with_backoff(max_retries=10, initial_delay=2, backoff_factor=2)
def embed_metadata(mp3_path, image_url=None, title=None, artist=None, proxies_list=None, token=None, timeout=15):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    proxy_dict = pick_proxy_dict(proxies_list)
    r = requests.get(image_url, proxies=proxy_dict, headers=headers, timeout=timeout)
    r.raise_for_status()
    image_bytes = r.content
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    
    audio = MP3(mp3_path, ID3=ID3)
    try: audio.add_tags()
    except error: pass

    if title: audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist: audio.tags["TPE1"] = TPE1(encoding=3, text=artist)

    for key in list(audio.tags.keys()):
        if key.startswith("APIC"): del audio.tags[key]

    audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
    audio.save(v2_version=3)

def check_page_exists(page_num, token_string, proxies_list=None):
    """Quickly check if a page has content (returns True if page has clips)."""
    base_api_url = "https://studio-api.prod.suno.com/api/feed/v2?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
    headers = {"Authorization": f"Bearer {token_string}"}
    api_url = f"{base_api_url}{page_num}"
    
    try:
        response = requests.get(api_url, headers=headers, proxies=pick_proxy_dict(proxies_list), timeout=10)
        if response.status_code in [401, 403]:
            return None  # Auth error
        if response.status_code == 404 or response.status_code >= 500:
            return False  # No content or error
        response.raise_for_status()
        data = response.json()
        clips = data if isinstance(data, list) else data.get("clips", [])
        return len(clips) > 0
    except requests.exceptions.RequestException:
        return False

def fetch_page_with_retry(page_num, token_container, proxies_list=None, max_retries=10):
    """Fetch a single page with retry logic. Returns the page data or raises exception."""
    base_api_url = "https://studio-api.prod.suno.com/api/feed/v2?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
    api_url = f"{base_api_url}{page_num}"
    
    delay = 2
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            # Get current token from container
            current_token = token_container[0] if isinstance(token_container, list) else token_container
            headers = {"Authorization": f"Bearer {current_token}"}
            
            response = requests.get(api_url, headers=headers, proxies=pick_proxy_dict(proxies_list), timeout=15)
            
            if response.status_code in [401, 403]:
                raise Exception(f"Authorization failed (status {response.status_code})")
            
            response.raise_for_status()
            data = response.json()
            clips = data if isinstance(data, list) else data.get("clips", [])
            
            return clips
            
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                log_with_timestamp(f"    -> Page {page_num} attempt {attempt + 1} failed: {e}", Fore.YELLOW)
                log_with_timestamp(f"    -> Retrying in {delay} seconds...", Fore.YELLOW)
                time.sleep(delay)
                delay *= 2
            else:
                log_with_timestamp(f"    -> Page {page_num} failed after {max_retries} attempts", Fore.RED)
    
    raise last_exception

def download_all_pages_parallel(last_page, token_string, proxies_list=None, token_container=None, max_workers=5):
    """
    Download all pages in parallel before processing songs.
    Returns a list of all song data in chronological order (oldest first).
    Stores pages in state as they're downloaded to track progress.
    """
    log_with_timestamp(f"📥 Pre-downloading all {last_page} pages in parallel...", Fore.CYAN)
    
    pages_data = {}  # {page_num: [song_data_list]}
    pages_lock = Lock()
    
    def fetch_single_page(page_num):
        """Fetch a single page with retry and token update support."""
        try:
            log_with_timestamp(f"  📄 Fetching page {page_num}/{last_page}...", Fore.MAGENTA)
            
            # Try to fetch with current token, handle auth errors specially
            try:
                clips = fetch_page_with_retry(page_num, token_container or [token_string], proxies_list, max_retries=10)
            except Exception as e:
                if "Authorization failed" in str(e):
                    log_with_timestamp(f"  ⚠️  Page {page_num} failed: Authorization error", Fore.RED)
                    
                    # Prompt for new token (synchronized to avoid multiple prompts)
                    with pages_lock:
                        if token_container:
                            # Get current token value
                            old_token = token_container[0]
                            new_token = prompt_for_new_token()
                            if new_token:
                                token_container[0] = new_token
                                log_with_timestamp(f"  🔄 Retrying page {page_num} with new token...", Fore.CYAN)
                                # Retry with new token
                                clips = fetch_page_with_retry(page_num, token_container, proxies_list, max_retries=10)
                            else:
                                raise Exception("No new token provided")
                        else:
                            raise Exception("Cannot update token - no token container")
                else:
                    raise
            
            log_with_timestamp(f"  ✅ Page {page_num}/{last_page} downloaded ({len(clips)} clips)", Fore.GREEN)
            
            # Process clips into song data
            page_songs = []
            for clip in clips:
                uuid = clip.get("id")
                title = clip.get("title")
                audio_url = clip.get("audio_url")
                image_url = clip.get("image_url")
                created_at = clip.get("created_at", "")
                
                if uuid and title and audio_url:
                    song_data = {
                        "uuid": uuid,
                        "title": title,
                        "audio_url": audio_url,
                        "image_url": image_url,
                        "display_name": clip.get("display_name"),
                        "created_at": created_at
                    }
                    page_songs.append(song_data)
            
            # Reverse songs within page (API returns newest first within each page)
            page_songs.reverse()
            
            with pages_lock:
                pages_data[page_num] = page_songs
            
            return True
            
        except Exception as e:
            log_with_timestamp(f"  ❌ Page {page_num} failed after all retries: {e}", Fore.RED)
            raise
    
    # Download all pages in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all pages in reverse order (last to first)
        futures = {executor.submit(fetch_single_page, page): page for page in range(last_page, 0, -1)}
        
        # Wait for all to complete
        for future in as_completed(futures):
            page_num = futures[future]
            try:
                future.result()
            except Exception as e:
                log_with_timestamp(f"❌ Failed to download page {page_num}: {e}", Fore.RED)
                raise Exception(f"Page download failed for page {page_num}. Cannot continue.")
    
    # Combine all pages in order (from last to first)
    all_songs = []
    for page_num in range(last_page, 0, -1):
        if page_num in pages_data:
            all_songs.extend(pages_data[page_num])
    
    log_with_timestamp(f"✅ All {last_page} pages downloaded successfully! Total songs: {len(all_songs)}", Fore.GREEN)
    return all_songs

def find_last_page(token_string, proxies_list=None):
    """Find the last page number using binary search (which contains the oldest songs)."""
    log_with_timestamp("🔍 Finding last page using binary search...", Fore.CYAN)
    
    # First check if page 1 exists
    exists = check_page_exists(1, token_string, proxies_list)
    if exists is None:
        log_with_timestamp("Authorization failed. Token may be expired.", Fore.RED)
        return 0
    if not exists:
        log_with_timestamp("No songs found on page 1", Fore.RED)
        return 0
    
    # Binary search to find the last page
    # First, find an upper bound by exponentially increasing
    low = 1
    high = 2
    
    log_with_timestamp(f"🔎 Searching for upper bound... checking page {high}", Fore.CYAN)
    while check_page_exists(high, token_string, proxies_list):
        low = high
        high *= 2
        log_with_timestamp(f"🔎 Page {low} exists, trying page {high}...", Fore.CYAN)
        time.sleep(0.1)  # Small delay to avoid rate limiting
    
    log_with_timestamp(f"🔎 Upper bound found between page {low} and {high}, binary searching...", Fore.CYAN)
    
    # Now binary search between low and high
    while low < high:
        mid = (low + high + 1) // 2
        log_with_timestamp(f"🔎 Checking page {mid} (range: {low}-{high})...", Fore.CYAN)
        
        if check_page_exists(mid, token_string, proxies_list):
            low = mid
        else:
            high = mid - 1
        time.sleep(0.1)  # Small delay to avoid rate limiting
    
    log_with_timestamp(f"✅ Found last page: {low}", Fore.GREEN)
    return low

def extract_private_song_info(token_string, proxies_list=None, song_queue=None, token_container=None):
    """
    Extract private song info with parallel page downloading.
    Returns songs in chronological order (oldest first).
    token_container: optional list containing [token] that can be updated when token expires
    """
    log_with_timestamp("Extracting private songs using Authorization Token...", Fore.CYAN)
    
    # Use token from container if provided, otherwise use token_string
    current_token = token_container[0] if token_container else token_string

    # Find the last page first
    last_page = find_last_page(current_token, proxies_list)
    if last_page == 0:
        return []
    
    # Download all pages in parallel with retry logic
    try:
        all_songs = download_all_pages_parallel(last_page, current_token, proxies_list, token_container, max_workers=5)
    except Exception as e:
        log_with_timestamp(f"Failed to download all pages: {e}", Fore.RED)
        return []
    
    # Add songs to queue if provided (for parallel song processing)
    if song_queue is not None:
        for song in all_songs:
            song_queue.put(song)
    
    log_with_timestamp(f"Total songs found: {len(all_songs)}", Fore.GREEN)
    return all_songs

def get_next_version_filename(base_filename, existing_files):
    """
    Get the next available version filename based on existing files.
    Uses existing_files set for O(1) lookup instead of filesystem checks.
    """
    if base_filename not in existing_files:
        return base_filename, 1
    
    name, extn = os.path.splitext(base_filename)
    counter = 2
    while True:
        new_filename = f"{name} v{counter}{extn}"
        if new_filename not in existing_files:
            return new_filename, counter
        counter += 1

@retry_with_backoff(max_retries=10, initial_delay=2, backoff_factor=2)
def download_file(url, filename, proxies_list=None, token=None, timeout=30):
    """Download a file with retry logic."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with requests.get(url, stream=True, proxies=pick_proxy_dict(proxies_list), headers=headers, timeout=timeout) as r:
        r.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk: f.write(chunk)
    return filename

def create_placeholder_file(filename, error_message):
    """Create a placeholder text file when download fails."""
    placeholder_name = filename.replace('.mp3', '_FAILED.txt')
    try:
        with open(placeholder_name, 'w', encoding='utf-8') as f:
            f.write(f"Download failed with error:\n{error_message}\n")
        return placeholder_name
    except Exception as e:
        print(f"{Fore.RED}Could not create placeholder file: {e}")
        return None

def set_file_timestamp(filepath, created_at_str):
    """Set file modification time based on Suno's created_at timestamp."""
    if not created_at_str:
        return
    
    try:
        # Parse ISO 8601 timestamp from Suno API
        from datetime import datetime
        # Handle formats like "2024-01-15T12:34:56.789Z"
        dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        timestamp = dt.timestamp()
        
        # Set both access and modification time
        os.utime(filepath, (timestamp, timestamp))
    except Exception as e:
        # Silently fail if timestamp parsing fails
        pass

def process_song(song_data, args, state, existing_files, proxies_list):
    """
    Process a single song download with retry logic and state management.
    Returns (uuid, filename, success, error_message, was_skipped)
    """
    uuid = song_data["uuid"]
    title = song_data["title"] or uuid
    
    # Check if already downloaded (return True for was_skipped)
    if uuid in state and os.path.exists(state[uuid]):
        if args.resume:
            log_with_timestamp(f"⏭️  Skipping: {title} [UUID: {uuid}] (already downloaded)", Fore.CYAN)
            return (uuid, state[uuid], True, None, True)  # was_skipped=True
    
    log_with_timestamp(f"🎵 Processing: {title} [UUID: {uuid}]", Fore.GREEN)
    
    fname = sanitize_filename(title) + ".mp3"
    base_path = os.path.join(args.directory, fname)
    
    # Get the next available filename
    with state_lock:
        final_filename, version = get_next_version_filename(fname, existing_files)
        final_path = os.path.join(args.directory, final_filename)
        existing_files.add(final_filename)
    
    try:
        log_with_timestamp(f"  ⬇️  Downloading: {title} [UUID: {uuid}]", Fore.WHITE)
        saved_path = download_file(
            song_data["audio_url"], 
            final_path, 
            proxies_list=proxies_list,
            token=args.token,
            timeout=30
        )
        
        if args.with_thumbnail and song_data.get("image_url"):
            log_with_timestamp(f"  🖼️  Embedding thumbnail: {title} [UUID: {uuid}]", Fore.WHITE)
            embed_metadata(
                saved_path, 
                image_url=song_data["image_url"], 
                token=args.token, 
                artist=song_data.get("display_name"), 
                title=title,
                proxies_list=proxies_list
            )
        
        # Set file timestamp to match Suno's created_at
        if song_data.get("created_at"):
            set_file_timestamp(saved_path, song_data["created_at"])
        
        # Show version info
        if version > 1:
            log_with_timestamp(f"  ✅ Saved as v{version}: {os.path.basename(saved_path)} [UUID: {uuid}]", Fore.YELLOW)
        else:
            log_with_timestamp(f"  ✅ Saved: {os.path.basename(saved_path)} [UUID: {uuid}]", Fore.GREEN)
        
        return (uuid, saved_path, True, None, False)  # was_skipped=False
        
    except Exception as e:
        error_msg = str(e)
        log_with_timestamp(f"  ❌ Failed: {title} [UUID: {uuid}] - {error_msg}", Fore.RED)
        
        # Create placeholder file
        placeholder = create_placeholder_file(final_path, error_msg)
        if placeholder:
            log_with_timestamp(f"  📝 Created placeholder: {os.path.basename(placeholder)}", Fore.YELLOW)
        
        return (uuid, None, False, error_msg, False)  # was_skipped=False

def main():
    parser = argparse.ArgumentParser(description="Bulk download your private suno songs")
    parser.add_argument("--token", type=str, required=True, help="Your Suno session Bearer Token.")
    parser.add_argument("--proxy", type=str, help="Proxy with protocol (comma-separated).")
    parser.add_argument("--directory", type=str, default="suno-downloads", help="Local directory for saving files.")
    parser.add_argument("--with-thumbnail", action="store_true", default=True, help="Embed the song's thumbnail (default: True)")
    parser.add_argument("--no-thumbnail", dest="with_thumbnail", action="store_false", help="Disable thumbnail embedding")
    parser.add_argument("--max-workers", type=int, default=10, help="Number of parallel downloads (default: 10)")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip already downloaded songs (default: True)")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Disable resume functionality")
    args = parser.parse_args()

    start_time = datetime.now()
    log_with_timestamp("=" * 60, Fore.CYAN)
    log_with_timestamp("🎵 SUNO DOWNLOADER STARTED 🎵", Fore.CYAN)
    log_with_timestamp("=" * 60, Fore.CYAN)
    log_with_timestamp(f"Settings: Workers={args.max_workers}, Resume={args.resume}, Thumbnails={args.with_thumbnail}", Fore.CYAN)

    # Create directory if it doesn't exist
    if not os.path.exists(args.directory):
        os.makedirs(args.directory)
    
    # Load state
    state = load_state(args.directory)
    log_with_timestamp(f"Loaded state: {len(state)} songs previously downloaded", Fore.CYAN)
    
    # Get existing files in directory for version tracking
    existing_files = set()
    for fname in os.listdir(args.directory):
        if fname.endswith('.mp3') or fname.endswith('_FAILED.txt'):
            existing_files.add(fname)
    
    proxies_list = args.proxy.split(",") if args.proxy else None
    
    # Token container that can be updated when token expires
    token_container = [args.token]
    
    # Use parallel processing if max_workers > 1, otherwise use queue-based approach
    if args.max_workers > 1:
        log_with_timestamp(f"Using parallel downloads with {args.max_workers} workers", Fore.CYAN)
        song_queue = Queue()
        
        # Start extraction in background and feed queue
        extraction_complete = threading.Event()
        total_songs = [0]  # Use list to allow modification in nested function
        extraction_incomplete = [False]  # Track if extraction stopped early
        
        def extract_songs():
            songs = extract_private_song_info(token_container[0], proxies_list, song_queue, token_container)
            total_songs[0] = len(songs)
            extraction_complete.set()
            
            # Check if we got all songs or stopped early
            try:
                last_page = find_last_page(token_container[0], proxies_list)
                expected_songs_approx = last_page * 20  # Rough estimate
                if len(songs) < expected_songs_approx * 0.9:  # If we got less than 90% of expected
                    extraction_incomplete[0] = True
            except:
                pass
            
            if extraction_incomplete[0]:
                log_with_timestamp(f"Extraction incomplete: {len(songs)} songs found (may be partial due to token expiration)", Fore.YELLOW)
            else:
                log_with_timestamp(f"Extraction complete: {len(songs)} total songs found", Fore.GREEN)
        
        extraction_thread = threading.Thread(target=extract_songs)
        extraction_thread.start()
        
        # Process songs as they come in
        downloaded_count = 0
        failed_count = 0
        skipped_count = 0
        last_progress_time = time.time()
        
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = []
            
            while not extraction_complete.is_set() or not song_queue.empty():
                # Submit new tasks as songs become available
                while len(futures) < args.max_workers and not song_queue.empty():
                    try:
                        song_data = song_queue.get_nowait()
                        future = executor.submit(process_song, song_data, args, state, existing_files, proxies_list)
                        futures.append(future)
                    except Empty:
                        break
                
                # Check completed tasks
                if futures:
                    done_futures = [f for f in futures if f.done()]
                    for future in done_futures:
                        try:
                            uuid, filename, success, error, was_skipped = future.result()
                            if success and filename:
                                state[uuid] = filename
                                if was_skipped:
                                    skipped_count += 1
                                else:
                                    downloaded_count += 1
                                # Save state periodically
                                if (downloaded_count + skipped_count) % 10 == 0:
                                    save_state(args.directory, state)
                            else:
                                failed_count += 1
                        except Exception as e:
                            log_with_timestamp(f"Unexpected error: {e}", Fore.RED)
                            failed_count += 1
                        futures.remove(future)
                
                # Progress update every 30 seconds (only if we've started processing)
                if time.time() - last_progress_time > 30:
                    total = downloaded_count + failed_count + skipped_count
                    # Only show progress if we've actually processed something or extraction is complete
                    if total > 0 or extraction_complete.is_set():
                        if total_songs[0] > 0:
                            progress = (total / total_songs[0]) * 100
                            log_with_timestamp(
                                f"📊 Progress: {total}/{total_songs[0]} ({progress:.1f}%) | "
                                f"✅ {downloaded_count} | ⏭️ {skipped_count} | ❌ {failed_count}",
                                Fore.CYAN
                            )
                        else:
                            log_with_timestamp(
                                f"📊 Processed: {total} | ✅ {downloaded_count} | ⏭️ {skipped_count} | ❌ {failed_count}",
                                Fore.CYAN
                            )
                    last_progress_time = time.time()
                
                time.sleep(0.1)
            
            # Wait for remaining futures
            for future in futures:
                try:
                    uuid, filename, success, error, was_skipped = future.result()
                    if success and filename:
                        state[uuid] = filename
                        if was_skipped:
                            skipped_count += 1
                        else:
                            downloaded_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    log_with_timestamp(f"Unexpected error: {e}", Fore.RED)
                    failed_count += 1
        
        extraction_thread.join()
        
    else:
        # Sequential processing (original behavior but improved)
        songs = extract_private_song_info(token_container[0], proxies_list, None, token_container)
        
        if not songs:
            log_with_timestamp("No songs found. Please check your token.", Fore.RED)
            sys.exit(1)
        
        log_with_timestamp(f"Starting Download Process ({len(songs)} songs to process)", Fore.CYAN)
        
        downloaded_count = 0
        failed_count = 0
        skipped_count = 0
        
        for i, song_data in enumerate(songs, 1):
            uuid, filename, success, error, was_skipped = process_song(song_data, args, state, existing_files, proxies_list)
            
            if success and filename:
                state[uuid] = filename
                if was_skipped:
                    skipped_count += 1
                else:
                    downloaded_count += 1
                # Save state periodically
                if (downloaded_count + skipped_count) % 10 == 0:
                    save_state(args.directory, state)
                    
                # Progress update
                if i % 10 == 0:
                    progress = (i / len(songs)) * 100
                    log_with_timestamp(
                        f"📊 Progress: {i}/{len(songs)} ({progress:.1f}%) | "
                        f"✅ {downloaded_count} | ⏭️ {skipped_count} | ❌ {failed_count}",
                        Fore.CYAN
                    )
            else:
                failed_count += 1
    
    # Final state save
    save_state(args.directory, state)
    
    end_time = datetime.now()
    duration = end_time - start_time
    
    # Check if extraction was incomplete
    was_incomplete = args.max_workers > 1 and 'extraction_incomplete' in locals() and extraction_incomplete[0]
    
    log_with_timestamp("=" * 60, Fore.CYAN)
    if was_incomplete:
        log_with_timestamp("⚠️  DOWNLOAD INCOMPLETE (TOKEN EXPIRED) ⚠️", Fore.YELLOW)
    else:
        log_with_timestamp("🎵 DOWNLOAD COMPLETE 🎵", Fore.GREEN)
    log_with_timestamp("=" * 60, Fore.CYAN)
    log_with_timestamp(f"✅ Successfully downloaded: {downloaded_count}", Fore.GREEN)
    if args.max_workers > 1 and 'skipped_count' in locals():
        log_with_timestamp(f"⏭️  Skipped (already downloaded): {skipped_count}", Fore.CYAN)
    if failed_count > 0:
        log_with_timestamp(f"❌ Failed: {failed_count}", Fore.RED)
    log_with_timestamp(f"⏱️  Total time: {duration}", Fore.CYAN)
    log_with_timestamp(f"📁 Files are in '{args.directory}'", Fore.CYAN)
    
    if was_incomplete:
        log_with_timestamp("", Fore.WHITE)
        log_with_timestamp("⚠️  Note: Extraction stopped early due to token expiration.", Fore.YELLOW)
        log_with_timestamp("Run the script again with --resume to continue downloading remaining songs.", Fore.YELLOW)
    
    log_with_timestamp("=" * 60, Fore.CYAN)
    
    sys.exit(0)


if __name__ == "__main__":
    main()