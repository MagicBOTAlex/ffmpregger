import argparse
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tqdm import tqdm

IMAGE_EXTS = {".jpg", ".jpeg"}
VIDEO_EXTS = {".mkv", ".mp4", ".iso"}


def get_video_info(file_path):
    """
    Uses ffprobe to extract width, height, and duration (in seconds).
    Returns (width, height, duration) or (None, None, 0.0) on failure.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        duration = float(data.get("format", {}).get("duration", 0))
        return stream.get("width"), stream.get("height"), duration
    except Exception:
        return None, None, 0.0


def parse_time_to_seconds(time_str):
    """Converts HH:MM:SS.microseconds into total seconds."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
    except ValueError:
        pass
    return 0.0


def process_file(src_path, dst_path, position_slot, main_pbar, lock):
    """
    Handles a single file: direct copies or tracks video transcode progress.
    Updates worker progress and continuously updates the shared main progress bar.
    """
    os.makedirs(dst_path.parent, exist_ok=True)
    ext = src_path.suffix.lower()

    # 1. Direct copy for image files
    if ext in IMAGE_EXTS:
        shutil.copy2(src_path, dst_path)
        return True, f"Copied: {src_path.name}"

    # 2. Transcode video files
    if ext in VIDEO_EXTS:
        dst_path = dst_path.with_suffix(".mp4")
        width, height, duration = get_video_info(src_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            "cuda",
            "-i",
            str(src_path),
        ]

        if width and height and (width > 1920 or height > 1080):
            cmd.extend(
                [
                    "-vf",
                    "scale_cuda=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease",
                ]
            )

        cmd.extend(
            [
                "-c:v",
                "hevc_nvenc",
                "-preset",
                "p7",
                "-tune",
                "hq",
                "-rc",
                "constqp",
                "-cq",
                "20",
                "-spatial-aq",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-progress",
                "pipe:1",  # Output machine-readable progress to stdout
                "-nostats",
                str(dst_path),
            ]
        )

        short_filename = (
            src_path.name if len(src_path.name) <= 70 else f"{src_path.name[:67]}..."
        )
        total_units = int(duration) if duration > 0 else 100

        # Worker progress bar with ETA/remaining time estimate
        worker_bar = tqdm(
            total=total_units,
            desc=f"Worker {position_slot + 1} [{short_filename}]",
            position=position_slot + 1,
            leave=False,
            unit="s",
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}, {postfix}]",
        )

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )

            current_speed = "0.0x"
            current_fps = "0"
            last_processed_sec = 0.0

            # Parse FFmpeg's key=value progress output
            for line in process.stdout:
                line = line.strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    if key == "out_time":
                        out_seconds = parse_time_to_seconds(val)
                        delta = out_seconds - last_processed_sec

                        if delta > 0:
                            # Update worker bar
                            worker_bar.update(delta)

                            # Update overall global progress bar (thread-safe)
                            with lock:
                                main_pbar.update(delta)

                            last_processed_sec = out_seconds

                    elif key == "speed":
                        current_speed = val.strip()
                    elif key == "fps":
                        current_fps = val.strip()
                    elif key == "progress" and val == "continue":
                        worker_bar.set_postfix_str(
                            f"Speed: {current_speed} | FPS: {current_fps}"
                        )

            process.wait()

            # Fill in any remaining fraction to complete the bar cleanly
            remaining_delta = total_units - worker_bar.n
            if remaining_delta > 0:
                worker_bar.update(remaining_delta)
                with lock:
                    main_pbar.update(remaining_delta)

            worker_bar.close()

            if process.returncode != 0:
                stderr_output = process.stderr.read()
                return (
                    False,
                    f"Failed: {src_path.name} - Error: {stderr_output}",
                )

            return True, f"Transcoded: {src_path.name}"

        except Exception as e:
            worker_bar.close()
            return False, f"Failed: {src_path.name} - Error: {str(e)}"

    return True, f"Skipped: {src_path.name}"


def main():
    parser = argparse.ArgumentParser(
        description="Recursive Media Converter for NVENC Pascal GPUs"
    )
    parser.add_argument("src", type=str, help="Source directory")
    parser.add_argument("dst", type=str, help="Destination directory")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of concurrent transcode streams (default: 3)",
    )
    args = parser.parse_args()

    src_dir = Path(args.src).resolve()
    dst_dir = Path(args.dst).resolve()

    if not src_dir.exists():
        print(f"Error: Source directory '{src_dir}' does not exist.")
        return

    valid_exts = IMAGE_EXTS | VIDEO_EXTS
    files_to_process = [
        p for p in src_dir.rglob("*") if p.is_file() and p.suffix.lower() in valid_exts
    ]

    print(f"Found {len(files_to_process)} target files. Calculating total duration...")

    # Pre-calculate total video duration for accurate overall ETA
    total_duration_sec = 0.0
    for p in files_to_process:
        if p.suffix.lower() in VIDEO_EXTS:
            _, _, duration = get_video_info(p)
            total_duration_sec += duration

    # Thread lock to safely update the shared global progress bar
    lock = threading.Lock()
    available_slots = list(range(args.workers))

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}

        # Main progress bar tracks total video seconds remaining
        main_pbar = tqdm(
            total=int(total_duration_sec)
            if total_duration_sec > 0
            else len(files_to_process),
            desc="Total Progress",
            position=0,
            unit="s" if total_duration_sec > 0 else "file",
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}, {rate_fmt}]",
        )

        for src_path in files_to_process:
            while not available_slots:
                # Wait for any worker to finish and free its slot
                done_futures = [f for f in futures if f.done()]
                for f in done_futures:
                    slot = futures.pop(f)
                    available_slots.append(slot)
                    success, msg = f.result()
                    if not success:
                        tqdm.write(f"[ERROR] {msg}")

            slot = available_slots.pop(0)
            rel_path = src_path.relative_to(src_dir)
            dst_path = dst_dir / rel_path

            future = executor.submit(
                process_file, src_path, dst_path, slot, main_pbar, lock
            )
            futures[future] = slot

        # Drain remaining running tasks
        for future in as_completed(futures):
            success, msg = future.result()
            if not success:
                tqdm.write(f"[ERROR] {msg}")

        main_pbar.close()


if __name__ == "__main__":
    main()
