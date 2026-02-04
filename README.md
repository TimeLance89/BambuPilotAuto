# BambuPilot 🚀
*(Formerly BambuLab Auto Ejector)*

![Version](https://img.shields.io/badge/version-1.1%20Stable-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Support](https://img.shields.io/badge/printers-X1C%20%7C%20P1S%20%7C%20P1P%20%-orange)

***

**BambuPilot** is the ultimate automation and fleet management solution for Bambu Lab 3D printers. Transform your hobby printer into a continuous manufacturing machine with intelligent auto-ejection, queue management, and multi-printer farm support.

> [!IMPORTANT]
> **Safety First:** This tool automates physical movements (bed lowering, sweeping). Always supervise the first run of any new print job to ensure safe ejection.

## ✨ Key Features

### 🏭 Farm & Fleet Management
*   **Multi-Printer Support:** Control multiple printers from a single dashboard.
*   **Live Monitoring:** Real-time status, temperatures, progress, and AMS colors for your entire fleet.
*   **Smart Routing:** Send jobs to specific printers directly from the CLI or Queue.

### 🔄 Continuous Production
*   **Auto-Loop:** Automatically reprint files `X` times or indefinitely (`∞`).
*   **Smart Ejection:** 
    *   **Cooldown Wait:** Intelligent waiting for bed adhesion release.
    *   **Flex-Plate Bending:** Repeated bed movements to "flex" spring steel sheets.
    *   **Dual-Speed Sweep:** Fast and slow sweep motions to clear the build plate.
*   **Prime Line Removal:** Automatically detects and strips the nozzle load line to prevent collisions.

### 📂 Job Management
*   **Print Queue:** Build a timeline of jobs. Status tracks from `PENDING` → `RUNNING` → `DONE`.
*   **Library:** Save your favorite jobs (with settings) for one-click re-queuing.
*   **Pin/Unpin:** Quickly toggle jobs between Queue and Library to prevent duplicates.
*   **Visual Preview:** See thumbnails of your 3MF files directly in the interface.

***

## 🚀 Quick Start

### Prerequisites
*   Windows OS
*   Python 3.8+
*   Bambu Lab Printer in **LAN Mode** (Get Access Code from Printer Display)

### Installation

1.  **Clone & Install**
    ```bash
    git clone https://github.com/TimeLance89/BambuPilotAuto
    cd BambuPilotAuto
    pip install -r requirements.txt
    ```

2.  **Launch**
    Double-click `run_bambu_tool.bat` or run:
    ```bash
    python BambuPilot.pyw
    ```

3.  **Configure Fleet**
    *   Go to the **Settings** tab.
    *   Click **+ Add Printer**.
    *   Enter **Name**, **IP Address**, **Access Code**, and **Serial Number**.
    *   Click **Save Changes**.

***

## 🖥️ User Interface Guide

### 1. Dashboard
The Mission Control center. Shows a grid of all connected printers.
*   **Status Indicators:** Online/Offline/Printing state.
*   **Live Data:** Bed/Nozzle temps, remaining time, percent complete.
*   **AMS:** Visualizes loaded filament colors.

### 2. Prepare (Generator)
Where jobs are born.
1.  **Source File:** Drag & drop a `.3mf` file.
2.  **Settings:**
    *   **Loop Count:** Set specific number or toggle **Infinite (∞)**.
    *   **Cooldown:** Set target temp (e.g., 30°C for PLA) for part release.
    *   **Options:** Toggle AMS or Sweep maneuvers.
3.  **Target Printers:** Select which machines to target (or specific ones).
4.  **Add to Queue:** Compiles the job and sends it to the pending list.

### 3. Print Queue
Manage your production timeline.
*   **Start Next:** Launches the top pending job on the assigned printer.
*   **Actions:**
    *   **Pin (☆/★):** Save job to Library for later use.
    *   **Edit (✎):** Modify loop count or temperatures of a pending job.
    *   **Remove (✕):** Delete job.
*   **Status Sync:** Automatically updates to `DONE` when the printer finishes.

### 4. Library
Your persistent catalog of proven print jobs.
*   **One-Click Import:** Click `▶` to send a library job back to the Queue.
*   **Management:** Delete obsolete jobs to keep it clean.

***

## 💻 CLI Reference

BambuPilot includes a powerful Command Line Interface (`bambu_cli.py`) for scripting and headless operation.

| Command | Description | Example |
| :--- | :--- | :--- |
| `--list` | Show current queue | `python bambu_cli.py --list` |
| `--file` | Direct print a file | `python bambu_cli.py -f box.3mf --printer 1` |
| `--add` | Add job to queue | `python bambu_cli.py -a box.3mf -n "Box Batch" -c 10` |
| `--queue` | Start job from queue | `python bambu_cli.py -q "Box Batch" -p "P1S_01"` |
| `--copies` | Set copy count | `-c 5` or `-c 1` |
| `--infinite` | Infinite loop | `-i` |
| `--printer` | Target printer | `-p 1` (Index) or `-p "SerialNo"` |

> [!TIP]
> Use batch scripts to automate loading your farm every morning!

***

## ⚙️ How Auto-Ejection Works

BambuPilot injects custom G-code at the end of your print to safely remove parts:

1.  **Safety Drop:** Bed lowers to clear the nozzle.
2.  **Cooldown Wait:** `M190 S{temp}` ensures the bed cools down, shrinking the plastic and releasing adhesion.
3.  **Flex Motion:** rapid Z-movements (`Z235` ↔ `Z200`) flex the spring steel plate to pop parts loose.
4.  **Sweep:** The print head moves back and forth at low height (Z=2mm-3mm) to push parts into the chute.

---

**Publisher:** TheJester  
**License:** MIT  

*Happy "Infinite" Printing!*
