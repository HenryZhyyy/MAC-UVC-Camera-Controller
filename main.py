#!/usr/bin/env python3
"""
UVCRealtimeController v3

Smoother realtime UVC controller for macOS.

The native IOKit helper stays alive for the whole session, opens the UVC
VideoControl interface once, and accepts READ/SET commands over stdin/stdout.
OpenCV is only used for preview, sliders, realtime rendering, and histogram UI.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path


HELPER_C = r"""
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef kUSBIn
#define kUSBIn 1
#endif
#ifndef kUSBOut
#define kUSBOut 0
#endif
#ifndef kUSBClass
#define kUSBClass 1
#endif
#ifndef kUSBInterface
#define kUSBInterface 1
#endif

#define UVC_VIDEO_CLASS 0x0e
#define UVC_VIDEO_CONTROL_SUBCLASS 0x01
#define UVC_CS_INTERFACE 0x24
#define UVC_VC_INPUT_TERMINAL 0x02
#define UVC_VC_PROCESSING_UNIT 0x05

#define UVC_GET_CUR 0x81
#define UVC_GET_MIN 0x82
#define UVC_GET_MAX 0x83
#define UVC_GET_DEF 0x87
#define UVC_SET_CUR 0x01

#define UNIT_PROCESSING 1
#define UNIT_CAMERA_TERMINAL 2
#define KIND_NUMBER 1
#define KIND_BITMAP 2

typedef struct {
    const char *name;
    int unit_kind;
    UInt8 selector;
    UInt16 size;
    int value_kind;
} ControlDef;

static const ControlDef CONTROLS[] = {
    {"brightness", UNIT_PROCESSING, 0x02, 2, KIND_NUMBER},
    {"contrast", UNIT_PROCESSING, 0x03, 2, KIND_NUMBER},
    {"gain", UNIT_PROCESSING, 0x04, 2, KIND_NUMBER},
    {"saturation", UNIT_PROCESSING, 0x07, 2, KIND_NUMBER},
    {"sharpness", UNIT_PROCESSING, 0x08, 2, KIND_NUMBER},
    {"white_balance", UNIT_PROCESSING, 0x0A, 2, KIND_NUMBER},
    {"white_balance_auto", UNIT_PROCESSING, 0x0B, 1, KIND_BITMAP},
    {"exposure", UNIT_CAMERA_TERMINAL, 0x04, 4, KIND_NUMBER},
    {"exposure_auto", UNIT_CAMERA_TERMINAL, 0x02, 1, KIND_BITMAP},
};

static IOUSBInterfaceInterface190 **g_interface = NULL;
static int g_processing_unit_id = -1;
static int g_camera_terminal_id = -1;
static int g_interface_number = -1;

static UInt8 make_bm_request_type(int direction, int type, int recipient) {
    return (UInt8)(((direction & kUSBRqDirnMask) << kUSBRqDirnShift) |
                   ((type & kUSBRqTypeMask) << kUSBRqTypeShift) |
                   (recipient & kUSBRqRecipientMask));
}

static int parse_int(const char *s) {
    return (int)strtol(s, NULL, 0);
}

static const ControlDef *find_control(const char *name) {
    size_t count = sizeof(CONTROLS) / sizeof(CONTROLS[0]);
    for (size_t i = 0; i < count; i++) {
        if (strcmp(CONTROLS[i].name, name) == 0) {
            return &CONTROLS[i];
        }
    }
    return NULL;
}

