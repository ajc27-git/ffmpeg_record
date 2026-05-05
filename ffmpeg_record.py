import subprocess
import time
import signal
import sys
from datetime import datetime
import os


def get_dynamic_cameras():
    """
    Parses v4l2-ctl --list-devices to find cameras, excluding specific models.
    Returns a list of dictionaries with device path and sequential output names.
    """
    excluded_model = "USB2.0 HD UVC WebCam"
    cameras = []
    
    try:
        # Run the command and get output
        result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
        output = result.stdout
        
        # Split by empty lines to separate device blocks
        blocks = output.strip().split('\n\n')
        
        camera_idx = 0
        for block in blocks:
            lines = block.strip().split('\n')
            if not lines:
                continue
            
            # The first line contains the name of the camera
            device_name = lines[0]
            
            # Skip if the excluded model name is in the device header
            if excluded_model in device_name:
                continue
            
            # Find the first line that starts with /dev/video
            for line in lines[1:]:
                clean_line = line.strip()
                if clean_line.startswith("/dev/video"):
                    cameras.append({
                        "device": clean_line,
                        "name": device_name,
                        "output": f"cam{camera_idx}.avi"
                    })
                    camera_idx += 1
                    break # Only take the first /dev/videoX for this physical device
                    
    except Exception as e:
        print(f"❌ Error detecting cameras: {e}")
        
    return cameras

# ===== CAMERA SETUP =====
def setup_camera(device, name, fps):
    """Configure camera for the requested fps"""
    print(f"\n⚙️  Configuring {device} ({name}) for {fps} fps...")
    
    # FIRST, reset controls
    commands = [
        # Reset parameters
        ["v4l2-ctl", "-d", device, f"--set-parm={fps}"],
        
        # Exposure configuration
        ["v4l2-ctl", "-d", device, "--set-ctrl=auto_exposure=1"],  # Manual
        ["v4l2-ctl", "-d", device, "--set-ctrl=exposure_dynamic_framerate=0"],
        
        # Disable enhancements that might duplicate frames
        ["v4l2-ctl", "-d", device, "--set-ctrl=power_line_frequency=0"],
        ["v4l2-ctl", "-d", device, "--set-ctrl=backlight_compensation=0"],
        
        # Verify
        ["v4l2-ctl", "-d", device, "--get-parm"],
        ["v4l2-ctl", "-d", device, "--get-ctrl=exposure_time_absolute"],
    ]

    # Set exposure level according to device name
    if "EMEET" in name:
        commands.append(["v4l2-ctl", "-d", device, "--set-ctrl=exposure_time_absolute=10"])
    elif "Global Shutter" in name:
        commands.append(["v4l2-ctl", "-d", device, "--set-ctrl=exposure_time_absolute=80"])
    else:
        commands.append(["v4l2-ctl", "-d", device, "--set-ctrl=exposure_time_absolute=120"])
    
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            print(f"  {cmd[-1]}: {result.stdout.strip()}")
        except Exception as e:
            print(f"  Error: {e}")

# ===== Dynamic Camera Setup =====
cameras = get_dynamic_cameras()

if not cameras:
    print("❌ No compatible cameras found.")
    sys.exit(1)

print(f"✅ Cameras detected: {len(cameras)}")
for c in cameras:
    print(f"   - {c['device']} -> {c['output']}")

# ===== Monitor FPS =====
def monitor_fps(process, camera_id):
    """Monitor real-time framerate during recording"""
    import threading
    
    def monitor():
        frame_count = 0
        start_time = time.time()
        
        while process.poll() is None:
            # Read ffmpeg output
            try:
                line = process.stderr.readline()
                if "frame=" in line:
                    frame_count = int(line.split("frame=")[1].split()[0])
                    elapsed = time.time() - start_time
                    if elapsed > 1:
                        current_fps = frame_count / elapsed
                        print(f"Camera {camera_id}: {current_fps:.1f} fps")
            except:
                pass
    
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

