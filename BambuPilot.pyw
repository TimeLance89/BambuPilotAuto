import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import zipfile
import re
import os
import shutil
import tempfile
import hashlib
import json
import threading
import socket
import ftplib
import ssl
import time
from PIL import Image

try:
    from zeroconf import Zeroconf, ServiceBrowser
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

def extract_thumbnail_from_3mf(file_path):
    try:
        if not os.path.exists(file_path): return None
        with zipfile.ZipFile(file_path, 'r') as z:
            names = z.namelist()
            tgt = None
            if "Metadata/plate_1.png" in names: tgt = "Metadata/plate_1.png"
            elif "Metadata/plate_1.jpg" in names: tgt = "Metadata/plate_1.jpg"
            elif "Metadata/model.png" in names: tgt = "Metadata/model.png"
            
            if tgt:
                out = file_path[:-4] + ".png"
                with open(out, "wb") as f:
                    f.write(z.read(tgt))
                return out
    except: pass
    return None

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

CONFIG_FILE = "printer_config.json"
QUEUE_FILE = "print_queue.json"
LIBRARY_FILE = "job_library.json"

# --- HELPER CLASSES (FTP & Discovery) ---

class ImplicitFTP_TLS(ftplib.FTP_TLS):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = self.sock.session
            if isinstance(self.sock, ssl.SSLSocket):
                session = self.sock.session
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size

    def storbinary_no_unwrap(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            conn.close()
        return self.voidresp()

class PrinterListener:
    def __init__(self, callback):
        self.callback = callback
    
    def remove_service(self, zeroconf, type, name):
        pass

    def update_service(self, zeroconf, type, name):
        pass

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        if info:
            try:
                if info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    self.callback(name, ip, info)
            except Exception as e:
                print(f"Error parsing service info: {e}")

class PrinterMonitor:
    def __init__(self, ip, access_code, serial, status_callback):
        self.ip = ip
        self.access_code = access_code
        self.serial = serial
        self.status_callback = status_callback
        self.client = None
        self.connected = False
        self.running = False
        # Cache for persistent state (handling partial updates)
        self._state = {
            "percent": 0,
            "left_min": "--",
            "state": "UNKNOWN",
            "job_name": "--"
        }
        self.last_push = 0

    def start(self):
        if not MQTT_AVAILABLE or not self.ip:
            return

        self.running = True
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set("bblp", self.access_code)
        
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(context)
        
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            # Use connect_async to prevent blocking UI main thread
            self.client.connect_async(self.ip, 8883, keepalive=60)
            self.client.loop_start()
            
            # Start a background thread to keep requesting updates
            threading.Thread(target=self._keep_alive_loop, daemon=True).start()
            
        except Exception as e:
            print(f"Monitor Connection Failed: {e}")

    def _keep_alive_loop(self):
        while self.running:
            if self.connected:
                try:
                    # Request full status update (pushall)
                    payload = {
                        "pushing": {
                            "sequence_id": "0",
                            "command": "pushall"
                        }
                    }
                    topic = f"device/{self.serial}/request"
                    self.client.publish(topic, json.dumps(payload))
                except:
                    pass
            time.sleep(2) # Request every 2 seconds to force updates

    def stop(self):
        self.running = False
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            topic = f"device/{self.serial}/report"
            client.subscribe(topic)
            print(f"Subscribed to {topic}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            print_status = payload.get("print", {})
            
            # Debug: Uncomment to see raw data if needed
            # print(f"Raw State: {print_status.get('gcode_state')}")

            # Debug: Dump payload to file to check keys
            try:
                with open("mqtt_dump.json", "w") as f:
                    json.dump(print_status, f, indent=4)
            except: pass

            # Update cache with any available fields
            if "mc_percent" in print_status:
                self._state["percent"] = print_status["mc_percent"] or 0
                
            if "mc_remaining_time" in print_status:
                val = print_status["mc_remaining_time"]
                if val is not None:
                     self._state["left_min"] = val
                
            if "gcode_state" in print_status:
                self._state["state"] = print_status["gcode_state"] or "UNKNOWN"
                
            if "subtask_name" in print_status:
                 self._state["job_name"] = print_status["subtask_name"] or "--"

            # Parse Extra Details
            if "nozzle_temper" in print_status:
                self._state["nozzle_temp"] = int(print_status["nozzle_temper"])
            
            if "bed_temper" in print_status:
                self._state["bed_temp"] = int(print_status["bed_temper"])
                
            if "cooling_fan_speed" in print_status:
                # Often returned as string "0" or int
                try: self._state["fan_speed"] = int(print_status["cooling_fan_speed"])
                except: pass
                
            if "layer_num" in print_status:
                self._state["layer_num"] = print_status["layer_num"]
                
            if "total_layer_num" in print_status:
                self._state["total_layer_num"] = print_status["total_layer_num"]

            # AMS Colors
            if "ams" in print_status and "ams" in print_status["ams"]:
                try:
                    # Usually ams['ams'][0]['tray'] list of dicts
                    ams_data = print_status["ams"]["ams"]
                    colors = []
                    if len(ams_data) > 0:
                        trays = ams_data[0].get("tray", [])
                        for t in trays:
                            # Color is typically 8 hex chars (RRGGBBFF) or 6. We want first 6.
                            c = t.get("tray_color", "FFFFFF")
                            if len(c) > 6: c = c[:6]
                            colors.append(f"#{c}")
                    
                    # Pad to 4
                    while len(colors) < 4: colors.append(None)
                    self._state["ams_colors"] = colors
                except: pass

            self.status_callback(self._state)
                
        except Exception as e:
            pass
                
        except Exception as e:
            pass

# --- CORE LOGIC (Load/Save/Generate/Upload) ---

def load_printer_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                     data["name"] = "Default Printer"
                     return [data]
                if isinstance(data, list):
                    return data
        except: pass
    return []



def load_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, 'r') as f:
            return json.load(f)
    return []

def save_queue(queue):
    with open(QUEUE_FILE, 'w') as f:
        json.dump(queue, f, indent=4)

def load_library():
    log_path = "debug_lib.log"
    try:
        abs_path = os.path.abspath(LIBRARY_FILE)
        with open(log_path, "a") as log:
            log.write(f"Loading Library from: {abs_path}\n")
            if not os.path.exists(LIBRARY_FILE):
                log.write("File does not exist.\n")
                return []
            
            with open(LIBRARY_FILE, 'r') as f:
                data = json.load(f)
                log.write(f"Loaded {len(data)} items.\n")
                return data
    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"Error loading library: {e}\n")
    return []