static int find_units(IOUSBConfigurationDescriptorPtr config) {
    UInt8 *ptr = (UInt8 *)config;
    UInt16 total = USBToHostWord(config->wTotalLength);
    UInt16 offset = 0;
    int current_vc_interface = -1;

    while (offset + 2 <= total) {
        UInt8 length = ptr[offset];
        UInt8 descriptor_type = ptr[offset + 1];
        if (length == 0 || offset + length > total) {
            break;
        }

        if (descriptor_type == kUSBInterfaceDesc && length >= sizeof(IOUSBInterfaceDescriptor)) {
            IOUSBInterfaceDescriptor *desc = (IOUSBInterfaceDescriptor *)(ptr + offset);
            if (desc->bInterfaceClass == UVC_VIDEO_CLASS &&
                desc->bInterfaceSubClass == UVC_VIDEO_CONTROL_SUBCLASS) {
                current_vc_interface = desc->bInterfaceNumber;
                g_interface_number = current_vc_interface;
            } else {
                current_vc_interface = -1;
            }
        } else if (current_vc_interface >= 0 &&
                   descriptor_type == UVC_CS_INTERFACE &&
                   length >= 4) {
            UInt8 subtype = ptr[offset + 2];
            if (subtype == UVC_VC_INPUT_TERMINAL) {
                g_camera_terminal_id = ptr[offset + 3];
            } else if (subtype == UVC_VC_PROCESSING_UNIT) {
                g_processing_unit_id = ptr[offset + 3];
            }
        }
        offset += length;
    }
    return g_processing_unit_id >= 0 && g_camera_terminal_id >= 0 && g_interface_number >= 0 ? 0 : -1;
}

static IOReturn get_iokit_interface(io_service_t service,
                                    CFUUIDRef plugin_type,
                                    CFUUIDRef interface_id,
                                    void ***out_interface) {
    IOCFPlugInInterface **plugin = NULL;
    SInt32 score = 0;
    IOReturn kr = IOCreatePlugInInterfaceForService(service,
                                                    plugin_type,
                                                    kIOCFPlugInInterfaceID,
                                                    &plugin,
                                                    &score);
    if (kr != kIOReturnSuccess || plugin == NULL) {
        return kr == kIOReturnSuccess ? kIOReturnError : kr;
    }
    HRESULT result = (*plugin)->QueryInterface(plugin,
                                               CFUUIDGetUUIDBytes(interface_id),
                                               (LPVOID *)out_interface);
    (*plugin)->Release(plugin);
    return result == S_OK ? kIOReturnSuccess : kIOReturnError;
}

static int32_t sign_extend_value(int32_t value, UInt16 size) {
    if (size == 1) {
        return value & 0xff;
    }
    if (size == 2) {
        return (int16_t)(value & 0xffff);
    }
    return value;
}

static UInt8 unit_id_for(const ControlDef *control) {
    return control->unit_kind == UNIT_CAMERA_TERMINAL
        ? (UInt8)g_camera_terminal_id
        : (UInt8)g_processing_unit_id;
}

static IOReturn uvc_request(UInt8 request_type,
                            UInt8 request,
                            const ControlDef *control,
                            int32_t *value) {
    IOUSBDevRequest dev_request;
    memset(&dev_request, 0, sizeof(dev_request));
    dev_request.bmRequestType = request_type;
    dev_request.bRequest = request;
    dev_request.wValue = (UInt16)(control->selector << 8);
    dev_request.wIndex = (UInt16)(((UInt16)unit_id_for(control) << 8) | g_interface_number);
    dev_request.wLength = control->size;
    dev_request.pData = value;
    return (*g_interface)->ControlRequest(g_interface, 0, &dev_request);
}

static int print_value(const ControlDef *control, UInt8 request, const char *suffix) {
    int32_t value = 0;
    IOReturn kr = uvc_request(make_bm_request_type(kUSBIn, kUSBClass, kUSBInterface),
                              request,
                              control,
                              &value);
    if (kr != kIOReturnSuccess) {
        printf("%s.%s.error=0x%08x\n", control->name, suffix, kr);
        return -1;
    }
    value = sign_extend_value(value, control->size);
    printf("%s.%s=%d\n", control->name, suffix, value);
    return 0;
}

static int handle_read(const ControlDef *control) {
    int failed = 0;
    if (control->value_kind == KIND_NUMBER) {
        failed |= print_value(control, UVC_GET_MIN, "min");
        failed |= print_value(control, UVC_GET_MAX, "max");
    }
    failed |= print_value(control, UVC_GET_DEF, "default");
    failed |= print_value(control, UVC_GET_CUR, "current");
    return failed ? 1 : 0;
}