# ===== Main Recording Function =====
def start_recording(use_format="mjpeg", fps=90):
    """Start recording with a specific format"""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("recordings", exist_ok=True)
    
    # ===== OPTION 1: NATIVE MJPEG (RECOMMENDED) =====
    input_params_mjpeg = [
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", "1920x1080",
        # "-video_size", "1280x720",
        "-framerate", str(fps),
        "-use_wallclock_as_timestamps", "1",  # Use system clock
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",  # Generate consistent PTS
        "-i"
    ]

    output_params_mjpeg = [
        "-c:v", "copy",  # Copy MJPEG stream directly
        "-r", str(fps),
        "-vsync", "passthrough",  # Pass original timestamps
        "-an",
        "-y",
        "-f", "avi"  # Force AVI format
    ]

    # ===== OPTION 2: RAW YUYV (if MJPEG causes issues) =====
    input_params_yuyv = [
        "-f", "v4l2",
        "-input_format", "yuyv422",  # RAW Format
        "-video_size", "1920x1200",
        "-framerate", str(fps),
        "-use_wallclock_as_timestamps", "1",
        "-i"
    ]

    output_params_yuyv = [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-r", str(fps),
        "-pix_fmt", "yuv422p",
        "-an",
        "-y"
    ]

    # ===== OPTION 3: Force specific interval =====
    input_params_interval = [
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", "1920x1200",
        "-framerate", str(fps),
        "-re",  # Read input at native framerate
        "-use_wallclock_as_timestamps", "1",
        "-i"
    ]
    
    ffmpeg_cmds = []
    
    for cam in cameras:
        cmd = ["ffmpeg", "-loglevel", "info"]  # Info level to see fps
        
        if use_format == "mjpeg":
            cmd.extend(input_params_mjpeg)
            output_suffix = f"_{timestamp}.avi"
        elif use_format == "yuyv":
            cmd.extend(input_params_yuyv)
            output_suffix = f"_{timestamp}_raw.mp4"
        else:
            cmd.extend(input_params_interval)
            output_suffix = f"_{timestamp}_interval.avi"
        
        cmd.append(cam["device"])
        
        if use_format == "mjpeg":
            cmd.extend(output_params_mjpeg)
        elif use_format == "yuyv":
            cmd.extend(output_params_yuyv)
        else:
            cmd.extend(output_params_mjpeg)
        
        # Filename with timestamp
        cam["output_final"] = "recordings/" + cam["output"].replace(".avi", output_suffix)
        cmd.append(cam["output_final"])
        
        ffmpeg_cmds.append(cmd)
        
        print(f"\n📹 Command {cam['device']}:")
        print(" ".join(cmd[:10]), "...", " ".join(cmd[-5:]))
    
    return ffmpeg_cmds

# ===== Signal Handler =====
def signal_handler(sig, frame):
    print("\n\n🛑 Stopping recording...")
    
    for i, process in enumerate(processes):
        if process.poll() is not None:
            continue
        try:
            # Send 'q' to ffmpeg to terminate cleanly
            print(f"   Stopping camera {i}...", end=" ", flush=True)
            process.stdin.write(b'q')
            process.stdin.flush()
            process.wait(timeout=10)
            print("Done")
        except Exception as e:
            print(f"Timeout/Error: {e}. Force killing...")
            process.terminate()
    
    print("✅ Recording complete")
    
    # Verify REAL framerate
    print("\n🔍 Final fps verification:")
    print("=" * 40)
    
    for cam in cameras:
        if 'output_final' in cam:
            print(f"\n{cam['output_final']}:")
            subprocess.run([
                "ffprobe", "-v", "error",
                "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames,r_frame_rate,duration",
                "-of", "default=noprint_wrappers=1",
                cam["output_final"]
            ])

    print("\n⏳ Waiting for file system sync...")
    time.sleep(2)

    # Post-process for Kdenlive (faststart)
    print("\n✨ Post-processing for Kdenlive (faststart)...")
    for cam in cameras:
        if 'output_final' in cam and os.path.exists(cam['output_final']):
            input_file = cam['output_final']
            base, ext = os.path.splitext(input_file)
            temp_file = f"{base}_temp{ext}"
            
            print(f"   Processing {input_file}...", end=" ", flush=True)
            
            cmd = ["ffmpeg", "-y", "-i", input_file, "-c", "copy", "-map", "0", "-movflags", "+faststart", temp_file]
            
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.replace(temp_file, input_file)
                print("✅ Done")
            except Exception as e:
                print(f"❌ Error: {e}")
                if os.path.exists(temp_file):
                    os.remove(temp_file)
    
    sys.exit(0)

# ===== MAIN =====
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    
    print("=" * 60)
    print("🎥 RECORDING SYSTEM")
    print("=" * 60)
    
    # Select framerate
    print("\n⏱️  Select recording framerate:")
    fps_input = input("Framerate (default 90): ").strip()
    fps = int(fps_input) if fps_input.isdigit() else 90
    
    # Configure each camera
    for cam in cameras:
        setup_camera(cam["device"], cam["name"], fps)
    
    # Select format
    print("\n📋 Select recording format:")
    print("  1) Native MJPEG (recommended, lower CPU)")
    print("  2) YUYV raw + H.264 (better compatibility)")
    print("  3) Force 0.011s interval")
    
    choice = input("\nOption [1-3]: ").strip()
    
    if choice == "1":
        use_format = "mjpeg"
    elif choice == "2":
        use_format = "yuyv"
    else:
        use_format = "interval"
    
    # Create commands
    ffmpeg_cmds = start_recording(use_format, fps)
    
    # Start recording
    processes = []
    print(f"\n⏺️  STARTING RECORDING AT {fps} FPS")
    print(f"   Cameras: {len(cameras)}")
    print(f"   Format: {use_format}")
    print("   Press Ctrl+C to stop")
    print("-" * 60)
    
    for i, cmd in enumerate(ffmpeg_cmds):
        print(f"Starting camera {i} ({cameras[i]['device']})...")
        
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        processes.append(process)
        #monitor_fps(process, i)  # Optional: real-time monitoring
    
    # Keep the script alive
    try:
        while True:
            time.sleep(0.1)
            # Show stats periodically
            for i, process in enumerate(processes):
                if process.poll() is not None:
                    print(f"Camera {i} stopped unexpectedly")
                    out, err = process.communicate()
                    if err:
                        print(f"Error: {err[-200:]}")
    except KeyboardInterrupt:
        signal_handler(None, None)