def save_library(data):
    try:
        with open(LIBRARY_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except: pass

def generate_autoloop_file(source_path, copies=1, use_sweep=True, use_cooldown=False, cooldown_temp=30, output_dir=None):
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        import time
        base_name = os.path.splitext(os.path.basename(source_path))[0]
        filename = f"{base_name}_AutoLoop_{int(time.time())}.3mf"
        output_path = os.path.join(output_dir, filename)
    else:
        output_path = os.path.splitext(source_path)[0] + "_AutoLoop.3mf"
        
    drop_z = 250.0
    
    # Generate 60x M190 commands for reliable cooldown wait
    cooldown_commands = "\n".join([f"M190 S{cooldown_temp}" for _ in range(60)])
    
    # Ejection G-code
    cooldown_cmd = ""
    if use_cooldown:
        cooldown_cmd = f"M140 S0\\nM106 S255\\nG4 S{cooldown_temp * 10}\\nM190 R{cooldown_temp}\\nM106 S0"

    ejection_gcode = f"""
; === AUTO EJECTION (Optimized Cooldown) ===
M400
; Phase 1: First Z-drop to break adhesion
G90
G1 Z{drop_z} F1200
G4 P1000
G1 Z50.0 F3000

; Phase 2: Cooling cycle - Fan max, wait for bed to cool
M140 S0       ; Heater off FIRST
G4 P2000      ; Short pause
M106 P1 S255  ; Part cooling fan max
M106 P2 S255  ; Aux fan max

;Cooldown Start
{cooldown_commands}
;Cooldown End

; Phase 3: BENDING MOTION - Flex plate to loosen parts
G1 Z235 F12000          ; bend-up stroke #1
G1 Z200 F12000          ; bend-down stroke #1
G1 Z235 F12000          ; bend-up stroke #2
G1 Z200 F12000          ; bend-down stroke #2
G1 Z235 F12000          ; bend-up stroke #3
G1 Z200 F12000          ; bend-down stroke #3
G1 Z235 F12000          ; bend-up stroke #4
G1 Z200 F12000          ; bend-down stroke #4
G1 Z235 F12000          ; bend-up stroke #5
G1 Z200 F12000          ; bend-down stroke #5
G1 Z235 F12000          ; bend-up stroke #6
G1 Z200 F12000          ; bend-down stroke #6

; Phase 4: SLOW SWEEP (Z=3mm, F3000)
M106 P1 S0    ; Fan off
M106 P2 S0
G1 Z3.0 F10000
M400

; Central sweeps (2x)
G1 X125 F3000
G1 Y250 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 Y0   F3000

; Extended right-to-left rake (7 passes)
G1 Y250 F3000
G1 X220 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X190 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X160 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X130 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X100 F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X70  F3000
G1 Y0   F3000
G1 Y250 F3000
G1 X30  F3000
G1 Y0   F3000

; Phase 5: FAST SWEEP (Z=2mm, F12000)
G1 Y250 F3000
M400
G1 X220 F3000
M400
G1 Z2.0 F12000
M400

; Fast right-to-left rake (7 passes)
G1 Y250 F12000
G1 X220 F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X190 F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X160 F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X130 F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X100 F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X70  F12000
G1 Y0   F12000
G1 Y250 F12000
G1 X30  F12000
G1 Y0   F12000

; Park at safe position
G1 X65 Y245 F12000
G1 Y265 F3000
M400
"""

    try:
        with zipfile.ZipFile(source_path, 'r') as zin:
            gcode_filename = "Metadata/plate_1.gcode"
            if gcode_filename not in zin.namelist():
                candidates = [n for n in zin.namelist() if n.endswith(".gcode")]
                if not candidates: raise Exception("No .gcode found")
                gcode_filename = candidates[0]
            
            original_bytes = zin.read(gcode_filename)
            has_bom = original_bytes.startswith(b'\\xef\\xbb\\xbf')
            content_str = original_bytes[3:].decode('utf-8') if has_bom else original_bytes.decode('utf-8')

            # Prime Line Removal - Using EXACT Bambu Studio markers
            # Analyzed from actual sliced G-code files
            PRIME_START = ";===== nozzle load line"
            PRIME_END = "M1002 gcode_claim_action"
            
            lines = content_str.split('\\n')
            new_lines = []
            in_prime_section = False
            
            for line in lines:
                # Check for exact start marker
                if PRIME_START in line:
                    in_prime_section = True
                
                # If in prime section, comment out the line
                if in_prime_section:
                    new_lines.append("; REMOVED PRIME: " + line)
                    # Check for exact end marker
                    if PRIME_END in line:
                        in_prime_section = False
                else:
                    new_lines.append(line)
            
            content_str = '\\n'.join(new_lines)

            # Split at EXECUTABLE_BLOCK_END
            marker = "; EXECUTABLE_BLOCK_END"
            unit = content_str.split(marker)[0] if marker in content_str else content_str
            suffix = marker if marker in content_str else ""
            
            unit += "\\n" + ejection_gcode
            
            final_str = ""
            for i in range(1, copies + 1):
                final_str += f"\\n; >>> LOOP {i} <<<\\n" + unit
            
            if suffix: final_str += "\\n" + suffix
            
            final_bytes = (b'\\xef\\xbb\\xbf' + final_str.encode('utf-8')) if has_bom else final_str.encode('utf-8')
            final_md5 = hashlib.md5(final_bytes).hexdigest().encode('utf-8')

        with zipfile.ZipFile(source_path, 'r') as zin:
            with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == gcode_filename:
                        zout.writestr(item.filename, final_bytes)
                    elif item.filename == gcode_filename + ".md5":
                        zout.writestr(item.filename, final_md5)
                    else:
                        zout.writestr(item, zin.read(item.filename))
        return output_path
    except Exception as e:
        raise e

def upload_and_start_print(ip, access_code, serial, file_path, use_ams=True, status_callback=None):
    filename = os.path.basename(file_path)
    def log(msg):
        if status_callback: status_callback(msg)
        print(msg)
    
    try:
        # FTP
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try: context.set_ciphers('DEFAULT:@SECLEVEL=1')
        except: pass
        
        ftps = ImplicitFTP_TLS(context=context)
        ftps.connect(ip, 990, timeout=30)
        ftps.login('bblp', access_code)
        ftps.prot_p()
        ftps.set_pasv(True)
        with open(file_path, "rb") as f:
            ftps.storbinary_no_unwrap(f"STOR /" + filename, f)
        ftps.quit()

        # MQTT
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set("bblp", access_code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)
        client.connect(ip, 8883, keepalive=10)
        client.loop_start()

        payload = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "subtask_name": filename.replace(".3mf", ""),
                "url": f"ftp://{filename}",
                "bed_type": "auto",
                "timelapse": False,
                "bed_leveling": True,
                "flow_cali": True,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": use_ams,
                "ams_mapping": [0, 1, 2, 3] if use_ams else []
            }
        }
        client.publish(f"device/{serial}/request", json.dumps(payload)).wait_for_publish(timeout=5)
        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        return True, "Print started"
    except Exception as e:
        return False, str(e)

# --- NEW UI CLASSES ---

class BambuAutoEjectorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("BambuPilot")
        self.geometry("1100x700")
        
        # Grid Configuration (1x2)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # Variables
        self.file_path = tk.StringVar()
        self.copies_var = tk.IntVar(value=5)
        self.use_ams_var = tk.BooleanVar(value=True) # Default ON
        self.sweep_var = tk.BooleanVar(value=True)
        self.cooldown_var = tk.BooleanVar(value=False)
        self.cooldown_temp_var = tk.IntVar(value=30)  # Default 30°C
        self.status_var = tk.StringVar(value="Ready")
        
        # Printer Config (Farm Support)
        self.printers_config = load_printer_config() # List of dicts
        self.monitors = {} # IP -> PrinterMonitor
        
        self.queue = []
        
        # Load Data
        self.load_queue()
        
        # --- UI LAYOUT ---
        self.create_sidebar()
        self.create_pages()
        
        # Start
        self.select_frame("Dashboard")
        self.start_monitor()

    def create_sidebar(self):
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)

        logo = ctk.CTkLabel(self.sidebar_frame, text="BambuPilot", font=ctk.CTkFont(size=24, weight="bold"))
        logo.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        self.btn_dashboard = self.create_nav_btn("Dashboard", 1)
        self.btn_prepare = self.create_nav_btn("Prepare/Generator", 2)
        self.btn_queue = self.create_nav_btn("Print Queue", 3)
        self.btn_library = self.create_nav_btn("Library", 4)
        self.btn_settings = self.create_nav_btn("Settings", 5)
        
        # Status Footer
        self.status_footer = ctk.CTkLabel(self.sidebar_frame, text="Disconnected", text_color="gray")
        self.status_footer.grid(row=6, column=0, padx=20, pady=20)

    def create_nav_btn(self, text, row):
        btn = ctk.CTkButton(self.sidebar_frame, corner_radius=0, height=40, border_spacing=10, 
                            text=text, fg_color="transparent", text_color=("gray10", "gray90"), 
                            hover_color=("gray70", "gray30"), anchor="w", 
                            command=lambda t=text: self.select_frame(t))
        btn.grid(row=row, column=0, sticky="ew")
        return btn

    def create_pages(self):
        self.pages = {}
        self.init_dashboard_page()
        self.init_prepare_page()
        self.init_queue_page()
        self.init_settings_page()
        self.init_library_page()
        
        self.select_frame("Dashboard")
        self.start_monitor()

    def _zombie_spawn_printer_card(self, conf, index):
        sn = conf.get("serial")
        name = conf.get("name", f"Printer {index+1}")
        
        # Card Frame
        card = ctk.CTkFrame(self.dash_scroll, fg_color=("#3B8ED0", "#1f538d"))
        card.grid(row=index//2, column=index%2, sticky="nsew", padx=10, pady=10)
        
        # Header
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=15, pady=10)
        ctk.CTkLabel(head, text=name, font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        lbl_state = ctk.CTkLabel(head, text="OFFLINE", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray80")
        lbl_state.pack(side="right")
        
        # Main Status
        lbl_job = ctk.CTkLabel(card, text="--", font=ctk.CTkFont(size=14))
        lbl_job.pack(pady=5)
        
        # Progress Bar
        prog_bar = ctk.CTkProgressBar(card)
        prog_bar.pack(fill="x", padx=20, pady=5)
        prog_bar.set(0)
        
        # AMS Grid
        ams_frame = ctk.CTkFrame(card, fg_color="transparent")
        ams_frame.pack(pady=10)
        ams_slots = []
        for j in range(4):
            cnt = ctk.CTkFrame(ams_frame, fg_color="transparent")
            cnt.pack(side="left", padx=5)
            ctk.CTkLabel(cnt, text=f"S{j+1}", font=ctk.CTkFont(size=10)).pack(pady=(0,2))
            slot = ctk.CTkFrame(cnt, width=25, height=25, corner_radius=12, fg_color="gray30", border_width=1)
            slot.pack()
            ams_slots.append(slot)
            
        # Stats Grid
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=10, pady=10)
        for c in range(2): stats.grid_columnconfigure(c, weight=1)
        
        lbl_time = ctk.CTkLabel(stats, text="-- min", font=ctk.CTkFont(size=12))
        lbl_time.grid(row=0, column=0, sticky="ew")
        
        lbl_temps = ctk.CTkLabel(stats, text="N: -- / B: --", font=ctk.CTkFont(size=12, weight="bold"))
        lbl_temps.grid(row=0, column=1, sticky="ew")
        
        # Store References
        self.printer_cards[sn] = {
            "lbl_state": lbl_state,
            "lbl_job": lbl_job,
            "prog": prog_bar,
            "lbl_time": lbl_time,
            "lbl_temps": lbl_temps,
            "ams_slots": ams_slots
        }

        # 2. Prepare (Generator)
        fr_prep = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Prepare/Generator"] = fr_prep
        


        
        # --- CARD 1: SOURCE FILE ---
        file_card = ctk.CTkFrame(fr_prep)
        file_card.pack(fill="x", padx=30, pady=10)
        
        ctk.CTkLabel(file_card, text="1. Source File", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20, pady=(15, 5))
        f_row = ctk.CTkFrame(file_card, fg_color="transparent")
        f_row.pack(fill="x", padx=10, pady=(0, 20))
        
        ctk.CTkEntry(f_row, textvariable=self.file_path, placeholder_text="Select a .3mf file...").pack(side="left", fill="x", expand=True, padx=(10, 10))
        ctk.CTkButton(f_row, text="Browse Folder 📂", width=120, height=35, command=self.browse_file).pack(side="right", padx=(0, 10))

        # --- CARD 2: CONFIGURATION ---
        conf_card = ctk.CTkFrame(fr_prep)
        conf_card.pack(fill="x", padx=30, pady=10)
        
        ctk.CTkLabel(conf_card, text="2. Print Settings", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20, pady=(15, 10))
        
        # Grid Layout for Settings
        conf_grid = ctk.CTkFrame(conf_card, fg_color="transparent")
        conf_grid.pack(fill="x", padx=10, pady=(0, 20))
        
        # Col 1: Loops - Stepper Design
        loop_frame = ctk.CTkFrame(conf_grid, fg_color="transparent")
        loop_frame.grid(row=0, column=0, sticky="nw", padx=10)
        
        ctk.CTkLabel(loop_frame, text="Loop Count", text_color="gray").pack(anchor="w")
        
        # Stepper Row: [-] [Entry] [+]
        step_row = ctk.CTkFrame(loop_frame, fg_color="transparent")
        step_row.pack(fill="x", pady=5)
        
        def decrease_loops():
            try:
                v = self.copies_var.get()
                if v > 1: self.copies_var.set(v - 1)
            except: self.copies_var.set(1)

        def increase_loops():
            try:
                v = self.copies_var.get()
                self.copies_var.set(v + 1)
            except: self.copies_var.set(1)
            
        self.btn_minus = ctk.CTkButton(step_row, text="-", width=40, command=decrease_loops, fg_color="#E07A5F")
        self.btn_minus.pack(side="left", padx=(0, 5))
        
        self.entry_copies = ctk.CTkEntry(step_row, width=60, textvariable=self.copies_var, justify="center")
        self.entry_copies.pack(side="left", padx=5)
        
        self.btn_plus = ctk.CTkButton(step_row, text="+", width=40, command=increase_loops, fg_color="#2CC985")
        self.btn_plus.pack(side="left", padx=(5, 0))
        
        # Infinite Switch with Event
        def toggle_infinite():
            if self.chk_infinite.get() == 1: # ON
                self.entry_copies.configure(state="disabled")
                self.btn_minus.configure(state="disabled")
                self.btn_plus.configure(state="disabled")
            else:
                self.entry_copies.configure(state="normal")
                self.btn_minus.configure(state="normal")
                self.btn_plus.configure(state="normal")

        self.chk_infinite = ctk.CTkSwitch(loop_frame, text="Infinite Loop (∞)", command=toggle_infinite)
        self.chk_infinite.pack(anchor="w", pady=(15, 0))

        # Col 2: Options
        opts_frame = ctk.CTkFrame(conf_grid, fg_color="transparent")
        opts_frame.grid(row=0, column=1, sticky="ne", padx=40)
        
        ctk.CTkCheckBox(opts_frame, text="Use AMS (Multi-Color)", variable=self.use_ams_var).pack(anchor="w", pady=5)
        ctk.CTkCheckBox(opts_frame, text="Enable Sweep (Auto Eject)", variable=self.sweep_var).pack(anchor="w", pady=5)
        
        # Cooldown Temperature Slider
        temp_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        temp_frame.pack(anchor="w", pady=(15, 5), fill="x")
        
        ctk.CTkLabel(temp_frame, text="Cooldown Target", text_color="gray").pack(anchor="w")
        
        slider_row = ctk.CTkFrame(temp_frame, fg_color="transparent")
        slider_row.pack(fill="x", pady=5)
        
        self.temp_label = ctk.CTkLabel(slider_row, text="30 °C", font=ctk.CTkFont(size=18, weight="bold"), text_color="#2CC985")
        self.temp_label.pack(side="right", padx=(10, 0))
        
        def update_temp_label(value):
            temp = int(float(value))
            self.cooldown_temp_var.set(temp)
            self.temp_label.configure(text=f"{temp} °C")
        
        self.temp_slider = ctk.CTkSlider(slider_row, from_=20, to=50, number_of_steps=30, 
                                          variable=self.cooldown_temp_var, command=update_temp_label,
                                          width=150, progress_color="#2CC985", button_color="#2CC985")
        self.temp_slider.pack(side="left", fill="x", expand=True)
        
        # --- CARD 3: TARGET PRINTERS ---
        tgt_card = ctk.CTkFrame(fr_prep)
        tgt_card.pack(fill="x", padx=30, pady=10)
        
        t_head = ctk.CTkFrame(tgt_card, fg_color="transparent")
        t_head.pack(fill="x", padx=20, pady=(5, 5))
        ctk.CTkLabel(t_head, text="3. Target Printers", font=ctk.CTkFont(weight="bold")).pack(side="left")
        
        # Select All Helper
        def toggle_all_printers():
            # If all checked, uncheck all. Else check all.
            all_on = all(v.get() for v in self.printer_check_vars.values())
            for v in self.printer_check_vars.values():
                v.set(not all_on)

        ctk.CTkButton(t_head, text="Toggle All", width=60, height=20, font=ctk.CTkFont(size=11), fg_color="gray50", command=toggle_all_printers).pack(side="right")
        
        # Printer List
        # CONDITIONAL: Use regular Frame for small lists (<=3), Scrollable for large (>3)
        p_count = len(self.printers_config)
        
        if p_count <= 3:
             # Regular frame shrinks to content
             self.tgt_scroll = ctk.CTkFrame(tgt_card, fg_color="transparent")
        else:
             # Scrollable frame for larger lists
             dyn_height = min(150, p_count * 30)
             self.tgt_scroll = ctk.CTkScrollableFrame(tgt_card, height=dyn_height, fg_color="transparent")
             
        self.tgt_scroll.pack(fill="x", padx=10, pady=(0, 10))
        
        self.printer_check_vars = {} # Serial -> BooleanVar
        
        if not self.printers_config:
            ctk.CTkLabel(self.tgt_scroll, text="No printers configured.", text_color="gray").pack()
        else:
            for conf in self.printers_config:
                sn = conf.get("serial")
                name = conf.get("name", "Unknown")
                if sn:
                    # Default: Check first printer or all? Let's check all by default for farm mode
                    var = tk.BooleanVar(value=True) 
                    self.printer_check_vars[sn] = var
                    ctk.CTkCheckBox(self.tgt_scroll, text=f"{name} ({sn})", variable=var).pack(anchor="w", pady=2)

        # --- ACTIONS ---
        act_row = ctk.CTkFrame(fr_prep, fg_color="transparent")
        act_row.pack(fill="x", padx=30, pady=20)
        
        ctk.CTkButton(act_row, text="Add to Queue ➕", height=50, fg_color="#2CC985", font=ctk.CTkFont(size=16, weight="bold"), command=self.add_to_queue).pack(fill="x")
        
        # 3. Queue
        fr_queue = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Print Queue"] = fr_queue
        
        t_row = ctk.CTkFrame(fr_queue, fg_color="transparent")
        t_row.pack(fill="x", padx=20, pady=20)
        ctk.CTkLabel(t_row, text="Job Queue", font=ctk.CTkFont(size=24)).pack(side="left")
        ctk.CTkButton(t_row, text="Start Next", fg_color="#E07A5F", command=self.start_next_queue_item).pack(side="right")
        ctk.CTkButton(t_row, text="Clear", fg_color="transparent", border_width=1, command=self.clear_queue).pack(side="right", padx=10)
        
        self.queue_scroll = ctk.CTkScrollableFrame(fr_queue)
        self.queue_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # 4. Settings (Fleet Management)
        fr_set = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Settings"] = fr_set
        
        # Split Layout
        fr_set.grid_columnconfigure(0, weight=1)
        fr_set.grid_columnconfigure(1, weight=2)
        fr_set.grid_rowconfigure(0, weight=1)
        
        # Left: List
        p_list_frame = ctk.CTkFrame(fr_set)
        p_list_frame.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        
        ctk.CTkLabel(p_list_frame, text="Printer Fleet", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        self.p_list_scroll = ctk.CTkScrollableFrame(p_list_frame)
        self.p_list_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        
        ctk.CTkButton(p_list_frame, text="+ Add Printer", command=self.add_new_printer).pack(pady=10)

        # Right: Details
        self.p_detail_frame = ctk.CTkFrame(fr_set)
        self.p_detail_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 20), pady=20)
        
        ctk.CTkLabel(self.p_detail_frame, text="Printer Details", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        
        self.edit_name = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Printer Name")
        self.edit_name.pack(fill="x", padx=20, pady=10)
        
        self.edit_ip = ctk.CTkEntry(self.p_detail_frame, placeholder_text="IP Address")
        self.edit_ip.pack(fill="x", padx=20, pady=10)
        
        self.edit_ac = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Access Code")
        self.edit_ac.pack(fill="x", padx=20, pady=10)
        
        self.edit_sn = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Serial Number")
        self.edit_sn.pack(fill="x", padx=20, pady=10)
        
        self.btn_save_p = ctk.CTkButton(self.p_detail_frame, text="Save Changes", command=self.save_current_printer, state="disabled")
        self.btn_save_p.pack(pady=10)
        
        self.btn_del_p = ctk.CTkButton(self.p_detail_frame, text="Remove Printer", command=self.delete_current_printer, fg_color="#B55B42", hover_color="#903020", state="disabled")
        self.btn_del_p.pack(pady=10)
        
        self.selected_printer_idx = -1
        self.refresh_printer_list()

    def refresh_printer_list(self):
        for w in self.p_list_scroll.winfo_children(): w.destroy()
        
        for i, conf in enumerate(self.printers_config):
            name = conf.get("name", f"Printer {i+1}")
            btn = ctk.CTkButton(self.p_list_scroll, text=name, command=lambda x=i: self.load_printer_details(x), fg_color="transparent", border_width=1, border_color="gray")
            btn.pack(fill="x", padx=5, pady=2)
            
    def load_printer_details(self, idx):
        self.selected_printer_idx = idx
        conf = self.printers_config[idx]
        
        self.edit_name.delete(0, "end"); self.edit_name.insert(0, conf.get("name", ""))
        self.edit_ip.delete(0, "end"); self.edit_ip.insert(0, conf.get("ip", ""))
        self.edit_ac.delete(0, "end"); self.edit_ac.insert(0, conf.get("access_code", ""))
        self.edit_sn.delete(0, "end"); self.edit_sn.insert(0, conf.get("serial", ""))
        
        self.btn_save_p.configure(state="normal")
        self.btn_del_p.configure(state="normal")
        
    def add_new_printer(self):
        new_p = {"name": "New Printer", "ip": "", "access_code": "", "serial": ""}
        self.printers_config.append(new_p)
        self.refresh_printer_list()
        self.load_printer_details(len(self.printers_config)-1)

    def save_current_printer(self):
        if self.selected_printer_idx < 0: return
        
        idx = self.selected_printer_idx
        self.printers_config[idx]["name"] = self.edit_name.get()
        self.printers_config[idx]["ip"] = self.edit_ip.get()
        self.printers_config[idx]["access_code"] = self.edit_ac.get()
        self.printers_config[idx]["serial"] = self.edit_sn.get()
        
        # Save to Disk
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.printers_config, f, indent=4)
            
        messagebox.showinfo("Saved", "Printer settings saved!")
        self.refresh_printer_list()
        
        # Restart Monitors (Simplest way to apply changes)
        self.start_monitor()
        # Re-spawn dashboard cards
        for w in self.dash_scroll.winfo_children(): w.destroy()
        self.printer_cards.clear()
        for i, conf in enumerate(self.printers_config):
            self.spawn_printer_card(conf, i)

    def delete_current_printer(self):
        if self.selected_printer_idx < 0: return
        del self.printers_config[self.selected_printer_idx]
        self.selected_printer_idx = -1
        
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.printers_config, f, indent=4)
            
        self.edit_name.delete(0, "end")
        self.edit_ip.delete(0, "end")
        self.edit_ac.delete(0, "end")
        self.edit_sn.delete(0, "end")
        self.btn_save_p.configure(state="disabled")
        self.btn_del_p.configure(state="disabled")
        
        self.refresh_printer_list()
        self.start_monitor()
        # Refresh Dash
        for w in self.dash_scroll.winfo_children(): w.destroy()
        self.printer_cards.clear()
        for i, conf in enumerate(self.printers_config):
            self.spawn_printer_card(conf, i)

    def create_stat_box(self, parent, title, value, row, col):
        f = ctk.CTkFrame(parent)
        f.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        ctk.CTkLabel(f, text=title, text_color="gray", font=ctk.CTkFont(size=12)).pack(pady=(10, 0))
        lbl = ctk.CTkLabel(f, text=value, font=ctk.CTkFont(size=20, weight="bold"))
        lbl.pack(pady=(0, 10))
        return lbl

    def select_frame(self, name):
        # Reset color
        self.btn_dashboard.configure(fg_color="transparent")
        self.btn_prepare.configure(fg_color="transparent")
        self.btn_queue.configure(fg_color="transparent")
        self.btn_library.configure(fg_color="transparent")
        self.btn_settings.configure(fg_color="transparent")
        
        # Highlight current
        if name == "Dashboard": self.btn_dashboard.configure(fg_color=("gray75", "gray25"))
        if name == "Prepare/Generator": self.btn_prepare.configure(fg_color=("gray75", "gray25"))
        if name == "Print Queue": self.btn_queue.configure(fg_color=("gray75", "gray25"))
        if name == "Library": self.btn_library.configure(fg_color=("gray75", "gray25"))
        if name == "Settings": self.btn_settings.configure(fg_color=("gray75", "gray25"))
        
        # Show Page
        for frame in self.pages.values():
            frame.grid_forget()
        
        if name in self.pages:
            self.pages[name].grid(row=0, column=1, sticky="nsew")

        if name == "Print Queue":
            self.refresh_queue_display()
        if name == "Library":
            self.refresh_library_display()

    # --- LOGIC ---

    def browse_file(self):
        f = filedialog.askopenfilename(filetypes=[("3MF", "*.3mf")])
        if f: self.file_path.set(f)

    def add_to_queue(self):
        if not self.file_path.get():
            messagebox.showerror("Error", "Select file first.")
            return

        # Farm: Get Selected Printers
        selected_serials = [sn for sn, var in self.printer_check_vars.items() if var.get()]
        
        # Fallback for single-printer legacy config or empty selection
        if not selected_serials and not self.printer_check_vars and self.printers_config:
             # Logic if no checkboxes exist yet? Should not happen with new UI.
             # Just default to first if broadcasting not setup?
             pass
             
        if not selected_serials:
            messagebox.showerror("Error", "Please select at least one target printer.")
            return

        # Use values directly from UI
        copies = -1 if self.chk_infinite.get() == 1 else self.copies_var.get()
        base_job_name = os.path.basename(self.file_path.get()).replace(".3mf", "")
        
        # Determine paths
        project_dir = os.path.dirname(os.path.abspath(__file__))
        queue_dir = os.path.join(project_dir, "queue_jobs")
        
        # Generate file ONCE (Optimization)
        try:
            generated_path = generate_autoloop_file(
                self.file_path.get(), 
                copies=copies, 
                use_sweep=self.sweep_var.get(), 
                use_cooldown=True,
                cooldown_temp=self.cooldown_temp_var.get(),
                output_dir=queue_dir
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to generate file: {e}")
            return
            
        # Extract preview ONCE
        thumb_path = extract_thumbnail_from_3mf(generated_path)
        
        # Create Job for EACH selected printer
        count = 0
        for sn in selected_serials:
            # Find printer name for label
            p_name = next((p["name"] for p in self.printers_config if p["serial"] == sn), sn)
            
            self.queue.append({
                "name": f"{base_job_name} [{p_name}]", # Distinct name
                "target_serial": sn,
                "source_file": self.file_path.get(),
                "generated_file": generated_path,
                "thumbnail": thumb_path,
                "copies": copies,
                "use_sweep": self.sweep_var.get(),
                "use_ams": self.use_ams_var.get(),
                "cooldown_temp": self.cooldown_temp_var.get(),
                "status": "pending"
            })
            count += 1
            
        save_queue(self.queue)
        self.refresh_queue_display()
        self.select_frame("Print Queue")
        messagebox.showinfo("Success", f"Added {count} jobs to queue.")

    def refresh_library_display(self):
        # Ensure UI updates on main thread if needed
        if threading.current_thread() is not threading.main_thread():
             self.after(0, self.refresh_library_display)
             return

        lib = load_library()
        
        # Clear existing
        for w in self.lib_scroll.winfo_children(): w.destroy()
        
        if not hasattr(self, 'lib_images'): self.lib_images = []
        else: self.lib_images.clear()
        
        if not lib:
            ctk.CTkLabel(self.lib_scroll, text="Library is empty. Save jobs from Queue (★) to see them here.", text_color="gray").pack(pady=20)
            return

        for i, job in enumerate(lib):
            f = ctk.CTkFrame(self.lib_scroll, fg_color=("gray85", "gray25"))
            f.pack(fill="x", pady=4, padx=5)
            
            # 1. Thumb
            thumb_frame = ctk.CTkFrame(f, width=60, height=60, fg_color="transparent")
            thumb_frame.pack(side="left", padx=5, pady=5)
            thumb_frame.pack_propagate(False)
            
            if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                try:
                    pil_img = Image.open(job["thumbnail"])
                    pil_img.thumbnail((50, 50))
                    ctk_thumb = ctk.CTkImage(light_image=pil_img, size=pil_img.size)
                    self.lib_images.append(ctk_thumb)
                    lbl = ctk.CTkLabel(thumb_frame, text="", image=ctk_thumb)
                    lbl.pack(expand=True)
                    lbl.bind("<Button-1>", lambda e, p=job["thumbnail"]: self.show_preview_modal(p))
                    lbl.bind("<Enter>", lambda e, l=lbl: l.configure(cursor="hand2"))
                    lbl.bind("<Leave>", lambda e, l=lbl: l.configure(cursor="arrow"))
                except: ctk.CTkLabel(thumb_frame, text="📷").pack(expand=True)
            else:
                 ctk.CTkLabel(thumb_frame, text="📦", font=("Arial", 20)).pack(expand=True)
            
            # 2. Info
            info_frame = ctk.CTkFrame(f, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            
            ctk.CTkLabel(info_frame, text=job["name"], font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x")
            
            c_txt = "∞" if job["copies"] == -1 else str(job["copies"])
            meta = f"x{c_txt} • {job.get('cooldown_temp', 30)}°C • {'AMS' if job.get('use_ams', True) else 'No AMS'} • {'Sweep' if job.get('use_sweep', True) else 'No Sweep'}"
            ctk.CTkLabel(info_frame, text=meta, text_color="gray", font=ctk.CTkFont(size=11), anchor="w").pack(fill="x")
            
            # 3. Actions
            act_frame = ctk.CTkFrame(f, fg_color="transparent")
            act_frame.pack(side="right", padx=10)
            
            # PLAY
            ctk.CTkButton(act_frame, text="▶", width=35, height=35, fg_color="transparent", border_width=1, border_color="gray", text_color=("black", "white"), hover_color="green", command=lambda x=i: self.import_library_job(x)).pack(side="left", padx=5)
            
            # DELETE
            ctk.CTkButton(act_frame, text="✕", width=35, height=35, fg_color="#B55B42", hover_color="#903020", command=lambda x=i: self.remove_from_library(x)).pack(side="left", padx=5)
            
        self.lib_scroll.update_idletasks()

    def refresh_queue_display(self):
        for w in self.queue_scroll.winfo_children(): w.destroy()
        
        # Keep references to images to prevent GC
        self.queue_images = []
        
        for i, job in enumerate(self.queue):
            # Main Item Frame
            f = ctk.CTkFrame(self.queue_scroll, fg_color=("gray85", "gray25")) 
            f.pack(fill="x", pady=4, padx=5)
            
            # --- 1. THUMBNAIL (Left) ---
            thumb_frame = ctk.CTkFrame(f, width=60, height=60, fg_color="transparent")
            thumb_frame.pack(side="left", padx=5, pady=5)
            thumb_frame.pack_propagate(False) # Force size
            
            if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                try:
                    pil_img = Image.open(job["thumbnail"])
                    # Create small thumb
                    pil_img.thumbnail((50, 50))
                    ctk_thumb = ctk.CTkImage(light_image=pil_img, size=pil_img.size)
                    self.queue_images.append(ctk_thumb) # Keep ref
                    
                    lbl_thumb = ctk.CTkLabel(thumb_frame, text="", image=ctk_thumb)
                    lbl_thumb.pack(expand=True, fill="both")
                    
                    # Bind Click for Big View
                    lbl_thumb.bind("<Button-1>", lambda e, p=job["thumbnail"]: self.show_preview_modal(p))
                    lbl_thumb.bind("<Enter>", lambda e, l=lbl_thumb: l.configure(cursor="hand2"))
                    lbl_thumb.bind("<Leave>", lambda e, l=lbl_thumb: l.configure(cursor="arrow"))
                except:
                    ctk.CTkLabel(thumb_frame, text="📷").pack(expand=True)
            else:
                ctk.CTkLabel(thumb_frame, text="📄", font=("Arial", 20)).pack(expand=True)

            # --- 2. INFO (Middle) ---
            info_frame = ctk.CTkFrame(f, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            
            # Name
            ctk.CTkLabel(info_frame, text=job["name"], font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x")
            
            # Details Row
            c_txt = "∞" if job["copies"] == -1 else str(job["copies"])
            temp = job.get("cooldown_temp", 30)
            ams = "AMS" if job.get("use_ams", True) else "No AMS"
            sweep = "Sweep" if job.get("use_sweep", True) else "No Sweep"
            status = job.get("status", "pending").upper()
            
            # Color for status
            st_col = "gray"
            if status == "RUNNING": st_col = "#E69F00" # Orange
            if status == "DONE": st_col = "#009E73" # Green
            if status == "UPLOADING": st_col = "#56B4E9" # Blue
            
            meta_text = f"x{c_txt}  •  {temp}°C  •  {ams}  •  {sweep}"
            
            meta_row = ctk.CTkFrame(info_frame, fg_color="transparent")
            meta_row.pack(fill="x", pady=(2, 0))
            
            ctk.CTkLabel(meta_row, text=status, text_color=st_col, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(meta_row, text="  |  ", text_color="gray").pack(side="left")
            ctk.CTkLabel(meta_row, text=meta_text, text_color="gray", font=ctk.CTkFont(size=11)).pack(side="left")

            # --- 3. ACTIONS (Right) ---
            act_frame = ctk.CTkFrame(f, fg_color="transparent")
            act_frame.pack(side="right", padx=10, pady=5)
            
            # Star (Save to Library)
            btn_star = ctk.CTkButton(act_frame, text="★", width=35, height=35, fg_color="transparent", border_width=1, border_color="#FFD700", text_color="#FFD700", hover_color=("gray75", "gray35"), command=lambda x=i: self.add_to_library(x))
            btn_star.pack(side="left", padx=5)
            
            # Edit
            btn_edit = ctk.CTkButton(act_frame, text="✎", width=35, height=35, fg_color="transparent", border_width=1, border_color="gray", text_color=("black", "white"), hover_color=("gray75", "gray35"), command=lambda x=i: self.edit_job(x))
            btn_edit.pack(side="left", padx=5)
            
            # Delete
            btn_del = ctk.CTkButton(act_frame, text="✕", width=35, height=35, fg_color="#B55B42", hover_color="#903020", command=lambda x=i: self.remove_q(x))
            btn_del.pack(side="left", padx=5)


    def remove_q(self, idx):
        job = self.queue[idx]
        for k in ["generated_file", "thumbnail"]:
            if job.get(k) and os.path.exists(job[k]):
                try: os.remove(job[k])
                except: pass

        del self.queue[idx]
        save_queue(self.queue)
        self.refresh_queue_display()
    
    def clear_queue(self):
        for job in self.queue:
            for k in ["generated_file", "thumbnail"]:
                if job.get(k) and os.path.exists(job[k]):
                    try: os.remove(job[k])
                    except: pass
        
        self.queue = []
        save_queue(self.queue)
        self.refresh_queue_display()

    def start_next_queue_item(self):
        pending = [j for j in self.queue if j["status"] == "pending"]
        if not pending:
            messagebox.showinfo("Queue Empty", "No pending jobs.")
            return
        
        job = pending[0]
        idx = self.queue.index(job)
        
        self.queue[idx]["status"] = "processing"
        save_queue(self.queue)
        self.refresh_queue_display()
        
        threading.Thread(target=self.run_job, args=(job, idx), daemon=True).start()

    def run_job(self, job, idx):
        try:
            # 1. Get File
            out = None
            if job.get("generated_file") and os.path.exists(job["generated_file"]):
                out = job["generated_file"]
            else:
                # Fallback: Generate if missing
                out = generate_autoloop_file(
                    job["source_file"], 
                    copies=999 if job["copies"] == -1 else job["copies"],
                    use_sweep=job["use_sweep"],
                    use_cooldown=True,
                    cooldown_temp=job.get("cooldown_temp", 30)
                )
            
            # 2. Upload/Print
            self.queue[idx]["status"] = "uploading"
            save_queue(self.queue)
            
            # Resolve Credentials
            t_sn = job.get("target_serial")
            p_conf = None
            if t_sn:
                p_conf = next((p for p in self.printers_config if p.get("serial") == t_sn), None)
            
            if not p_conf and self.printers_config:
                p_conf = self.printers_config[0] # Fallback
            
            if not p_conf:
                 raise Exception("No printer configuration found for target.")
            
            ok, msg = upload_and_start_print(
                p_conf.get("ip"), 
                p_conf.get("access_code"), 
                p_conf.get("serial"), 
                out,
                use_ams=job.get("use_ams", True)
            )
            
            if ok:
                self.queue[idx]["status"] = "running"
            else:
                self.queue[idx]["status"] = "error"
                print(msg)
            
        except Exception as e:
            self.queue[idx]["status"] = "error"
            print(e)
        
        save_queue(self.queue)
        # Update UI if we are on Queue page? - thread safe call needed
        self.after(0, self.refresh_queue_display)

    def load_printer_config(self):
        data = load_printer_config()
        self.printer_ip.set(data.get("ip", ""))
        self.printer_access_code.set(data.get("access_code", ""))
        self.printer_serial.set(data.get("serial", ""))
    
    def load_queue(self):
        self.queue = load_queue()

    def save_printer_config(self):
        data = {
            "ip": self.printer_ip.get(),
            "access_code": self.printer_access_code.get(),
            "serial": self.printer_serial.get()
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f)
        messagebox.showinfo("Saved", "Settings Saved")
        self.start_monitor()

    def start_monitor(self):
        # Stop existing
        for serial, monitor in self.monitors.items():
            if monitor: monitor.stop()
        self.monitors.clear()
        
        # Start new monitors
        for conf in self.printers_config:
            ip = conf.get("ip")
            ac = conf.get("access_code")
            sn = conf.get("serial")
            if ip and ac and sn:
                # IMPORTANT: Pass serial to callback to identify source
                mon = PrinterMonitor(ip, ac, sn, lambda d, s=sn: self.update_monitor_ui(d, s))
                mon.start()
                self.monitors[sn] = mon

    def update_monitor_ui(self, data, serial):
        if not self.winfo_exists(): return
        
        # Get card widgets
        if serial not in self.printer_cards:
            return
            
        widgets = self.printer_cards[serial]
        
        pct = data.get("percent", 0)
        left = data.get("left_min", "--")
        state = data.get("state", "OFFLINE")
        job = data.get("job_name", "None")
        
        noz = data.get("nozzle_temp", "--")
        bed = data.get("bed_temp", "--")
        
        # Update Widgets
        widgets["lbl_state"].configure(text=state)
        
        # Update Global Footer (Summary)
        online_count = sum(1 for m in self.monitors.values() if m.connected)
        total_count = len(self.monitors)
        self.status_footer.configure(text=f"Online: {online_count} / {total_count}", text_color="#2CC985" if online_count > 0 else "gray")
        widgets["lbl_job"].configure(text=job)
        widgets["prog"].set(pct / 100)
        widgets["lbl_time"].configure(text=f"{left} min")
        widgets["lbl_temps"].configure(text=f"N: {noz}° / B: {bed}°")
        
        if state == "FINISH" or int(pct) >= 100:
            # Find running job for this printer in queue and mark done
            for q_job in self.queue:
                if q_job.get("status") == "running" and q_job.get("target_serial") == serial:
                    q_job["status"] = "done"
                    save_queue(self.queue)
                    # Refresh if on queue page
                    if self.pages["Print Queue"].winfo_ismapped():
                        self.refresh_queue_display()
        
        # AMS
        ams_cols = data.get("ams_colors", [None]*4)
        for i, c in enumerate(ams_cols):
             if i < 4:
                 col = c if c else "gray30"
                 widgets["ams_slots"][i].configure(fg_color=col)
            


    # --- FIXED METHODS (Overrides) ---
    
    def init_dashboard_page(self):
        # 1. Dashboard (Farm View)
        fr_dash = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Dashboard"] = fr_dash
        
        # Scrollable Grid Container
        self.dash_scroll = ctk.CTkScrollableFrame(fr_dash, fg_color="transparent")
        self.dash_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Grid Config
        self.dash_scroll.grid_columnconfigure(0, weight=1)
        self.dash_scroll.grid_columnconfigure(1, weight=1)
        
        self.printer_cards = {} # Serial -> Widgets Dict
        
        # Spawn Cards
        if not self.printers_config:
             ctk.CTkLabel(self.dash_scroll, text="No printers configured. Go to Settings.").pack(pady=20)
        else:
            for i, conf in enumerate(self.printers_config):
                self.spawn_printer_card(conf, i)

    def spawn_printer_card(self, conf, index):
        sn = conf.get("serial")
        name = conf.get("name", f"Printer {index+1}")
        
        # Card Frame
        card = ctk.CTkFrame(self.dash_scroll, fg_color=("#3B8ED0", "#1f538d"))
        card.grid(row=index//2, column=index%2, sticky="nsew", padx=10, pady=10)
        
        # Header
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=15, pady=10)
        ctk.CTkLabel(head, text=name, font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        lbl_state = ctk.CTkLabel(head, text="OFFLINE", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray80")
        lbl_state.pack(side="right")
        
        # Main Status
        lbl_job = ctk.CTkLabel(card, text="--", font=ctk.CTkFont(size=14))
        lbl_job.pack(pady=5)
        
        # Progress Bar
        prog_bar = ctk.CTkProgressBar(card)
        prog_bar.pack(fill="x", padx=20, pady=5)
        prog_bar.set(0)
        
        # AMS Grid
        ams_frame = ctk.CTkFrame(card, fg_color="transparent")
        ams_frame.pack(pady=10)
        ams_slots = []
        for j in range(4):
            cnt = ctk.CTkFrame(ams_frame, fg_color="transparent")
            cnt.pack(side="left", padx=5)
            ctk.CTkLabel(cnt, text=f"S{j+1}", font=ctk.CTkFont(size=10)).pack(pady=(0,2))
            slot = ctk.CTkFrame(cnt, width=25, height=25, corner_radius=12, fg_color="gray30", border_width=1)
            slot.pack()
            ams_slots.append(slot)
            
        # Stats Grid
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=10, pady=10)
        for c in range(2): stats.grid_columnconfigure(c, weight=1)
        
        lbl_time = ctk.CTkLabel(stats, text="-- min", font=ctk.CTkFont(size=12))
        lbl_time.grid(row=0, column=0, sticky="ew")
        
        lbl_temps = ctk.CTkLabel(stats, text="N: -- / B: --", font=ctk.CTkFont(size=12, weight="bold"))
        lbl_temps.grid(row=0, column=1, sticky="ew")
        
        # Store References
        self.printer_cards[sn] = {
            "lbl_state": lbl_state,
            "lbl_job": lbl_job,
            "prog": prog_bar,
            "lbl_time": lbl_time,
            "lbl_temps": lbl_temps,
            "ams_slots": ams_slots
        }

    def init_prepare_page(self):
        # 2. Prepare (Generator)
        fr_prep = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Prepare/Generator"] = fr_prep
        
        # --- CARD 1: SOURCE FILE ---
        file_card = ctk.CTkFrame(fr_prep)
        file_card.pack(fill="x", padx=30, pady=10)
        
        ctk.CTkLabel(file_card, text="1. Source File", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20, pady=(15, 5))
        f_row = ctk.CTkFrame(file_card, fg_color="transparent")
        f_row.pack(fill="x", padx=10, pady=(0, 20))
        
        ctk.CTkEntry(f_row, textvariable=self.file_path, placeholder_text="Select a .3mf file...").pack(side="left", fill="x", expand=True, padx=(10, 10))
        ctk.CTkButton(f_row, text="Browse Folder 📂", width=120, height=35, command=self.browse_file).pack(side="right", padx=(0, 10))

        # --- CARD 2: CONFIGURATION ---
        conf_card = ctk.CTkFrame(fr_prep)
        conf_card.pack(fill="x", padx=30, pady=10)
        
        ctk.CTkLabel(conf_card, text="2. Print Settings", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20, pady=(15, 10))
        
        # Grid Layout for Settings
        conf_grid = ctk.CTkFrame(conf_card, fg_color="transparent")
        conf_grid.pack(fill="x", padx=10, pady=(0, 20))
        
        # Col 1: Loops
        loop_frame = ctk.CTkFrame(conf_grid, fg_color="transparent")
        loop_frame.grid(row=0, column=0, sticky="nw", padx=10)
        
        ctk.CTkLabel(loop_frame, text="Loop Count", text_color="gray").pack(anchor="w")
        
        step_row = ctk.CTkFrame(loop_frame, fg_color="transparent")
        step_row.pack(fill="x", pady=5)
        
        def decrease_loops():
            try:
                v = self.copies_var.get()
                if v > 1: self.copies_var.set(v - 1)
            except: self.copies_var.set(1)

        def increase_loops():
            try:
                v = self.copies_var.get()
                self.copies_var.set(v + 1)
            except: self.copies_var.set(1)
            
        self.btn_minus = ctk.CTkButton(step_row, text="-", width=40, command=decrease_loops, fg_color="#E07A5F")
        self.btn_minus.pack(side="left", padx=(0, 5))
        
        self.entry_copies = ctk.CTkEntry(step_row, width=60, textvariable=self.copies_var, justify="center")
        self.entry_copies.pack(side="left", padx=5)
        
        self.btn_plus = ctk.CTkButton(step_row, text="+", width=40, command=increase_loops, fg_color="#2CC985")
        self.btn_plus.pack(side="left", padx=(5, 0))
        
        def toggle_infinite():
            if self.chk_infinite.get() == 1: # ON
                self.entry_copies.configure(state="disabled")
                self.btn_minus.configure(state="disabled")
                self.btn_plus.configure(state="disabled")
            else:
                self.entry_copies.configure(state="normal")
                self.btn_minus.configure(state="normal")
                self.btn_plus.configure(state="normal")

        self.chk_infinite = ctk.CTkSwitch(loop_frame, text="Infinite Loop (∞)", command=toggle_infinite)
        self.chk_infinite.pack(anchor="w", pady=(15, 0))

        # Col 2: Options
        opts_frame = ctk.CTkFrame(conf_grid, fg_color="transparent")
        opts_frame.grid(row=0, column=1, sticky="ne", padx=40)
        
        ctk.CTkCheckBox(opts_frame, text="Use AMS (Multi-Color)", variable=self.use_ams_var).pack(anchor="w", pady=5)
        ctk.CTkCheckBox(opts_frame, text="Enable Sweep (Auto Eject)", variable=self.sweep_var).pack(anchor="w", pady=5)
        
        # Temp Slider
        temp_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        temp_frame.pack(anchor="w", pady=(15, 5), fill="x")
        
        ctk.CTkLabel(temp_frame, text="Cooldown Target", text_color="gray").pack(anchor="w")
        
        slider_row = ctk.CTkFrame(temp_frame, fg_color="transparent")
        slider_row.pack(fill="x", pady=5)
        
        self.temp_label = ctk.CTkLabel(slider_row, text="30 °C", font=ctk.CTkFont(size=18, weight="bold"), text_color="#2CC985")
        self.temp_label.pack(side="right", padx=(10, 0))
        
        def update_temp_label(value):
            temp = int(float(value))
            self.cooldown_temp_var.set(temp)
            self.temp_label.configure(text=f"{temp} °C")
        
        self.temp_slider = ctk.CTkSlider(slider_row, from_=20, to=50, number_of_steps=30, 
                                          variable=self.cooldown_temp_var, command=update_temp_label,
                                          width=150, progress_color="#2CC985", button_color="#2CC985")
        self.temp_slider.pack(side="left", fill="x", expand=True)
        
        # --- CARD 3: TARGET PRINTERS ---
        tgt_card = ctk.CTkFrame(fr_prep)
        tgt_card.pack(fill="x", padx=30, pady=10)
        
        t_head = ctk.CTkFrame(tgt_card, fg_color="transparent")
        t_head.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(t_head, text="3. Target Printers", font=ctk.CTkFont(weight="bold")).pack(side="left")
        
        def toggle_all_printers():
            all_on = all(v.get() for v in self.printer_check_vars.values())
            for v in self.printer_check_vars.values():
                v.set(not all_on)

        ctk.CTkButton(t_head, text="Toggle All", width=80, height=24, fg_color="gray50", command=toggle_all_printers).pack(side="right")
        
        self.tgt_scroll = ctk.CTkScrollableFrame(tgt_card, height=120, fg_color="transparent")
        self.tgt_scroll.pack(fill="x", padx=10, pady=(0, 20))
        
        self.printer_check_vars = {} # Serial -> BooleanVar
        
        if not self.printers_config:
            ctk.CTkLabel(self.tgt_scroll, text="No printers configured. Go to Settings.").pack(pady=20)
        else:
            for conf in self.printers_config:
                sn = conf.get("serial")
                name = conf.get("name", "Unknown")
                if sn:
                    var = tk.BooleanVar(value=True) 
                    self.printer_check_vars[sn] = var
                    ctk.CTkCheckBox(self.tgt_scroll, text=f"{name} ({sn})", variable=var).pack(anchor="w", pady=2)

        # --- ACTIONS ---
        act_row = ctk.CTkFrame(fr_prep, fg_color="transparent")
        act_row.pack(fill="x", padx=30, pady=20)
        
        ctk.CTkButton(act_row, text="Add to Queue ➕", height=50, fg_color="#2CC985", font=ctk.CTkFont(size=16, weight="bold"), command=self.add_to_queue).pack(fill="x")

    def init_queue_page(self):
        # 3. Queue
        fr_queue = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Print Queue"] = fr_queue
        
        t_row = ctk.CTkFrame(fr_queue, fg_color="transparent")
        t_row.pack(fill="x", padx=20, pady=20)
        ctk.CTkLabel(t_row, text="Job Queue", font=ctk.CTkFont(size=24)).pack(side="left")
        ctk.CTkButton(t_row, text="Start Next", fg_color="#E07A5F", command=self.start_next_queue_item).pack(side="right")
        ctk.CTkButton(t_row, text="Clear", fg_color="transparent", border_width=1, command=self.clear_queue).pack(side="right", padx=10)
        
        self.queue_scroll = ctk.CTkScrollableFrame(fr_queue)
        self.queue_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    def refresh_queue_display(self):
        for w in self.queue_scroll.winfo_children(): w.destroy()
        
        self.queue_images = []
        lib_names = {j.get("name") for j in load_library()} # Set for fast lookup
        
        for i, job in enumerate(self.queue):
            f = ctk.CTkFrame(self.queue_scroll, fg_color=("gray85", "gray25")) 
            f.pack(fill="x", pady=4, padx=5)
            
            # --- 1. THUMBNAIL ---
            thumb_frame = ctk.CTkFrame(f, width=60, height=60, fg_color="transparent")
            thumb_frame.pack(side="left", padx=5, pady=5)
            thumb_frame.pack_propagate(False)
            
            if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                try:
                    pil_img = Image.open(job["thumbnail"])
                    pil_img.thumbnail((50, 50))
                    ctk_thumb = ctk.CTkImage(light_image=pil_img, size=pil_img.size)
                    self.queue_images.append(ctk_thumb)
                    lbl_thumb = ctk.CTkLabel(thumb_frame, text="", image=ctk_thumb)
                    lbl_thumb.pack(expand=True, fill="both")
                    lbl_thumb.bind("<Button-1>", lambda e, p=job["thumbnail"]: self.show_preview_modal(p))
                    lbl_thumb.bind("<Enter>", lambda e, l=lbl_thumb: l.configure(cursor="hand2"))
                    lbl_thumb.bind("<Leave>", lambda e, l=lbl_thumb: l.configure(cursor="arrow"))
                except:
                    ctk.CTkLabel(thumb_frame, text="📷").pack(expand=True)
            else:
                ctk.CTkLabel(thumb_frame, text="📄", font=("Arial", 20)).pack(expand=True)

            # --- 2. INFO ---
            info_frame = ctk.CTkFrame(f, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=5)
            
            ctk.CTkLabel(info_frame, text=job["name"], font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x")
            
            c_txt = "∞" if job["copies"] == -1 else str(job["copies"])
            temp = job.get("cooldown_temp", 30)
            ams = "AMS" if job.get("use_ams", True) else "No AMS"
            sweep = "Sweep" if job.get("use_sweep", True) else "No Sweep"
            status = job.get("status", "pending").upper()
            
            st_col = "gray"
            if status == "RUNNING": st_col = "#E69F00"
            if status == "DONE": st_col = "#009E73"
            if status == "UPLOADING": st_col = "#56B4E9"
            
            meta_text = f"x{c_txt}  •  {temp}°C  •  {ams}  •  {sweep}"
            
            meta_row = ctk.CTkFrame(info_frame, fg_color="transparent")
            meta_row.pack(fill="x", pady=(2, 0))
            
            ctk.CTkLabel(meta_row, text=status, text_color=st_col, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            ctk.CTkLabel(meta_row, text="  |  ", text_color="gray").pack(side="left")
            ctk.CTkLabel(meta_row, text=meta_text, text_color="gray", font=ctk.CTkFont(size=11)).pack(side="left")

            # --- 3. ACTIONS ---
            act_frame = ctk.CTkFrame(f, fg_color="transparent")
            act_frame.pack(side="right", padx=10, pady=5)
            
            # PIN TOGGLE
            is_pinned = job["name"] in lib_names
            pin_text = "★" if is_pinned else "☆"
            pin_col = "#FFD700" if is_pinned else "gray50"
            pin_cmd = lambda x=i: self.toggle_library_pin(x)
            
            # Using partial or lambda properly
            btn_star = ctk.CTkButton(act_frame, text=pin_text, width=35, height=35, fg_color="transparent", 
                                     border_width=1, border_color=pin_col, text_color=pin_col, 
                                     hover_color=("gray75", "gray35"), command=pin_cmd)
            btn_star.pack(side="left", padx=5)
            
            ctk.CTkButton(act_frame, text="✎", width=35, height=35, fg_color="transparent", border_width=1, border_color="gray", 
                          text_color=("black", "white"), hover_color=("gray75", "gray35"), 
                          command=lambda x=i: self.edit_job(x)).pack(side="left", padx=5)
            
            ctk.CTkButton(act_frame, text="✕", width=35, height=35, fg_color="#B55B42", hover_color="#903020", 
                          command=lambda x=i: self.remove_q(x)).pack(side="left", padx=5)

    def init_settings_page(self):
        # 4. Settings (Fleet Management)
        fr_set = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Settings"] = fr_set
        
        fr_set.grid_columnconfigure(0, weight=1)
        fr_set.grid_columnconfigure(1, weight=2)
        fr_set.grid_rowconfigure(0, weight=1)
        
        # Left: List
        p_list_frame = ctk.CTkFrame(fr_set)
        p_list_frame.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        
        ctk.CTkLabel(p_list_frame, text="Printer Fleet", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        self.p_list_scroll = ctk.CTkScrollableFrame(p_list_frame)
        self.p_list_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        
        ctk.CTkButton(p_list_frame, text="+ Add Printer", command=self.add_new_printer).pack(pady=10)

        # Right: Details
        self.p_detail_frame = ctk.CTkFrame(fr_set)
        self.p_detail_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 20), pady=20)
        
        ctk.CTkLabel(self.p_detail_frame, text="Printer Details", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        
        self.edit_name = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Printer Name")
        self.edit_name.pack(fill="x", padx=20, pady=10)
        
        self.edit_ip = ctk.CTkEntry(self.p_detail_frame, placeholder_text="IP Address")
        self.edit_ip.pack(fill="x", padx=20, pady=10)
        
        self.edit_ac = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Access Code")
        self.edit_ac.pack(fill="x", padx=20, pady=10)
        
        self.edit_sn = ctk.CTkEntry(self.p_detail_frame, placeholder_text="Serial Number")
        self.edit_sn.pack(fill="x", padx=20, pady=10)
        
        self.btn_save_p = ctk.CTkButton(self.p_detail_frame, text="Save Changes", command=self.save_current_printer, state="disabled")
        self.btn_save_p.pack(pady=10)
        
        self.btn_del_p = ctk.CTkButton(self.p_detail_frame, text="Remove Printer", command=self.delete_current_printer, fg_color="#B55B42", hover_color="#903020", state="disabled")
        self.btn_del_p.pack(pady=10)
        
        self.selected_printer_idx = -1
        self.refresh_printer_list()

    def refresh_printer_list(self):
        for w in self.p_list_scroll.winfo_children(): w.destroy()
        
        for i, conf in enumerate(self.printers_config):
            name = conf.get("name", f"Printer {i+1}")
            btn = ctk.CTkButton(self.p_list_scroll, text=name, command=lambda x=i: self.load_printer_details(x), fg_color="transparent", border_width=1, border_color="gray")
            btn.pack(fill="x", padx=5, pady=2)
            
    def load_printer_details(self, idx):
        self.selected_printer_idx = idx
        conf = self.printers_config[idx]
        
        self.edit_name.delete(0, "end"); self.edit_name.insert(0, conf.get("name", ""))
        self.edit_ip.delete(0, "end"); self.edit_ip.insert(0, conf.get("ip", ""))
        self.edit_ac.delete(0, "end"); self.edit_ac.insert(0, conf.get("access_code", ""))
        self.edit_sn.delete(0, "end"); self.edit_sn.insert(0, conf.get("serial", ""))
        
        self.btn_save_p.configure(state="normal")
        self.btn_del_p.configure(state="normal")
        
    def add_new_printer(self):
        new_p = {"name": "New Printer", "ip": "", "access_code": "", "serial": ""}
        self.printers_config.append(new_p)
        self.refresh_printer_list()
        self.load_printer_details(len(self.printers_config)-1)

    def save_current_printer(self):
        if self.selected_printer_idx < 0: return
        
        idx = self.selected_printer_idx
        self.printers_config[idx]["name"] = self.edit_name.get()
        self.printers_config[idx]["ip"] = self.edit_ip.get()
        self.printers_config[idx]["access_code"] = self.edit_ac.get()
        self.printers_config[idx]["serial"] = self.edit_sn.get()
        
        # Save to Disk
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.printers_config, f, indent=4)
            
        messagebox.showinfo("Saved", "Printer settings saved!")
        self.refresh_printer_list()
        
        # Restart Monitors (Simplest way to apply changes)
        self.start_monitor()
        # Re-spawn dashboard cards
        for w in self.dash_scroll.winfo_children(): w.destroy()
        self.printer_cards.clear()
        for i, conf in enumerate(self.printers_config):
            self.spawn_printer_card(conf, i)

    def delete_current_printer(self):
        if self.selected_printer_idx < 0: return
        del self.printers_config[self.selected_printer_idx]
        self.selected_printer_idx = -1
        
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.printers_config, f, indent=4)
            
        self.edit_name.delete(0, "end")
        self.edit_ip.delete(0, "end")
        self.edit_ac.delete(0, "end")
        self.edit_sn.delete(0, "end")
        self.btn_save_p.configure(state="disabled")
        self.btn_del_p.configure(state="disabled")
        
        self.refresh_printer_list()
        self.start_monitor()
        # Refresh Dash
        for w in self.dash_scroll.winfo_children(): w.destroy()
        self.printer_cards.clear()
        for i, conf in enumerate(self.printers_config):
            self.spawn_printer_card(conf, i)

    def init_library_page(self):
        # 5. Library
        fr_lib = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.pages["Library"] = fr_lib
        
        t_row_lib = ctk.CTkFrame(fr_lib, fg_color="transparent")
        t_row_lib.pack(fill="x", padx=20, pady=20)
        ctk.CTkLabel(t_row_lib, text="Job Library", font=ctk.CTkFont(size=24)).pack(side="left")
        
        self.lib_scroll = ctk.CTkScrollableFrame(fr_lib)
        self.lib_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
    def create_stat_box(self, parent, title, value, row, col):
        f = ctk.CTkFrame(parent)
        f.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        ctk.CTkLabel(f, text=title, text_color="gray", font=ctk.CTkFont(size=12)).pack(pady=(10, 0))
        lbl = ctk.CTkLabel(f, text=value, font=ctk.CTkFont(size=20, weight="bold"))
        lbl.pack(pady=(0, 10))
        return lbl

    def show_preview_modal(self, path):
        if not path or not os.path.exists(path): return
        
        top = ctk.CTkToplevel(self)
        top.title("Preview")
        
        w, h = 600, 600
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            top.geometry(f"{w}x{h}+{x}+{y}")
        except:
            top.geometry("600x600")
            
        top.attributes("-topmost", True)
        top.grab_set()
        
        try:
            pil_img = Image.open(path)
            ratio = min(w/pil_img.width, h/pil_img.height)
            new_size = (int(pil_img.width*ratio), int(pil_img.height*ratio))
            pil_img = pil_img.resize(new_size, Image.Resampling.LANCZOS)
            
            img = ctk.CTkImage(light_image=pil_img, size=new_size)
            
            lbl = ctk.CTkLabel(top, text="", image=img)
            lbl.pack(expand=True)
            lbl.bind("<Button-1>", lambda e: top.destroy())
        except: 
            top.destroy()

    def edit_job(self, idx):
        job = self.queue[idx]
        top = ctk.CTkToplevel(self)
        top.title("Edit Job")
        top.geometry("350x550")
        
        # Force on top
        top.attributes("-topmost", True)
        top.lift()
        top.focus_force()
        top.grab_set()
        
        ctk.CTkLabel(top, text="Edit Job", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=10)
        
        ctk.CTkLabel(top, text="Name").pack()
        name_var = tk.StringVar(value=job["name"])
        ctk.CTkEntry(top, textvariable=name_var).pack(pady=5)
        
        ctk.CTkLabel(top, text="Copies (-1 = Inf)").pack()
        copies_var = tk.IntVar(value=job["copies"])
        ctk.CTkEntry(top, textvariable=copies_var).pack(pady=5)
        
        ctk.CTkLabel(top, text="Cooldown Temp (°C)").pack()
        temp_var = tk.IntVar(value=job.get("cooldown_temp", 30))
        def up(v): lbl_t.configure(text=f"{int(float(v))}°C")
        s = ctk.CTkSlider(top, from_=20, to=50, variable=temp_var, number_of_steps=30, command=up)
        s.pack(pady=5)
        lbl_t = ctk.CTkLabel(top, text=f"{temp_var.get()}°C")
        lbl_t.pack()
        
        sw_var = tk.BooleanVar(value=job.get("use_sweep", True))
        ctk.CTkCheckBox(top, text="Enable Sweep", variable=sw_var).pack(pady=10)
        
        ams_var = tk.BooleanVar(value=job.get("use_ams", True))
        ctk.CTkCheckBox(top, text="Use AMS", variable=ams_var).pack(pady=5)
        
        def save():
            job["name"] = name_var.get()
            job["copies"] = copies_var.get()
            job["cooldown_temp"] = int(temp_var.get())
            job["use_sweep"] = sw_var.get()
            job["use_ams"] = ams_var.get()
            
            # Regenerate
            if job.get("generated_file") and os.path.exists(job["generated_file"]):
                try: os.remove(job["generated_file"])
                except: pass
            
            try:
                project_dir = os.path.dirname(os.path.abspath(__file__))
                queue_dir = os.path.join(project_dir, "queue_jobs")
                new_path = generate_autoloop_file(
                    job["source_file"],
                    copies=999 if job["copies"] == -1 else job["copies"],
                    use_sweep=job["use_sweep"],
                    use_cooldown=True,
                    cooldown_temp=job["cooldown_temp"],
                    output_dir=queue_dir
                )
                job["generated_file"] = new_path
                
                if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                     try: os.remove(job["thumbnail"])
                     except: pass
                job["thumbnail"] = extract_thumbnail_from_3mf(new_path)
                
                save_queue(self.queue)
                self.refresh_queue_display()
                top.destroy()
                messagebox.showinfo("Success", "Job Updated")
            except Exception as e:
                messagebox.showerror("Error", str(e))
                
        ctk.CTkButton(top, text="Save Changes", command=save, fg_color="#2CC985").pack(pady=20)

    def add_to_library(self, idx, silent=False):
        try:
            job = self.queue[idx]
            lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library_jobs")
            os.makedirs(lib_dir, exist_ok=True)
            
            # Unique ID for files
            ts = int(time.time())
            safe_name = "".join([c for c in job["name"] if c.isalnum() or c in (' ', '-', '_')]).strip()
            new_name_base = f"Lib_{safe_name}_{ts}"
            new_3mf = os.path.join(lib_dir, new_name_base + ".3mf")
            new_thumb = os.path.join(lib_dir, new_name_base + ".png")
            
            # Copy 3mf
            if job.get("generated_file") and os.path.exists(job["generated_file"]):
                shutil.copy2(job["generated_file"], new_3mf)
            elif job.get("source_file") and os.path.exists(job["source_file"]):
                 shutil.copy2(job["source_file"], new_3mf)
            
            # Copy Thumb
            if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                shutil.copy2(job["thumbnail"], new_thumb)
                
            lib = load_library()
            new_entry = job.copy()
            new_entry["generated_file"] = new_3mf
            new_entry["thumbnail"] = new_thumb
            new_entry["status"] = "library" # Marker
            
            lib.append(new_entry)
            save_library(lib)
            if not silent: messagebox.showinfo("Saved", "Job saved to Library!")
            self.refresh_library_display()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def toggle_library_pin(self, idx):
        job = self.queue[idx]
        name = job["name"]
        
        lib = load_library()
        exists = any(j.get("name") == name for j in lib)
        
        if exists:
            self.remove_from_library_by_name(name)
        else:
            self.add_to_library(idx, silent=True)
            
        self.refresh_queue_display()

    def remove_from_library_by_name(self, name):
         lib = load_library()
         to_remove = [item for item in lib if item.get("name") == name]
         
         if not to_remove: return

         # Delete files
         for job in to_remove:
             for k in ["generated_file", "thumbnail"]:
                if job.get(k) and os.path.exists(job[k]):
                    try: os.remove(job[k])
                    except: pass
         
         # Filter list
         lib = [item for item in lib if item.get("name") != name]
         save_library(lib)
         self.refresh_library_display()

    def remove_from_library(self, idx):
        lib = load_library()
        job = lib[idx]
        # Delete files
        for k in ["generated_file", "thumbnail"]:
            if job.get(k) and os.path.exists(job[k]):
                try: os.remove(job[k])
                except: pass
        del lib[idx]
        save_library(lib)
        self.refresh_library_display()

    def import_library_job(self, idx):
        try:
            lib = load_library()
            job = lib[idx]
            
            # Work in Queue Dir
            project_dir = os.path.dirname(os.path.abspath(__file__))
            queue_dir = os.path.join(project_dir, "queue_jobs")
            os.makedirs(queue_dir, exist_ok=True)
            
            ts = int(time.time())
            safe_name = "".join([c for c in job["name"] if c.isalnum() or c in (' ', '-', '_')]).strip()
            new_name_base = f"{safe_name}_Import_{ts}"
            target_3mf = os.path.join(queue_dir, new_name_base + ".3mf")
            target_thumb = os.path.join(queue_dir, new_name_base + ".png")
            
            if job.get("generated_file") and os.path.exists(job["generated_file"]):
                shutil.copy2(job["generated_file"], target_3mf)
                
            if job.get("thumbnail") and os.path.exists(job["thumbnail"]):
                shutil.copy2(job["thumbnail"], target_thumb)
                
            queue_job = job.copy()
            queue_job["generated_file"] = target_3mf
            queue_job["thumbnail"] = target_thumb
            queue_job["status"] = "pending"
            
            self.queue.append(queue_job)
            save_queue(self.queue)
            self.refresh_queue_display()
            self.select_frame("Print Queue")
        except Exception as e:
            messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    app = BambuAutoEjectorApp()
    app.mainloop()
