import os
import cv2
import time
import json
import threading
import queue
import glob
from tkinter import Tk, filedialog
from vimba import Vimba, Frame, AllocationMode

# ---------------- USER SETTINGS ----------------
NUM_TRIALS = 100

# Spontaneous-period end acquisition (triggered by one extra TTL pulse after all trials)
# Spike2 fires a 20-second camera TTL at t=580-600 s of the spontaneous window.
ACQUIRE_SPONTANEOUS_END = True   # set False to skip this extra acquisition
SPONT_END_FOLDER_NAME  = "spontaneous_end"   # sub-folder name inside the session dir

POLL_INTERVAL = 0.001
EDGE_DEBOUNCE_S = 0.005

EXPOSURE_US = 1000
GAIN_DB = 24

TRIGGER_LINE = "Line0"

SHOW_PREVIEW = True
PREVIEW_WIN = "Live Camera"

IMAGE_EXT = ".bmp"     # ".bmp" recommended
PNG_COMPRESSION = 1    # only used if IMAGE_EXT == ".png"

BUFFER_COUNT = 256
MAX_QUEUE = 20000

USE_SESSION_SUBFOLDER = True
# ------------------------------------------------

latest_frame = None
latest_lock = threading.Lock()

recording_event = threading.Event()
frames_lock = threading.Lock()

trial_frame_idx = 0
trial_t0_ns = 0

written_ts_by_idx = {}   # idx -> ts_rel_ns (only for confirmed-written frames)
queue_drops = 0
write_failures = 0

write_q: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_QUEUE)
writer_stop = threading.Event()
writer_thread = None

current_trial_dir = ""

trial0_ttl_rising_ns  = None   # perf_counter_ns when poll loop first saw rising edge, trial 0
trial0_ttl_falling_ns = None   # perf_counter_ns when poll loop first saw falling edge, trial 0


def safe_set(feature, value, label=""):
    try:
        feature.set(value)
        return True
    except Exception as e:
        if label:
            print(f"Warning: could not set {label} to {value}: {e}")
        return False


def safe_run(cmd_feature, label=""):
    try:
        cmd_feature.run()
        return True
    except Exception as e:
        if label:
            print(f"Warning: could not run {label}: {e}")
        return False


def safe_get(feature, default=None):
    try:
        return feature.get()
    except Exception:
        return default


def get_line_status(cam) -> bool:
    cam.LineSelector.set(TRIGGER_LINE)
    return bool(cam.LineStatus.get())


def read_camera_fps(cam):
    try:
        v = float(cam.AcquisitionFrameRate.get())
        if v > 0:
            return v
    except Exception:
        pass
    return None


def compute_measured_fps(ts_rel_ns):
    if ts_rel_ns is None or len(ts_rel_ns) < 2:
        return None
    dt = (ts_rel_ns[-1] - ts_rel_ns[0]) / 1e9
    if dt <= 0:
        return None
    return (len(ts_rel_ns) - 1) / dt


def pump_preview(show: bool):
    if not SHOW_PREVIEW or not show:
        return
    with latest_lock:
        lf = latest_frame
    if lf is not None:
        cv2.imshow(PREVIEW_WIN, lf)
        cv2.waitKey(1)


def reset_full_fov(cam):
    safe_run(cam.AcquisitionStop, "AcquisitionStop")

    safe_set(cam.OffsetX, 0, "OffsetX")
    safe_set(cam.OffsetY, 0, "OffsetY")

    try:
        wmax = int(cam.WidthMax.get())
        hmax = int(cam.HeightMax.get())
        cam.Width.set(wmax)
        cam.Height.set(hmax)
    except Exception as e:
        print(f"Warning: could not reset full FOV: {e}")

    safe_set(cam.OffsetX, 0, "OffsetX")
    safe_set(cam.OffsetY, 0, "OffsetY")

    try:
        print(f"Reset to full FOV: {int(cam.Width.get())}x{int(cam.Height.get())}")
    except Exception:
        pass


