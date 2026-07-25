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

# Pre-defined GPU profiles
PRESETS = {
    "gtx1080": [
        "-c:v", "hevc_nvenc",
        "-preset", "p7",
        "-tune", "hq",
        "-rc", "constqp",
        "-cq", "20",
        "-spatial-aq", "1",
    ],
    "rtx4080s": [
        "-c:v", "hevc_nvenc",
        "-preset", "p7",            # Highest quality / slowest preset
        "-tune", "hq",              # FFmpeg accepted NVENC tune parameter
        "-multipass", "fullres",    # 2-Pass full resolution encoding
        "-rc", "constqp",
        "-cq", "20",
        "-spatial-aq", "1",
        "-bf", "3",                 # HEVC B-frames (supported on RTX 20/30/40 series)
        "-b_ref_mode", "middle",    # B-frames used as references
    ]
}


def parse_float_safe(val, default=0.0):
    """Safely converts string or number to float, handling 'N/A' or empty strings."""
    if val is None:
        return default
    val_str = str(val).strip().rstrip("xX")
    try:
        return float(val_str)
    except ValueError:
        return default


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
        "stream=width,height,duration:format=duration",
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
        fmt = data.get("format", {})

        # Try format duration first, fall back to stream duration
        raw_duration = fmt.get("duration") or stream.get("duration")
        duration = parse_float_safe(raw_duration, default=0.0)

        return stream.get("width"), stream.get("height"), duration
    except Exception:
        return None, None, 0.0


def parse_time_to_seconds(time_str):
    """Converts HH:MM:SS.microseconds into total seconds."""
    if not time_str or "N/A" in time_str:
        return 0.0
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + parse_float_safe(s)
    except ValueError:
        pass
    return 0.0


def process_file(src_path, dst_path, position_slot, main_pbar, lock, enc_args):
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
        dst_path = dst_path.with_suffix(".mkv")
        width, height, duration = get_video_info(src_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            "cuda",
            "-i",
            str(src_path),
            "-map",
            "0",  # Preserves all video, audio, and subtitle streams
        ]

        if width and height and (width > 1920 or height > 1080):
            cmd.extend(
                [
                    "-vf",
                    "scale_cuda=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease",
                ]
            )

        # Inject encoder arguments (GTX 1080 / RTX 4080 Super)
        cmd.extend(enc_args)

        # Audio, Subtitle copy, and Progress Flags
        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-c:s",
                "copy",  # Direct pass-through for subtitle streams (srt/ass)
                "-progress",
                "pipe:1",
                "-nostats",
                str(dst_path),
            ]
        )

        short_filename = (
            src_path.name if len(src_path.name) <= 70 else f"{src_path.name[:67]}..."
        )
        total_units = int(duration) if duration > 0 else 100

        # Worker progress bar
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
                    val = val.strip()

                    if key == "out_time":
                        out_seconds = parse_time_to_seconds(val)
                        delta = out_seconds - last_processed_sec

                        if delta > 0:
                            worker_bar.update(round(delta))

                            with lock:
                                main_pbar.update(round(delta))

                            last_processed_sec = out_seconds

                    elif key == "speed":
                        if "N/A" in val:
                            current_speed = "N/A"
                        else:
                            spd = parse_float_safe(val)
                            current_speed = f"{round(spd, 1)}x"
                    elif key == "fps":
                        current_fps = str(int(parse_float_safe(val)))
                    elif key == "progress" and val == "continue":
                        worker_bar.set_postfix_str(
                            f"Speed: {current_speed} | FPS: {current_fps}"
                        )

            process.wait()

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
        description="Recursive Media Converter supporting Pascal (GTX 1080) & Ada Lovelace (RTX 4080 Super) NVENC."
    )
    parser.add_argument("src", type=str, help="Source directory")
    parser.add_argument("dst", type=str, help="Destination directory")
    parser.add_argument(
        "--preset",
        type=str,
        choices=["gtx1080", "rtx4080s"],
        default="gtx1080",
        help="Target GPU encoding profile (default: gtx1080)",
    )
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

    encoder_args = PRESETS[args.preset]

    valid_exts = IMAGE_EXTS | VIDEO_EXTS
    files_to_process = [
        p for p in src_dir.rglob("*") if p.is_file() and p.suffix.lower() in valid_exts
    ]

    print(
        f"Found {len(files_to_process)} target files. Using profile [{args.preset.upper()}]. Calculating total duration..."
    )

    total_duration_sec = 0.0
    for p in files_to_process:
        if p.suffix.lower() in VIDEO_EXTS:
            _, _, duration = get_video_info(p)
            total_duration_sec += duration

    lock = threading.Lock()
    available_slots = list(range(args.workers))

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}

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
                process_file, src_path, dst_path, slot, main_pbar, lock, encoder_args
            )
            futures[future] = slot

        for future in as_completed(futures):
            success, msg = future.result()
            if not success:
                tqdm.write(f"[ERROR] {msg}")

        main_pbar.close()


if __name__ == "__main__":
    main()
