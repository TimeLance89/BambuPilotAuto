#!/usr/bin/env python3
"""
BambuLab CLI - Command Line Interface for Print Jobs

Usage:
    python bambu_cli.py --file "path/to/file.3mf" --copies 5
    python bambu_cli.py --file "path/to/file.3mf" --copies 1   # Single print + eject
    python bambu_cli.py --file "path/to/file.3mf" --infinite   # Infinite loop
    python bambu_cli.py --queue "JobName"                       # Start job from queue
    python bambu_cli.py --list                                  # List queue
    python bambu_cli.py --add "path/to/file.3mf" --name "MyJob" --copies 10
"""

import argparse
import json
import os
import sys

# Import from main module
from BambuPilot import (
    generate_autoloop_file,
    upload_and_start_print,
    load_printer_config,
    load_queue,
    save_queue,
    load_library,
    CONFIG_FILE,
    QUEUE_FILE,
    LIBRARY_FILE
)

def print_status(msg):
    print(f"  → {msg}")

def cmd_list_queue():
    """List all jobs in the queue."""
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return
    
    print("\n=== Print Queue ===")
    for i, job in enumerate(queue):
        copies = "∞" if job.get("copies") == -1 else job.get("copies", 1)
        status = job.get("status", "pending")
        print(f"  [{i+1}] {job.get('name', 'Unknown')} (x{copies}) - {status}")
    print()

def cmd_add_to_queue(file_path, name=None, copies=1, sweep=True):
    """Add a new job to the queue."""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    if name is None:
        name = os.path.splitext(os.path.basename(file_path))[0]
    
    job = {
        "name": name,
        "source_file": os.path.abspath(file_path),
        "copies": copies,
        "use_sweep": sweep,

        "use_cooldown": False,
        "cooldown_temp": 30,
        "status": "pending"
    }
    
    queue = load_queue()
    queue.append(job)
    save_queue(queue)
    
    copies_str = "∞" if copies == -1 else copies
    print(f"Added to queue: '{name}' (x{copies_str})")

def resolve_printer_config(identifier=None):
    """Select a specific printer from the config list."""
    configs = load_printer_config()
    if not configs:
        print("Error: No printers configured. Run the GUI to add printers.")
        sys.exit(1)
        
    if identifier is None:
        # Default to first
        return configs[0]
        
    # Try Index (1-based)
    try:
        idx = int(identifier) - 1
        if 0 <= idx < len(configs):
            return configs[idx]
    except ValueError:
        pass
        
    # Try Serial or Name
    identifier_lower = identifier.lower()
    for conf in configs:
        if conf.get("serial", "").lower() == identifier_lower:
            return conf
        if conf.get("name", "").lower() == identifier_lower:
            return conf
            
    print(f"Error: Printer '{identifier}' not found.")
    print("Available printers:")
    for i, c in enumerate(configs):
        print(f"  [{i+1}] {c.get('name')} ({c.get('serial')})")
    sys.exit(1)

def cmd_start_queue_job(job_identifier, printer_id=None):
    """Start a specific job from the queue by name or index."""
    queue = load_queue()
    config = resolve_printer_config(printer_id)
    
    if not config.get("ip") or not config.get("access_code") or not config.get("serial"):
        print(f"Error: Invalid configuration for printer '{config.get('name')}'.")
        sys.exit(1)
    
    # Find job by name or index
    job = None
    job_index = -1
    
    try:
        idx = int(job_identifier) - 1  # 1-indexed for user
        if 0 <= idx < len(queue):
            job = queue[idx]
            job_index = idx
    except ValueError:
        # Search by name
        for i, j in enumerate(queue):
            if j.get("name", "").lower() == job_identifier.lower():
                job = j
                job_index = i
                break
    
    if job is None:
        print(f"Error: Job '{job_identifier}' not found in queue.")
        cmd_list_queue()
        sys.exit(1)
    
    print(f"\nStarting job: {job.get('name')} on {config.get('name')}")
    
    # Generate file
    copies = job.get("copies", 1)
    if copies == -1:
        copies = 1  # For CLI, infinite means we run 1 at a time
    
    print_status("Generating G-code...")
    output_file = generate_autoloop_file(
        job["source_file"],
        copies=copies,
        use_sweep=job.get("use_sweep", True),

        use_cooldown=job.get("use_cooldown", False),
        cooldown_temp=job.get("cooldown_temp", 30)
    )
    
    # Upload and print
    success, msg = upload_and_start_print(
        config["ip"],
        config["access_code"],
        config["serial"],
        output_file,
        use_ams=job.get("use_ams", True),
        status_callback=print_status
    )
    
    if success:
        print(f"\n✅ {msg}")
        if job.get("copies") != -1:
            queue[job_index]["status"] = "done"
            queue[job_index]["target_serial"] = config["serial"] # Track where it ran
            save_queue(queue)
    else:
        print(f"\n❌ {msg}")
        sys.exit(1)