def load_default_userset(cam):
    safe_run(cam.AcquisitionStop, "AcquisitionStop")
    time.sleep(0.05)

    if not hasattr(cam, "UserSetSelector") or not hasattr(cam, "UserSetLoad"):
        return

    for candidate in ("Default", "UserSet0", "UserSet1"):
        if safe_set(cam.UserSetSelector, candidate, "UserSetSelector"):
            if safe_run(cam.UserSetLoad, "UserSetLoad"):
                time.sleep(0.15)
                return


def configure_manual_exposure_gain(cam, tag=""):
    safe_set(cam.ExposureAuto, "Off", f"{tag}ExposureAuto")
    safe_set(cam.GainAuto, "Off", f"{tag}GainAuto")

    if hasattr(cam, "ExposureMode"):
        safe_set(cam.ExposureMode, "Timed", f"{tag}ExposureMode")

    try:
        cam.ExposureTime.set(EXPOSURE_US)
    except Exception as e:
        print(f"Warning: could not set {tag}ExposureTime={EXPOSURE_US}: {e}")

    try:
        cam.Gain.set(GAIN_DB)
    except Exception as e:
        print(f"Warning: could not set {tag}Gain={GAIN_DB}: {e}")

    applied_exp = safe_get(cam.ExposureTime, None)
    applied_gain = safe_get(cam.Gain, None)
    print(f"{tag}Applied ExposureTime={applied_exp} us, Gain={applied_gain} dB")


def disarm_trigger_for_roi(cam):
    if hasattr(cam, "TriggerSelector"):
        safe_set(cam.TriggerSelector, "AcquisitionStart", "TriggerSelector")
    safe_set(cam.TriggerMode, "Off", "TriggerMode")


def arm_hardware_trigger(cam):
    safe_set(cam.TriggerSelector, "AcquisitionStart", "TriggerSelector")
    safe_set(cam.TriggerActivation, "RisingEdge", "TriggerActivation")
    safe_set(cam.TriggerMode, "On", "TriggerMode")


def cleanup_camera(cam):
    safe_run(cam.AcquisitionStop, "AcquisitionStop")
    if hasattr(cam, "TriggerSelector"):
        safe_set(cam.TriggerSelector, "AcquisitionStart", "TriggerSelector")
    safe_set(cam.TriggerMode, "Off", "TriggerMode")
    load_default_userset(cam)
    time.sleep(0.1)


def acquire_single_frame_for_roi(cam):
    configure_manual_exposure_gain(cam, tag="ROI ")
    disarm_trigger_for_roi(cam)
    cam.AcquisitionMode.set("SingleFrame")
    frame = cam.get_frame(timeout_ms=3000)
    return frame.as_numpy_ndarray()


def select_and_confirm_roi(img):
    roi = cv2.selectROI("Select ROI", img, fromCenter=False, showCrosshair=True)
    cv2.destroyWindow("Select ROI")

    x, y, w, h = roi
    if w == 0 or h == 0:
        raise RuntimeError("ROI selection cancelled")

    preview = img.copy()
    cv2.rectangle(preview, (x, y), (x + w, y + h), (0, 255, 0), 2)
    cv2.imshow("Confirm ROI (Y/N)", preview)

    while True:
        key = cv2.waitKey(0) & 0xFF
        if key in (ord("y"), ord("Y")):
            cv2.destroyWindow("Confirm ROI (Y/N)")
            return x, y, w, h
        if key in (ord("n"), ord("N")):
            cv2.destroyWindow("Confirm ROI (Y/N)")
            raise RuntimeError("ROI rejected by user")


