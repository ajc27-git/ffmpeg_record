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
def setup_camera(device, name, fps, exposure):
    """Configure camera for the requested fps and exposure"""
    print(f"\n⚙️  Configuring {device} ({name}) for {fps} fps and {exposure} exposure...")
    
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

    commands.append(["v4l2-ctl", "-d", device, f"--set-ctrl=exposure_time_absolute={exposure}"])
    
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            print(f"  {cmd[-1]}: {result.stdout.strip()}")
        except Exception as e:
            print(f"  Error: {e}")

# ===== USB BANDWIDTH QUIRK =====
def apply_usb_bandwidth_quirk():
    print("🔧 Applying USB bandwidth quirk (quirks=128)")
    print("   (You may be asked for sudo password)")
    try:
        # Remove the module safely (ignore errors if not loaded)
        subprocess.run(["sudo", "rmmod", "uvcvideo"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        
        # Load module with patch
        subprocess.run(["sudo", "modprobe", "uvcvideo", "quirks=128"], check=True)
        time.sleep(2) # Give the system time to recognize the cameras again
        print("✅ USB patch applied correctly.")
    except Exception as e:
        print(f"⚠️ Warning: Could not apply USB patch: {e}")

def restore_usb_bandwidth_quirk():
    print("\n🔧 Restoring original USB configuration...")
    try:
        subprocess.run(["sudo", "rmmod", "uvcvideo"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "modprobe", "uvcvideo"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ Original USB configuration restored.")
    except Exception:
        pass

# ===== Dynamic Camera Setup =====
apply_usb_bandwidth_quirk()
cameras = get_dynamic_cameras()

if not cameras:
    print("❌ No compatible cameras found.")
    sys.exit(1)

print(f"✅ Cameras detected: {len(cameras)}")
for i, c in enumerate(cameras):
    print(f"   {i}) {c['device']} -> {c['output']}")

selected_input = input("\nSelect cameras by comma-separated digits (e.g., 0,2) or press Enter for all: ").strip()
if selected_input:
    try:
        indices = [int(x.strip()) for x in selected_input.split(',')]
        cameras = [cameras[i] for i in indices if 0 <= i < len(cameras)]
        print(f"✅ Using selected cameras: {len(cameras)}")
        for c in cameras:
            print(f"   - {c['device']} -> {c['output']}")
        if not cameras:
            print("❌ No valid cameras selected.")
            sys.exit(1)
    except ValueError:
        print("❌ Invalid input. Using all cameras.")

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
def start_recording(use_format="mjpeg", fps=90, resolution="1920x1080"):
    """Start recording with a specific format"""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("recordings", exist_ok=True)
    
    # ===== OPTION 1: NATIVE MJPEG (RECOMMENDED) =====
    input_params_mjpeg = [
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", resolution,
        # "-video_size", "1280x720",
        "-framerate", str(fps),
        "-use_wallclock_as_timestamps", "1",  # Use system clock
        "-avoid_negative_ts", "make_zero",
        "-fflags", "+genpts",  # Generate consistent PTS
        "-i"
    ]

    output_params_mjpeg = [
        "-c:v", "copy",  # Just copy the stream (Fastest)
        # "-c:v", "mjpeg",  # Re-encode to allow frame dropping
        # "-q:v", "3",      # Maintain high quality
        # "-r", str(fps),   # Force target framerate (drops frames if necessary)
        "-an",
        "-y",
        "-f", "avi"  # Force AVI format
    ]

    # ===== OPTION 2: RAW YUYV (if MJPEG causes issues) =====
    input_params_yuyv = [
        "-f", "v4l2",
        "-input_format", "yuyv422",  # RAW Format
        "-video_size", resolution,
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
        "-video_size", resolution,
        "-framerate", str(fps),
        "-re",  # Read input at native framerate
        "-use_wallclock_as_timestamps", "1",
        "-i"
    ]

    # ===== OPTION 4: NATIVE H264 =====
    input_params_h264 = [
        "-f", "v4l2",
        "-input_format", "h264",
        "-video_size", resolution,
        "-framerate", str(fps),
        "-i"
    ]

    output_params_h264 = [
        "-c:v", "copy",
        "-an",
        "-y",
        "-f", "mp4"
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
        elif use_format == "h264":
            cmd.extend(input_params_h264)
            output_suffix = f"_{timestamp}.mp4"
        else:
            cmd.extend(input_params_interval)
            output_suffix = f"_{timestamp}_interval.avi"
        
        cmd.append(cam["device"])
        
        if use_format == "mjpeg":
            cmd.extend(output_params_mjpeg)
        elif use_format == "yuyv":
            cmd.extend(output_params_yuyv)
        elif use_format == "h264":
            cmd.extend(output_params_h264)
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
            process.stdin.write('q')
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
                    
    # Restore USB config on exit
    restore_usb_bandwidth_quirk()
    
    sys.exit(0)

# ===== MAIN =====
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    
    print("=" * 60)
    print("🎥 RECORDING SYSTEM")
    print("=" * 60)
    
    print("\n⚙️  Select Configuration Preset:")
    print("  1) 60fps, H264, 3840x2160, 150exp, 3s")
    print("  2) 60fps, H264, 2560x1440, 150exp, 3s")
    print("  3) 60fps, H264, 1920x1080, 150exp, 3s")
    print("  4) 90fps, MJPEG, 1920x1080, 150exp, 3s")
    print("  5) Custom")
    preset_choice = input("\nPreset [1-4] (default 1): ").strip()

    if preset_choice == "1":
        fps = 60
        use_format = "h264"
        resolution = "3840x2160"
        exposure = 150
        delay_seconds = 3
    elif preset_choice == "2":
        fps = 60
        use_format = "h264"
        resolution = "2560x1440"
        exposure = 150
        delay_seconds = 3
    elif preset_choice == "3":
        fps = 60
        use_format = "h264"
        resolution = "1920x1080"
        exposure = 150
        delay_seconds = 3
    elif preset_choice == "4":
        fps = 90
        use_format = "mjpeg"
        resolution = "1920x1080"
        exposure = 150
        delay_seconds = 3
    else:
        # Select framerate
        print("\n⏱️  Select recording framerate:")
        fps_input = input("Framerate (default 90): ").strip()
        fps = int(fps_input) if fps_input.isdigit() else 90
        
        # Select format
        print("\n📋 Select recording format:")
        print("  1) Native MJPEG (recommended, lower CPU)")
        print("  2) YUYV raw + H.264 (better compatibility)")
        print("  3) Force 0.011s interval")
        print("  4) Native H.264")
        
        choice = input("\nOption [1-4]: ").strip()
        
        if choice == "1":
            use_format = "mjpeg"
        elif choice == "2":
            use_format = "yuyv"
        elif choice == "4":
            use_format = "h264"
        else:
            use_format = "interval"
            
        # Select resolution
        print("\n📐 Select video resolution:")
        print("  1) 1920x1080 (default)")
        print("  2) 1280x960")
        print("  3) 1280x720")
        print("  4) 1920x1200")
        print("  5) 3840x2160")
        res_choice = input("Resolution [1-5]: ").strip()
        
        if res_choice == "2":
            resolution = "1280x960"
        elif res_choice == "3":
            resolution = "1280x720"
        elif res_choice == "4":
            resolution = "1920x1200"
        elif res_choice == "5":
            resolution = "3840x2160"
        else:
            resolution = "1920x1080"
            
        print("\n💡 Set manual exposure (exposure_time_absolute)?")
        exposure_input = input("Exposure (default 150): ").strip()
        exposure = int(exposure_input) if exposure_input.isdigit() else 150
        
        print("\n⏳ Add a delay before starting (in seconds)?")
        delay_input = input("Delay (default 0): ").strip()
        delay_seconds = int(delay_input) if delay_input.isdigit() else 0

    # Configure each camera
    for cam in cameras:
        setup_camera(cam["device"], cam["name"], fps, exposure)
    
    # Create commands
    ffmpeg_cmds = start_recording(use_format, fps, resolution)
    
    if delay_seconds > 0:
        print(f"\n⏳ Delaying start for {delay_seconds} seconds...")
        for i in range(delay_seconds, 0, -1):
            sys.stdout.write(f"\r   {i} seconds remaining...  ")
            sys.stdout.flush()
            time.sleep(1)
        print("\n")
    
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
