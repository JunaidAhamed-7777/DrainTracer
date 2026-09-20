# Process Resource Leak Detector (MVP)

A lightweight Windows CLI tool that launches processes via a native file selector, monitors resource consumption in real-time, and flags memory and handle leaks directly in an interactive Terminal UI.

## Features

- **Native Windows File Picker:** File selection via standard Windows file dialog directly on startup.
- **Real-Time Resource Metrics:** Tracks RAM (Working Set/VMS), CPU, System Handles, and Thread counts across parent and child processes.
- **Automated Leak Detection:** Uses sliding-window heuristic analysis to detect unreleased handles and monotonic memory growth.
- **Interactive TUI Dashboard:** Dynamic CLI interface with visual sparklines, resource alerts, and process control keybindings.

## Directory Structure

```text
├── src/
│   ├── main.py            # Main entry point & CLI setup
│   ├── file_picker.py     # Windows native file selection interface
│   ├── process_runner.py  # Process spawning and PID hierarchy manager
│   ├── leak_detector.py   # Metric sampling & leak heuristics engine
│   └── ui/
│       └── dashboard.py   # Rich/Textual CLI-GUI interface
├── requirements.txt
└── README.md

```

## Quick Start

### Prerequisites

* Windows 10 / 11
* Python 3.10+

### Installation

1. Clone the repository:
```bash
git clone [https://github.com/your-username/process-resource-leak-detector.git](https://github.com/your-username/process-resource-leak-detector.git)
cd process-resource-leak-detector

```


2. Create a virtual environment and install dependencies:
```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

```



### Running the Application

Execute the main script:

```bash
python src/main.py

```

1. Select any target executable or script in the Windows file picker pop-up.
2. Monitor real-time resource telemetry and leak warnings directly inside your terminal.

## Keybindings

| Key | Action |
| --- | --- |
| `Space` | Pause / Resume real-time monitoring |
| `K` | Terminate monitored target process |
| `E` | Export session metrics to `leak_report.json` |
| `Q` | Exit application |

## License

MIT License
