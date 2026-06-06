#!/usr/bin/env python3
"""
MakeMKV DVD Ripper - Automated disc ripping with looping disc detection.

This script continuously monitors for DVDs, automatically rips titles that meet
the minimum duration threshold, ejects the disc, and waits for the next one.
Configuration is managed via config.toml with CLI overrides.
"""

import argparse
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Handle TOML import for Python 3.11+ vs older versions
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        print("Error: tomli (or Python 3.11+) is required. Install with: pip install -r requirements.txt")
        sys.exit(1)


class MakeMKVRipper:
    """Automated DVD ripper using MakeMKV."""

    def __init__(self, config: dict, args: argparse.Namespace):
        """Initialize ripper with configuration and arguments."""
        self.config = config
        self.args = args
        self.debug = args.debug
        self.logger = self._setup_logging()
        self._running = True
        self._lock_path = Path(__file__).with_name(f"rip.{self._get_drive_index()}.lock")
        self._lock_fd: Optional[int] = None
        self._last_query_status = "idle"

    def _setup_logging(self) -> logging.Logger:
        """Configure logging to console and file."""
        logger = logging.getLogger("makemkv_ripper")
        logger.handlers.clear()
        logger.propagate = False
        
        # Get log level and file path from config, apply CLI overrides
        log_level_str = self.config["logging"].get("log_level", "INFO")
        log_level = getattr(logging, log_level_str.upper(), logging.INFO)
        logger.setLevel(log_level)
        
        log_file = self.config["logging"].get("log_file", "rip.log")
        log_file = os.path.expanduser(log_file)
        
        # Create formatters
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File handler
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except IOError as e:
            logger.warning(f"Could not open log file {log_file}: {e}")
        
        return logger

    def _get_makemkvcon_path(self) -> str:
        """Get the path to makemkvcon executable."""
        if self.args.makemkvcon_path:
            return self.args.makemkvcon_path
        return self.config["makemkv"].get("path", "makemkvcon")

    def _is_pid_running(self, pid: int) -> bool:
        """Return True if a process with the given PID appears to be alive."""
        if pid <= 0:
            return False

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            # On Windows, os.kill(pid, 0) may raise OSError instead of ProcessLookupError
            # Treat this as process not running so we can clean up stale lock files
            return False
        else:
            return True

    def _acquire_lock(self) -> bool:
        """Prevent multiple rip.py instances from fighting over the same drive."""
        while True:
            try:
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                self._lock_fd = fd
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                os.fsync(fd)
                return True
            except FileExistsError:
                try:
                    existing_pid = int(self._lock_path.read_text(encoding="ascii").strip() or "0")
                except (OSError, ValueError):
                    existing_pid = 0

                if existing_pid and self._is_pid_running(existing_pid):
                    self.logger.error(
                        f"Another rip.py instance is already running (PID {existing_pid}). "
                        "Stop it before starting a new one."
                    )
                    return False

                self.logger.warning(f"Removing stale lock file: {self._lock_path}")
                try:
                    self._lock_path.unlink()
                except OSError as e:
                    self.logger.error(f"Could not remove stale lock file {self._lock_path}: {e}")
                    return False
        return False  # Unreachable, but satisfies type checkers

    def _release_lock(self):
        """Release the instance lock if we own it."""
        fd = self._lock_fd
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            finally:
                self._lock_fd = None

        try:
            if self._lock_path.exists():
                lock_pid = self._lock_path.read_text(encoding="ascii").strip()
                if lock_pid == str(os.getpid()):
                    self._lock_path.unlink()
        except OSError:
            pass

    def _get_drive_index(self) -> int:
        """Get the drive index to use."""
        if self.args.drive is not None:
            return self.args.drive
        return self.config["makemkv"].get("drive", 0)

    def _get_output_directory(self) -> Path:
        """Get and expand the output directory."""
        if self.args.output:
            output_dir = self.args.output
        else:
            output_dir = self.config["output"].get("directory", "~/Videos/rips")
        
        path = Path(output_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_min_duration(self) -> int:
        """Get minimum duration in seconds."""
        if self.args.min_duration is not None:
            return self.args.min_duration
        return self.config["rip"].get("min_duration_seconds", 1800)

    def _get_poll_interval(self) -> int:
        """Get poll interval in seconds."""
        return self.config["rip"].get("poll_interval_seconds", 10)

    def _get_query_timeout(self) -> int:
        """Get query timeout in seconds."""
        return self.config["rip"].get("query_timeout_seconds", 60)

    def _extract_robot_messages(self, output: str) -> list[str]:
        """Extract human-readable text from MakeMKV robot-mode MSG lines."""
        messages = []
        for line in output.splitlines():
            match = re.match(r'^MSG:\d+,\d+,\d+,"([^"]+)"', line.strip())
            if match:
                messages.append(match.group(1))
        return messages

    def _summarize_makemkv_output(self, output: str) -> str:
        """Return a compact summary of the most useful MakeMKV output lines."""
        messages = self._extract_robot_messages(output)
        if messages:
            msg_len = len(messages)
            start_idx = msg_len - 3 if msg_len >= 3 else 0
            recent_msgs = []
            for i in range(start_idx, msg_len):
                recent_msgs.append(messages[i])
            return " | ".join(recent_msgs)

        lines = [line.strip() for line in output.splitlines() if line.strip()]
        line_len = len(lines)
        start_idx = line_len - 3 if line_len >= 3 else 0
        recent_lines = []
        for i in range(start_idx, line_len):
            recent_lines.append(lines[i])
        return " | ".join(recent_lines)

    def _describe_makemkv_failure(self, output: str, fallback: str) -> str:
        """Build a readable error message from MakeMKV output."""
        summary = self._summarize_makemkv_output(output)
        if summary:
            return f"{fallback}. MakeMKV said: {summary}"
        return fallback

    def _handle_query_failure(self, drive_idx: int, output: str, error_text: str):
        """Log a query failure and stop polling when MakeMKV is unusable."""
        detail = self._describe_makemkv_failure(output, error_text)
        lower_output = output.lower()
        lower_detail = detail.lower()

        if "version is too old" in lower_output or "registration key" in lower_output:
            self._last_query_status = "fatal"
            self.logger.error(f"makemkvcon query failed (drive {drive_idx}): {detail}")
            self.logger.error("MakeMKV requires an update or registration key before ripping can continue.")
            self._running = False
            return

        if "timed out" in lower_detail:
            self._last_query_status = "timeout"
        else:
            self._last_query_status = "error"

        self.logger.warning(f"makemkvcon query failed (drive {drive_idx}): {detail}")

    def _run_command(self, cmd: list) -> tuple[int, str, str]:
        """
        Run a shell command and capture output.
        
        Returns:
            (return_code, stdout, stderr)
        """
        try:
            # Use Popen instead of run to avoid deadlocks with large output
            # Redirect stderr to stdout to combine them
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            try:
                stdout, _ = process.communicate(timeout=self._get_query_timeout())
            except subprocess.TimeoutExpired as e:
                partial_output = e.stdout or ""
                process.kill()
                stdout, _ = process.communicate()
                combined_output = f"{partial_output}{stdout or ''}"
                timeout_value = self._get_query_timeout()
                return -1, combined_output, f"Command timed out after {timeout_value} seconds"
            
            returncode = process.returncode
            return returncode, stdout or "", ""
        except Exception as e:
            self.logger.warning(f"Error running command {cmd[0]}: {str(e)}")
            return -1, "", str(e)

    def _query_disc_titles(self) -> list[dict]:
        """
        Query makemkvcon for available titles on the disc.
        
        Returns list of dicts with 'index' and 'duration_seconds' keys.
        """
        makemkvcon = self._get_makemkvcon_path()
        drive_idx = self._get_drive_index()
        
        cmd = [makemkvcon, "-r", "info", f"disc:{drive_idx}"]
        returncode, stdout, stderr = self._run_command(cmd)
        
        if returncode != 0:
            self._handle_query_failure(drive_idx, stdout, stderr or "Command failed")
            return []
        
        self._last_query_status = "ok"
        titles: dict[int, dict[str, int]] = {}
        # Parse TINFO lines: TINFO:title_idx,info_type,field,"value"
        # info_type 9 = duration in HH:MM:SS format
        tinfo_count: int = 0
        found_durations: int = 0
        for line in stdout.split('\n'):
            line = line.strip()  # Remove leading/trailing whitespace and line endings
            if not line:
                continue
            if line.startswith('TINFO:'):
                current_tinfo_count: int = tinfo_count
                tinfo_count = current_tinfo_count + 1
                # Format: TINFO:0,9,0,"1:42:28"
                # Split on comma but preserve quoted values
                parts = line.split(',', 3)  # Split into max 4 parts
                if len(parts) >= 4:
                    try:
                        title_idx = int(parts[0].split(':')[1])
                        info_type = int(parts[1])
                        # The value is in the 4th part, in quotes
                        value_str = parts[3].strip().strip('"')
                        
                        # Info type 9 is duration in HH:MM:SS format
                        if info_type == 9:
                            current_duration_count: int = found_durations
                            found_durations = current_duration_count + 1
                            # Parse HH:MM:SS format
                            time_parts = value_str.split(':')
                            if len(time_parts) == 3:
                                try:
                                    hours = int(time_parts[0])
                                    minutes = int(time_parts[1])
                                    seconds = int(time_parts[2])
                                    duration_seconds = hours * 3600 + minutes * 60 + seconds
                                    
                                    if title_idx not in titles:
                                        titles[title_idx] = {}
                                    titles[title_idx]['duration_seconds'] = duration_seconds
                                    
                                    if self.debug:
                                        self.logger.info(f"[PARSE] Title {title_idx}: {value_str} = {duration_seconds}s")
                                except (ValueError, TypeError) as e:
                                    self.logger.warning(f"[PARSE ERROR] Failed to parse time '{value_str}' from line: {line} - {e}")
                            else:
                                self.logger.debug(f"[PARSE] Time format mismatch for line: {line} (got {len(time_parts)} parts)")
                    except (ValueError, IndexError) as e:
                        self.logger.warning(f"[PARSE ERROR] Failed to parse TINFO line: {line} - {e}")
                        continue
        
        # Log parsing statistics
        if tinfo_count == 0:
            self.logger.warning(f"[DETECT] No TINFO lines found in makemkvcon output")
        else:
            self.logger.info(f"[DETECT] Found {tinfo_count} TINFO lines, {found_durations} with duration info")
        
        # Convert dict to list of dicts
        result = [{'index': idx, 'duration_seconds': data['duration_seconds']} 
                  for idx, data in titles.items() if 'duration_seconds' in data]
        
        # Log what we found
        if not result:
            self.logger.warning(f"No valid titles with duration found")
        else:
            titles_info = ", ".join([f"Title {t['index']}: {t['duration_seconds']//60}m{t['duration_seconds']%60}s" for t in result])
            self.logger.info(f"Disc detected: {titles_info}")
        
        return result

    def _get_disc_label(self) -> str:
        """
        Extract the disc label/name from makemkvcon output.
        
        Returns the disc label or a default name if not found.
        """
        makemkvcon = self._get_makemkvcon_path()
        drive_idx = self._get_drive_index()
        
        cmd = [makemkvcon, "-r", "info", f"disc:{drive_idx}"]
        returncode, stdout, stderr = self._run_command(cmd)
        
        if returncode == 0:
            # Parse CINFO lines: CINFO:2,0,"disc_label"
            # CINFO with type 2 is the disc label
            for line in stdout.split('\n'):
                if line.startswith('CINFO:2,'):
                    try:
                        # Extract quoted value
                        parts = line.split('"')
                        if len(parts) >= 3:
                            disc_label = parts[1]
                            if disc_label:
                                # Sanitize for use as filename
                                safe_label = "".join(c for c in disc_label if c.isalnum() or c in (' ', '-', '_'))
                                return safe_label.strip()
                    except (ValueError, IndexError):
                        pass
        
        return "disc"

    def _filter_titles_by_duration(self, titles: list[dict]) -> list[dict]:
        """Filter titles to only those meeting minimum duration."""
        min_duration = self._get_min_duration()
        filtered = [t for t in titles if t['duration_seconds'] >= min_duration]
        return filtered

    def _rip_title(self, title_index: int, output_dir: Path) -> bool:
        """
        Rip a single title using makemkvcon.
        
        Returns True if successful, False otherwise.
        """
        makemkvcon = self._get_makemkvcon_path()
        drive_idx = self._get_drive_index()
        
        cmd = [
            makemkvcon,
            "mkv",
            f"disc:{drive_idx}",
            str(title_index),
            str(output_dir)
        ]
        
        self.logger.info(f"Starting rip of title {title_index}...")
        
        try:
            # Use Popen to stream output in real-time
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            last_progress = -1
            while True:
                # Handle Pyre2 NoneType error for process.stdout
                stdout = process.stdout
                if not stdout:
                    break
                    
                line = stdout.readline()
                if not line:
                    break
                
                line = line.rstrip()
                if not line:
                    continue
                
                # Parse progress message: PRGT:c1,n1,p50  (50% = p50)
                if line.startswith('PRGT:'):
                    try:
                        parts = line.split(',')
                        if len(parts) >= 3:
                            progress_str = parts[2]  # p50
                            if progress_str.startswith('p'):
                                # Handle Pyre2 slice type checking explicitly
                                p_val = progress_str.replace('p', '', 1)
                                progress = int(p_val)
                                # Only log every 10% to avoid spam
                                # Cast to int to ensure type checker knows it's int subtraction
                                if int(progress) - int(last_progress) >= 10 or progress == 100:
                                    self.logger.info(f"Title {title_index} ripping: {progress}%")
                                    last_progress = progress
                    except (ValueError, IndexError):
                        pass
                
                # Log other messages at debug level
                elif line.startswith('MSG:') or line.startswith('PRG'):
                    self.logger.debug(f"[makemkvcon] {line}")
            
            returncode = process.wait()
            
            if returncode == 0:
                self.logger.info(f"Successfully ripped title {title_index}")
                return True
            else:
                self.logger.error(f"Failed to rip title {title_index} (exit code {returncode})")
                return False
        
        except subprocess.TimeoutExpired:
            self.logger.error(f"Ripping title {title_index} timed out")
            process.kill()
            return False
        except Exception as e:
            self.logger.error(f"Error ripping title {title_index}: {e}")
            return False

    def _get_drive_letter(self) -> Optional[str]:
        """
        Extract the drive letter for the configured drive from makemkvcon output.
        Returns the drive letter (e.g., 'D:') or None if not found.
        """
        makemkvcon = self._get_makemkvcon_path()
        drive_idx = self._get_drive_index()
        
        cmd = [makemkvcon, "-r", "info", f"disc:{drive_idx}"]
        returncode, stdout, stderr = self._run_command(cmd)
        
        if returncode != 0:
            return None
        
        # Parse DRV lines to find the drive letter
        # Format: DRV:0,2,999,1,"DVD+R-DL ASUS DRW-24B1ST   j 1.00 F1D0CL024644","5_CARD_STUD","F:"
        for line in stdout.split('\n'):
            if line.startswith(f'DRV:{drive_idx},'):
                # Extract drive letter from the last quoted field
                parts = line.split('"')
                if len(parts) >= 6:
                    drive_letter = parts[-2]  # Second to last quoted part
                    if drive_letter and ':' in drive_letter:
                        return drive_letter
        
        return None

    def _eject_disc(self) -> bool:
        """
        Eject the disc using platform-specific method.
        
        Returns True if successful, False otherwise.
        """
        try:
            system = platform.system()
            
            if system == "Linux":
                # Use eject command
                result = subprocess.run(["eject"], capture_output=True)
                if result.returncode == 0:
                    self.logger.info("Disc ejected successfully")
                    return True
            
            elif system == "Windows":
                # First, try to get the actual drive letter from makemkvcon
                drive_letter = self._get_drive_letter()
                if drive_letter:
                    self.logger.debug(f"Detected drive letter: {drive_letter}")
                    drives_to_try = [drive_letter]
                else:
                    # Fallback to common DVD drive letters if detection fails
                    self.logger.debug("Drive letter detection failed, trying common letters")
                    drives_to_try = ['D:', 'E:', 'F:', 'G:']
                
                for drive in drives_to_try:
                    try:
                        # Use Windows API via PowerShell to eject
                        ps_cmd = f"(New-Object -ComObject Shell.Application).NameSpace(17).ParseName('{drive}').InvokeVerb('Eject')"
                        result = subprocess.run(
                            ["powershell", "-NoProfile", "-Command", ps_cmd],
                            capture_output=True,
                            timeout=5
                        )
                        
                        # PowerShell returns 0 for success or no error
                        if result.returncode == 0:
                            self.logger.info(f"Disc ejected successfully (drive {drive})")
                            time.sleep(1)  # Brief pause to ensure ejection completes
                            return True
                    except Exception as e:
                        self.logger.debug(f"Failed to eject {drive}: {e}")
                        continue
            
            elif system == "Darwin":
                # macOS - use diskutil
                result = subprocess.run(["diskutil", "eject", "/Volumes/MACOS"], capture_output=True)
                if result.returncode == 0:
                    self.logger.info("Disc ejected successfully")
                    return True
            
            self.logger.warning("Could not eject disc automatically")
            return False
        
        except Exception as e:
            self.logger.warning(f"Error ejecting disc: {e}")
            return False

    def _wait_for_disc(self) -> bool:
        """
        Poll until a disc with suitable titles is detected.
        
        Returns True if disc detected with titles, False if interrupted.
        """
        try:
            poll_interval = self._get_poll_interval()
            drive_idx = self._get_drive_index()
            self.logger.info(f"Checking for DVD disc on drive {drive_idx}...")
            
            # Try immediately first, with a few quick retries (100ms apart)
            for attempt in range(5):
                try:
                    titles = self._query_disc_titles()
                    if titles:
                        self.logger.info(f"Disc detected with {len(titles)} total titles")
                        return True
                except Exception as e:
                    self.logger.debug(f"Error querying disc (attempt {attempt+1}): {e}")

                if self._last_query_status in {"fatal", "timeout", "error"}:
                    break
                
                if attempt < 4:
                    time.sleep(0.1)  # 100ms between quick retries

            if not self._running:
                return False
            
            # No disc found after quick retries, now wait and poll every configured interval
            self.logger.info(f"No disc found. Waiting for next disc... (checking every {poll_interval}s)")
            poll_count = 0
            while self._running:
                time.sleep(poll_interval)
                try:
                    titles = self._query_disc_titles()
                    if titles:
                        self.logger.info(f"Disc detected with {len(titles)} total titles")
                        return True
                except Exception as e:
                    self.logger.debug(f"Error querying disc during polling: {e}")
                
                poll_count += 1
                if poll_count % 12 == 0:  # Status update every 2 minutes (10s * 12)
                    self.logger.debug(f"Still waiting for disc on drive {drive_idx}... (checked {poll_count+5} times)")
            
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error in _wait_for_disc: {e}", exc_info=True)
            return False

    def _process_disc(self):
        """Process a single disc: query, filter, rip, eject."""
        titles = self._query_disc_titles()
        if not titles:
            if self._running:
                self.logger.warning("No titles found on disc")
            return
        
        self.logger.info(f"Found {len(titles)} titles on disc")
        
        filtered = self._filter_titles_by_duration(titles)
        if not filtered:
            self.logger.warning(
                f"No titles meet minimum duration of "
                f"{self._get_min_duration()} seconds"
            )
            return
        
        self.logger.info(f"{len(filtered)} titles meet duration threshold")
        
        base_output_dir = self._get_output_directory()
        disc_label = self._get_disc_label()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        
        # Create a unique folder for this specific disc
        output_dir = base_output_dir / f"{disc_label}_{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger.info(f"Output directory: {output_dir}")
        
        for title in filtered:
            if not self._running:
                break
            self._rip_title(title['index'], output_dir)
        
        if self._running:
            self._eject_disc()

    def run(self):
        """Main loop: continuously process discs."""
        if not self._acquire_lock():
            return

        self.logger.info("MakeMKV Ripper started")
        self.logger.info(f"Using drive index: {self._get_drive_index()}")
        self.logger.info(f"Using makemkvcon: {self._get_makemkvcon_path()}")
        self.logger.info(f"Output directory: {self._get_output_directory()}")
        self.logger.info(f"Minimum duration: {self._get_min_duration()} seconds")
        self.logger.info(f"Poll interval: {self._get_poll_interval()} seconds")
        
        try:
            while self._running:
                if self._wait_for_disc():
                    self._process_disc()
                    if self._running:
                        self.logger.info("Disc processed, waiting for next disc...")
        
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user")
        
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
        
        finally:
            self._running = False
            self.logger.info("MakeMKV Ripper stopped")
            self._release_lock()

    def stop(self):
        """Signal the ripper to stop gracefully."""
        self._running = False


def load_config(config_path: str) -> dict:
    """Load configuration from TOML file."""
    path = Path(config_path).expanduser()
    
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    
    with open(path, 'rb') as f:
        return tomllib.load(f)


def main():
    """Parse arguments and start the ripper."""
    import sys
    print(f"[DEBUG] Script starting... Python {sys.version}", file=sys.stderr)
    print(f"[DEBUG] CWD: {os.getcwd()}", file=sys.stderr)
    
    parser = argparse.ArgumentParser(
        description="Automated DVD ripper using MakeMKV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rip.py                          # Use defaults from config.toml
  rip.py --config custom.toml     # Use custom config file
  rip.py --output ~/MyRips        # Override output directory
  rip.py --min-duration 900       # Only rip titles >= 15 minutes
        """
    )
    
    parser.add_argument(
        "--config",
        type=str,
        default="config.toml",
        help="Path to config file (default: config.toml)"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output directory (overrides config)"
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        help="Minimum title duration in seconds (overrides config)"
    )
    parser.add_argument(
        "--drive",
        type=int,
        help="Drive index 0, 1, 2, etc. (overrides config)"
    )
    parser.add_argument(
        "--makemkvcon-path",
        type=str,
        help="Path to makemkvcon executable (overrides config)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output (show raw makemkvcon output)"
    )
    
    args = parser.parse_args()
    
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)
    
    ripper = MakeMKVRipper(config, args)
    ripper.run()


if __name__ == "__main__":
    main()