static int handle_set(const ControlDef *control, int32_t value) {
    IOReturn kr = uvc_request(make_bm_request_type(kUSBOut, kUSBClass, kUSBInterface),
                              UVC_SET_CUR,
                              control,
                              &value);
    if (kr != kIOReturnSuccess) {
        printf("%s.set.error=0x%08x\n", control->name, kr);
        return 1;
    }
    printf("%s.set=%d\n", control->name, value);
    return print_value(control, UVC_GET_CUR, "current.after") == 0 ? 0 : 1;
}

static int open_uvc(int vid, int pid, int override_processing, int override_camera, int override_interface) {
    CFMutableDictionaryRef matching = IOServiceMatching(kIOUSBDeviceClassName);
    if (matching == NULL) {
        fprintf(stderr, "IOServiceMatching failed\n");
        return 1;
    }

    CFNumberRef vid_number = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &vid);
    CFNumberRef pid_number = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &pid);
    CFDictionarySetValue(matching, CFSTR(kUSBVendorID), vid_number);
    CFDictionarySetValue(matching, CFSTR(kUSBProductID), pid_number);
    CFRelease(vid_number);
    CFRelease(pid_number);

    io_iterator_t iterator = 0;
    IOReturn kr = IOServiceGetMatchingServices(kIOMasterPortDefault, matching, &iterator);
    if (kr != kIOReturnSuccess) {
        fprintf(stderr, "IOServiceGetMatchingServices failed: 0x%08x\n", kr);
        return 1;
    }

    io_service_t device_service = IOIteratorNext(iterator);
    IOObjectRelease(iterator);
    if (device_service == IO_OBJECT_NULL) {
        fprintf(stderr, "USB device not found for VID=0x%04x PID=0x%04x\n", vid, pid);
        return 1;
    }

    IOUSBDeviceInterface **device = NULL;
    kr = get_iokit_interface(device_service,
                             kIOUSBDeviceUserClientTypeID,
                             kIOUSBDeviceInterfaceID,
                             (void ***)&device);
    IOObjectRelease(device_service);
    if (kr != kIOReturnSuccess || device == NULL) {
        fprintf(stderr, "Could not obtain IOUSBDeviceInterface: 0x%08x\n", kr);
        return 1;
    }

    IOUSBConfigurationDescriptorPtr config = NULL;
    kr = (*device)->GetConfigurationDescriptorPtr(device, 0, &config);
    if (kr != kIOReturnSuccess || config == NULL) {
        fprintf(stderr, "GetConfigurationDescriptorPtr failed: 0x%08x\n", kr);
        (*device)->Release(device);
        return 1;
    }

    if (find_units(config) != 0) {
        fprintf(stderr, "Could not parse all UVC units; using fallbacks where needed\n");
    }
    if (override_processing >= 0) {
        g_processing_unit_id = override_processing;
    }
    if (override_camera >= 0) {
        g_camera_terminal_id = override_camera;
    }
    if (override_interface >= 0) {
        g_interface_number = override_interface;
    }
    if (g_processing_unit_id < 0) {
        g_processing_unit_id = 2;
    }
    if (g_camera_terminal_id < 0) {
        g_camera_terminal_id = 1;
    }
    if (g_interface_number < 0) {
        g_interface_number = 0;
    }

    IOUSBFindInterfaceRequest request;
    request.bInterfaceClass = UVC_VIDEO_CLASS;
    request.bInterfaceSubClass = UVC_VIDEO_CONTROL_SUBCLASS;
    request.bInterfaceProtocol = kIOUSBFindInterfaceDontCare;
    request.bAlternateSetting = kIOUSBFindInterfaceDontCare;

    io_iterator_t interface_iterator = 0;
    kr = (*device)->CreateInterfaceIterator(device, &request, &interface_iterator);
    if (kr != kIOReturnSuccess) {
        fprintf(stderr, "CreateInterfaceIterator failed: 0x%08x\n", kr);
        (*device)->Release(device);
        return 1;
    }

    io_service_t interface_service = IOIteratorNext(interface_iterator);
    IOObjectRelease(interface_iterator);
    (*device)->Release(device);
    if (interface_service == IO_OBJECT_NULL) {
        fprintf(stderr, "VideoControl interface not found\n");
        return 1;
    }

    kr = get_iokit_interface(interface_service,
                             kIOUSBInterfaceUserClientTypeID,
                             kIOUSBInterfaceInterfaceID190,
                             (void ***)&g_interface);
    IOObjectRelease(interface_service);
    if (kr != kIOReturnSuccess || g_interface == NULL) {
        fprintf(stderr, "Could not obtain IOUSBInterfaceInterface190: 0x%08x\n", kr);
        return 1;
    }

    printf("READY device=0x%04x:0x%04x processingUnitId=%d cameraTerminalId=%d interfaceNumber=%d\n",
           vid, pid, g_processing_unit_id, g_camera_terminal_id, g_interface_number);
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv) {
    int vid = 0x0c45;
    int pid = 0x636b;
    int override_processing = -1;
    int override_camera = -1;
    int override_interface = -1;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--vid") == 0 && i + 1 < argc) {
            vid = parse_int(argv[++i]);
        } else if (strcmp(argv[i], "--pid") == 0 && i + 1 < argc) {
            pid = parse_int(argv[++i]);
        } else if (strcmp(argv[i], "--unit") == 0 && i + 1 < argc) {
            override_processing = parse_int(argv[++i]);
        } else if (strcmp(argv[i], "--camera-unit") == 0 && i + 1 < argc) {
            override_camera = parse_int(argv[++i]);
        } else if (strcmp(argv[i], "--interface") == 0 && i + 1 < argc) {
            override_interface = parse_int(argv[++i]);
        }
    }

    if (open_uvc(vid, pid, override_processing, override_camera, override_interface) != 0) {
        return 1;
    }

    char line[256];
    while (fgets(line, sizeof(line), stdin) != NULL) {
        char command[32] = {0};
        char control_name[64] = {0};
        int value = 0;
        int rc = 0;

        if (sscanf(line, "%31s %63s %d", command, control_name, &value) < 1) {
            continue;
        }

        if (strcmp(command, "QUIT") == 0) {
            printf("END 0\n");
            fflush(stdout);
            break;
        } else if (strcmp(command, "INFO") == 0) {
            printf("processingUnitId=%d\n", g_processing_unit_id);
            printf("cameraTerminalId=%d\n", g_camera_terminal_id);
            printf("interfaceNumber=%d\n", g_interface_number);
            printf("END 0\n");
            fflush(stdout);
            continue;
        }

        const ControlDef *control = find_control(control_name);
        if (control == NULL) {
            printf("error=unknown_control\nEND 2\n");
            fflush(stdout);
            continue;
        }

        if (strcmp(command, "READ") == 0) {
            rc = handle_read(control);
        } else if (strcmp(command, "SET") == 0) {
            rc = handle_set(control, value);
        } else {
            printf("error=unknown_command\n");
            rc = 2;
        }
        printf("END %d\n", rc);
        fflush(stdout);
    }

    if (g_interface != NULL) {
        (*g_interface)->Release(g_interface);
    }
    return 0;
}
"""


@dataclass(frozen=True)
class Control:
    name: str
    label: str
    fallback_min: int
    fallback_max: int
    fallback_default: int


CONTROLS = [
    Control("brightness", "Brightness", -64, 64, 0),
    Control("contrast", "Contrast", 0, 64, 32),
    Control("gain", "Gain", 0, 255, 0),
    Control("saturation", "Saturation", 0, 128, 64),
    Control("sharpness", "Sharpness", 0, 64, 0),
    Control("white_balance", "White Balance", 2800, 6500, 4000),
    Control("exposure", "Exposure", 1, 1000, 156),
]

EXPOSURE_AUTO_CONTROL = "exposure_auto"
WHITE_BALANCE_AUTO_CONTROL = "white_balance_auto"
EXPOSURE_MANUAL = 1
EXPOSURE_AUTO = 8
WHITE_BALANCE_MANUAL = 0
WHITE_BALANCE_AUTO = 1


def build_helper(force: bool = False) -> Path:
    root = Path(tempfile.gettempdir()) / "uvc_realtime_controller_v3"
    source = root / "uvc_iokit_daemon.c"
    binary = root / "uvc_iokit_daemon"
    root.mkdir(parents=True, exist_ok=True)
    source.write_text(HELPER_C, encoding="utf-8")

    if force or not binary.exists() or source.stat().st_mtime > binary.stat().st_mtime:
        cmd = [
            "clang",
            "-Wall",
            "-Wextra",
            "-O2",
            str(source),
            "-framework",
            "IOKit",
            "-framework",
            "CoreFoundation",
            "-o",
            str(binary),
        ]
        subprocess.run(cmd, check=True)
    return binary


def parse_key_values(lines: list[str]) -> dict[str, int | str]:
    values: dict[str, int | str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, raw_value = line.strip().split("=", 1)
        try:
            values[key] = int(raw_value, 0)
        except ValueError:
            values[key] = raw_value
    return values


class UVCSession:
    def __init__(self, helper: Path, args: argparse.Namespace):
        cmd = [str(helper), "--vid", args.vid, "--pid", args.pid]
        if args.unit is not None:
            cmd += ["--unit", str(args.unit)]
        if args.camera_unit is not None:
            cmd += ["--camera-unit", str(args.camera_unit)]
        if args.interface is not None:
            cmd += ["--interface", str(args.interface)]

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self.process.stdout.readline() if self.process.stdout else ""
        if not ready.startswith("READY"):
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError(f"helper did not start: {ready}{stderr}")
        print(ready, end="")
        self._stderr_thread = self._start_stderr_drain()

    def _start_stderr_drain(self) -> threading.Thread | None:
        if self.process.stderr is None:
            return None

        def drain_stderr() -> None:
            for line in self.process.stderr:
                print(line.rstrip("\n"), file=sys.stderr)

        thread = threading.Thread(target=drain_stderr, daemon=True)
        thread.start()
        return thread

    def transact(self, command: str) -> tuple[int, list[str]]:
        if self.process.stdin is None or self.process.stdout is None:
            return 1, ["error=helper_closed"]
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

        lines: list[str] = []
        while True:
            line = self.process.stdout.readline()
            if not line:
                return 1, lines + ["error=helper_eof"]
            line = line.rstrip("\n")
            if line.startswith("END "):
                try:
                    return int(line.split()[1]), lines
                except Exception:
                    return 1, lines
            lines.append(line)

    def read(self, control: str) -> tuple[int, dict[str, int | str], list[str]]:
        rc, lines = self.transact(f"READ {control}")
        return rc, parse_key_values(lines), lines

    def set(self, control: str, value: int) -> tuple[int, dict[str, int | str], list[str]]:
        rc, lines = self.transact(f"SET {control} {value}")
        return rc, parse_key_values(lines), lines

    def close(self) -> None:
        try:
            self.transact("QUIT")
        except Exception:
            pass
        if self.process.poll() is None:
            self.process.terminate()


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def preview_loop(session: UVCSession, args: argparse.Namespace) -> int:
    try:
        import cv2  # type: ignore[reportMissingImports]
        import numpy as np
    except ImportError:
        print("Install preview dependencies:", file=sys.stderr)
        print("  /usr/local/bin/python3 -m pip install opencv-python numpy", file=sys.stderr)
        return 1

    control_state: dict[str, dict[str, int | str]] = {}
    active_controls: list[Control] = []
    for control in CONTROLS:
        rc, values, lines = session.read(control.name)
        for line in lines:
            print(line)
        if rc == 0:
            control_state[control.name] = values
            active_controls.append(control)

    if not active_controls:
        print("No supported controls detected.", file=sys.stderr)
        return 1

    _, exposure_values, _ = session.read(EXPOSURE_AUTO_CONTROL)
    _, wb_values, _ = session.read(WHITE_BALANCE_AUTO_CONTROL)
    exposure_auto = int(exposure_values.get(f"{EXPOSURE_AUTO_CONTROL}.current", EXPOSURE_MANUAL)) != EXPOSURE_MANUAL
    white_balance_auto = int(wb_values.get(f"{WHITE_BALANCE_AUTO_CONTROL}.current", WHITE_BALANCE_MANUAL)) != WHITE_BALANCE_MANUAL

    backend = cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else 0
    cap = cv2.VideoCapture(args.camera_index, backend)
    if not cap.isOpened():
        print(f"Could not open preview camera index {args.camera_index}", file=sys.stderr)
        return 1
    if args.width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(args.width * 0.75))

    window = "UVC Realtime Controller v3"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    last_values: dict[str, int] = {}
    control_ranges: dict[str, tuple[int, int]] = {}
    pending_values: dict[str, int] = {}
    last_write_at: dict[str, float] = {}
    status = {"text": "ready"}
    toggles = {"exposure_auto": exposure_auto, "white_balance_auto": white_balance_auto}
    render_profiles = [
        {
            "highlights": 0,
            "shadows": 0,
            "gamma_x100": 100,
            "contrast_x100": 100,
            "saturation_x100": 100,
        }
        for _ in range(3)
    ]
    selected_output = {"index": 0}
    syncing_render_sliders = {"active": False}
    auto_guard_state = {"enabled": 0}
    cached_panel = None
    frame_index = 0

    def value_for(control: Control, suffix: str, fallback: int) -> int:
        return int(control_state[control.name].get(f"{control.name}.{suffix}", fallback))

    def set_status(text: str) -> None:
        status["text"] = text
        print(text)

    def make_slider_callback(control: Control, minimum: int):
        def on_change(position: int) -> None:
            value = minimum + position
            if last_values.get(control.name) == value:
                return
            if control.name == "exposure" and toggles["exposure_auto"]:
                return
            if control.name == "white_balance" and toggles["white_balance_auto"]:
                return
            pending_values[control.name] = value
        return on_change

    for control in active_controls:
        minimum = value_for(control, "min", control.fallback_min)
        maximum = value_for(control, "max", control.fallback_max)
        current = int(control_state[control.name].get(f"{control.name}.current", control.fallback_default))
        if maximum <= minimum:
            maximum = minimum + 1
        current = clamp(current, minimum, maximum)
        last_values[control.name] = current
        control_ranges[control.name] = (minimum, maximum)
        cv2.createTrackbar(control.label, window, current - minimum, maximum - minimum, make_slider_callback(control, minimum))

    def set_auto(control_name: str, value: int, toggle_key: str, label: str) -> None:
        rc, _, lines = session.set(control_name, value)
        if rc == 0:
            toggles[toggle_key] = bool(value != EXPOSURE_MANUAL if control_name == EXPOSURE_AUTO_CONTROL else value)
            set_status(f"{label}: {'on' if toggles[toggle_key] else 'off'}")
        else:
            set_status(f"{label} failed")
            print("\n".join(lines))

    cv2.createTrackbar(
        "Exposure Auto 0/1",
        window,
        1 if exposure_auto else 0,
        1,
        lambda pos: set_auto(EXPOSURE_AUTO_CONTROL, EXPOSURE_AUTO if pos else EXPOSURE_MANUAL, "exposure_auto", "exposure auto"),
    )
    cv2.createTrackbar(
        "WhiteBalance Auto 0/1",
        window,
        1 if white_balance_auto else 0,
        1,
        lambda pos: set_auto(WHITE_BALANCE_AUTO_CONTROL, WHITE_BALANCE_AUTO if pos else WHITE_BALANCE_MANUAL, "white_balance_auto", "white balance auto"),
    )

    def current_profile() -> dict[str, int]:
        return render_profiles[selected_output["index"]]

    def sync_render_trackbars() -> None:
        profile = current_profile()
        syncing_render_sliders["active"] = True
        cv2.setTrackbarPos("Highlights", window, profile["highlights"])
        cv2.setTrackbarPos("Shadows", window, profile["shadows"])
        cv2.setTrackbarPos("Gamma x100", window, profile["gamma_x100"])
        cv2.setTrackbarPos("Render Contrast x100", window, profile["contrast_x100"])
        cv2.setTrackbarPos("Render Saturation x100", window, profile["saturation_x100"])
        syncing_render_sliders["active"] = False
        set_status(f"editing output {selected_output['index'] + 1}")

    def set_selected_output(position: int) -> None:
        selected_output["index"] = clamp(position, 0, 2)
        sync_render_trackbars()

    def set_profile_value(key: str, value: int, minimum: int = 0) -> None:
        if syncing_render_sliders["active"]:
            return
        current_profile()[key] = max(minimum, value)

    cv2.createTrackbar("Edit Output 1-3", window, 0, 2, set_selected_output)
    cv2.createTrackbar("Highlights", window, 0, 100, lambda v: set_profile_value("highlights", v))
    cv2.createTrackbar("Shadows", window, 0, 100, lambda v: set_profile_value("shadows", v))
    cv2.createTrackbar("Gamma x100", window, 100, 300, lambda v: set_profile_value("gamma_x100", v, 10))
    cv2.createTrackbar("Render Contrast x100", window, 100, 250, lambda v: set_profile_value("contrast_x100", v, 10))
    cv2.createTrackbar("Render Saturation x100", window, 100, 250, lambda v: set_profile_value("saturation_x100", v))
    cv2.createTrackbar("Auto Guard 0/1", window, 0, 1, lambda v: auto_guard_state.__setitem__("enabled", v))

    def apply_pending_writes() -> None:
        now = time.monotonic()
        for control, value in list(pending_values.items()):
            if now - last_write_at.get(control, 0.0) < args.write_interval:
                continue
            rc, _, lines = session.set(control, value)
            last_write_at[control] = now
            pending_values.pop(control, None)
            if rc == 0:
                last_values[control] = value
                set_status(f"{control}={value}")
            else:
                set_status(f"{control} SET failed")
                print("\n".join(lines))

    def apply_render(frame, profile: dict[str, int]):
        if (
            profile["highlights"] == 0
            and profile["shadows"] == 0
            and profile["gamma_x100"] == 100
            and profile["contrast_x100"] == 100
            and profile["saturation_x100"] == 100
        ):
            return frame.copy()
        image = frame.astype(np.float32) / 255.0
        shadows = profile["shadows"] / 100.0
        highlights = profile["highlights"] / 100.0
        gamma = max(0.1, profile["gamma_x100"] / 100.0)
        contrast = max(0.1, profile["contrast_x100"] / 100.0)
        saturation = max(0.0, profile["saturation_x100"] / 100.0)
        if shadows:
            image += shadows * 0.55 * (1.0 - image) * ((1.0 - image) ** 2)
        if highlights:
            image -= highlights * 0.45 * image * image
        image = np.clip(image, 0.0, 1.0)
        if abs(gamma - 1.0) > 0.01:
            image = np.power(image, 1.0 / gamma)
        if abs(contrast - 1.0) > 0.01:
            image = np.clip((image - 0.5) * contrast + 0.5, 0.0, 1.0)
        output = (image * 255.0).astype(np.uint8)
        if abs(saturation - 1.0) > 0.01:
            hsv = cv2.cvtColor(output, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
            output = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        return output

    def histogram_panel(frame):
        height = frame.shape[0]
        panel = np.zeros((height, 360, 3), dtype=np.uint8)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        if hist.max() > 0:
            hist = hist / hist.max()
        graph_top = 90
        graph_bottom = min(height - 112, graph_top + 210)
        graph_height = max(80, graph_bottom - graph_top)
        for x in range(256):
            y = int(hist[x] * graph_height)
            color = (90, 220, 90)
            if x < 16:
                color = (255, 150, 80)
            elif x > 239:
                color = (80, 120, 255)
            cv2.line(panel, (40 + x, graph_bottom), (40 + x, graph_bottom - y), color, 1)
        dark_pct = float((gray <= 8).mean() * 100.0)
        clipped_pct = float((gray >= 248).mean() * 100.0)
        mean_value = float(gray.mean())
        cv2.putText(panel, "Luma Histogram", (24, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.76, (230, 230, 230), 2, cv2.LINE_AA)
        cv2.putText(panel, f"Mean: {mean_value:5.1f}", (24, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(panel, f"Dark <=8: {dark_pct:4.1f}%", (24, graph_bottom + 34), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (255, 170, 90), 1, cv2.LINE_AA)
        cv2.putText(panel, f"Clipped >=248: {clipped_pct:4.1f}%", (24, graph_bottom + 62), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (90, 130, 255), 1, cv2.LINE_AA)
        warning = "OK"
        color = (90, 220, 90)
        if clipped_pct > 2.0:
            warning, color = "Too bright", (70, 90, 255)
        elif dark_pct > 8.0 or mean_value < 45:
            warning, color = "Too dark", (255, 180, 80)
        cv2.putText(panel, warning, (24, height - 36), cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2, cv2.LINE_AA)
        return panel, dark_pct, clipped_pct, mean_value

    def auto_guard(dark_pct: float, clipped_pct: float, mean_value: float) -> None:
        if not auto_guard_state["enabled"] or toggles["exposure_auto"]:
            return
        if "exposure" not in last_values or "exposure" not in control_ranges:
            return
        exp_min, exp_max = control_ranges["exposure"]
        exp = last_values["exposure"]
        step = max(1, (exp_max - exp_min) // 100)
        if clipped_pct > 2.0:
            pending_values["exposure"] = clamp(exp - step, exp_min, exp_max)
        elif dark_pct > 10.0 or mean_value < 42:
            pending_values["exposure"] = clamp(exp + step, exp_min, exp_max)

    print("v3 preview opened with 3 outputs. Select Edit Output 1-3 to tune each render. Press q or Esc to quit.")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Could not read frame.", file=sys.stderr)
            break
        if args.display_width and frame.shape[1] > args.display_width:
            scale = args.display_width / frame.shape[1]
            frame = cv2.resize(frame, (args.display_width, int(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)

        apply_pending_writes()
        rendered_outputs = [apply_render(frame, profile) for profile in render_profiles]
        for index, output in enumerate(rendered_outputs):
            selected = index == selected_output["index"]
            label = f"Output {index + 1}{' *' if selected else ''}"
            color = (0, 255, 0) if selected else (220, 220, 220)
            cv2.putText(output, label, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.76, color, 2, cv2.LINE_AA)
        frame_index += 1
        selected_frame = rendered_outputs[selected_output["index"]]
        if cached_panel is None or frame_index % args.hist_every == 0:
            cached_panel, dark_pct, clipped_pct, mean_value = histogram_panel(selected_frame)
            if frame_index % max(args.hist_every * 4, 1) == 0:
                auto_guard(dark_pct, clipped_pct, mean_value)

        cv2.putText(selected_frame, status["text"], (16, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 0), 2, cv2.LINE_AA)
        output_row = cv2.hconcat(rendered_outputs)
        cv2.imshow(window, cv2.hconcat([output_row, cached_panel]))
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord("q")):
            break

    cap.release()
    cv2.destroyAllWindows()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smooth macOS UVC realtime controller using IOKit ControlRequest.")
    parser.add_argument("--vid", default="0x0c45")
    parser.add_argument("--pid", default="0x636b")
    parser.add_argument("--unit", type=int)
    parser.add_argument("--camera-unit", type=int)
    parser.add_argument("--interface", type=int)
    parser.add_argument("--camera-index", default=0, type=int)
    parser.add_argument("--width", default=640, type=int, help="capture width hint, default 640")
    parser.add_argument("--display-width", default=800, type=int, help="downscale preview width, default 800")
    parser.add_argument("--write-interval", default=0.12, type=float, help="seconds between UVC SET writes per control")
    parser.add_argument("--hist-every", default=5, type=int, help="compute histogram every N frames")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    args.hist_every = max(1, args.hist_every)
    args.write_interval = max(0.0, args.write_interval)
    args.width = max(0, args.width)
    args.display_width = max(0, args.display_width)

    helper = build_helper(force=args.rebuild)
    session = UVCSession(helper, args)
    try:
        return preview_loop(session, args)
    finally:
        session.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"failed to build native helper: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
