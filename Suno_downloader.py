import argparse
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
from colorama import Fore, init
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error
from mutagen.mp3 import MP3

init(autoreset=True)

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'
STATE_FILE = "suno_download_state.json"

# Global lock for thread-safe file operations
state_lock = Lock()

def sanitize_filename(name, maxlen=200):
    safe = re.sub(FILENAME_BAD_CHARS, "_", name)
    safe = safe.strip(" .")
    return safe[:maxlen] if len(safe) > maxlen else safe

def retry_with_backoff(max_retries=3, initial_delay=1, backoff_factor=2):
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

@retry_with_backoff(max_retries=3, initial_delay=2, backoff_factor=2)
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

def extract_private_song_info(token_string, proxies_list=None, song_queue=None):
    """
    Extract private song info and optionally add songs to a queue for immediate processing.
    Returns songs in chronological order (oldest first).
    """
    print(f"{Fore.CYAN}Extracting private songs using Authorization Token...")
    base_api_url = "https://studio-api.prod.suno.com/api/feed/v2?hide_disliked=true&hide_gen_stems=true&hide_studio_clips=true&page="
    headers = {"Authorization": f"Bearer {token_string}"}

    all_songs = []
    page = 1
    
    while True:
        api_url = f"{base_api_url}{page}"
        try:
            print(f"{Fore.MAGENTA}Fetching songs (Page {page})...")
            response = requests.get(api_url, headers=headers, proxies=pick_proxy_dict(proxies_list), timeout=15)
            if response.status_code in [401, 403]:
                print(f"{Fore.RED}Authorization failed (status {response.status_code}). Your token is likely expired or incorrect.")
                return []
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"{Fore.RED}Request failed on page {page}: {e}")
            return []

        clips = data if isinstance(data, list) else data.get("clips", [])
        if not clips:
            print(f"{Fore.YELLOW}No more clips found on page {page}.")
            break

        print(f"{Fore.GREEN}Found {len(clips)} clips on page {page}.")
        
        # Collect songs from this page
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
        
        # Add page songs to queue if provided (for parallel processing)
        # Note: API returns newest first, so we reverse each page and the final list
        # to achieve oldest-first chronological order
        if song_queue is not None:
            for song in reversed(page_songs):
                song_queue.put(song)
        
        all_songs.extend(page_songs)
        page += 1
        time.sleep(5)
    
    # Return songs in chronological order (oldest first)
    # The API returns newest first, so we reverse the entire list
    all_songs.reverse()
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

@retry_with_backoff(max_retries=3, initial_delay=2, backoff_factor=2)
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

def process_song(song_data, args, state, existing_files, proxies_list):
    """
    Process a single song download with retry logic and state management.
    Returns (uuid, filename, success, error_message)
    """
    uuid = song_data["uuid"]
    title = song_data["title"] or uuid
    
    # Check if already downloaded
    if uuid in state and os.path.exists(state[uuid]):
        if args.resume:
            print(f"Skipping: {Fore.CYAN}🎵 {title} (already downloaded as {os.path.basename(state[uuid])})")
            return (uuid, state[uuid], True, None)
    
    print(f"Processing: {Fore.GREEN}🎵 {title}")
    
    fname = sanitize_filename(title) + ".mp3"
    base_path = os.path.join(args.directory, fname)
    
    # Get the next available filename
    with state_lock:
        final_filename, version = get_next_version_filename(fname, existing_files)
        final_path = os.path.join(args.directory, final_filename)
        existing_files.add(final_filename)
    
    try:
        print(f"  -> Downloading...")
        saved_path = download_file(
            song_data["audio_url"], 
            final_path, 
            proxies_list=proxies_list,
            token=args.token,
            timeout=30
        )
        
        if args.with_thumbnail and song_data.get("image_url"):
            print(f"  -> Embedding thumbnail...")
            embed_metadata(
                saved_path, 
                image_url=song_data["image_url"], 
                token=args.token, 
                artist=song_data.get("display_name"), 
                title=title,
                proxies_list=proxies_list
            )
        
        # Show version info if not the first version
        if version > 1:
            print(f"{Fore.YELLOW}  -> Saved as version {version}: {os.path.basename(saved_path)}")
        else:
            print(f"{Fore.GREEN}  -> Saved: {os.path.basename(saved_path)}")
        
        return (uuid, saved_path, True, None)
        
    except Exception as e:
        error_msg = str(e)
        print(f"{Fore.RED}Failed on {title}: {error_msg}")
        
        # Create placeholder file
        placeholder = create_placeholder_file(final_path, error_msg)
        if placeholder:
            print(f"{Fore.YELLOW}  -> Created placeholder: {os.path.basename(placeholder)}")
        
        return (uuid, None, False, error_msg)