def apply_roi(cam, x, y, w, h):
    safe_run(cam.AcquisitionStop, "AcquisitionStop")

    w_inc = cam.Width.get_increment()
    h_inc = cam.Height.get_increment()
    offx_inc = cam.OffsetX.get_increment()
    offy_inc = cam.OffsetY.get_increment()

    w = max(w_inc, (w // w_inc) * w_inc)
    h = max(h_inc, (h // h_inc) * h_inc)
    x = (x // offx_inc) * offx_inc
    y = (y // offy_inc) * offy_inc

    cam.OffsetX.set(0)
    cam.OffsetY.set(0)
    cam.Width.set(w)
    cam.Height.set(h)

    max_x = cam.WidthMax.get() - w
    max_y = cam.HeightMax.get() - h
    x = min(max(x, 0), max_x)
    y = min(max(y, 0), max_y)

    cam.OffsetX.set(x)
    cam.OffsetY.set(y)

    print(f"Applied ROI: x={x}, y={y}, w={w}, h={h}")


def writer_thread_fn():
    """
    Queue item: ("image", idx, ts_rel, final_path, img)
    Writes to tmp with SAME extension (e.g., frame_000001.tmp.bmp), then atomically renames.
    Only ACK timestamp after final file exists and size>0.
    """
    global write_failures, written_ts_by_idx

    while not writer_stop.is_set():
        try:
            item = write_q.get(timeout=0.1)
        except queue.Empty:
            continue

        if item is None:
            write_q.task_done()
            break

        kind, idx, ts_rel, final_path, img = item
        ok = False

        try:
            if kind == "image":
                root, ext = os.path.splitext(final_path)
                tmp_path = root + ".tmp" + ext  # <-- FIX: keep real extension

                # Remove stale tmp if present
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

                # Write to tmp
                if ext.lower() == ".png":
                    ok = cv2.imwrite(tmp_path, img, [cv2.IMWRITE_PNG_COMPRESSION, int(PNG_COMPRESSION)])
                else:
                    ok = cv2.imwrite(tmp_path, img)

                if ok:
                    # Replace final atomically
                    try:
                        if os.path.exists(final_path):
                            os.remove(final_path)
                    except Exception:
                        pass

                    os.replace(tmp_path, final_path)

                    # Verify exists and non-empty
                    ok = os.path.exists(final_path) and (os.path.getsize(final_path) > 0)

        except Exception:
            ok = False
            # Clean tmp if possible
            try:
                root, ext = os.path.splitext(final_path)
                tmp_path = root + ".tmp" + ext
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

        with frames_lock:
            if ok:
                written_ts_by_idx[idx] = int(ts_rel)
            else:
                write_failures += 1

        write_q.task_done()


def frame_handler(cam, frame: Frame):
    global latest_frame, trial_frame_idx, queue_drops

    img = frame.as_numpy_ndarray()

    with latest_lock:
        latest_frame = img

    if recording_event.is_set():
        ts_rel = time.perf_counter_ns() - trial_t0_ns
        img_copy = img.copy()

        with frames_lock:
            idx = trial_frame_idx
            trial_frame_idx += 1

        fname = f"frame_{idx:06d}{IMAGE_EXT}"
        path = os.path.join(current_trial_dir, fname)

        try:
            write_q.put_nowait(("image", idx, ts_rel, path, img_copy))
        except queue.Full:
            with frames_lock:
                queue_drops += 1

    cam.queue_frame(frame)


def save_trial_metadata(cam, trial_idx, trial_dir):
    with frames_lock:
        ts_list = [written_ts_by_idx[k] for k in sorted(written_ts_by_idx.keys())]
        qd = int(queue_drops)
        wf = int(write_failures)

    mfps = compute_measured_fps(ts_list)
    files = glob.glob(os.path.join(trial_dir, f"frame_*{IMAGE_EXT}"))
    files_count = len(files)

    meta = {
        "trial_index": trial_idx,
        "image_ext": IMAGE_EXT,
        "frames_written_by_ack": len(ts_list),
        "frames_found_on_disk": files_count,
        "queue_drops": qd,
        "write_failures": wf,
        "camera_reported_fps": read_camera_fps(cam),
        "measured_fps": mfps,
        "exposure_us": float(cam.ExposureTime.get()),
        "gain": float(cam.Gain.get()),
        "roi": {
            "offset_x": int(cam.OffsetX.get()),
            "offset_y": int(cam.OffsetY.get()),
            "width": int(cam.Width.get()),
            "height": int(cam.Height.get()),
        },
        "frame_timestamps_rel_ns": ts_list,
    }

    if trial_idx == 0 and trial0_ttl_rising_ns is not None:
        t0 = trial0_ttl_rising_ns
        rising_rel  = 0  # rising edge is t0 by definition
        falling_abs = trial0_ttl_falling_ns
        falling_rel = int(falling_abs - t0) if falling_abs is not None else None
        meta["ttl_timing_trial0"] = {
            "note": "timestamps from perf_counter_ns; _rel_ is ns after rising edge (=trial_t0_ns)",
            "poll_interval_s": POLL_INTERVAL,
            "edge_debounce_s": EDGE_DEBOUNCE_S,
            "ttl_rising_detected_abs_ns":  int(t0),
            "ttl_rising_detected_rel_ns":  rising_rel,
            "ttl_falling_detected_abs_ns": int(falling_abs) if falling_abs is not None else None,
            "ttl_falling_detected_rel_ns": falling_rel,
            "ttl_duration_detected_ns":    falling_rel,
            "first_frame_rel_ns": ts_list[0]  if ts_list else None,
            "last_frame_rel_ns":  ts_list[-1] if ts_list else None,
            "first_frame_latency_ns": ts_list[0] if ts_list else None,
            "last_frame_to_ttl_fall_ns": (falling_rel - ts_list[-1]) if (ts_list and falling_rel is not None) else None,
        }

    with open(os.path.join(trial_dir, "trial_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    return len(ts_list), files_count, qd, wf


def pick_master_folder():
    root = Tk()
    root.withdraw()
    base = filedialog.askdirectory(title="Select master folder for all trial folders")
    root.destroy()
    return base


def maybe_make_session_folder(base_save_dir):
    if not USE_SESSION_SUBFOLDER:
        return base_save_dir
    stamp = time.strftime("session_%Y%m%d_%H%M%S")
    session_dir = os.path.join(base_save_dir, stamp)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def run_live_feed(cam):
    """
    Stream a live preview until the user presses Q in the preview window.
    Nothing is saved. Camera runs in free-run (trigger Off) so frames arrive
    without any TTL. Use Spike2 key 'A' to fire test air puffs and watch
    the eye in real time. Press Q when ready to proceed.
    """
    print("\n" + "="*60)
    print("LIVE FEED  -  camera streaming, nothing saved.")
    print("Use Spike2 key 'A' to fire test air puffs.")
    print("Press  Q  in the preview window to proceed to acquisition.")
    print("="*60 + "\n")

    # Free-run: disable hardware trigger so frames arrive without TTL
    disarm_trigger_for_roi(cam)
    configure_manual_exposure_gain(cam, tag="LiveFeed ")
    cam.AcquisitionMode.set("Continuous")

    def _live_handler(cam, frame):
        global latest_frame
        img = frame.as_numpy_ndarray()
        with latest_lock:
            latest_frame = img
        cam.queue_frame(frame)

    cam.start_streaming(
        handler=_live_handler,
        buffer_count=BUFFER_COUNT,
        allocation_mode=AllocationMode.AnnounceFrame,
    )

    try:
        while True:
            with latest_lock:
                lf = latest_frame
            if lf is not None:
                cv2.imshow("Live Feed  (press Q to proceed)", lf)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q")):
                break
    finally:
        cam.stop_streaming()
        cv2.destroyWindow("Live Feed  (press Q to proceed)")
        print("Live feed ended. Proceeding to acquisition setup.\n")


def main():
    global trial_frame_idx, trial_t0_ns, current_trial_dir
    global written_ts_by_idx, queue_drops, write_failures
    global writer_thread

    base_save_dir = pick_master_folder()
    if not base_save_dir:
        raise RuntimeError("No save directory selected")

    base_save_dir = maybe_make_session_folder(base_save_dir)
    print(f"Saving session to: {base_save_dir}")

    writer_stop.clear()
    writer_thread = threading.Thread(target=writer_thread_fn, daemon=True)
    writer_thread.start()

    with Vimba.get_instance() as vimba:
        cams = vimba.get_all_cameras()
        if not cams:
            raise RuntimeError("No camera found")

        with cams[0] as cam:
            print(f"Using camera: {cam.get_name()}")

            load_default_userset(cam)
            reset_full_fov(cam)

            print("Acquiring single frame for ROI selection...")
            roi_img = acquire_single_frame_for_roi(cam)

            x, y, w, h = select_and_confirm_roi(roi_img)
            apply_roi(cam, x, y, w, h)

            # Live feed: free-run preview for test air puffs.
            # run_live_feed streams frames without saving until user presses Q.
            # After Q the camera is re-configured for triggered acquisition.
            run_live_feed(cam)

            # Re-configure for triggered acquisition
            configure_manual_exposure_gain(cam, tag="Trial ")

            cam.AcquisitionMode.set("Continuous")
            arm_hardware_trigger(cam)

            # session metadata
            session_meta = {
                "camera_name": cam.get_name(),
                "num_trials": NUM_TRIALS,
                "acquire_spontaneous_end": ACQUIRE_SPONTANEOUS_END,
                "spont_end_folder": SPONT_END_FOLDER_NAME if ACQUIRE_SPONTANEOUS_END else None,
                "exposure_us_requested": EXPOSURE_US,
                "gain_db_requested": GAIN_DB,
                "exposure_us_applied": float(cam.ExposureTime.get()),
                "gain_db_applied": float(cam.Gain.get()),
                "roi": {
                    "offset_x": int(cam.OffsetX.get()),
                    "offset_y": int(cam.OffsetY.get()),
                    "width": int(cam.Width.get()),
                    "height": int(cam.Height.get()),
                },
                "camera_reported_fps": read_camera_fps(cam),
                "trigger_line": TRIGGER_LINE,
                "edge_debounce_s": EDGE_DEBOUNCE_S,
                "buffer_count": BUFFER_COUNT,
                "queue_max": MAX_QUEUE,
                "image_ext": IMAGE_EXT,
                "png_compression": PNG_COMPRESSION if IMAGE_EXT.lower() == ".png" else None,
                "show_preview": SHOW_PREVIEW,
                "use_session_subfolder": USE_SESSION_SUBFOLDER,
            }
            with open(os.path.join(base_save_dir, "session_metadata.json"), "w") as f:
                json.dump(session_meta, f, indent=2)

            print("Waiting for TTL pulse...")

            cam.start_streaming(
                handler=frame_handler,
                buffer_count=BUFFER_COUNT,
                allocation_mode=AllocationMode.AnnounceFrame
            )

            prev = get_line_status(cam)
            last_edge_time = time.perf_counter()

            trials_done = 0

            try:
                # ── Phase 1: spontaneous-end acquisition (580–600 s) ──────────────
                # The spontaneous period runs first in Spike2 (600 s total).
                # At t=580 s Spike2 raises the camera TTL for 20 s, then lowers it.
                # We catch that single pulse here, before the trial loop begins.
                if ACQUIRE_SPONTANEOUS_END:
                    print("Waiting for spontaneous-end TTL (fires at t=580 s of spontaneous period) ...")

                    spont_end_dir = os.path.join(base_save_dir, SPONT_END_FOLDER_NAME)
                    os.makedirs(spont_end_dir, exist_ok=True)

                    spont_done = False

                    while not spont_done:
                        cur = get_line_status(cam)
                        now = time.perf_counter()

                        pump_preview(show=cur)

                        edge = (cur != prev) and ((now - last_edge_time) >= EDGE_DEBOUNCE_S)
                        if edge:
                            if (not prev) and cur and (not recording_event.is_set()):
                                # Rising edge → start spontaneous-end recording
                                current_trial_dir = spont_end_dir

                                with frames_lock:
                                    trial_frame_idx = 0
                                    trial_t0_ns = time.perf_counter_ns()
                                    written_ts_by_idx = {}
                                    queue_drops = 0
                                    write_failures = 0

                                recording_event.set()
                                print("Spontaneous-end recording START (580–600 s)")

                            elif prev and (not cur) and recording_event.is_set():
                                # Falling edge → stop
                                recording_event.clear()

                                write_q.join()

                                ack_n, disk_n, qd, wf = save_trial_metadata(
                                    cam, -1, spont_end_dir   # -1 = sentinel index for spontaneous
                                )
                                print(
                                    f"Spontaneous-end recording END — "
                                    f"files={disk_n} (ack={ack_n}, queue_drops={qd}, write_failures={wf})"
                                )
                                spont_done = True

                            last_edge_time = now

                        prev = cur
                        time.sleep(POLL_INTERVAL)

                    # Re-read line state cleanly before entering trial loop
                    prev = get_line_status(cam)
                    last_edge_time = time.perf_counter()
                    print("Spontaneous period complete. Waiting for first trial TTL pulse...")

                # ── Phase 2: 100 triggered trials ──────────────────────────────────
                while trials_done < NUM_TRIALS:
                    cur = get_line_status(cam)
                    now = time.perf_counter()

                    pump_preview(show=cur)

                    edge = (cur != prev) and ((now - last_edge_time) >= EDGE_DEBOUNCE_S)
                    if edge:
                        if (not prev) and cur and (not recording_event.is_set()):
                            current_trial_dir = os.path.join(base_save_dir, f"trial_{trials_done:03d}")
                            os.makedirs(current_trial_dir, exist_ok=True)

                            with frames_lock:
                                trial_frame_idx = 0
                                trial_t0_ns = time.perf_counter_ns()
                                written_ts_by_idx = {}
                                queue_drops = 0
                                write_failures = 0

                            if trials_done == 0:
                                global trial0_ttl_rising_ns
                                trial0_ttl_rising_ns = trial_t0_ns  # same instant: poll detected rising edge

                            recording_event.set()
                            print(f"Trial {trials_done + 1} START")

                        elif prev and (not cur) and recording_event.is_set():
                            recording_event.clear()

                            if trials_done == 0:
                                global trial0_ttl_falling_ns
                                trial0_ttl_falling_ns = time.perf_counter_ns()

                            write_q.join()

                            ack_n, disk_n, qd, wf = save_trial_metadata(cam, trials_done, current_trial_dir)
                            print(f"Trial {trials_done + 1} END — files={disk_n} (ack={ack_n}, queue_drops={qd}, write_failures={wf})")

                            trials_done += 1
                            if trials_done < NUM_TRIALS:
                                print("Waiting for TTL pulse...")

                        last_edge_time = now

                    prev = cur
                    time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("Interrupted by user")

            finally:
                recording_event.clear()

                try:
                    cam.stop_streaming()
                except Exception:
                    pass

                try:
                    write_q.join()
                except Exception:
                    pass

                cleanup_camera(cam)

    writer_stop.set()
    try:
        write_q.put_nowait(None)
    except Exception:
        pass
    try:
        if writer_thread is not None:
            writer_thread.join(timeout=2.0)
    except Exception:
        pass

    try:
        cv2.waitKey(1)
    except Exception:
        pass
    cv2.destroyAllWindows()
    print("Done.")


if __name__ == "__main__":
    main()