def cmd_direct_print(file_path, copies=1, sweep=True, cooldown=False, printer_id=None):
    """Directly generate and print a file without adding to queue."""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    config = resolve_printer_config(printer_id)
    
    if not config.get("ip") or not config.get("access_code") or not config.get("serial"):
        print(f"Error: Invalid configuration for printer '{config.get('name')}'.")
        sys.exit(1)
    
    copies_str = "∞" if copies == -1 else copies
    print(f"\nDirect Print: {os.path.basename(file_path)} (x{copies_str}) on {config.get('name')}")
    
    # For direct print, generate with specified copies
    actual_copies = 1 if copies == -1 else copies
    
    print_status("Generating G-code...")
    output_file = generate_autoloop_file(
        file_path,
        copies=actual_copies,
        use_sweep=sweep,

        use_cooldown=cooldown,
        cooldown_temp=30
    )
    
    success, msg = upload_and_start_print(
        config["ip"],
        config["access_code"],
        config["serial"],
        output_file,
        status_callback=print_status
    )
    
    if success:
        print(f"\n✅ {msg}")
    else:
        print(f"\n❌ {msg}")
        sys.exit(1)

def cmd_list_library():
    """List jobs in library."""
    lib = load_library()
    if not lib:
        print("Library is empty.")
        return
    print("\n=== Job Library ===")
    for i, job in enumerate(lib):
        copies = "∞" if job.get("copies") == -1 else job.get("copies", 1)
        print(f"  [{i+1}] {job.get('name', 'Unknown')} (x{copies})")
    print()

def cmd_run_library_job(identifier, printer_id=None):
    """Add a library job to the queue."""
    lib = load_library()
    job = None
    
    # Resolve Identifier
    try:
        idx = int(identifier) - 1
        if 0 <= idx < len(lib):
            job = lib[idx]
    except ValueError:
        for j in lib:
            if j.get("name").lower() == identifier.lower():
                job = j
                break
    
    if not job:
        print(f"Error: Library job '{identifier}' not found.")
        sys.exit(1)
        
    # Copy to Queue logic
    import time
    import shutil
    
    project_dir = os.path.dirname(os.path.abspath(__file__))
    queue_dir = os.path.join(project_dir, "queue_jobs")
    os.makedirs(queue_dir, exist_ok=True)
    
    ts = int(time.time())
    safe_name = "".join([c for c in job["name"] if c.isalnum() or c in (' ', '-', '_')]).strip()
    new_name_base = f"{safe_name}_CLI_Import_{ts}"
    target_3mf = os.path.join(queue_dir, new_name_base + ".3mf")
    
    copied = False
    if job.get("generated_file") and os.path.exists(job["generated_file"]):
        try:
            shutil.copy2(job["generated_file"], target_3mf)
            copied = True
        except: pass
            
    queue_job = job.copy()
    if copied:
        queue_job["generated_file"] = target_3mf
    else:
        if "generated_file" in queue_job: del queue_job["generated_file"]
        
    if "thumbnail" in queue_job: del queue_job["thumbnail"]
    queue_job["status"] = "pending"
    
    queue = load_queue()
    queue.append(queue_job)
    save_queue(queue)
    print(f"Added library job '{job['name']}' to queue.")

def main():
    parser = argparse.ArgumentParser(
        description="BambuLab CLI - Command Line Print Job Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --file model.3mf --copies 5          # Print 5 copies with auto-eject
  %(prog)s --file model.3mf --copies 1          # Single print + eject
  %(prog)s --file model.3mf --infinite          # Infinite loop (1 at a time)
  %(prog)s --file model.3mf --printer 2         # Print on 2nd printer
  %(prog)s --list                               # Show queue
  %(prog)s --queue "MyJob" --printer "P1S_01"   # Start job on named printer
  %(prog)s --add model.3mf --name Job1 --copies 10  # Add to queue
        """
    )
    
    # Direct print options
    parser.add_argument("--file", "-f", help="3MF file to print")
    parser.add_argument("--copies", "-c", type=int, default=1, help="Number of copies (default: 1)")
    parser.add_argument("--infinite", "-i", action="store_true", help="Infinite loop mode")
    parser.add_argument("--no-sweep", action="store_true", help="Disable sweep (push off bed)")
    parser.add_argument("--printer", "-p", help="Target Printer (Index, Name, or Serial)")

    
    # Queue options
    parser.add_argument("--list", "-l", action="store_true", help="List queue")
    parser.add_argument("--queue", "-q", help="Start job from queue (by name or index)")
    parser.add_argument("--add", "-a", help="Add file to queue")
    parser.add_argument("--name", "-n", help="Job name (for --add)")
    
    # Library options
    parser.add_argument("--list-lib", action="store_true", help="List recurring jobs library")
    parser.add_argument("--run-lib", help="Run a job from library (by name or index)")
    
    args = parser.parse_args()
    
    # Handle commands
    if args.list_lib:
        cmd_list_library()
        return
        
    if args.run_lib:
        cmd_run_library_job(args.run_lib)
        return
    if args.list:
        cmd_list_queue()
        return
    
    if args.add:
        copies = -1 if args.infinite else args.copies
        cmd_add_to_queue(
            args.add,
            name=args.name,
            copies=copies,
            sweep=not args.no_sweep,

        )
        return
    
    if args.queue:
        cmd_start_queue_job(args.queue, printer_id=args.printer)
        return
    
    if args.file:
        copies = -1 if args.infinite else args.copies
        cmd_direct_print(
            args.file,
            copies=copies,
            sweep=not args.no_sweep,
            printer_id=args.printer
        )
        return
    
    # No valid command
    parser.print_help()

if __name__ == "__main__":
    main()