def main():
    parser = argparse.ArgumentParser(description="Bulk download your private suno songs")
    parser.add_argument("--token", type=str, required=True, help="Your Suno session Bearer Token.")
    parser.add_argument("--proxy", type=str, help="Proxy with protocol (comma-separated).")
    parser.add_argument("--directory", type=str, default="suno-downloads", help="Local directory for saving files.")
    parser.add_argument("--with-thumbnail", action="store_true", help="Embed the song's thumbnail.")
    parser.add_argument("--max-workers", type=int, default=1, help="Number of parallel downloads (default: 1)")
    parser.add_argument("--resume", action="store_true", help="Skip already downloaded songs based on state file.")
    args = parser.parse_args()

    # Create directory if it doesn't exist
    if not os.path.exists(args.directory):
        os.makedirs(args.directory)
    
    # Load state
    state = load_state(args.directory)
    print(f"{Fore.CYAN}Loaded state: {len(state)} songs previously downloaded")
    
    # Get existing files in directory for version tracking
    existing_files = set()
    for fname in os.listdir(args.directory):
        if fname.endswith('.mp3') or fname.endswith('_FAILED.txt'):
            existing_files.add(fname)
    
    proxies_list = args.proxy.split(",") if args.proxy else None
    
    # Use parallel processing if max_workers > 1, otherwise use queue-based approach
    if args.max_workers > 1:
        print(f"{Fore.CYAN}Using parallel downloads with {args.max_workers} workers")
        song_queue = Queue()
        
        # Start extraction in background and feed queue
        extraction_complete = threading.Event()
        
        def extract_songs():
            songs = extract_private_song_info(args.token, proxies_list, song_queue)
            extraction_complete.set()
            print(f"{Fore.GREEN}Extraction complete: {len(songs)} total songs found")
        
        extraction_thread = threading.Thread(target=extract_songs)
        extraction_thread.start()
        
        # Process songs as they come in
        downloaded_count = 0
        failed_count = 0
        
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
                            uuid, filename, success, error = future.result()
                            if success and filename:
                                state[uuid] = filename
                                downloaded_count += 1
                                # Save state periodically
                                if downloaded_count % 10 == 0:
                                    save_state(args.directory, state)
                            else:
                                failed_count += 1
                        except Exception as e:
                            print(f"{Fore.RED}Unexpected error: {e}")
                            failed_count += 1
                        futures.remove(future)
                
                time.sleep(0.1)
            
            # Wait for remaining futures
            for future in futures:
                try:
                    uuid, filename, success, error = future.result()
                    if success and filename:
                        state[uuid] = filename
                        downloaded_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    print(f"{Fore.RED}Unexpected error: {e}")
                    failed_count += 1
        
        extraction_thread.join()
        
    else:
        # Sequential processing (original behavior but improved)
        songs = extract_private_song_info(args.token, proxies_list)
        
        if not songs:
            print(f"{Fore.RED}No songs found. Please check your token.")
            sys.exit(1)
        
        print(f"\n{Fore.CYAN}--- Starting Download Process ({len(songs)} songs to process) ---")
        
        downloaded_count = 0
        failed_count = 0
        
        for song_data in songs:
            uuid, filename, success, error = process_song(song_data, args, state, existing_files, proxies_list)
            
            if success and filename:
                state[uuid] = filename
                downloaded_count += 1
                # Save state periodically
                if downloaded_count % 10 == 0:
                    save_state(args.directory, state)
            else:
                failed_count += 1
    
    # Final state save
    save_state(args.directory, state)
    
    print(f"\n{Fore.BLUE}Download process complete!")
    print(f"{Fore.GREEN}Successfully downloaded: {downloaded_count}")
    if failed_count > 0:
        print(f"{Fore.RED}Failed: {failed_count}")
    print(f"{Fore.CYAN}Files are in '{args.directory}'")
    
    sys.exit(0)


if __name__ == "__main__":
    main()