#!/usr/bin/env python3
"""
Basic tests for the Suno downloader functionality.
Tests retry logic, state management, and file versioning.
"""

import json
import os
import sys
import tempfile
import shutil
from unittest.mock import Mock, patch, MagicMock
import time

# Import the module
sys.path.insert(0, os.path.dirname(__file__))
import Suno_downloader as sd


def test_sanitize_filename():
    """Test filename sanitization."""
    print("Testing sanitize_filename...")
    
    # Test with special characters
    result = sd.sanitize_filename('test<>:"/\\|?*file')
    assert '_' in result  # Should replace special chars with underscores
    assert '<' not in result and '>' not in result  # No special chars
    
    # Test with long name
    long_name = 'a' * 300
    result = sd.sanitize_filename(long_name)
    assert len(result) == 200
    
    print("✓ sanitize_filename tests passed")


def test_get_next_version_filename():
    """Test version filename generation."""
    print("Testing get_next_version_filename...")
    
    existing_files = set()
    
    # First file should not have version
    filename, version = sd.get_next_version_filename("test.mp3", existing_files)
    assert filename == "test.mp3"
    assert version == 1
    
    # Add to existing and try again
    existing_files.add("test.mp3")
    filename, version = sd.get_next_version_filename("test.mp3", existing_files)
    assert filename == "test v2.mp3"
    assert version == 2
    
    # Add v2 and try again
    existing_files.add("test v2.mp3")
    filename, version = sd.get_next_version_filename("test.mp3", existing_files)
    assert filename == "test v3.mp3"
    assert version == 3
    
    print("✓ get_next_version_filename tests passed")


def test_state_management():
    """Test state file load/save."""
    print("Testing state management...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Test saving state
        test_state = {
            "uuid1": "/path/to/file1.mp3",
            "uuid2": "/path/to/file2.mp3"
        }
        
        sd.save_state(tmpdir, test_state)
        
        # Verify file was created
        state_file = os.path.join(tmpdir, sd.STATE_FILE)
        assert os.path.exists(state_file)
        
        # Test loading state
        loaded_state = sd.load_state(tmpdir)
        assert loaded_state == test_state
        
        # Test loading from non-existent directory
        empty_state = sd.load_state("/nonexistent/path")
        assert empty_state == {}
    
    print("✓ state management tests passed")


def test_retry_decorator():
    """Test retry with backoff decorator."""
    print("Testing retry decorator...")
    
    # Create a function that fails twice then succeeds
    call_count = [0]
    
    @sd.retry_with_backoff(max_retries=3, initial_delay=0.1, backoff_factor=2)
    def failing_function():
        call_count[0] += 1
        if call_count[0] < 3:
            raise Exception("Temporary failure")
        return "success"
    
    result = failing_function()
    assert result == "success"
    assert call_count[0] == 3
    
    # Test function that always fails
    @sd.retry_with_backoff(max_retries=2, initial_delay=0.1, backoff_factor=2)
    def always_fails():
        raise Exception("Always fails")
    
    try:
        always_fails()
        assert False, "Should have raised exception"
    except Exception as e:
        assert str(e) == "Always fails"
    
    print("✓ retry decorator tests passed")


def test_create_placeholder_file():
    """Test placeholder file creation."""
    print("Testing placeholder file creation...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test.mp3")
        error_msg = "Connection timeout"
        
        placeholder = sd.create_placeholder_file(test_file, error_msg)
        
        assert placeholder == test_file.replace('.mp3', '_FAILED.txt')
        assert os.path.exists(placeholder)
        
        with open(placeholder, 'r') as f:
            content = f.read()
            assert error_msg in content
    
    print("✓ placeholder file tests passed")


def test_process_song_with_resume():
    """Test song processing with resume functionality."""
    print("Testing process_song with resume...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create mock args
        args = Mock()
        args.directory = tmpdir
        args.resume = True
        args.with_thumbnail = False
        args.token = "test_token"
        
        # Create a song that's already in state
        existing_file = os.path.join(tmpdir, "existing_song.mp3")
        with open(existing_file, 'w') as f:
            f.write("dummy")
        
        state = {"uuid123": existing_file}
        existing_files = set(["existing_song.mp3"])
        
        song_data = {
            "uuid": "uuid123",
            "title": "Existing Song",
            "audio_url": "http://example.com/song.mp3",
            "image_url": None,
            "display_name": "Artist"
        }
        
        # Should skip the download
        uuid, filename, success, error = sd.process_song(
            song_data, args, state, existing_files, None
        )
        
        assert success is True
        assert filename == existing_file
        assert error is None
    
    print("✓ process_song with resume tests passed")


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Running Suno Downloader Tests")
    print("=" * 60)
    
    try:
        test_sanitize_filename()
        test_get_next_version_filename()
        test_state_management()
        test_retry_decorator()
        test_create_placeholder_file()
        test_process_song_with_resume()
        
        print("\n" + "=" * 60)
        print("✓ All tests passed!")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
