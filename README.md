# MAC-UVC-Camera-Controller
Python-based UVC camera control and preview tool for macOS using native IOKit USB control requests.

Unlike typical UVC utilities that rely on OpenCV camera properties, PyUSB, or AVFoundation camera APIs, this project communicates directly with USB Video Class (UVC) devices through macOS IOKit ControlRequest calls, providing low-level hardware parameter control with real-time visual feedback.

---

## Features

### Hardware-Level UVC Control

- Brightness
- Contrast
- Gain
- Saturation
- Sharpness
- White Balance
- Exposure
- Auto Exposure
- Auto White Balance

All parameter updates are performed through native IOKit USB control transfers.

---

### Real-Time Preview

- Live camera preview using OpenCV
- Interactive slider-based parameter adjustment
- Immediate visual feedback during tuning

---

### Histogram Analysis

Built-in luminance histogram visualization:

- Mean brightness
- Shadow percentage
- Highlight percentage
- Clipped highlight detection
- Dark region detection

Useful for image quality evaluation and exposure tuning.

---

### Auto Exposure Guard

Optional exposure protection mode:

- Detects overexposure
- Detects underexposure
- Automatically adjusts exposure when clipping thresholds are exceeded

---

### Native Helper Architecture

A lightweight C helper is compiled automatically and communicates with USB devices through:

- IOKit
- IOUSBDeviceInterface
- IOUSBInterfaceInterface190
- UVC ControlRequest

Advantages:

- Low latency
- Reliable hardware access
- No vendor SDK required
- Direct USB Video Class communication

---

## Architecture

text Python UI Layer │ ├── OpenCV Preview ├── Histogram Analysis ├── Slider Controls │ └── Native Helper (C)         │         └── macOS IOKit                 │                 └── USB UVC Device 

---

## Requirements

### Operating System

- macOS 12+
- macOS 13+
- macOS 14+
- Apple Silicon and Intel supported

### Software

- Python 3.10+
- OpenCV
- NumPy
- Xcode Command Line Tools

Install Xcode tools:

bash xcode-select --install 

Install Python dependencies:

bash pip install opencv-python numpy 

---

## Installation

Clone repository:

bash git clone https://github.com/YOUR_USERNAME/uvc-realtime-controller.git cd uvc-realtime-controller 

Install dependencies:

bash pip install -r requirements.txt 

---

## Usage

Launch preview interface:

bash python main.py 

Specify camera index:

bash python main.py --camera-index 1 

Force rebuild native helper:

bash python main.py --rebuild 

---

## Example Controls

Adjust brightness:

bash python main.py --no-preview --control brightness --set 10 

Adjust contrast:

bash python main.py --no-preview --control contrast --set 40 

Adjust gain:

bash python main.py --no-preview --control gain --set 20 

Adjust exposure:

bash python main.py --no-preview --control exposure --set 200 

Enable auto exposure:

bash python main.py --no-preview --control exposure_auto --set 8 

Disable auto exposure:

bash python main.py --no-preview --control exposure_auto --set 1 

---

## Supported Controls

| Control | Supported |
|----------|-----------|
| Brightness | ✓ |
| Contrast | ✓ |
| Gain | ✓ |
| Saturation | ✓ |
| Sharpness | ✓ |
| White Balance | ✓ |
| White Balance Auto | ✓ |
| Exposure | ✓ |
| Exposure Auto | ✓ |

Support depends on the UVC implementation of the connected camera.

---

## Technical Notes

This project intentionally avoids:

- PyUSB
- OpenCV CAP_PROP camera controls
- AVFoundation property APIs
- Vendor-specific SDKs

All camera parameter access is performed through standard UVC control requests over USB.

---

## Use Cases

- Camera bring-up
- Embedded vision prototyping
- Image quality evaluation
- Exposure tuning
- Computer vision experiments
- UVC device debugging
- Hardware validation

---

## Future Work

- Multi-camera support
- Camera profile save/load
- Video recording
- Remote control interface
- Linux backend support
- Windows backend support

---

## License

MIT License
