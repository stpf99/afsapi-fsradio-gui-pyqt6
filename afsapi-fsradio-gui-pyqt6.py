#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FSRadio GUI (PyQt6) for Frontier Silicon / UNDOK using afsapi (async, Python 3.12+)

Hardening:
- Never calls API unless connected (strict guards)
- Disconnects volume slider command while initializing to avoid spurious calls
- Proper widget states (enabled/disabled)
- Auto-tries base URLs if you enter only IP/host (…/device, …/fsapi, plain host)
"""

import json
import re
import requests
import socket
import threading
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, List

import asyncio
from concurrent.futures import Future

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QComboBox, QLineEdit, QCheckBox,
    QGroupBox, QMessageBox, QInputDialog, QScrollArea
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

try:
    from afsapi import AFSAPI
except ImportError:
    AFSAPI = None

CONFIG_DIR = Path.home() / ".config" / "fsradio-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "url": "192.168.0.153",  # you can enter just IP/host; app tries common roots
    "pin": 1234,
    "timeout": 2,
    "last_mode": "IRadio"
}

# -------------------- Async service --------------------
class RadioService:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._api: Optional[AFSAPI] = None
        self._connected = False
        self.url_used: Optional[str] = None
        self.pin = 1234
        self.timeout = 2

    def _ensure_loop(self):
        if self._loop:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run_coro(self, coro) -> Any:
        self._ensure_loop()
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    @staticmethod
    def _normalize_candidates(user_input: str) -> List[str]:
        s = user_input.strip()
        if not s:
            return []
        if not s.startswith(("http://", "https://")):
            s = "http://" + s

        # If user already gave a path with /device or /fsapi, try as-is first.
        if re.search(r"/(device|fsapi)(/)?$", s):
            return [s]

        host_port = s.rstrip("/")
        host_only = host_port.split("//", 1)[1]
        has_port = ":" in host_only
        candidates = [
            (host_port + "/device") if has_port else (host_port + ":80/device"),
            (host_port + "/fsapi")  if has_port else (host_port + ":80/fsapi"),
            host_port,
        ]
        # dedupe
        seen, uniq = set(), []
        for c in candidates:
            if c not in seen:
                uniq.append(c); seen.add(c)
        return uniq

    def connect(self, user_url: str, pin: int, timeout: int = 2):
        if AFSAPI is None:
            raise RuntimeError("Dependency missing: install with `pip install afsapi` in your venv.")
        self.pin = int(pin)
        self.timeout = int(timeout)

        # Optional DNS/IP check
        try:
            host = user_url
            if host.startswith("http"):
                host = host.split("://", 1)[1]
            host = host.split("/", 1)[0]
            socket.gethostbyname(host.split(":")[0])
        except Exception:
            pass

        self._api = None
        self._connected = False
        self.url_used = None
        last_err = None

        for base in self._normalize_candidates(user_url):
            try:
                async def _create():
                    api = await AFSAPI.create(base, self.pin, self.timeout)
                    await api.get_friendly_name()  # probe
                    return api
                api = self._run_coro(_create())
                self._api = api
                self._connected = True
                self.url_used = base
                break
            except Exception as e:
                last_err = e
                continue

        if not self._connected:
            raise RuntimeError(
                "Could not connect to the radio. "
                f"Tried: {', '.join(self._normalize_candidates(user_url))}. Last error: {last_err}"
            )

    def is_connected(self) -> bool:
        return self._connected and self._api is not None

    # ---------- guarded wrappers ----------
    def _require(self):
        if not self.is_connected():
            raise RuntimeError("Not connected to a radio.")

    def get_friendly_name(self) -> str:
        self._require()
        async def _c(): return await self._api.get_friendly_name()
        return self._run_coro(_c())

    def get_power(self) -> bool:
        self._require()
        async def _c(): return await self._api.get_power()
        return bool(self._run_coro(_c()))

    def set_power(self, on: bool):
        self._require()
        async def _c(): return await self._api.set_power(bool(on))
        return self._run_coro(_c())

    def get_volume(self) -> int:
        self._require()
        async def _c(): return await self._api.get_volume()
        return int(self._run_coro(_c()))

    def set_volume(self, value: int):
        self._require()
        async def _c(): return await self._api.set_volume(int(value))
        return self._run_coro(_c())

    def get_modes(self) -> Iterable[str]:
        self._require()
        async def _c(): return await self._api.get_modes()
        return self._run_coro(_c())

    def set_mode(self, mode: str):
        self._require()
        async def _c(): return await self._api.set_mode(mode)
        return self._run_coro(_c())

    def get_presets(self):
        self._require()
        async def _c(): 
            presets = await self._api.get_presets()
            # If presets is a generator or limited list, try to get all
            if hasattr(presets, '__aiter__'):
                # It's an async iterator
                all_presets = []
                async for preset in presets:
                    all_presets.append(preset)
                return all_presets
            else:
                # It's already a list
                return list(presets)
        return self._run_coro(_c())

    def get_stations(self):
        """Get all available stations (not just presets/favorites)"""
        self._require()
        async def _c():
            # Try to get the full station list
            if hasattr(self._api, 'get_stations'):
                stations = await self._api.get_stations()
            elif hasattr(self._api, 'get_station_list'):
                stations = await self._api.get_station_list()
            elif hasattr(self._api, 'nav_list'):
                # Navigate to station list
                stations = await self._api.nav_list()
            else:
                # Fallback to presets
                return await self._api.get_presets()
            
            # Handle async iterators
            if hasattr(stations, '__aiter__'):
                all_stations = []
                idx = 0
                async for station in stations:
                    # Add index to station dict if it doesn't have one
                    if isinstance(station, dict) and 'index' not in station:
                        station['index'] = idx
                    all_stations.append(station)
                    idx += 1
                return all_stations
            else:
                station_list = list(stations)
                # Add index to each station
                for idx, station in enumerate(station_list):
                    if isinstance(station, dict) and 'index' not in station:
                        station['index'] = idx
                return station_list
        return self._run_coro(_c())

    def recall_preset(self, preset):
        self._require()
        async def _c():
            print(f"DEBUG: Preset type: {type(preset)}, value: {preset}")
            
            # Handle tuple format (key, value_dict)
            if isinstance(preset, tuple):
                if len(preset) >= 2:
                    key = preset[0]
                    value = preset[1]
                    print(f"DEBUG: Tuple preset - key: {key}, value: {value}")
                    
                    # Try to use the key (usually an integer index)
                    if hasattr(self._api, 'nav_select_item'):
                        print(f"DEBUG: Calling nav_select_item with key {key}")
                        return await self._api.nav_select_item(key)
                    elif hasattr(self._api, 'select_preset'):
                        print(f"DEBUG: Calling select_preset with key {key}")
                        return await self._api.select_preset(key)
                    elif hasattr(self._api, 'play_item'):
                        print(f"DEBUG: Calling play_item with key {key}")
                        return await self._api.play_item(key)
                else:
                    raise ValueError(f"Unexpected tuple format: {preset}")
            
            # For station dictionaries from nav_list
            if isinstance(preset, dict):
                print(f"DEBUG: Preset is dict: {preset}")
                
                # For navigation lists, use nav_select_item with the index
                if hasattr(self._api, 'nav_select_item'):
                    idx = preset.get('index', 0)
                    print(f"DEBUG: Calling nav_select_item with index {idx}")
                    return await self._api.nav_select_item(idx)
                
                # Try play_item if available
                if hasattr(self._api, 'play_item'):
                    idx = preset.get('index', 0)
                    print(f"DEBUG: Calling play_item with index {idx}")
                    return await self._api.play_item(idx)
                
                raise AttributeError(
                    f"Cannot select station from list. "
                    f"Available methods: {[m for m in dir(self._api) if not m.startswith('_')]}"
                )
            
            # For preset objects (from get_presets)
            print(f"DEBUG: Preset is object: {type(preset)}")
            
            if hasattr(preset, 'play'):
                print("DEBUG: Calling preset.play()")
                return await preset.play()
            elif hasattr(preset, 'select'):
                print("DEBUG: Calling preset.select()")
                return await preset.select()
            elif hasattr(self._api, 'recall_preset'):
                print("DEBUG: Calling api.recall_preset()")
                return await self._api.recall_preset(preset)
            elif hasattr(self._api, 'select_preset'):
                print("DEBUG: Calling api.select_preset()")
                return await self._api.select_preset(preset)
            elif hasattr(self._api, 'play_preset'):
                print("DEBUG: Calling api.play_preset()")
                return await self._api.play_preset(preset)
            else:
                raise AttributeError(f"Cannot play this preset. Type: {type(preset)}")
        return self._run_coro(_c())


# -------------------- Worker Threads --------------------
class ConnectThread(QThread):
    success = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, service, url, pin, timeout):
        super().__init__()
        self.service = service
        self.url = url
        self.pin = pin
        self.timeout = timeout

    def run(self):
        try:
            self.service.connect(self.url, self.pin, self.timeout)
            self.success.emit()
        except Exception as ex:
            self.error.emit(str(ex))


class PresetLoadThread(QThread):
    presets_loaded = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, service, load_all=False):
        super().__init__()
        self.service = service
        self.load_all = load_all

    def run(self):
        try:
            if self.load_all:
                presets = self.service.get_stations()
            else:
                presets = self.service.get_presets()
            self.presets_loaded.emit(list(presets))
        except Exception as ex:
            self.error.emit(str(ex))


class ApiCallThread(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            self.func(*self.args)
            self.finished.emit()
        except Exception as ex:
            self.error.emit(str(ex))


# ------------------------------ GUI ------------------------------
class GuiView(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FSRadio – Remote")
        self.setMinimumSize(520, 460)
        
        self.service = RadioService()
        self.config_data = self._load_config()
        self.preset_buttons = []
        self._during_init = False
        self._slider_blocked = False
        
        # Keep references to threads to prevent premature destruction
        self.active_threads = []
        
        # Store mode objects for later use
        self.mode_objects = []

        self.init_ui()
        
        # Optional auto-connect - use QTimer instead of QThread.msleep
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(300, self.on_connect)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Connection
        conn_group = QGroupBox("Connection")
        conn_layout = QHBoxLayout()

        conn_layout.addWidget(QLabel("Radio URL or IP:"))
        self.url_edit = QLineEdit(self.config_data["url"])
        self.url_edit.setMinimumWidth(250)
        conn_layout.addWidget(self.url_edit)

        conn_layout.addWidget(QLabel("PIN:"))
        self.pin_edit = QLineEdit(str(self.config_data["pin"]))
        self.pin_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_edit.setMaximumWidth(60)
        conn_layout.addWidget(self.pin_edit)

        conn_layout.addWidget(QLabel("Timeout:"))
        self.timeout_edit = QLineEdit(str(self.config_data["timeout"]))
        self.timeout_edit.setMaximumWidth(40)
        conn_layout.addWidget(self.timeout_edit)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.on_connect)
        conn_layout.addWidget(self.btn_connect)

        conn_group.setLayout(conn_layout)
        layout.addWidget(conn_group)

        # Power & Volume
        pv_group = QGroupBox("Power & Volume")
        pv_layout = QHBoxLayout()

        self.power_checkbox = QCheckBox("Power On")
        self.power_checkbox.stateChanged.connect(self.on_power_toggle)
        pv_layout.addWidget(self.power_checkbox)

        pv_layout.addWidget(QLabel("Volume"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setMinimum(0)
        self.vol_slider.setMaximum(40)
        self.vol_slider.setValue(10)
        self.vol_slider.valueChanged.connect(self.on_volume_change)
        pv_layout.addWidget(self.vol_slider)

        pv_group.setLayout(pv_layout)
        layout.addWidget(pv_group)

        # Modes
        mode_group = QGroupBox("Mode")
        mode_layout = QVBoxLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.currentIndexChanged.connect(self.on_mode_change)
        mode_layout.addWidget(self.mode_combo)

        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        # Presets
        preset_group = QGroupBox("Presets")
        preset_layout = QVBoxLayout()

        # Scroll area for presets
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.preset_container = QWidget()
        self.preset_container_layout = QVBoxLayout(self.preset_container)
        self.preset_container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self.preset_container)
        preset_layout.addWidget(scroll)

        self.btn_reload_presets = QPushButton("Reload Presets (Favorites)")
        self.btn_reload_presets.clicked.connect(self.on_load_presets)
        preset_layout.addWidget(self.btn_reload_presets)
        
        self.btn_reload_stations = QPushButton("Show All Stations")
        self.btn_reload_stations.clicked.connect(self.on_load_all_stations)
        preset_layout.addWidget(self.btn_reload_stations)

        preset_group.setLayout(preset_layout)
        layout.addWidget(preset_group)

        # Initial state
        self._enable_controls(False)

    # -------------- config --------------
    def _load_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config = {}
        if CONFIG_FILE.exists():
            try:
                config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

        missing = {k: v for k, v in DEFAULT_CONFIG.items() if k not in config or config[k] in ("", None)}
        if missing:
            for key, default in missing.items():
                prompt = f"Enter {key.replace('_', ' ').title()}:"
                if key == "last_mode":
                    value = get_last_mode_from_api(config.get("url", default), config.get("pin", default))
                elif key in ("pin", "timeout"):
                    value, ok = QInputDialog.getInt(None, "Config Required", prompt, default)
                    if not ok:
                        value = default
                else:
                    value, ok = QInputDialog.getText(None, "Config Required", prompt, text=str(default))
                    if not ok or not value:
                        value = default
                config[key] = value
            try:
                CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        return config

    def _save_config(self):
        self.config_data["url"] = self.url_edit.text().strip()
        try:
            self.config_data["pin"] = int(self.pin_edit.text().strip())
        except ValueError:
            self.config_data["pin"] = DEFAULT_CONFIG["pin"]
        try:
            self.config_data["timeout"] = int(self.timeout_edit.text().strip())
        except ValueError:
            self.config_data["timeout"] = DEFAULT_CONFIG["timeout"]
        try:
            self.config_data["last_mode"] = self.mode_combo.currentText()
        except Exception:
            self.config_data["last_mode"] = DEFAULT_CONFIG["last_mode"]
        CONFIG_FILE.write_text(json.dumps(self.config_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------- helpers --------------
    def _enable_controls(self, on: bool):
        self.vol_slider.setEnabled(on)
        self.mode_combo.setEnabled(on)
        self.power_checkbox.setEnabled(on)
        self.btn_reload_presets.setEnabled(on)
        self.btn_reload_stations.setEnabled(on)
        for b in self.preset_buttons:
            b.setEnabled(on)

    def _cleanup_finished_threads(self):
        """Remove finished threads from active_threads list"""
        self.active_threads = [t for t in self.active_threads if t.isRunning()]

    def _add_thread(self, thread):
        """Add a thread to active_threads and connect cleanup"""
        self._cleanup_finished_threads()
        self.active_threads.append(thread)
        thread.finished.connect(self._cleanup_finished_threads)

    # -------------- events --------------
    def on_connect(self):
        url = self.url_edit.text().strip()
        pin = self.pin_edit.text().strip()
        timeout = self.timeout_edit.text().strip()

        self._enable_controls(False)
        self._during_init = True
        self._slider_blocked = True

        def after_ok():
            try:
                name = self.service.get_friendly_name()
                v = self.service.get_volume()
                self.vol_slider.setValue(v)
                self.power_checkbox.setChecked(self.service.get_power())
                modes_raw = list(self.service.get_modes())
                
                # Store mode objects and create display names
                self.mode_objects = modes_raw
                self.mode_combo.clear()
                
                for mode in modes_raw:
                    # Try to get a friendly display name
                    if hasattr(mode, 'label'):
                        display_name = str(mode.label)
                    elif hasattr(mode, 'name'):
                        display_name = str(mode.name)
                    elif hasattr(mode, 'key'):
                        display_name = str(mode.key)
                    else:
                        display_name = str(mode)
                    
                    # Store the mode object as user data
                    self.mode_combo.addItem(display_name, mode)
                
                # Try to select last mode
                last_mode = self.config_data.get("last_mode")
                if last_mode:
                    for i in range(self.mode_combo.count()):
                        mode_obj = self.mode_combo.itemData(i)
                        mode_key = getattr(mode_obj, 'key', str(mode_obj))
                        if str(mode_key) == str(last_mode):
                            self.mode_combo.setCurrentIndex(i)
                            break
                elif modes_raw:
                    self.mode_combo.setCurrentIndex(0)
                self._enable_controls(True)
                self._save_config()
                self.on_load_presets()
                used = self.service.url_used or url
                self.setWindowTitle(f"FSRadio – {name} [{used}]")
            except Exception as ex:
                QMessageBox.critical(self, "Init error", str(ex))
            finally:
                self._slider_blocked = False
                self._during_init = False

        def on_error(err):
            QMessageBox.critical(self, "Connection failed", err)
            self._slider_blocked = False
            self._during_init = False

        thread = ConnectThread(self.service, url, int(pin), int(timeout))
        thread.success.connect(after_ok)
        thread.error.connect(on_error)
        self._add_thread(thread)
        thread.start()

    def on_power_toggle(self):
        if not self.service.is_connected():
            return
        on = self.power_checkbox.isChecked()
        thread = ApiCallThread(self.service.set_power, on)
        thread.error.connect(lambda err: QMessageBox.critical(self, "Error", err))
        self._add_thread(thread)
        thread.start()

    def on_volume_change(self):
        if self._slider_blocked or self._during_init or not self.service.is_connected():
            return
        v = self.vol_slider.value()
        thread = ApiCallThread(self.service.set_volume, v)
        thread.error.connect(lambda err: QMessageBox.critical(self, "Error", err))
        self._add_thread(thread)
        thread.start()

    def on_mode_change(self):
        if not self.service.is_connected():
            return
        
        # Get the actual mode object stored in combo box
        mode_obj = self.mode_combo.currentData()
        if not mode_obj:
            return
        
        # Save the mode key for next time
        if hasattr(mode_obj, 'key'):
            self.config_data["last_mode"] = str(mode_obj.key)
        else:
            self.config_data["last_mode"] = str(mode_obj)
        
        self._save_config()
        
        # Send the actual mode object to the API
        thread = ApiCallThread(self.service.set_mode, mode_obj)
        thread.error.connect(lambda err: QMessageBox.critical(self, "Error", err))
        self._add_thread(thread)
        thread.start()

    def on_load_presets(self):
        self._load_stations_internal(load_all=False, title="Favorites")

    def on_load_all_stations(self):
        self._load_stations_internal(load_all=True, title="All Stations")

    def _load_stations_internal(self, load_all=False, title="Presets"):
        # Clear existing buttons
        for i in reversed(range(self.preset_container_layout.count())):
            widget = self.preset_container_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()
        self.preset_buttons = []

        if not self.service.is_connected():
            return

        def build_buttons(presets):
            if not presets:
                no_presets_label = QLabel(f"No {title.lower()} found")
                self.preset_container_layout.addWidget(no_presets_label)
                return
                
            for idx, p in enumerate(presets, start=1):
                # Try to get a friendly display name from preset
                label = None
                
                # Handle tuple format (key, value_dict)
                if isinstance(p, tuple) and len(p) >= 2:
                    key, value = p[0], p[1]
                    if isinstance(value, dict):
                        label = value.get("name") or value.get("label") or value.get("title") or value.get("text")
                    print(f"DEBUG: Tuple preset {idx}: key={key}, label={label}")
                
                # Handle dictionary format
                elif isinstance(p, dict):
                    label = p.get("name") or p.get("label") or p.get("title") or p.get("text")
                
                # Handle object format
                elif hasattr(p, 'name'):
                    name_val = getattr(p, 'name')
                    # Check if name is a dict (nested structure)
                    if isinstance(name_val, dict):
                        label = name_val.get("name") or name_val.get("label") or name_val.get("title")
                    else:
                        label = str(name_val)
                elif hasattr(p, 'label'):
                    label_val = getattr(p, 'label')
                    if isinstance(label_val, dict):
                        label = label_val.get("name") or label_val.get("label") or label_val.get("title")
                    else:
                        label = str(label_val)
                elif hasattr(p, 'title'):
                    title_val = getattr(p, 'title')
                    if isinstance(title_val, dict):
                        label = title_val.get("name") or title_val.get("label") or title_val.get("title")
                    else:
                        label = str(title_val)
                
                if not label:
                    label = f"Station {idx}"
                
                # Add index number for easier identification
                button_text = f"{idx}. {label}"
                
                b = QPushButton(button_text)
                # Pass the entire preset object, not just index
                b.clicked.connect(lambda checked, pv=p: self.on_preset(pv))
                self.preset_container_layout.addWidget(b)
                self.preset_buttons.append(b)
            
            # Add info label
            info_label = QLabel(f"Total {title.lower()}: {len(presets)}")
            info_label.setStyleSheet("font-style: italic; color: gray;")
            self.preset_container_layout.addWidget(info_label)
            
            self._enable_controls(True)

        thread = PresetLoadThread(self.service, load_all=load_all)
        thread.presets_loaded.connect(build_buttons)
        thread.error.connect(lambda err: QMessageBox.critical(self, f"{title} error", err))
        self._add_thread(thread)
        thread.start()

    def on_preset(self, preset):
        if not self.service.is_connected():
            return
        thread = ApiCallThread(self.service.recall_preset, preset)
        thread.error.connect(lambda err: QMessageBox.critical(self, "Error", err))
        self._add_thread(thread)
        thread.start()

    def closeEvent(self, event):
        """Clean up threads when closing the application"""
        # Wait for all threads to finish
        for thread in self.active_threads:
            if thread.isRunning():
                thread.quit()
                thread.wait(1000)  # Wait up to 1 second
        event.accept()


def get_last_mode_from_api(url, pin):
    try:
        # Example endpoint; adjust as needed for your API
        response = requests.get(f"{url}/api/status", params={"pin": pin}, timeout=5)
        response.raise_for_status()
        data = response.json()
        # Adjust key as needed based on API response structure
        return data.get("mode", "DAB")
    except Exception:
        return "DAB"


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GuiView()
    window.show()
    sys.exit(app.exec())